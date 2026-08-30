"""Tests for the seed_academy_launch management command.

The properties worth testing here are the safety ones. This command is
designed to be run against a production database that already has real
students on it, so what matters is not "does it create rows" but:

  * --dry-run really writes nothing (the first version of this command used
    transaction.savepoint() outside an atomic block, which is a silent no-op
    in autocommit mode, and committed everything it claimed to discard);
  * creating content notifies nobody;
  * it never displaces a teacher who is already on a subject;
  * --undo refuses to delete a batch that real data now depends on;
  * --go-live never publishes a recording, because there is no real video
    behind one and the student player has no error state for that.
"""
import uuid
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import LearnerProfile, TeacherProfile
from activity.models import Activity
from assignments.models import Assignment
from courses.models import Batch, Chapter, Course, Subject, TeachingAssignment
from courses.models_recordings import SessionRecording
from enrollments.models import Enrollment
from materials.models import StudyMaterial
from notifications.models import Notification
from quizzes.models import Quiz

from courses.management.commands.seed_academy_launch import (
    BATCH_CODE,
    SEED_EMAIL_DOMAIN,
)

User = get_user_model()


class SeedAcademyLaunchTestBase(TestCase):
    def setUp(self):
        self.course = Course.objects.create(
            title="Class 11 Science",
            status=Course.STATUS_PUBLISHED,
            kind=Course.KIND_ACADEMIC,
        )
        self.physics = Subject.objects.create(course=self.course, name="Physics")
        self.english = Subject.objects.create(course=self.course, name="English")
        Chapter.objects.create(subject=self.physics, title="Laws of Motion", order=1)

    def run_cmd(self, *args):
        out = StringIO()
        call_command("seed_academy_launch", *args, stdout=out, stderr=out)
        return out.getvalue()

    def seed_users(self):
        return User.objects.filter(email__endswith=f"@{SEED_EMAIL_DOMAIN}")


class DryRunTest(SeedAcademyLaunchTestBase):
    def test_dry_run_writes_absolutely_nothing(self):
        """The regression that motivated this test: a dry run that committed.

        transaction.savepoint() returns None outside an atomic block and the
        matching savepoint_rollback() is silently a no-op, so the original
        --dry-run created every user and batch it was reporting on. On a
        production database that is the whole ballgame.
        """
        before = {
            "users": User.objects.count(),
            "batches": Batch.objects.count(),
            "assignments": Assignment.objects.count(),
            "materials": StudyMaterial.objects.count(),
            "quizzes": Quiz.objects.count(),
            "recordings": SessionRecording.objects.count(),
            "teaching": TeachingAssignment.objects.count(),
        }

        output = self.run_cmd("--dry-run", f"--flagship={self.course.id}")

        self.assertIn("DRY RUN", output)
        self.assertEqual(User.objects.count(), before["users"])
        self.assertEqual(Batch.objects.count(), before["batches"])
        self.assertEqual(Assignment.objects.count(), before["assignments"])
        self.assertEqual(StudyMaterial.objects.count(), before["materials"])
        self.assertEqual(Quiz.objects.count(), before["quizzes"])
        self.assertEqual(SessionRecording.objects.count(), before["recordings"])
        self.assertEqual(TeachingAssignment.objects.count(), before["teaching"])

    def test_dry_run_still_reports_real_numbers(self):
        """A dry run that reported nothing would be useless as a preview."""
        output = self.run_cmd("--dry-run", f"--flagship={self.course.id}")
        self.assertIn("example teacher account", output)
        self.assertIn(BATCH_CODE, output)


