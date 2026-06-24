"""
Retire the legacy one-to-one ``Profile`` model.

Personal / academic data now lives on ``LearnerProfile`` (keyed by account +
relationship). This migration is data-preserving and idempotent:

  1. For every existing ``Profile``, find (or create) the account's SELF
     ``LearnerProfile`` and copy across any field the learner doesn't already
     have. We only fill blanks, so richer data already captured on the
     LearnerProfile is never clobbered.
  2. Delete the ``Profile`` model.

If there are no ``Profile`` rows (fresh DB / already migrated), step 1 is a
no-op and only the table drop runs.
"""
from django.db import migrations


# Fields that exist on BOTH Profile and LearnerProfile and are safe to carry over.
SHARED_FIELDS = [
    "first_name", "last_name", "full_name", "phone", "gender", "date_of_birth",
    "avatar_emoji", "state", "district", "city_town", "pin_code",
    "father_name", "father_phone", "mother_name", "mother_phone",
    "guardian_name", "guardian_phone", "parent_guardian_email",
    "currently_studying", "current_class", "stream", "board", "board_other",
    "school_name", "academic_year", "highest_education", "reason_not_studying",
]
# Image/file fields copied by their stored name (the underlying file is untouched).
IMAGE_FIELDS = ["profile_photo", "avatar_image"]


def copy_profiles_to_learners(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    LearnerProfile = apps.get_model("accounts", "LearnerProfile")

    for profile in Profile.objects.select_related("user").all():
        account = profile.user
        if account is None:
            continue

        # Resolve the account's SELF learner profile (prefer default), else create one.
        learner = (
            LearnerProfile.objects.filter(account=account, relationship="SELF")
            .order_by("-is_default", "created_at")
            .first()
        )
        if learner is None:
            has_default = LearnerProfile.objects.filter(
                account=account, is_default=True
            ).exists()
            display_name = (profile.first_name or "").strip() or account.email.split("@")[0]
            learner = LearnerProfile.objects.create(
                account=account,
                display_name=display_name or "Learner",
                relationship="SELF",
                is_default=not has_default,
            )

        dirty = False

        # Fill only the blanks on the learner.
        for field in SHARED_FIELDS:
            current = getattr(learner, field, None)
            incoming = getattr(profile, field, None)
            if not current and incoming:
                setattr(learner, field, incoming)
                dirty = True

        # student_id is unique — copy only if free and not already taken.
        if not learner.student_id and profile.student_id:
            taken = (
                LearnerProfile.objects.filter(student_id=profile.student_id)
                .exclude(pk=learner.pk)
                .exists()
            )
            if not taken:
                learner.student_id = profile.student_id
                dirty = True

        # Image fields: copy the stored name if the learner has none.
        for field in IMAGE_FIELDS:
            incoming = getattr(profile, field, None)
            name = getattr(incoming, "name", None)
            if name and not getattr(learner, field, None):
                setattr(learner, field, name)
                dirty = True

        if not learner.display_name:
            learner.display_name = (learner.full_name or account.email.split("@")[0] or "Learner")
            dirty = True

        if dirty:
            learner.save()


def noop_reverse(apps, schema_editor):
    # Profile is dropped; there is nothing to restore on reverse.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_learnerprofile_and_more"),
    ]

    operations = [
        migrations.RunPython(copy_profiles_to_learners, noop_reverse),
        migrations.DeleteModel(name="Profile"),
    ]
