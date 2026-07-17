"""Correct default category colors to match the delivered Explore.html exactly.

0002 mis-transcribed several colors on first seed; this re-applies the fixed
DEFAULT_CATEGORIES by slug. Idempotent — only touches rows that already exist.
"""
from django.db import migrations

from documents.constants import DEFAULT_CATEGORIES


def fix_colors(apps, schema_editor):
    DocumentCategory = apps.get_model("documents", "DocumentCategory")
    for order, (slug, name, icon, color, blurb) in enumerate(DEFAULT_CATEGORIES):
        DocumentCategory.objects.filter(slug=slug).update(
            name=name, icon=icon, color=color, blurb=blurb, order=order,
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0002_seed_categories"),
    ]

    operations = [
        migrations.RunPython(fix_colors, noop),
    ]
