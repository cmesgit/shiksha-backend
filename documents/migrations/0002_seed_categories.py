"""Seed the default browsable document categories (idempotent).

Mirrors the delivered Explore.html "Browse by category" grid. Safe to re-run;
uses get_or_create keyed on slug and refreshes display metadata.
"""
from django.db import migrations

from documents.constants import DEFAULT_CATEGORIES


def seed(apps, schema_editor):
    DocumentCategory = apps.get_model("documents", "DocumentCategory")
    for order, (slug, name, icon, color, blurb) in enumerate(DEFAULT_CATEGORIES):
        cat, created = DocumentCategory.objects.get_or_create(
            slug=slug,
            defaults={"name": name, "icon": icon, "color": color,
                      "blurb": blurb, "order": order},
        )
        if not created:
            cat.name, cat.icon, cat.color, cat.blurb = name, icon, color, blurb
            cat.save(update_fields=["name", "icon", "color", "blurb"])


def unseed(apps, schema_editor):
    DocumentCategory = apps.get_model("documents", "DocumentCategory")
    slugs = [c[0] for c in DEFAULT_CATEGORIES]
    DocumentCategory.objects.filter(slug__in=slugs).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
