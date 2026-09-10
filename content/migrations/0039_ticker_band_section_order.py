"""Give the ticker band a HomeSectionOrder row.

⚠ WHY THIS IS REQUIRED, not tidy-up. Adding a value to `HomeSection` does not
create the ordering row that makes it real. Two things break without it:

1. `ShikshaHome.jsx` renders the sections the ORDER endpoint returns. A
   section with no row is simply never in that list, so the band would never
   appear no matter what an admin puts in the queue — and nothing would say
   why.
2. `home-section-order/reorder/` demands the COMPLETE ordered set of section
   keys and 400s on a partial list, deliberately, so a stale tab cannot drop a
   section off the homepage. An enum value with no row is "unexpected" to that
   endpoint.

**Appended last, not slotted in.** Its position is an admin's decision, and
inserting it mid-list would renumber every section below it — silently
reordering a homepage that somebody has already arranged. Last is the one
position that changes nothing that already exists.

`is_visible=True` is safe despite that: the band renders nothing at all unless
`live_ticker_enabled` is on AND the queue holds an item targeting the
`home_band` slot. An admin turning the flag on does not get a surprise empty
band.
"""
from django.db import migrations

SECTION = "ticker_band"


def add_row(apps, schema_editor):
    HomeSectionOrder = apps.get_model("content", "HomeSectionOrder")
    if HomeSectionOrder.objects.filter(section=SECTION).exists():
        return
    # Append: one past whatever is currently last. `order` is a
    # PositiveSmallIntegerField, so start from 0 on an empty table.
    last = HomeSectionOrder.objects.order_by("-order").first()
    HomeSectionOrder.objects.create(
        section=SECTION,
        order=(last.order + 1) if last else 0,
        is_visible=True,
    )


def drop_row(apps, schema_editor):
    HomeSectionOrder = apps.get_model("content", "HomeSectionOrder")
    HomeSectionOrder.objects.filter(section=SECTION).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0038_alter_homecontentblock_section_and_more"),
    ]

    operations = [
        migrations.RunPython(add_row, drop_row),
    ]
