# PLACEMENT: backend/content/management/commands/localize_showcase_images.py
#
# Pulls externally-hosted ShowcaseCourse artwork into our own media storage,
# so the homepage stops hotlinking third-party CDNs.
#
#     python manage.py localize_showcase_images           # dry run (default)
#     python manage.py localize_showcase_images --yes     # actually write
#
# Why this exists
# ---------------
# Every one of the 18 featured cards on production resolved its thumbnail to
# an images.unsplash.com URL. That URL was never uploaded by anyone — it came
# in with the seed data (_catalog_seed_data.py / _homepage_seed_data.py), was
# written to ShowcaseCourse.image_url, and PublicFeaturedView's thumbnail
# fallback chain ends on image_url, so it is what every visitor loads.
#
# Three problems with leaving it:
#   * Every homepage view makes 18 requests to a CDN we do not control, which
#     can rate-limit, re-crop, or remove the asset with no warning to us.
#   * The images are stock photos under Unsplash's licence, presented as this
#     platform's own course artwork.
#   * An editor who opens the Showcase CMS sees a URL field, not an image they
#     can meaningfully replace.
#
# This command downloads each one ONCE into ShowcaseCourse.image (BunnyCDN
# when BUNNY_STORAGE_* is configured, local disk otherwise) and clears
# image_url, so the fallback chain lands on `card.image` instead. After this
# runs, the artwork is genuinely ours and swappable from the admin UI.
#
# Safety model (mirrors seed_about_images)
# ----------------------------------------
# * Dry run by default. Nothing is written and nothing is downloaded without
#   --yes, so you can see exactly what would change first.
# * CREATE-ONLY: a card that already has an uploaded `image` is left alone.
#   This can never clobber artwork an editor uploaded.
# * Idempotent: once a card is localized its image_url is empty, so a second
#   run sees nothing to do.
# * Refuses anything that isn't an http(s) URL returning a real image, and
#   caps the download size — the URLs come from the database, so this command
#   should not be a way to make the server fetch arbitrary things.

from urllib.parse import urlparse

import requests
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction

from content.models import ShowcaseCourse

# A stock photo at the card's render size is ~100 kB; 10 MB is a generous
# ceiling that still stops a mistyped URL streaming something huge into media.
MAX_BYTES = 10 * 1024 * 1024
TIMEOUT = 20

EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
}


def _slugish(text, fallback="card"):
    keep = [c.lower() if c.isalnum() else "-" for c in (text or "")]
    out = "".join(keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return (out or fallback)[:60]


class Command(BaseCommand):
    help = ("Download externally-hosted ShowcaseCourse image_urls into our own "
            "media storage (dry run unless --yes).")

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually download and write. Without this the command only reports.",
        )
        parser.add_argument(
            "--update", action="store_true",
            help="Also re-localize cards that already have an uploaded image "
                 "(default is create-only, which never touches an editor's upload).",
        )

    def handle(self, *args, **opts):
        write = opts["yes"]
        update = opts["update"]

        cards = ShowcaseCourse.objects.all().order_by("order", "id")
        localized = skipped = failed = 0

        with transaction.atomic():
            for card in cards:
                label = f"#{card.id} {card.title or '(untitled)'}"
                url = (card.image_url or "").strip()

                if card.image and not update:
                    self.stdout.write(f"  = {label}: already has an uploaded image, skipped")
                    skipped += 1
                    continue
                if not url:
                    self.stdout.write(f"  = {label}: no image_url, nothing to localize")
                    skipped += 1
                    continue

                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    self.stderr.write(self.style.WARNING(
                        f"  ! {label}: image_url is not an http(s) URL ({url!r}), skipped"
                    ))
                    skipped += 1
                    continue

                self.stdout.write(f"  + {label}: {parsed.netloc}{parsed.path[:48]}")
                if not write:
                    localized += 1
                    continue

                try:
                    payload, ext = self._fetch(url)
                except Exception as exc:  # noqa: BLE001 - report and keep going
                    self.stderr.write(self.style.ERROR(f"    ✗ {exc}"))
                    failed += 1
                    continue

                filename = f"{_slugish(card.title)}-{card.id}{ext}"
                # save=False here, then one explicit save() below, so the
                # image_url clear and the file attach land in a single UPDATE.
                card.image.save(filename, ContentFile(payload), save=False)
                card.image_url = ""
                card.save(update_fields=["image", "image_url", "updated_at"])
                self.stdout.write(self.style.SUCCESS(
                    f"    ✓ {len(payload) // 1024} kB → {card.image.name}"
                ))
                localized += 1

            if not write:
                # Nothing above should have written, but the whole loop runs
                # inside this atomic block — roll back explicitly so a future
                # edit that forgets a `if write:` guard cannot leak a write.
                transaction.set_rollback(True)

        if write:
            self.stdout.write(self.style.SUCCESS(
                f"Done. localized={localized} skipped={skipped} failed={failed}"
            ))
            if failed:
                self.stdout.write(self.style.WARNING(
                    "Cards that failed kept their original image_url and still "
                    "render — re-run to retry just those."
                ))
        else:
            self.stdout.write(self.style.WARNING(
                f"Dry run — {localized} card(s) would be localized, {skipped} skipped. "
                f"Re-run with --yes to apply."
            ))

    def _fetch(self, url):
        """Return (bytes, extension) for an image URL, or raise."""
        resp = requests.get(url, timeout=TIMEOUT, stream=True)
        resp.raise_for_status()

        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in EXT_BY_TYPE:
            raise ValueError(f"not an image (Content-Type: {ctype or 'unknown'})")

        # Read with a hard ceiling rather than trusting Content-Length, which a
        # remote host can understate or omit entirely.
        chunks, total = [], 0
        for chunk in resp.iter_content(64 * 1024):
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError(f"larger than the {MAX_BYTES // 1024 // 1024} MB cap")
            chunks.append(chunk)

        payload = b"".join(chunks)
        if not payload:
            raise ValueError("empty response body")
        return payload, EXT_BY_TYPE[ctype]
