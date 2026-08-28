#!/usr/bin/env python3
# scripts/provision_bunny_egress_zone.py — run from repo root:
#   BUNNY_ACCOUNT_API_KEY=... python3 scripts/provision_bunny_egress_zone.py
#
# Creates the dedicated Bunny Storage Zone (+ its own Pull Zone) that
# automatic class recording needs, then prints the exact env lines to set.
#
# WHY A SCRIPT AND NOT THE DASHBOARD: three of the values are easy to get
# subtly wrong by hand, and each one fails as an opaque SigV4 error rather
# than a useful message —
#   • the zone must have S3 support enabled (StorageZoneType=1); a zone
#     created without it has no S3 endpoint at all,
#   • BUNNY_EGRESS_S3_HOST must be the S3 host, NOT the native Edge Storage
#     host (storage.bunnycdn.com) that config/bunny_storage.py uses,
#   • BUNNY_EGRESS_REGION must match the zone's actual region.
# The create response carries all three authoritatively, so this reads them
# back rather than deriving them.
#
# SECRETS: your account API key is read from the environment and never
# written anywhere. The zone password it prints IS a secret — put it in your
# .env, don't paste it into a chat or a commit.
#
# Docs: https://bunny.net/docs/api-reference/core/storage-zone/add-storage-zone
#       https://bunny.net/docs/api-reference/core/pull-zone/add-pull-zone
#       https://bunny.net/docs/storage/s3
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.bunny.net"

# Region codes accepted for a zone's PRIMARY region. Lower-cased, these are
# also the SigV4 signing region and the S3 host's subdomain.
PRIMARY_REGIONS = ["DE", "NY", "LA", "SG"]


