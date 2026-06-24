# accounts/migrations/0015_teacher_track_status.py
#
# Adds per-track status to TeacherProfile so the academy/skill-dev dashboard
# switch (in both the teacher and student apps) can show locked / pending /
# approved states, and so a teacher can apply for the *other* track later.
#
# NOTE on dependencies: this repo currently has TWO 0014 migrations that both
# build on 0013 (a fork — one alters `teacher_password`, one removes it). The
# live model keeps `teacher_password`, so we attach to the matching leaf. If
# `migrate` reports multiple leaf nodes, run `makemigrations --merge` (or drop
# the stale 0014_remove_teacher_password.py, which contradicts the model).

from django.db import migrations, models


LOCKED = "locked"
PENDING = "pending"
APPROVED = "approved"

STATUS_CHOICES = [
    (LOCKED, "Locked"),
    (PENDING, "Pending review"),
    (APPROVED, "Approved"),
]


def backfill_track_status(apps, schema_editor):
    """Derive academy/skill status from the legacy teacher_type + is_approved.

    academy == FACULTY track, skill == GUEST track.
    Guest teachers are auto-listed (approved); faculty need admin approval.
    """
    TeacherProfile = apps.get_model("accounts", "TeacherProfile")
    for tp in TeacherProfile.objects.all():
        ttype = tp.teacher_type
        approved = bool(tp.is_approved)

        if ttype == "BOTH":
            academy = APPROVED if approved else PENDING
            skill = APPROVED
        elif ttype == "GUEST":
            academy = LOCKED
            skill = APPROVED if approved else PENDING
        elif ttype == "FACULTY":
            academy = APPROVED if approved else PENDING
            skill = LOCKED
        else:
            academy = LOCKED
            skill = LOCKED

        tp.academy_status = academy
        tp.skill_status = skill
        tp.save(update_fields=["academy_status", "skill_status"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0014_alter_teacherprofile_teacher_password"),
    ]

    operations = [
        migrations.AddField(
            model_name="teacherprofile",
            name="academy_status",
            field=models.CharField(
                choices=STATUS_CHOICES, default=LOCKED, max_length=10
            ),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="skill_status",
            field=models.CharField(
                choices=STATUS_CHOICES, default=LOCKED, max_length=10
            ),
        ),
        migrations.RunPython(backfill_track_status, noop),
    ]