class StagedPostureTest(SeedAcademyLaunchTestBase):
    def setUp(self):
        super().setUp()
        # A real, active, un-batched enrollee. Un-batched is the worst case:
        # _enrollments_for treats batch=NULL as "sees everything", so this
        # learner is in scope for batch-scoped content too and would be
        # notified if anything published.
        account = User.objects.create_user(
            email="real.student@shiksha.test", username="real.student",
            password="x",
        )
        self.profile = LearnerProfile.objects.create(
            account=account, relationship=LearnerProfile.RELATIONSHIP_SELF,
            display_name="Real Student", is_default=True,
        )
        Enrollment.objects.create(
            user=account, learner_profile=self.profile, course=self.course,
            status=Enrollment.STATUS_ACTIVE, batch=None,
        )

    def test_creating_content_notifies_nobody(self):
        activity_before = Activity.objects.count()
        notif_before = Notification.objects.count()

        self.run_cmd(f"--flagship={self.course.id}")

        self.assertEqual(Activity.objects.count(), activity_before)
        self.assertEqual(Notification.objects.count(), notif_before)

    def test_content_is_created_invisible(self):
        self.run_cmd(f"--flagship={self.course.id}")

        self.assertTrue(Assignment.objects.exists())
        self.assertFalse(Assignment.objects.filter(is_published=True).exists())

        self.assertTrue(Quiz.objects.exists())
        self.assertFalse(Quiz.objects.filter(is_assigned=True).exists())

        self.assertTrue(SessionRecording.objects.exists())
        self.assertFalse(SessionRecording.objects.filter(is_published=True).exists())

    def test_example_teachers_are_not_chat_visible(self):
        """approved is the ONLY filter the chat directory applies.

        chat/services.py directory_entries selects on academy_status alone —
        no role check, no is_active check — so an approved example teacher is
        immediately DM-able and globally searchable by every real student.
        """
        self.run_cmd(f"--flagship={self.course.id}")

        self.assertTrue(self.seed_users().exists())
        self.assertFalse(
            TeacherProfile.objects.filter(
                user__in=self.seed_users(),
                academy_status=TeacherProfile.TRACK_APPROVED,
            ).exists()
        )


class StaffingTest(SeedAcademyLaunchTestBase):
    def test_never_displaces_an_existing_teacher(self):
        real = User.objects.create_user(
            email="real.teacher@shiksha.test", username="real.teacher",
            password="x",
        )
        existing = TeachingAssignment.objects.create(
            subject=self.physics, teacher=real, batch=None,
            role=TeachingAssignment.ROLE_PRIMARY, is_active=True,
        )

        self.run_cmd(f"--flagship={self.course.id}")

        existing.refresh_from_db()
        self.assertEqual(existing.teacher_id, real.id)
        self.assertTrue(existing.is_active)
        # Physics keeps exactly one course-wide primary, and it is the real one.
        primaries = TeachingAssignment.objects.filter(
            subject=self.physics, batch__isnull=True, is_active=True,
            role=TeachingAssignment.ROLE_PRIMARY,
        )
        self.assertEqual(primaries.count(), 1)
        self.assertEqual(primaries.first().teacher_id, real.id)

    def test_fills_an_unstaffed_subject(self):
        self.run_cmd(f"--flagship={self.course.id}")

        primary = TeachingAssignment.objects.filter(
            subject=self.english, batch__isnull=True, is_active=True,
            role=TeachingAssignment.ROLE_PRIMARY,
        ).first()
        self.assertIsNotNone(primary)
        self.assertTrue(primary.teacher.email.endswith(f"@{SEED_EMAIL_DOMAIN}"))

    def test_an_empty_primary_slot_does_not_produce_null_authored_content(self):
        """Found by dry-running against production.

        TeachingAssignment.teacher is null=True with on_delete=SET_NULL, so a
        subject can carry an active PRIMARY row with nobody in it once a
        teacher account is deleted. Treating that as "has a teacher" made the
        content author None, which is a not-null violation on StudyMaterial —
        the command died partway through.
        """
        TeachingAssignment.objects.create(
            subject=self.physics, teacher=None, batch=None,
            role=TeachingAssignment.ROLE_PRIMARY, is_active=True,
        )

        self.run_cmd(f"--flagship={self.course.id}")

        material = StudyMaterial.objects.get(subject=self.physics)
        self.assertIsNotNone(material.uploaded_by_id)
        self.assertTrue(
            material.uploaded_by.email.endswith(f"@{SEED_EMAIL_DOMAIN}")
        )

    def test_an_empty_primary_slot_gets_adopted_rather_than_skipped(self):
        """It occupies the one-active-primary-per-subject slot, so leaving it
        empty means the subject can never be staffed by anyone."""
        orphan = TeachingAssignment.objects.create(
            subject=self.physics, teacher=None, batch=None,
            role=TeachingAssignment.ROLE_PRIMARY, is_active=True,
        )

        output = self.run_cmd(f"--flagship={self.course.id}")

        orphan.refresh_from_db()
        self.assertIsNotNone(orphan.teacher_id)
        self.assertIn("empty 'primary teacher' slot", output)
        # Adopted, not duplicated — the constraint would refuse a second one.
        self.assertEqual(
            TeachingAssignment.objects.filter(
                subject=self.physics, batch__isnull=True, is_active=True,
                role=TeachingAssignment.ROLE_PRIMARY,
            ).count(),
            1,
        )

    def test_content_is_attributed_to_the_real_teacher_when_there_is_one(self):
        """Content filed under someone with no assignment is content nobody sees."""
        real = User.objects.create_user(
            email="real.teacher2@shiksha.test", username="real.teacher2",
            password="x",
        )
        TeachingAssignment.objects.create(
            subject=self.physics, teacher=real, batch=None,
            role=TeachingAssignment.ROLE_PRIMARY, is_active=True,
        )

        self.run_cmd(f"--flagship={self.course.id}")

        material = StudyMaterial.objects.get(subject=self.physics)
        self.assertEqual(material.uploaded_by_id, real.id)


