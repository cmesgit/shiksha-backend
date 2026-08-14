# Data-fix migration.
#
# BUG (found and fixed same-day as 0012, before this ever reached
# production): `translation_group = models.UUIDField(default=uuid.uuid4, ...)`
# was assumed to give every EXISTING row its own distinct UUID when the
# column was added, per the design note in 0012's own model comment ("every
# existing post becomes a translation-group-of-one until a Hindi sibling is
# added later"). That's true on SQLite, which rebuilds the whole table row by
# row and genuinely calls the Python callable once per row. It is NOT true on
# PostgreSQL: Django's schema editor evaluates a callable `default` used by
# `AddField` exactly ONCE, and applies that single resulting value as a plain
# SQL `DEFAULT` backfilled onto every pre-existing row. On shiksha-dev's real
# Postgres database this meant all 115 legacy BlogPost rows ended up sharing
# one identical `translation_group` — which would have made
# `duplicate_translation`'s sibling check and the public API's `translations`
# array treat any two unrelated legacy posts as if they were translations of
# each other the moment a single real translation pair was ever created
# anywhere in that shared group.
#
# Fix: any translation_group shared by MORE than 2 rows is unambiguously this
# artifact (a real translation pair is at most one row per locale — 2 today,
# `en`+`hi`). Reassign every row in such an oversized group its own fresh,
# distinct UUID. A genuine 2-row (or 1-row) group is left untouched. Safe to
# run on an already-correct database (no groups will exceed 2 rows, so it's a
# no-op) and on a fresh empty database (no rows at all).
import uuid

from django.db import migrations
from django.db.models import Count


def fix_shared_translation_groups(apps, schema_editor):
    BlogPost = apps.get_model("content", "BlogPost")
    oversized = (
        BlogPost.objects.values("translation_group")
        .annotate(n=Count("id"))
        .filter(n__gt=2)
    )
    for row in oversized:
        for post in BlogPost.objects.filter(translation_group=row["translation_group"]):
            post.translation_group = uuid.uuid4()
            post.save(update_fields=["translation_group"])


def noop_reverse(apps, schema_editor):
    # Not reversible in any meaningful sense (the original shared-group state
    # was a bug, not a state worth restoring) — a plain no-op lets `migrate`
    # still un-apply this migration if ever needed without erroring.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0012_remove_blogpost_content_blog_unique_chapter_and_more"),
    ]

    operations = [
        migrations.RunPython(fix_shared_translation_groups, noop_reverse),
    ]