def _call(method, path, key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={
            "AccessKey": key,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        sys.exit(f"✗ {method} {path} → HTTP {e.code}\n  {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"✗ {method} {path} → could not reach Bunny: {e.reason}")


def find_storage_zone(key, name):
    page = _call("GET", "/storagezone?page=1&perPage=1000", key)
    items = page.get("Items", page if isinstance(page, list) else [])
    return next((z for z in items if z.get("Name") == name), None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default="shiksha-class-egress",
                    help="storage zone name (default: shiksha-class-egress)")
    ap.add_argument("--region", default="DE", choices=PRIMARY_REGIONS,
                    help="primary region (default: DE). Pick the one nearest "
                         "your LiveKit Cloud region to cut egress latency.")
    ap.add_argument("--pull-zone", default=None,
                    help="pull zone name (default: <name>-pull)")
    ap.add_argument("--cms-zone", default=os.getenv("BUNNY_STORAGE_ZONE", ""),
                    help="your existing CMS storage zone, refused as a "
                         "collision (defaults to $BUNNY_STORAGE_ZONE)")
    ap.add_argument("--verify", action="store_true",
                    help="after creating, do a real S3 PUT/GET/DELETE round "
                         "trip to prove the credentials work (needs boto3)")
    args = ap.parse_args()

    key = os.getenv("BUNNY_ACCOUNT_API_KEY", "").strip()
    if not key:
        sys.exit(
            "Set BUNNY_ACCOUNT_API_KEY first — your ACCOUNT API key, from\n"
            "  https://dash.bunny.net/account/settings  (Account → API).\n"
            "This is not the same as a storage zone password or a Stream key."
        )

    # The one hard rule from config/settings_base.py, enforced before anything
    # is created rather than at Django import time on the next deploy.
    if args.cms_zone and args.name == args.cms_zone:
        sys.exit(
            f"✗ '{args.name}' is your CMS storage zone.\n"
            "  Class recordings need their own zone: the CMS zone is shared\n"
            "  between dev and prod, so dev's test recordings would land in\n"
            "  the bucket prod serves publicly. settings_base.py raises\n"
            "  ImproperlyConfigured on exactly this. Pick another --name."
        )

    pull_name = args.pull_zone or f"{args.name}-pull"

    existing = find_storage_zone(key, args.name)
    if existing:
        print(f"• storage zone '{args.name}' already exists (id {existing['Id']})")
        zone = existing
        if not zone.get("Password"):
            sys.exit(
                "✗ Bunny's list endpoint did not return this zone's password.\n"
                "  Read it from the dashboard: Storage → your zone →\n"
                "  Access → 'Password', and set BUNNY_EGRESS_API_KEY to it."
            )
    else:
        print(f"• creating storage zone '{args.name}' in {args.region} …")
        zone = _call("POST", "/storagezone", key, {
            "Name": args.name,
            "Region": args.region,
            # 1 = S3 support enabled. Without this the zone has no
            # S3-compatible endpoint and LiveKit egress cannot write to it
            # at all.
            "StorageZoneType": 1,
            "ZoneTier": 0,  # Standard (HDD). Egress writes once, reads once.
        })
        print(f"  ✓ id {zone['Id']}")

    s3_host = (zone.get("S3Hostname") or "").replace("https://", "").strip("/")
    if not s3_host:
        # Documented shape, used only if the API stops returning the field.
        s3_host = f"{args.region.lower()}-s3.storage.bunnycdn.com"
        print(f"  ! API returned no S3Hostname; falling back to {s3_host}")
        print("    Confirm it under Storage → zone → Access → S3.")

    pulls = _call("GET", "/pullzone?page=1&perPage=1000", key)
    pull_items = pulls.get("Items", pulls if isinstance(pulls, list) else [])
    pull = next((p for p in pull_items if p.get("Name") == pull_name), None)
    if pull:
        print(f"• pull zone '{pull_name}' already exists (id {pull['Id']})")
    else:
        print(f"• creating pull zone '{pull_name}' → storage zone {zone['Id']} …")
        pull = _call("POST", "/pullzone", key, {
            "Name": pull_name,
            "StorageZoneId": zone["Id"],
            "OriginType": 2,  # storage zone origin
        })
        print(f"  ✓ id {pull['Id']}")

    hostnames = [h.get("Value") for h in (pull.get("Hostnames") or [])
                 if h.get("Value")]
    pull_host = hostnames[0] if hostnames else f"{pull_name}.b-cdn.net"

    print("\n" + "=" * 68)
    print("Add to your backend .env (the password is a SECRET):")
    print("=" * 68)
    print(f"LIVEKIT_EGRESS_ENABLED=true")
    print(f"BUNNY_EGRESS_ZONE={zone['Name']}")
    print(f"BUNNY_EGRESS_API_KEY={zone.get('Password', '<read from dashboard>')}")
    print(f"BUNNY_EGRESS_REGION={(zone.get('Region') or args.region).lower()}")
    print(f"BUNNY_EGRESS_S3_HOST={s3_host}")
    print(f"BUNNY_EGRESS_PULL_HOST={pull_host}")
    print("=" * 68)
    print(
        "\nNote: the pull zone is PUBLIC, which phase 3 needs — Bunny Stream's\n"
        "/videos/fetch cannot read a signed URL. Object keys carry a random\n"
        "segment for that reason, and phase 4 deletes each mp4 as soon as\n"
        "Stream has ingested it. Do not point this pull zone at any other\n"
        "bucket."
    )

    if args.verify:
        verify_s3(zone, s3_host, args)


def verify_s3(zone, s3_host, args):
    """Prove the credentials actually work — the one thing the Django test
    suite cannot cover, since it has no real zone to talk to."""
    print("\n• verifying S3 credentials with a real round trip …")
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        print("  ! boto3 not installed; skipping. `pip install boto3` to run this.")
        print("    (boto3 is NOT an app dependency — LiveKit does the S3 talking.)")
        return

    region = (zone.get("Region") or args.region).lower()
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{s3_host}",
        aws_access_key_id=zone["Name"],
        aws_secret_access_key=zone["Password"],
        region_name=region,
        # Bunny serves path-style only; this mirrors force_path_style=True in
        # livestream/services/egress.py.
        config=Config(s3={"addressing_style": "path"}),
    )
    key = "class-egress/_provision_check.txt"
    try:
        s3.put_object(Bucket=zone["Name"], Key=key, Body=b"ok")
        body = s3.get_object(Bucket=zone["Name"], Key=key)["Body"].read()
        assert body == b"ok", body
        s3.delete_object(Bucket=zone["Name"], Key=key)
    except Exception as e:
        print(f"  ✗ S3 round trip FAILED: {type(e).__name__}: {e}")
        print("    Check that the zone has S3 support enabled (Access → S3)")
        print("    and that the region matches. This is the exact failure")
        print("    LiveKit egress would hit, so fix it before rehearsing.")
        raise SystemExit(1)
    print("  ✓ PUT, GET and DELETE all succeeded — egress can write here.")


if __name__ == "__main__":
    main()