class DistributionTest(SeedAcademyLaunchTestBase):
    """Several teachers share a specialism, so subjects must spread across them.

    With one teacher per subject area, production ended up with a single
    English teacher holding 100 subjects and one social-science teacher
    holding 73 — not a plausible timetable.
    """

    def test_english_subjects_spread_across_more_than_one_teacher(self):
        names = [
            "English (Main Reader)", "English (Supplementary)",
            "English - Grammar and Writing Skills", "English (Honeydew)",
            "4A: English – Main Reader (Beehive)", "English (Snapshots)",
            "English ( First Flight)", "English (Grammar & Writing Skills)",
        ]
        for name in names:
            Subject.objects.create(course=self.course, name=name)

        self.run_cmd("--structure-only")

        holders = set(
            TeachingAssignment.objects
            .filter(subject__name__startswith="English", batch__isnull=True,
                    is_active=True)
            .values_list("teacher__email", flat=True)
        )
        self.assertGreater(len(holders), 1, f"all English went to {holders}")

    def test_a_specific_match_beats_a_broad_one(self):
        """"3B: Social Science – Geography" contains both "social" and
        "geograph". Only the geography teacher should get it."""
        subject = Subject.objects.create(
            course=self.course, name="3B: Social Science – Geography",
        )

        self.run_cmd("--structure-only")

        holder = TeachingAssignment.objects.get(
            subject=subject, batch__isnull=True, is_active=True,
        ).teacher
        self.assertEqual(holder.first_name, "Lalnunpuii")

    def test_assignment_is_stable_across_runs(self):
        """Keyed on the subject UUID, not a running counter — otherwise
        --rebalance would reshuffle the whole roster every time it ran."""
        subject = Subject.objects.create(course=self.course, name="English (X)")
        self.run_cmd("--structure-only")
        first = TeachingAssignment.objects.get(
            subject=subject, batch__isnull=True, is_active=True,
        ).teacher_id

        self.run_cmd("--rebalance")

        second = TeachingAssignment.objects.get(
            subject=subject, batch__isnull=True, is_active=True,
        ).teacher_id
        self.assertEqual(first, second)


