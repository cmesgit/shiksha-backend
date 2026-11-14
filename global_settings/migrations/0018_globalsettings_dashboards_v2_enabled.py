# Generated for design_handoff_dashboards_v2 Phase D2.
#
# ⚠ This is a DELIBERATE SIBLING of 0018_globalsettings_admin_v2_enabled.
# Both branch off 0017 because the two feature branches were authored in
# parallel without seeing each other's migration. Each branch is internally
# consistent (one leaf after 0017); the two leaves only meet when the
# branches merge, which is the documented workspace pattern — run
# `makemigrations --merge --noinput`, check `migrate --plan`, and commit the
# merge migration to both branches before deploying anywhere.
#
# Do NOT "fix" this by renumbering to 0019 and depending on the admin-v2
# migration: that one is uncommitted work on another branch, and depending on
# it would make this branch unmigratable on its own.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('global_settings', '0017_globalsettings_admission_abroad_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='globalsettings',
            name='dashboards_v2_enabled',
            field=models.BooleanField(default=False, help_text='Master switch for the redesigned student and teacher dashboard homes. While OFF the v2 routes are unreachable and the existing dashboards render unchanged. Adds routes; changes none.'),
        ),
    ]
