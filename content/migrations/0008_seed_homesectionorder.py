"""Seed one HomeSectionOrder row per homepage section (idempotent), in the
exact sequence shiksha-frontend's ShikshaHome.jsx already hardcodes — so
turning this feature on changes nothing about the live site until an admin
actually reorders something.
"""
from django.db import migrations

from content.models import HOMEPAGE_SECTIONS


def seed(apps, schema_editor):
    HomeSectionOrder = apps.get_model("content", "HomeSectionOrder")
    for order, section in enumerate(HOMEPAGE_SECTIONS):
        HomeSectionOrder.objects.get_or_create(
            section=section.value, defaults={"order": order, "is_visible": True},
        )


def unseed(apps, schema_editor):
    HomeSectionOrder = apps.get_model("content", "HomeSectionOrder")
    sections = [s.value for s in HOMEPAGE_SECTIONS]
    HomeSectionOrder.objects.filter(section__in=sections).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0007_homesectionorder"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
