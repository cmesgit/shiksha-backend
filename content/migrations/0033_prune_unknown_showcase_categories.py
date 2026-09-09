"""Strip category slugs that no ShowcaseCategory row can ever validate.

`ShowcaseCourse.clean()` rejects any slug in `categories` that is not a live
`ShowcaseCategory.slug`. Migration `0028_seed_showcase_categories` assumed
seeding `boards`/`class8-12`/`competitive` "makes every existing card valid
with no data migration on the JSON column itself" — but the seed data of the
day also tagged three cards with the reserved sentinel `"all"`, written
through `create()`/`save()`, neither of which runs `clean()`. And `"all"` can
never be made valid: `ShowcaseCategory.clean()` refuses it outright, because
the homepage renders its own "All" tab.

The consequence was not a bad tag, it was an unsaveable row. The admin
serializer's `FullCleanMixin` validates the whole stored instance on a PATCH,
so a one-key `{"status": "draft"}` write — the show/hide toggle — failed with
`Unknown category: all` on exactly the cards that predate the fix. Newly
created cards take their categories from the live taxonomy, so they were
fine, which is why this presented as "the old cards won't toggle".

Prunes rather than blanks: a card tagged `["competitive", "all"]` keeps
`["competitive"]`. Irreversible in the sense that matters — the removed slugs
were never valid, so there is nothing to put back.
"""

from django.db import migrations


def prune_unknown_categories(apps, schema_editor):
    ShowcaseCourse = apps.get_model("content", "ShowcaseCourse")
    ShowcaseCategory = apps.get_model("content", "ShowcaseCategory")

    valid = set(ShowcaseCategory.objects.values_list("slug", flat=True))
    if not valid:
        # No taxonomy to validate against. Pruning here would untag every
        # card on the strength of an empty table, so leave the data alone —
        # 0028 seeds the rows, and a fresh install has no cards yet anyway.
        return

    for card in ShowcaseCourse.objects.exclude(categories=[]):
        current = card.categories
        if not isinstance(current, list):
            # clean() would reject this too, but a non-list is a different
            # defect and guessing at its intent is worse than leaving it.
            continue
        kept = [slug for slug in current if slug in valid]
        if kept != current:
            card.categories = kept
            card.save(update_fields=["categories"])


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0032_backfill_course_media_usages"),
    ]

    operations = [
        migrations.RunPython(
            prune_unknown_categories, migrations.RunPython.noop,
        ),
    ]
