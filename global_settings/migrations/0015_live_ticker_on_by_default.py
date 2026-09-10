"""Phase 9 — turn the live ticker on.

⚠ THE `AlterField` ALONE WOULD DO NOTHING ON ANY DEPLOYED SYSTEM.
A model default applies when a row is CREATED, and GlobalSettings is a
singleton whose row already exists on dev and prod holding False. So this
also writes the existing row; without the second operation "the default is
now True" would be true of a fresh database and of nothing else.

Forcing the row to True is correct *here specifically* because the flag has
never been on in production: every False in existence means "not launched
yet", not "an admin turned this off". That stops being true the moment this
ships, which is why the data step runs once and the reverse leaves the row
alone.

Turning it on renders NOTHING by itself — every slot is empty until an admin
queues an item on /content/ticker, and the navbar strip keeps showing exactly
what it showed before. This opens the gate; it does not publish content.
"""
from django.db import migrations, models


def turn_on(apps, schema_editor):
    GlobalSettings = apps.get_model("global_settings", "GlobalSettings")
    GlobalSettings.objects.update(live_ticker_enabled=True)


def noop(apps, schema_editor):
    """Reversing restores the DEFAULT but not the row — a rollback should
    revert code, not silently switch the ticker off underneath whoever is
    using it. If they do want it off, that is one click in admin settings."""


class Migration(migrations.Migration):

    dependencies = [
        ("global_settings", "0014_globalsettings_live_ticker_enabled"),
    ]

    operations = [
        migrations.AlterField(
            model_name="globalsettings",
            name="live_ticker_enabled",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Master switch for the CMS live ticker across all its slots — the "
                    "navbar strip, homepage hero and band, courses rail, footer, "
                    "student dashboard rail and the login/signup screens. While OFF, "
                    "none of them render and the existing announcement strip is "
                    "unaffected."
                ),
            ),
        ),
        migrations.RunPython(turn_on, noop),
    ]