class RebalanceTest(SeedAcademyLaunchTestBase):
    def test_rebalance_never_moves_a_real_teacher(self):
        real = User.objects.create_user(
            email="real.english@shiksha.test", username="real.english",
            password="x",
        )
        subject = Subject.objects.create(course=self.course, name="English (Y)")
        row = TeachingAssignment.objects.create(
            subject=subject, teacher=real, batch=None,
            role=TeachingAssignment.ROLE_PRIMARY, is_active=True,
        )

        self.run_cmd("--structure-only")
        self.run_cmd("--rebalance")

        row.refresh_from_db()
        self.assertEqual(row.teacher_id, real.id)

    def test_rebalance_refiles_content_under_the_new_holder(self):
        """Every teacher-side list screen scopes through TeachingAssignment, so
        content left with the previous holder becomes invisible to whoever now
        teaches the subject."""
        subject = Subject.objects.create(course=self.course, name="English (Z)")
        self.run_cmd(f"--flagship={self.course.id}")

        material = StudyMaterial.objects.get(subject=subject)
        # Force it onto the wrong example teacher, then rebalance.
        other = self.seed_users().exclude(pk=material.uploaded_by_id).first()
        material.uploaded_by = other
        material.save(update_fields=["uploaded_by"])

        self.run_cmd("--rebalance")

        material.refresh_from_db()
        holder = TeachingAssignment.objects.get(
            subject=subject, batch__isnull=True, is_active=True,
        ).teacher
        self.assertEqual(material.uploaded_by_id, holder.id)

    def test_rebalance_does_not_move_content_off_a_real_teacher(self):
        real = User.objects.create_user(
            email="real.owner@shiksha.test", username="real.owner", password="x",
        )
        TeachingAssignment.objects.create(
            subject=self.physics, teacher=real, batch=None,
            role=TeachingAssignment.ROLE_PRIMARY, is_active=True,
        )
        self.run_cmd(f"--flagship={self.course.id}")
        material = StudyMaterial.objects.get(subject=self.physics)
        self.assertEqual(material.uploaded_by_id, real.id)

        self.run_cmd("--rebalance")

        material.refresh_from_db()
        self.assertEqual(material.uploaded_by_id, real.id)

    def test_rebalance_dry_run_writes_nothing(self):
        self.run_cmd("--structure-only")
        before = dict(
            TeachingAssignment.objects.values_list("id", "teacher_id")
        )
        self.run_cmd("--rebalance", "--dry-run")
        after = dict(
            TeachingAssignment.objects.values_list("id", "teacher_id")
        )
        self.assertEqual(before, after)


class RepairTeacherProfilesTest(SeedAcademyLaunchTestBase):
    def _teaching_user_without_profile(self):
        from accounts.models import Role, UserRole

        user = User.objects.create_user(
            email="halfsetup@shiksha.test", username="halfsetup", password="x",
        )
        role, _ = Role.objects.get_or_create(name="TEACHER")
        UserRole.objects.create(user=user, role=role, is_active=True)
        TeachingAssignment.objects.create(
            subject=self.physics, teacher=user, batch=None,
            role=TeachingAssignment.ROLE_PRIMARY, is_active=True,
        )
        return user

    def test_creates_a_profile_for_someone_already_teaching(self):
        """The faculty list queries TeacherProfile, so a teaching account with
        no profile row is invisible on every faculty surface while still owning
        subjects and content."""
        user = self._teaching_user_without_profile()
        self.assertFalse(TeacherProfile.objects.filter(user=user).exists())

        self.run_cmd("--repair-teacher-profiles")

        profile = TeacherProfile.objects.get(user=user)
        self.assertEqual(profile.academy_status, TeacherProfile.TRACK_APPROVED)

    def test_does_not_promote_someone_who_only_has_the_role(self):
        from accounts.models import Role, UserRole

        user = User.objects.create_user(
            email="roleonly@shiksha.test", username="roleonly", password="x",
        )
        role, _ = Role.objects.get_or_create(name="TEACHER")
        UserRole.objects.create(user=user, role=role, is_active=True)

        self.run_cmd("--repair-teacher-profiles")

        self.assertFalse(TeacherProfile.objects.filter(user=user).exists())

    def test_leaves_an_existing_profile_alone(self):
        user = self._teaching_user_without_profile()
        TeacherProfile.objects.create(
            user=user, teacher_type=TeacherProfile.TYPE_FACULTY,
            academy_status=TeacherProfile.TRACK_REJECTED,
        )

        self.run_cmd("--repair-teacher-profiles")

        profile = TeacherProfile.objects.get(user=user)
        self.assertEqual(profile.academy_status, TeacherProfile.TRACK_REJECTED)

    def test_dry_run_writes_nothing(self):
        user = self._teaching_user_without_profile()
        self.run_cmd("--repair-teacher-profiles", "--dry-run")
        self.assertFalse(TeacherProfile.objects.filter(user=user).exists())


