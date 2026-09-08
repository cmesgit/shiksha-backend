"""
courses/management/commands/repair_nonteacher_assignments.py

End ACTIVE TeachingAssignment rows held by accounts that do not hold the
TEACHER role, hand the subject to the teacher who is really staffing it, and
re-file any content those accounts own on it.

    python manage.py repair_nonteacher_assignments --dry-run   # report only
    python manage.py repair_nonteacher_assignments             # apply

WHAT THIS IS FIXING
-------------------
`courses/0029_migrate_subject_teacher_to_teaching_assignment` copied every
legacy `SubjectTeacher` row in verbatim, with no role check — and the
track-gated assignment picker only landed the day before. So prod acquired
course-wide PRIMARY rows for four accounts that have never held the TEACHER
role, never had a `TeacherProfile`, and never applied to teach: three
STUDENTs and one GUEST. One of them holds eleven subjects spanning Physics,
Chemistry, Maths, English, Hindi and three Social Science variants and has
never logged in — nobody teaches that combination.

The verdict is therefore END, not restore. There is nothing to restore:
`UserRole.Meta.unique_together` is `("user", "role")`, so an account either
has a TEACHER row or has never had one, and none of these four does.
Restoring would be *granting* the role for the first time, to accounts that
are learners.

WHY THE ROLE GAP IS NOT COSMETIC
--------------------------------
An active assignment outlives the TEACHER role, and a lot of the platform
reads the assignment rather than the role:

  * `courses.services.teaches_subject` is the read gate on study material
    (`materials/views.py:_authorize_subject_materials`), and it returns
    TEACHER_UNRESTRICTED — every batch's material on that subject, to an
    account whose only role is STUDENT.
  * `_require_material_editor` then accepts `teaches_subject`, so the same
    account may DELETE that material. `DeleteStudyMaterial` is
    `[IsAuthenticated]` with no teacher-context requirement, deliberately
    (see its docstring) — the class gate used to make the admin branch
    unreachable.

Quizzes are not exposed the same way: all four `quiz.created_by` gates call
`require_teacher_context`, and `_in_teacher_context` requires
`has_role("TEACHER")`, which these accounts fail.

WHY IT ALSO RE-FILES CONTENT
----------------------------
Ending the row closes `teaches_subject`, but `_require_material_editor` has a
second clause — `material.uploaded_by_id == user.id`. Ownership is itself a
standing grant to mutate, so ending the assignment alone would leave a
non-teacher account able to delete live, student-visible material it happens
to own. `uploaded_by` / `created_by` must move too, or the fix is half done.

Content is moved with `.update()`, never `instance.save()`. `Quiz` has a
pre_save/post_save pair (`activity/signals.py:443`) that notifies every
active enrollee on a False→True `is_assigned` transition. These quizzes are
already assigned so `_was_assigned` would be True and the receiver would
early-return — but a data repair should not be one refactor of that guard
away from mailing a course, so it bypasses signals outright.

WHY IT ALSO ADDS A COURSE-WIDE ROW
----------------------------------
171 of 172 staffed subjects carry BOTH a course-wide row and a batch-scoped
one. Merely ending these rows would make the affected subjects the only ones
on the platform with batch coverage alone — and `is_teacher_of` accepts a
course-wide row OR a row for that exact batch, so the first time a second
batch is created those subjects would be unstaffed for it while every other
subject stayed staffed. The command restores the normal shape instead.

SAFETY
------
  * It NEVER ends a row that would leave the subject with no active teacher.
    A subject with no role-holding replacement is reported and skipped, not
    emptied — an unstaffed subject is worse than a wrongly-staffed one.
  * A replacement must hold the TEACHER role AND be `is_active`, or it is not
    a replacement: a deactivated account cannot log in to look after what it
    owns. Same three predicates as `dashboard/admin_academy.py`'s options
    endpoint, for the same reason.
  * `--dry-run` rolls back by RAISING out of `transaction.atomic()`.
    `transaction.savepoint()` in autocommit returns None and its matching
    `savepoint_rollback()` does nothing, so a savepoint-based dry run commits
    everything it claims to be discarding. That bug shipped once here already
    (see `seed_academy_launch`); do not reintroduce it.
  * Rows are ENDED, never deleted (`courses/models.py:528-530`). "Who taught
    Class 10 Science in July" stays answerable.
  * `MaterialFile.uploaded_by` is deliberately left alone. It records who
    pushed the bytes, and the temp-file claim filter only matches unclaimed
    rows (`material IS NULL`), so nothing gates on it once the material
    exists.
  * Idempotent: it discovers its own work from the role/assignment mismatch,
    so a second run finds nothing to do.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from courses.models import TeachingAssignment


class _DryRun(Exception):
    """Raised to roll the transaction back. See SAFETY above."""


class Command(BaseCommand):
    help = (
        "End ACTIVE TeachingAssignment rows held by accounts without the "
        "TEACHER role, and re-file the content they own on those subjects."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and write nothing.",
        )

    # ------------------------------------------------------------------

    def _replacement_for(self, subject, excluded_ids):
        """The teacher who is really staffing `subject`, or None.

        PRIMARY wins over ASSISTANT/SUBSTITUTE; among equals, the lowest
        `order` — the same precedence the staffing screens display in.
        """
        rows = (
            TeachingAssignment.objects
            .filter(
                subject=subject,
                is_active=True,
                teacher__isnull=False,
                teacher__is_active=True,
                teacher__user_roles__role__name="TEACHER",
                teacher__user_roles__is_active=True,
            )
            .exclude(teacher_id__in=excluded_ids)
            # The user_roles join multiplies rows for an account holding the
            # role more than once across history.
            .distinct()
            .select_related("teacher")
            .order_by("order", "id")
        )
        return (
            rows.filter(role=TeachingAssignment.ROLE_PRIMARY).first()
            or rows.first()
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        try:
            with transaction.atomic():
                self._run(dry)
                if dry:
                    raise _DryRun
        except _DryRun:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — transaction rolled back, nothing was written."
            ))

    # ------------------------------------------------------------------

    def _run(self, dry):
        from assignments.models import Assignment
        from materials.models import StudyMaterial
        from quizzes.models import Quiz

        User = get_user_model()
        now = timezone.now()

        holders = [
            u for u in User.objects
            .filter(teaching_assignments__is_active=True)
            .distinct()
            .order_by("email")
            if not u.has_role("TEACHER")
        ]
        excluded_ids = [u.id for u in holders]

        self.stdout.write(
            "accounts holding an ACTIVE assignment without the TEACHER "
            "role: %d" % len(holders)
        )
        if not holders:
            self.stdout.write(self.style.SUCCESS("Nothing to do."))
            return

        ended = refiled_m = refiled_q = added_cw = skipped = 0

        for user in holders:
            roles = ", ".join(
                "%s%s" % (r.role.name, "" if r.is_active else " (inactive)")
                for r in user.user_roles.select_related("role")
            ) or "none"
            self.stdout.write("\n%s" % ("-" * 72))
            self.stdout.write("%s  [roles: %s]" % (user.email, roles))

            rows = (
                TeachingAssignment.objects
                .filter(teacher=user, is_active=True)
                .select_related("subject", "subject__course", "batch")
                .order_by("subject__course__title", "subject__name")
            )
            for ta in rows:
                subject = ta.subject
                label = "%s / %s" % (subject.course.title, subject.name)
                target = self._replacement_for(subject, excluded_ids)

                if target is None:
                    skipped += 1
                    self.stdout.write(self.style.ERROR(
                        "  SKIP  %s — no role-holding replacement; ending "
                        "this row would leave the subject unstaffed." % label
                    ))
                    continue

                new_owner = target.teacher
                self.stdout.write("  %s" % label)
                self.stdout.write(
                    "        end %s %s row  ->  hand to %s"
                    % (ta.role,
                       ta.batch.code if ta.batch_id else "course-wide",
                       new_owner.email)
                )

                # END, never delete. update() so no save()/signal fires.
                TeachingAssignment.objects.filter(pk=ta.pk).update(
                    is_active=False, ended_at=now,
                )
                ended += 1

                # Re-file content this account owns ON THIS SUBJECT. Scoped
                # per (subject, owner) rather than per owner: an account must
                # not shed content on a subject it legitimately still holds.
                n = StudyMaterial.objects.filter(
                    subject=subject, uploaded_by=user,
                ).update(uploaded_by=new_owner)
                if n:
                    refiled_m += n
                    self.stdout.write("        re-filed %d material(s)" % n)

                n = Quiz.objects.filter(
                    subject=subject, created_by=user,
                ).update(created_by=new_owner)
                if n:
                    refiled_q += n
                    self.stdout.write("        re-filed %d quiz(zes)" % n)

                # Reported, not moved: Assignment.created_by is nullable and
                # rows predating that column are NULL. A NULL owner is
                # "unknown", which is honest; inventing one is not.
                orphan_a = Assignment.objects.filter(
                    subject=subject, created_by__isnull=True,
                ).count()
                if orphan_a:
                    self.stdout.write(
                        "        note: %d assignment(s) on this subject have "
                        "created_by=NULL (left as-is)" % orphan_a
                    )

                # Restore the usual course-wide + batch-scoped shape. Only
                # possible now the conflicting row above is ended:
                # uniq_active_primary_per_subject_courselevel allows exactly
                # one active course-wide PRIMARY per subject.
                already_cw = TeachingAssignment.objects.filter(
                    subject=subject, teacher=new_owner,
                    batch__isnull=True, is_active=True,
                ).exists()
                if already_cw:
                    self.stdout.write(
                        "        course-wide row already present"
                    )
                    continue

                slot_taken = TeachingAssignment.objects.filter(
                    subject=subject, batch__isnull=True, is_active=True,
                    role=TeachingAssignment.ROLE_PRIMARY,
                ).exists()
                role = (
                    TeachingAssignment.ROLE_ASSISTANT if slot_taken
                    else TeachingAssignment.ROLE_PRIMARY
                )
                TeachingAssignment.objects.create(
                    subject=subject, batch=None, teacher=new_owner,
                    role=role, order=target.order, is_active=True,
                )
                added_cw += 1
                self.stdout.write(
                    "        added course-wide %s row for %s"
                    % (role, new_owner.email)
                )

        self.stdout.write("\n%s" % ("=" * 72))
        self.stdout.write(
            "assignments ended        : %d\n"
            "materials re-filed       : %d\n"
            "quizzes re-filed         : %d\n"
            "course-wide rows added   : %d\n"
            "subjects skipped (unsafe): %d"
            % (ended, refiled_m, refiled_q, added_cw, skipped)
        )
        self.stdout.write(
            "\nNo UserRole row was created. These accounts have never held "
            "the TEACHER role; granting it is a separate, human decision."
        )
