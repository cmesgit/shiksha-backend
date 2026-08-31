"""Seed the three filter tabs the homepage has always had.

Until 0027 the tab list lived in `ShowcaseCourse.CATEGORY_CHOICES` (and, in
duplicate, in shiksha-frontend's homeData.js and Admin-dashboard's
CardFormModal.jsx). Cards already store these slugs in their `categories`
JSON column, and `ShowcaseCourse.clean()` now validates against this table —
so without these rows every existing card would fail validation on its next
save. That makes this migration load-bearing, not cosmetic.

The labels match what the homepage renders today, including the EN DASH in
"Class 8–12" (U+2013, not a hyphen) — the tab label is copy, and changing it
here would silently change the public site.

`get_or_create` on slug so this is safe to re-run and cannot collide with a
row an admin has already created by hand.
"""

from django.db import migrations

# slug, label, order — order matches the current left-to-right tab order.
SEED = [
    ("boards", "Boards", 0),
    ("class8-12", "Class 8–12", 1),
    ("competitive", "Competitive", 2),
]


def seed(apps, schema_editor):
    ShowcaseCategory = apps.get_model("content", "ShowcaseCategory")
    for slug, label, order in SEED:
        ShowcaseCategory.objects.get_or_create(
            slug=slug, defaults={"label": label, "order": order, "is_active": True},
        )


def unseed(apps, schema_editor):
    """Removes only the three seeded slugs, never an admin-authored tab.

    Note this deliberately does NOT clear those slugs out of
    ShowcaseCourse.categories: reversing a migration must not rewrite content
    rows, and the forward direction re-creates them.
    """
    ShowcaseCategory = apps.get_model("content", "ShowcaseCategory")
    ShowcaseCategory.objects.filter(slug__in=[s for s, _, _ in SEED]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0027_showcasecategory"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
