"""Tests for the repair_nonteacher_assignments management command.

What matters here is not "does it end rows" but the safety properties, since
this runs against a production database with real students on it:

  * --dry-run really writes nothing (savepoint() outside atomic() is a silent
    no-op in autocommit and commits everything it claims to discard — that
    bug already shipped once in seed_academy_launch);
  * it NEVER leaves a subject with no active teacher;
  * it ends rows rather than deleting them, so the audit trail survives;
  * it re-files ownership, because `uploaded_by` is itself a standing grant
    to delete via _require_material_editor;
  * re-filing notifies nobody;
  * it grants no TEACHER role;
  * it is idempotent.
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Role, UserRole
from courses.models import Batch, Course, Subject, TeachingAssignment
from materials.models import StudyMaterial
from notifications.models import Notification
from quizzes.models import Quiz

User = get_user_model()


class RepairBase(TestCase):
    def setUp(self):
        self.teacher_role, _ = Role.objects.get_or_create(name="TEACHER")
        self.student_role, _ = Role.objects.get_or_create(name="STUDENT")

        self.course = Course.objects.create(
            title="Class 10",
            status=Course.STATUS_PUBLISHED,
            kind=Course.KIND_ACADEMIC,
        )
        self.subject = Subject.objects.create(course=self.course, name="Science")
        self.batch = Batch.objects.create(
            course=self.course, code="2026-27", year=2026, name="Batch 2026-27",
        )

        # The mis-picked account: STUDENT role only, holding the course-wide
        # PRIMARY row that migration 0029 copied in.
        self.impostor = User.objects.create_user(
            username="bridget", email="bridget@example.test", password="x",
        )
        UserRole.objects.create(
            user=self.impostor, role=self.student_role, is_active=True,
        )
        self.bad_row = TeachingAssignment.objects.create(
            subject=self.subject, batch=None, teacher=self.impostor,
            role=TeachingAssignment.ROLE_PRIMARY, is_active=True,
        )

        # The teacher really staffing it, via the batch-scoped row.
        self.real = User.objects.create_user(
            username="kavita", email="kavita@example.test", password="x",
        )
        UserRole.objects.create(
            user=self.real, role=self.teacher_role, is_active=True,
        )
        self.good_row = TeachingAssignment.objects.create(
            subject=self.subject, batch=self.batch, teacher=self.real,
            role=TeachingAssignment.ROLE_PRIMARY, is_active=True,
        )

        self.material = StudyMaterial.objects.create(
            subject=self.subject, title="Science — example notes",
            uploaded_by=self.impostor,
        )
        self.quiz = Quiz.objects.create(
            subject=self.subject, title="Science — example quiz",
            created_by=self.impostor, is_assigned=True,
        )

    def run_cmd(self, *args):
        out = StringIO()
        call_command(
            "repair_nonteacher_assignments", *args, stdout=out, stderr=out,
        )
        return out.getvalue()


class DryRunTest(RepairBase):
    def test_dry_run_writes_absolutely_nothing(self):
        out = self.run_cmd("--dry-run")

        self.bad_row.refresh_from_db()
        self.material.refresh_from_db()
        self.quiz.refresh_from_db()

        self.assertTrue(self.bad_row.is_active)
        self.assertIsNone(self.bad_row.ended_at)
        self.assertEqual(self.material.uploaded_by, self.impostor)
        self.assertEqual(self.quiz.created_by, self.impostor)
        self.assertEqual(TeachingAssignment.objects.count(), 2)
        self.assertIn("DRY RUN", out)

    def test_dry_run_still_reports_the_planned_change(self):
        out = self.run_cmd("--dry-run")
        self.assertIn("bridget@example.test", out)
        self.assertIn("kavita@example.test", out)
        self.assertIn("assignments ended        : 1", out)


class ApplyTest(RepairBase):
    def test_row_is_ended_not_deleted(self):
        """courses/models.py:528-530 — END the row, never delete it, so
        "who taught Class 10 Science in July" stays answerable."""
        self.run_cmd()

        self.bad_row.refresh_from_db()
        self.assertFalse(self.bad_row.is_active)
        self.assertIsNotNone(self.bad_row.ended_at)
        self.assertEqual(self.bad_row.teacher, self.impostor)
        self.assertTrue(
            TeachingAssignment.objects.filter(pk=self.bad_row.pk).exists()
        )

    def test_content_is_refiled_onto_the_real_teacher(self):
        """Ending the assignment closes teaches_subject, but
        _require_material_editor also accepts `uploaded_by == user`, so a
        non-teacher owner keeps the right to delete live material until
        ownership moves."""
        self.run_cmd()

        self.material.refresh_from_db()
        self.quiz.refresh_from_db()
        self.assertEqual(self.material.uploaded_by, self.real)
        self.assertEqual(self.quiz.created_by, self.real)

    def test_course_wide_row_is_restored_for_the_real_teacher(self):
        """171 of 172 staffed subjects carry both a course-wide and a
        batch row. is_teacher_of accepts course-wide OR that exact batch, so
        batch-only coverage silently unstaffs the subject for the next
        batch created."""
        self.run_cmd()

        cw = TeachingAssignment.objects.filter(
            subject=self.subject, teacher=self.real,
            batch__isnull=True, is_active=True,
        )
        self.assertEqual(cw.count(), 1)
        self.assertEqual(cw.first().role, TeachingAssignment.ROLE_PRIMARY)

    def test_no_teacher_role_is_granted(self):
        self.run_cmd()
        self.assertFalse(
            UserRole.objects.filter(
                user=self.impostor, role=self.teacher_role,
            ).exists()
        )
        self.assertFalse(self.impostor.has_role("TEACHER"))

    def test_refiling_notifies_nobody(self):
        """Quiz has a pre_save/post_save pair that mails every active
        enrollee on a False->True is_assigned transition. The repair must not
        trip it."""
        before = Notification.objects.count()
        self.run_cmd()
        self.assertEqual(Notification.objects.count(), before)

    def test_is_idempotent(self):
        self.run_cmd()
        out = self.run_cmd()
        self.assertIn(
            "accounts holding an ACTIVE assignment without the TEACHER "
            "role: 0",
            out,
        )
        self.assertEqual(
            TeachingAssignment.objects.filter(
                subject=self.subject, is_active=True,
            ).count(),
            2,
        )


class NeverUnstaffTest(RepairBase):
    def test_row_is_kept_when_there_is_no_replacement(self):
        """An unstaffed subject is worse than a wrongly-staffed one: every
        teacher-side screen scopes through TeachingAssignment, so emptying
        the subject hides its content from everyone."""
        self.good_row.delete()

        out = self.run_cmd()

        self.bad_row.refresh_from_db()
        self.material.refresh_from_db()
        self.assertTrue(self.bad_row.is_active)
        self.assertEqual(self.material.uploaded_by, self.impostor)
        self.assertIn("SKIP", out)
        self.assertIn("subjects skipped (unsafe): 1", out)

    def test_a_deactivated_replacement_does_not_count(self):
        """A deactivated account cannot log in, so it cannot look after what
        it owns — the same predicate dashboard/admin_academy.py applies."""
        self.real.is_active = False
        self.real.save(update_fields=["is_active"])

        out = self.run_cmd()

        self.bad_row.refresh_from_db()
        self.assertTrue(self.bad_row.is_active)
        self.assertIn("subjects skipped (unsafe): 1", out)

    def test_a_replacement_without_the_role_does_not_count(self):
        """Two role-less accounts on one subject must not hand it to each
        other and both come out looking repaired.

        Deactivating the real teacher's role makes them a holder-without-role
        as well, so BOTH rows are in scope and BOTH are skipped — the subject
        keeps the staffing it has, wrong as it is, rather than being emptied.
        """
        UserRole.objects.filter(user=self.real, role=self.teacher_role).update(
            is_active=False,
        )

        out = self.run_cmd()

        self.bad_row.refresh_from_db()
        self.good_row.refresh_from_db()
        self.assertTrue(self.bad_row.is_active)
        self.assertTrue(self.good_row.is_active)
        self.assertIn("subjects skipped (unsafe): 2", out)
        self.assertIn("assignments ended        : 0", out)


class ScopingTest(RepairBase):
    def test_content_on_a_subject_the_account_still_holds_is_untouched(self):
        """Re-filing is scoped per (subject, owner), not per owner: an
        account must not shed content on a subject it legitimately holds."""
        other_subject = Subject.objects.create(
            course=self.course, name="Mathematics",
        )
        keeper = StudyMaterial.objects.create(
            subject=other_subject, title="Maths notes",
            uploaded_by=self.impostor,
        )

        self.run_cmd()

        keeper.refresh_from_db()
        self.assertEqual(keeper.uploaded_by, self.impostor)
