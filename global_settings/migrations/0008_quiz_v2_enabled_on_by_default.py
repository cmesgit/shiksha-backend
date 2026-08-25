"""Phase 10 item 3: `quiz_v2_enabled` on by default, and on the live row.

The AlterField alone would change nothing observable. GlobalSettings is a
SINGLETON — the one existing row was created with the old default and keeps
`False` — so a default change only affects rows that will never be created.
The RunPython is what actually flips it.

⚠ This flag gates NOTHING. The rebuilt teacher builder, student hub, attempt
and results screens all shipped unconditionally; the only mention of it
outside admin settings is the default shape in each app's AuthContext. It is
now a truthful record that v2 is the shipped system, not a switch — leaving it
False claimed "v2 is off" while v2 was live in production, which is worse than
useless to the next reader. Turning it off does NOT roll anything back.

`ai_question_drafting_enabled` is deliberately left OFF: that one is a real
gate (the "Generate with AI" slot in QuizBuilder reads it) and PROMPT.md
non-negotiable #6 says it ships off and admin-controlled.

Reverse restores default=False and sets the row back, so this is fully
reversible — it carries no information that cannot be recomputed.
"""

from django.db import migrations, models


def turn_on(apps, schema_editor):
    GlobalSettings = apps.get_model("global_settings", "GlobalSettings")
    GlobalSettings.objects.all().update(quiz_v2_enabled=True)


def turn_off(apps, schema_editor):
    GlobalSettings = apps.get_model("global_settings", "GlobalSettings")
    GlobalSettings.objects.all().update(quiz_v2_enabled=False)


class Migration(migrations.Migration):

    dependencies = [
        ('global_settings', '0007_globalsettings_ai_question_drafting_enabled_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='globalsettings',
            name='quiz_v2_enabled',
            field=models.BooleanField(default=True, help_text='Records that the redesigned quiz system is live. Gates nothing — the v2 screens ship unconditionally; turning this off does not revert them.'),
        ),
        migrations.RunPython(turn_on, turn_off),
    ]
