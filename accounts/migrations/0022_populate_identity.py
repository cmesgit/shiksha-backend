# PLACEMENT: backend/backend/accounts/migrations/0022_populate_identity.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/accounts/migrations/0022_populate_identity.py
#
# M1 data migration: one Identity row per existing LearnerProfile and
# TeacherProfile. Uses historical models (apps.get_model) exclusively — never
# imports the live accounts/chat modules — so this migration keeps working
# correctly if those modules change shape later.
#
# profile_id is explicitly str()'d: LearnerProfile's pk is a UUID,
# TeacherProfile's is a plain integer (BigAutoField) — profile_id is a
# CharField precisely so it can hold str(pk) for either without a UUIDField
# silently mangling the integer case (see accounts/models.py's Identity
# docstring for how that bug actually surfaced during M1 testing).
#
# display_name here is deliberately simple (LearnerProfile.display_name;
# User.username/email for teachers) rather than the fuller "best effort"
# name logic chat/views.py uses for the directory. That richer logic lives
# in accounts/signals.py, which runs on every future save and will correct
# these rows the first time either profile is touched after this deploys.
# This migration's only job is to make sure a row EXISTS for every profile
# that predates the registry.
from django.db import migrations


def populate_identities(apps, schema_editor):
    LearnerProfile = apps.get_model("accounts", "LearnerProfile")
    TeacherProfile = apps.get_model("accounts", "TeacherProfile")
    Identity = apps.get_model("accounts", "Identity")

    learner_rows = [
        Identity(
            kind="L",
            profile_id=str(lp.id),
            display_name=(lp.display_name or lp.full_name or "")[:150],
            account_id=lp.account_id,
            is_active=lp.is_active,
        )
        for lp in LearnerProfile.objects.all().iterator()
    ]
    Identity.objects.bulk_create(learner_rows, ignore_conflicts=True, batch_size=500)

    teacher_rows = []
    for tp in TeacherProfile.objects.select_related("user").all().iterator():
        name = (tp.user.username or tp.user.email or "")[:150]
        teacher_rows.append(Identity(
            kind="T",
            profile_id=str(tp.id),
            display_name=name,
            account_id=tp.user_id,
            is_active=True,
        ))
    Identity.objects.bulk_create(teacher_rows, ignore_conflicts=True, batch_size=500)


def remove_identities(apps, schema_editor):
    """Reverse: safe to just delete the L/T rows this migration created —
    they're a cache, not a source of truth, and re-running the forwards
    function regenerates them identically."""
    Identity = apps.get_model("accounts", "Identity")
    Identity.objects.filter(kind__in=["L", "T"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0021_identity"),
    ]

    operations = [
        migrations.RunPython(populate_identities, remove_identities),
    ]
