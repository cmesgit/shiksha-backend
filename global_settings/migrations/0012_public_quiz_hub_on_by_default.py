"""Phase 9 — turn the public Quiz Hub on.

⚠ THE `AlterField` ALONE WOULD DO NOTHING ON ANY DEPLOYED SYSTEM.
A model default applies when a row is CREATED, and GlobalSettings is a
singleton whose row already exists on dev and prod with the value False. So
this migration also writes the existing row; without the second operation
"the default is now True" would be true of a fresh database and of nothing
else, and the flag would still read False everywhere that matters.

Forcing the row to True is correct *here specifically* because the flag has
never been on: every False in existence is "not launched yet", not "an admin
turned this off". That stops being true the moment this ships, which is why
the data step only ever runs once and the reverse leaves the row alone.
"""
from django.db import migrations, models


def turn_on(apps, schema_editor):
    GlobalSettings = apps.get_model("global_settings", "GlobalSettings")
    GlobalSettings.objects.update(public_quiz_hub_enabled=True)


def noop(apps, schema_editor):
    """Reversing this restores the DEFAULT but not the row.

    Someone rolling back a deploy wants the code reverted, not the hub
    silently switched off underneath whoever is using it — and if they do
    want it off, that is one click in admin settings.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("global_settings", "0011_globalsettings_public_quiz_hub_enabled"),
    ]

    operations = [
        migrations.AlterField(
            model_name="globalsettings",
            name="public_quiz_hub_enabled",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Master switch for the public Quiz Hub at /quiz and the "
                    "admin question-bank authoring screens behind it. Turning "
                    "this OFF takes the public quiz pages down and returns 503 "
                    "from every public quiz endpoint — it does not affect the "
                    "academy."
                ),
            ),
        ),
        migrations.RunPython(turn_on, noop),
    ]
