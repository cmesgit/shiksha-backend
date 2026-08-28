"""Backfill usages for pictures embedded in post bodies.

Migration 0023 recorded a usage for every picture a row OWNED in a FileField.
It did not look inside `BlogPost.body_html` / `body_blocks`, so a picture that
only ever appeared inside a post's text reported "used on 0 pages" and could be
deleted from the library — leaving a broken image on a published post. That is
both the exact breakage the delete guard exists to prevent and, per
ContentImage's own docstring, the main thing the library is for.

Only ADDS usage rows. It never creates ContentImage rows: an embedded URL that
matches nothing in the library is a picture the library does not know about
(hand-written HTML, an external host), and inventing a row for it would put
files in the Pictures screen that nobody uploaded there.

Reverse deletes only the rows this created, identified by field_name — the
owned-field usages from 0023 use different field names and are left alone.
"""
import json
import re
from urllib.parse import unquote, urlsplit

from django.db import migrations

EMBEDDING_FIELDS = [("content", "BlogPost", "body_html"),
                    ("content", "BlogPost", "body_blocks")]

# Matches both HTML attributes (src="…") and JSON keys ("src": "…") — the
# block editor stores its bodies as JSON, where the key carries a closing quote
# before the colon.
_SRC_RE = re.compile(r'(?:src|href|url)["\']?\s*[=:]\s*["\']([^"\']+)["\']', re.I)


def _storage_name(url):
    path = urlsplit(url or "").path
    if "/media/" in path:
        path = path.split("/media/", 1)[1]
    return unquote(path).lstrip("/")


def backfill(apps, schema_editor):
    from django.contrib.contenttypes.models import ContentType

    ContentImage = apps.get_model("content", "ContentImage")
    MediaUsage = apps.get_model("content", "MediaUsage")

    # Path and basename indexes, built once — the alternative is a query per
    # embedded URL per post.
    by_path, by_base = {}, {}
    for asset in ContentImage.objects.all():
        name = asset.file.name if hasattr(asset.file, "name") else str(asset.file)
        if not name:
            continue
        by_path[name] = asset
        by_base.setdefault(name.rsplit("/", 1)[-1], asset)

    if not by_path:
        return

    for app_label, model_name, field in EMBEDDING_FIELDS:
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            continue
        ct = ContentType.objects.get_for_model(model)

        for row in model.objects.all().only("pk", field):
            value = getattr(row, field, None)
            if not value:
                continue
            text = value if isinstance(value, str) else json.dumps(value)
            for url in _SRC_RE.findall(text):
                name = _storage_name(url)
                if not name:
                    continue
                asset = by_path.get(name) or by_base.get(name.rsplit("/", 1)[-1])
                if asset is None:
                    continue
                MediaUsage.objects.get_or_create(
                    asset=asset, content_type=ct, object_id=row.pk,
                    field_name=field,
                )


def unbackfill(apps, schema_editor):
    MediaUsage = apps.get_model("content", "MediaUsage")
    MediaUsage.objects.filter(
        field_name__in=[f for (_, _, f) in EMBEDDING_FIELDS]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0024_remove_announcement_is_active_and_more"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(backfill, unbackfill)]