class AllCoursesTest(SeedAcademyLaunchTestBase):
    def test_all_courses_puts_content_everywhere(self):
        other = Course.objects.create(
            title="Class 9", status=Course.STATUS_PUBLISHED,
            kind=Course.KIND_ACADEMIC,
        )
        Subject.objects.create(course=other, name="Mathematics")

        self.run_cmd("--all-courses")

        self.assertTrue(
            StudyMaterial.objects.filter(subject__course=other).exists()
        )
        self.assertTrue(
            StudyMaterial.objects.filter(subject__course=self.course).exists()
        )


class IdempotencyTest(SeedAcademyLaunchTestBase):
    def test_running_twice_creates_one_set(self):
        self.run_cmd(f"--flagship={self.course.id}")
        counts = {
            "batches": Batch.objects.filter(code=BATCH_CODE).count(),
            "materials": StudyMaterial.objects.count(),
            "assignments": Assignment.objects.count(),
            "quizzes": Quiz.objects.count(),
            "recordings": SessionRecording.objects.count(),
            "users": self.seed_users().count(),
        }

        self.run_cmd(f"--flagship={self.course.id}")

        self.assertEqual(Batch.objects.filter(code=BATCH_CODE).count(), counts["batches"])
        self.assertEqual(StudyMaterial.objects.count(), counts["materials"])
        self.assertEqual(Assignment.objects.count(), counts["assignments"])
        self.assertEqual(Quiz.objects.count(), counts["quizzes"])
        self.assertEqual(SessionRecording.objects.count(), counts["recordings"])
        self.assertEqual(self.seed_users().count(), counts["users"])


class GoLiveTest(SeedAcademyLaunchTestBase):
    def test_go_live_reveals_content_but_never_a_recording(self):
        self.run_cmd(f"--flagship={self.course.id}")
        self.run_cmd("--go-live")

        self.assertTrue(Assignment.objects.exists())
        self.assertFalse(Assignment.objects.filter(is_published=False).exists())

        self.assertTrue(Quiz.objects.exists())
        self.assertFalse(Quiz.objects.filter(is_assigned=False).exists())

        self.assertTrue(
            TeacherProfile.objects.filter(
                user__in=self.seed_users(),
                academy_status=TeacherProfile.TRACK_APPROVED,
            ).exists()
        )

        # The one thing --go-live must never do. A made-up bunny_video_id
        # returns HTTP 200 from the playback endpoint and renders a live
        # iframe at a video that does not exist, and the player's error branch
        # only runs when the API call itself throws — so this would be a
        # silently broken player rather than a visible failure.
        self.assertTrue(SessionRecording.objects.exists())
        self.assertFalse(SessionRecording.objects.filter(is_published=True).exists())

    def test_go_live_dry_run_writes_nothing(self):
        self.run_cmd(f"--flagship={self.course.id}")
        self.run_cmd("--go-live", "--dry-run")
        self.assertFalse(Assignment.objects.filter(is_published=True).exists())
        self.assertFalse(Quiz.objects.filter(is_assigned=True).exists())


