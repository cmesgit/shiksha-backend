"""Put every pre-ticker Announcement into the navbar slot.

Separate from 0035 (which adds the columns) so the data step can be read,
reverted and reasoned about on its own.

⚠ WHY THIS IS NOT JUST A FIELD DEFAULT. `slots = JSONField(default=list)`
applies to rows created *after* the migration. Every row that already exists
gets `[]`, and `for_slot("navbar")` would then match none of them — silently
emptying the one ticker slot that is already live on the public site. The
default cannot fix that; only a data migration can.

Scale, measured on the boxes 2026-09-10: **one row on dev, one on prod** (and
prod's is a draft, so the strip renders nothing there today). So this is cheap
now — which is exactly the argument for landing it before it stops being.

`kind` is deliberately left blank. A pre-ticker announcement is a line of text
in a strip, not a card, and `Announcement.clean()` only requires a kind once a
slot other than the navbar is targeted. Inventing one here would put a card
treatment on rows nobody chose it for.
"""
from django.db import migrations

NAVBAR = "navbar"


def set_navbar_slot(apps, schema_editor):
    Announcement = apps.get_model("content", "Announcement")
    # Only rows with nothing set — so a re-run, or a row created between 0035
    # and this migration, is never overwritten.
    Announcement.objects.filter(slots=[]).update(slots=[NAVBAR])


def clear_navbar_slot(apps, schema_editor):
    """Reverse: drop the slot we added, leave anything richer alone.

    A row that has since been given more slots is not ours to empty, so only
    the exact `["navbar"]` value is reverted.
    """
    Announcement = apps.get_model("content", "Announcement")
    Announcement.objects.filter(slots=[NAVBAR]).update(slots=[])


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0035_announcement_body_announcement_image_and_more"),
    ]

    operations = [
        migrations.RunPython(set_navbar_slot, clear_navbar_slot),
    ]