class UndoTest(SeedAcademyLaunchTestBase):
    def test_undo_removes_what_it_created(self):
        self.run_cmd(f"--flagship={self.course.id}")
        self.assertTrue(self.seed_users().exists())

        self.run_cmd("--undo")

        self.assertFalse(self.seed_users().exists())
        self.assertFalse(Batch.objects.filter(code=BATCH_CODE).exists())
        self.assertFalse(StudyMaterial.objects.exists())
        self.assertFalse(Assignment.objects.exists())
        self.assertFalse(Quiz.objects.exists())
        self.assertFalse(SessionRecording.objects.exists())

    def test_undo_refuses_a_batch_that_has_enrollments(self):
        """Enrollment.batch is SET_NULL, and an un-batched student then sees
        every assignment in the course. Deleting the batch would silently
        widen a real learner's content visibility, so undo declines."""
        self.run_cmd(f"--flagship={self.course.id}")
        batch = Batch.objects.get(course=self.course, code=BATCH_CODE)

        account = User.objects.create_user(
            email="placed.student@shiksha.test", username="placed.student",
            password="x",
        )
        profile = LearnerProfile.objects.create(
            account=account, relationship=LearnerProfile.RELATIONSHIP_SELF,
            display_name="Placed Student", is_default=True,
        )
        enrollment = Enrollment.objects.create(
            user=account, learner_profile=profile, course=self.course,
            status=Enrollment.STATUS_ACTIVE, batch=batch,
        )

        output = self.run_cmd("--undo")

        self.assertIn("KEEP batch", output)
        self.assertTrue(Batch.objects.filter(pk=batch.pk).exists())
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.batch_id, batch.id)

    def test_undo_refuses_a_batch_carrying_a_real_teachers_assignment(self):
        """TeachingAssignment.batch is CASCADE — deleting the batch would
        hard-delete the row and revoke that teacher's access to the subject."""
        self.run_cmd(f"--flagship={self.course.id}")
        batch = Batch.objects.get(course=self.course, code=BATCH_CODE)

        real = User.objects.create_user(
            email="real.teacher3@shiksha.test", username="real.teacher3",
            password="x",
        )
        # A non-primary role, so it does not collide with the seeded primary.
        ta = TeachingAssignment.objects.create(
            subject=self.english, teacher=real, batch=batch,
            role=TeachingAssignment.ROLE_ASSISTANT, is_active=True,
        )

        output = self.run_cmd("--undo")

        self.assertIn("KEEP batch", output)
        self.assertTrue(Batch.objects.filter(pk=batch.pk).exists())
        self.assertTrue(TeachingAssignment.objects.filter(pk=ta.pk).exists())

    def test_undo_clears_orphaned_activity_and_notifications(self):
        """Nothing else does. Activity reaches its target through a
        GenericForeignKey with a plain object_id UUIDField and Notification
        only keeps the id in a JSON payload, so neither has a cascade — the
        rows would sit in real students' bells linking to a 404."""
        self.run_cmd(f"--flagship={self.course.id}")

        account = User.objects.create_user(
            email="notified@shiksha.test", username="notified", password="x",
        )
        profile = LearnerProfile.objects.create(
            account=account, relationship=LearnerProfile.RELATIONSHIP_SELF,
            display_name="Notified", is_default=True,
        )
        Enrollment.objects.create(
            user=account, learner_profile=profile, course=self.course,
            status=Enrollment.STATUS_ACTIVE, batch=None,
        )

        # Publishing is what writes the bell rows.
        self.run_cmd("--go-live")
        seeded_ids = {str(pk) for pk in Assignment.objects.values_list("id", flat=True)}
        self.assertTrue(Activity.objects.filter(object_id__in=seeded_ids).exists())

        self.run_cmd("--undo")

        self.assertFalse(Activity.objects.filter(object_id__in=seeded_ids).exists())
        self.assertFalse(
            Notification.objects.filter(
                payload__object_id__in=list(seeded_ids)
            ).exists()
        )

    def test_undo_is_safe_when_nothing_was_seeded(self):
        output = self.run_cmd("--undo")
        self.assertIn("nothing to undo", output.lower())
