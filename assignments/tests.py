"""
Regression cover: AssignmentDetailView and CourseAssignmentsView used to
branch on the account-level `user.has_role(Role.TEACHER)` instead of the
request's actual context. A dual-role account (STUDENT + an active TEACHER
role — this platform explicitly supports holding several active roles at
once) hit the teacher-ownership branch even while acting as a learner in a
learner-context token, 403'ing its own enrolled subject's assignment with
"Not assigned to this subject." Both views now use
accounts.permissions._in_teacher_context(), which additionally checks the
token's `context` claim, matching every other teacher-gated view in this
codebase.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User, Role, UserRole, LearnerProfile
from courses.models import Course, Subject, Chapter
from enrollments.models import Subscription
from assignments.models import Assignment, AssignmentFile


class DualRoleStudentAssignmentAccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")

        # A dual-role account: active STUDENT role AND an active (but
        # unrelated) TEACHER role — e.g. an approved faculty member who
        # also has their own learner profile.
        cls.account = User.objects.create_user(
            username="dual_role", email="dual_role@test.com", password="x",
            is_verified=True,
        )
        UserRole.objects.create(user=cls.account, role=Role.objects.get(name="STUDENT"), is_active=True, is_primary=True)
        UserRole.objects.create(user=cls.account, role=Role.objects.get(name="TEACHER"), is_active=True, is_primary=False)

        cls.profile = LearnerProfile.objects.create(account=cls.account, display_name="Learner side", is_default=True)

        cls.course = Course.objects.create(title="Physics Demo")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="Laws of Motion", order=0)

        now = timezone.now()
        Subscription.objects.create(
            user=cls.account, learner_profile=cls.profile, course=cls.course,
            status=Subscription.STATUS_ACTIVE, starts_at=now, expires_at=now + timedelta(days=30),
        )

        cls.assignment = Assignment.objects.create(
            chapter=cls.chapter, title="Problem set 1", due_date=now + timedelta(days=7),
        )

    def client_in_learner_context(self):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.profile.id)},
        )
        return c

    def test_assignment_detail_accessible_to_dual_role_student_in_learner_context(self):
        c = self.client_in_learner_context()
        r = c.get(f"/api/assignments/{self.assignment.id}/")
        self.assertEqual(r.status_code, 200, r.content)

    def test_course_assignments_list_accessible_to_dual_role_student_in_learner_context(self):
        c = self.client_in_learner_context()
        r = c.get(f"/api/assignments/courses/{self.course.id}/")
        self.assertEqual(r.status_code, 200, r.content)
        titles = [row["title"] for row in r.data]
        self.assertIn("Problem set 1", titles)


# ══════════════════════════════════════════════════════════════════════════
# Audit fixes 2026-08-24 — §1 and §5 of ACADEMY_DASHBOARD_AUDIT_2026-08-23.
#
# Four separate holes, one fixture. Each test below names the exact behaviour
# that shipped before the fix, so a revert fails loudly rather than quietly.
# ══════════════════════════════════════════════════════════════════════════

from django.core.files.uploadedfile import SimpleUploadedFile

from courses.models import Batch, TeachingAssignment
from enrollments.models import Enrollment
from assignments.models import AssignmentSubmission


def _teacher_client(user):
    c = APIClient()
    c.force_authenticate(user=user, token={"context": "teacher"})
    return c


def _learner_client(user, profile):
    c = APIClient()
    c.force_authenticate(
        user=user,
        token={"context": "learner", "active_profile": str(profile.id)},
    )
    return c


class AssignmentScopeFixtureMixin:
    """One course, one subject, two batches (10-A / 10-B), one teacher staffed
    on 10-B only, and one learner in each batch."""

    @classmethod
    def build_world(cls):
        Role.objects.get_or_create(name="TEACHER")
        Role.objects.get_or_create(name="STUDENT")

        cls.course = Course.objects.create(title="Class 10")
        cls.subject = Subject.objects.create(course=cls.course, name="Maths")
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Algebra", order=0)
        cls.batch_a = Batch.objects.create(
            course=cls.course, name="10-A", code="10A")
        cls.batch_b = Batch.objects.create(
            course=cls.course, name="10-B", code="10B")

        cls.teacher_b = User.objects.create_user(
            username="teacher_b", email="tb@test.com", password="x",
            is_verified=True)
        UserRole.objects.create(
            user=cls.teacher_b, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True)
        TeachingAssignment.objects.create(
            batch=cls.batch_b, subject=cls.subject, teacher=cls.teacher_b,
            is_active=True)

        now = timezone.now()
        cls.assignment_a = Assignment.objects.create(
            chapter=cls.chapter, batch=cls.batch_a, title="10-A worksheet",
            due_date=now + timedelta(days=7))
        cls.assignment_b = Assignment.objects.create(
            chapter=cls.chapter, batch=cls.batch_b, title="10-B worksheet",
            due_date=now + timedelta(days=7))

        cls.learner_account, cls.learner_profile = cls._make_learner(
            "kid_b", cls.batch_b)
        cls.other_account, cls.other_profile = cls._make_learner(
            "kid_a", cls.batch_a)

    @classmethod
    def _make_learner(cls, username, batch):
        account = User.objects.create_user(
            username=username, email=f"{username}@test.com", password="x",
            is_verified=True)
        UserRole.objects.create(
            user=account, role=Role.objects.get(name="STUDENT"),
            is_active=True, is_primary=True)
        profile = LearnerProfile.objects.create(
            account=account, display_name=username, is_default=True)
        now = timezone.now()
        Subscription.objects.create(
            user=account, learner_profile=profile, course=cls.course,
            status=Subscription.STATUS_ACTIVE, starts_at=now,
            expires_at=now + timedelta(days=30))
        Enrollment.objects.create(
            user=account, learner_profile=profile, course=cls.course,
            batch=batch, status=Enrollment.STATUS_ACTIVE)
        return account, profile


class SubmissionUploadValidationTest(AssignmentScopeFixtureMixin, TestCase):
    """§5 HIGH — `SubmitAssignmentView` wrote request.FILES["file"] straight
    onto the model. No extension check, no size cap, no validators on the
    field: `payload.html` was stored and served back to the teacher who
    clicked Review, executing on the media origin."""

    @classmethod
    def setUpTestData(cls):
        cls.build_world()

    def _submit(self, filename, content=b"x", content_type="text/html"):
        c = _learner_client(self.learner_account, self.learner_profile)
        return c.post(
            f"/api/assignments/{self.assignment_b.id}/submit/",
            {"file": SimpleUploadedFile(filename, content, content_type=content_type)},
            format="multipart",
        )

    def test_html_submission_is_rejected(self):
        r = self._submit("payload.html", b"<script>alert(1)</script>")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(AssignmentSubmission.objects.exists())

    def test_svg_submission_is_rejected(self):
        r = self._submit("payload.svg", b"<svg onload='alert(1)'/>")
        self.assertEqual(r.status_code, 400, r.content)

    def test_executable_submission_is_rejected(self):
        r = self._submit("nasty.exe", b"MZ")
        self.assertEqual(r.status_code, 400, r.content)

    def test_oversized_submission_is_rejected(self):
        from assignments.serializers import MAX_SUBMISSION_SIZE
        r = self._submit("huge.pdf", b"0" * (MAX_SUBMISSION_SIZE + 1),
                         content_type="application/pdf")
        self.assertEqual(r.status_code, 400, r.content)

    def test_pdf_submission_is_accepted(self):
        r = self._submit("homework.pdf", b"%PDF-1.4", "application/pdf")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(AssignmentSubmission.objects.count(), 1)


class TeacherExtraFileValidationTest(AssignmentScopeFixtureMixin, TestCase):
    """§1 HIGH — only the first upload (`attachment`) was validated. The rest
    came from request.FILES.getlist("files") and went straight into
    AssignmentFile.objects.create(), so a `.exe` was stored and served to
    every student in the batch."""

    @classmethod
    def setUpTestData(cls):
        cls.build_world()

    def test_second_file_is_validated(self):
        c = _teacher_client(self.teacher_b)
        r = c.post("/api/assignments/teacher/create/", {
            "chapter_id": str(self.chapter.id),
            "batch_id": str(self.batch_b.id),
            "title": "Worksheet 2",
            "description": "Do it",
            "due_date": (timezone.now() + timedelta(days=3)).isoformat(),
            "attachment": SimpleUploadedFile(
                "ok.pdf", b"%PDF", content_type="application/pdf"),
            "files": SimpleUploadedFile(
                "nasty.exe", b"MZ", content_type="application/octet-stream"),
        }, format="multipart")
        self.assertEqual(r.status_code, 400, r.content)
        # And nothing half-created: validation runs before the row is written.
        self.assertFalse(Assignment.objects.filter(title="Worksheet 2").exists())
        self.assertFalse(AssignmentFile.objects.exists())

    def test_clean_extra_file_still_accepted(self):
        c = _teacher_client(self.teacher_b)
        r = c.post("/api/assignments/teacher/create/", {
            "chapter_id": str(self.chapter.id),
            "batch_id": str(self.batch_b.id),
            "title": "Worksheet 3",
            "description": "Do it",
            "due_date": (timezone.now() + timedelta(days=3)).isoformat(),
            "attachment": SimpleUploadedFile(
                "ok.pdf", b"%PDF", content_type="application/pdf"),
            "files": SimpleUploadedFile(
                "extra.pdf", b"%PDF", content_type="application/pdf"),
        }, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(AssignmentFile.objects.count(), 1)


class LearnerAssignmentScopeTest(AssignmentScopeFixtureMixin, TestCase):
    """§5 HIGH — `AssignmentDetailView.get_queryset` filtered on NOTHING: not
    is_published, not batch, while both list endpoints filtered on both. Any
    subscribed learner holding the UUID read an unpublished draft in full and
    could submit to it, and a Batch-B learner could read and submit to a
    Batch-A assignment with a different due date."""

    @classmethod
    def setUpTestData(cls):
        cls.build_world()
        cls.draft = Assignment.objects.create(
            chapter=cls.chapter, batch=cls.batch_b, title="Tomorrow's paper",
            description="Not ready yet", is_published=False,
            due_date=timezone.now() + timedelta(days=7))

    def test_learner_cannot_read_another_batchs_assignment(self):
        c = _learner_client(self.learner_account, self.learner_profile)
        r = c.get(f"/api/assignments/{self.assignment_a.id}/")
        self.assertEqual(r.status_code, 404, r.content)

    def test_learner_cannot_submit_to_another_batchs_assignment(self):
        c = _learner_client(self.learner_account, self.learner_profile)
        r = c.post(
            f"/api/assignments/{self.assignment_a.id}/submit/",
            {"file": SimpleUploadedFile("hw.pdf", b"%PDF",
                                        content_type="application/pdf")},
            format="multipart",
        )
        self.assertEqual(r.status_code, 404, r.content)
        self.assertFalse(AssignmentSubmission.objects.exists())

    def test_learner_cannot_read_an_unpublished_draft(self):
        c = _learner_client(self.learner_account, self.learner_profile)
        r = c.get(f"/api/assignments/{self.draft.id}/")
        self.assertEqual(r.status_code, 404, r.content)

    def test_learner_cannot_submit_to_an_unpublished_draft(self):
        c = _learner_client(self.learner_account, self.learner_profile)
        r = c.post(
            f"/api/assignments/{self.draft.id}/submit/",
            {"file": SimpleUploadedFile("hw.pdf", b"%PDF",
                                        content_type="application/pdf")},
            format="multipart",
        )
        self.assertEqual(r.status_code, 404, r.content)

    def test_learner_can_still_read_their_own_batchs_assignment(self):
        c = _learner_client(self.learner_account, self.learner_profile)
        r = c.get(f"/api/assignments/{self.assignment_b.id}/")
        self.assertEqual(r.status_code, 200, r.content)


class TeacherBatchScopeTest(AssignmentScopeFixtureMixin, TestCase):
    """§1 HIGH / Theme T4 — `_assert_teacher_owns_assignment` gated on
    subject-level `teaches_subject()` while CREATE gated on batch-aware
    `is_teacher_of()`. A teacher staffed on 10-B could list, read, download,
    grade, edit and delete 10-A's assignments — all 200 — but could not
    create for 10-A, which is what proved it unintended."""

    @classmethod
    def setUpTestData(cls):
        cls.build_world()
        cls.submission_a = AssignmentSubmission.objects.create(
            assignment=cls.assignment_a, student=cls.other_account,
            learner_profile=cls.other_profile,
            submitted_file=SimpleUploadedFile("a.pdf", b"%PDF"))

    def test_all_assignments_list_excludes_other_batches(self):
        c = _teacher_client(self.teacher_b)
        r = c.get("/api/assignments/teacher/all/")
        self.assertEqual(r.status_code, 200, r.content)
        titles = [row["title"] for row in r.data]
        self.assertIn("10-B worksheet", titles)
        self.assertNotIn("10-A worksheet", titles)

    def test_subject_assignments_list_excludes_other_batches(self):
        c = _teacher_client(self.teacher_b)
        r = c.get(f"/api/assignments/teacher/subject/{self.subject.id}/")
        self.assertEqual(r.status_code, 200, r.content)
        titles = [row["title"] for row in r.data]
        self.assertEqual(titles, ["10-B worksheet"])

    def test_cannot_read_other_batchs_submissions(self):
        c = _teacher_client(self.teacher_b)
        r = c.get(f"/api/assignments/teacher/{self.assignment_a.id}/submissions/")
        self.assertEqual(r.status_code, 403, r.content)

    def test_cannot_grade_other_batchs_submission(self):
        c = _teacher_client(self.teacher_b)
        r = c.post(
            f"/api/assignments/teacher/submissions/{self.submission_a.id}/grade/",
            {"marks_obtained": 10})
        self.assertEqual(r.status_code, 403, r.content)

    def test_cannot_edit_other_batchs_assignment(self):
        c = _teacher_client(self.teacher_b)
        r = c.patch(f"/api/assignments/teacher/{self.assignment_a.id}/edit/",
                    {"title": "hijacked"}, format="multipart")
        self.assertEqual(r.status_code, 403, r.content)

    def test_cannot_delete_other_batchs_assignment(self):
        c = _teacher_client(self.teacher_b)
        r = c.delete(f"/api/assignments/teacher/{self.assignment_a.id}/delete/")
        self.assertEqual(r.status_code, 403, r.content)

    def test_cannot_bulk_download_other_batchs_submissions(self):
        c = _teacher_client(self.teacher_b)
        r = c.get(f"/api/assignments/teacher/{self.assignment_a.id}/download-all/")
        self.assertEqual(r.status_code, 403, r.content)

    def test_own_batch_is_unaffected(self):
        c = _teacher_client(self.teacher_b)
        self.assertEqual(
            c.get(f"/api/assignments/teacher/{self.assignment_b.id}/submissions/").status_code,
            200)
        self.assertEqual(
            c.patch(f"/api/assignments/teacher/{self.assignment_b.id}/edit/",
                    {"title": "renamed"}, format="multipart").status_code,
            200)

    def test_course_wide_staffing_still_reaches_every_batch(self):
        """A TeachingAssignment with batch=NULL covers the whole course — that
        is what is_teacher_of() already means, and this fix must not narrow
        it."""
        head = User.objects.create_user(
            username="head", email="head@test.com", password="x",
            is_verified=True)
        UserRole.objects.create(
            user=head, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True)
        TeachingAssignment.objects.create(
            batch=None, subject=self.subject, teacher=head, is_active=True)

        c = _teacher_client(head)
        titles = sorted(row["title"] for row in c.get("/api/assignments/teacher/all/").data)
        self.assertEqual(titles, ["10-A worksheet", "10-B worksheet"])
        self.assertEqual(
            c.get(f"/api/assignments/teacher/{self.assignment_a.id}/submissions/").status_code,
            200)

    def test_assignable_batches_lists_only_the_teachers_own(self):
        c = _teacher_client(self.teacher_b)
        r = c.get(f"/api/assignments/teacher/subject/{self.subject.id}/batches/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([b["name"] for b in r.data], ["10-B"])


class SubmissionRosterTest(AssignmentScopeFixtureMixin, TestCase):
    """§1 CRITICAL — the submissions endpoint returned AssignmentSubmission
    rows only, i.e. students who had already submitted. The screen's "Pending"
    count was therefore always 0 and the progress bar always 100%; the
    non-submitters, the entire reason to open the screen, were absent."""

    @classmethod
    def setUpTestData(cls):
        cls.build_world()
        # A second learner in 10-B who does NOT submit.
        cls.silent_account, cls.silent_profile = cls._make_learner(
            "kid_b2", cls.batch_b)
        AssignmentSubmission.objects.create(
            assignment=cls.assignment_b, student=cls.learner_account,
            learner_profile=cls.learner_profile,
            submitted_file=SimpleUploadedFile("b.pdf", b"%PDF"))

    def test_non_submitters_are_listed(self):
        c = _teacher_client(self.teacher_b)
        r = c.get(f"/api/assignments/teacher/{self.assignment_b.id}/submissions/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(r.data), 2)

        by_name = {row["student_name"]: row for row in r.data}
        self.assertEqual(by_name["kid_b"]["submission_status"], "On time")
        self.assertIsNotNone(by_name["kid_b"]["submitted_file"])

        pending = by_name["kid_b2"]
        self.assertIsNone(pending["id"])
        self.assertIsNone(pending["submitted_file"])
        self.assertEqual(pending["submission_status"], "Not submitted")
        self.assertEqual(pending["max_marks"], self.assignment_b.max_marks)

    def test_roster_is_scoped_to_the_assignments_batch(self):
        """kid_a sits in 10-A and was never set this work — listing them as
        'pending' would invent a missing submission."""
        c = _teacher_client(self.teacher_b)
        r = c.get(f"/api/assignments/teacher/{self.assignment_b.id}/submissions/")
        self.assertNotIn("kid_a", [row["student_name"] for row in r.data])

    def test_response_is_a_bare_array(self):
        c = _teacher_client(self.teacher_b)
        r = c.get(f"/api/assignments/teacher/{self.assignment_b.id}/submissions/")
        self.assertIsInstance(r.data, list)


class ResubmissionNotificationTest(AssignmentScopeFixtureMixin, TestCase):
    """§1 MEDIUM — activity/signals.assignment_submitted returned early on
    `if not created`, but SubmitAssignmentView uses update_or_create. A
    student re-uploading a corrected PDF notified nobody, while submitted_at
    moved and silently flipped the teacher's On-time/Late chip."""

    @classmethod
    def setUpTestData(cls):
        cls.build_world()

    def _submit(self, name):
        c = _learner_client(self.learner_account, self.learner_profile)
        return c.post(
            f"/api/assignments/{self.assignment_b.id}/submit/",
            {"file": SimpleUploadedFile(name, b"%PDF",
                                        content_type="application/pdf")},
            format="multipart",
        )

    def test_resubmission_notifies_the_teacher(self):
        from activity.models import Activity

        self.assertEqual(self._submit("v1.pdf").status_code, 200)
        first = Activity.objects.filter(
            user=self.teacher_b, type=Activity.TYPE_SUBMISSION).count()
        self.assertEqual(first, 1)

        r = self._submit("v2.pdf")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.data["resubmitted"])

        acts = Activity.objects.filter(
            user=self.teacher_b, type=Activity.TYPE_SUBMISSION)
        self.assertEqual(acts.count(), 2)
        self.assertTrue(acts.filter(title__contains="re-submitted").exists())

    def test_grading_does_not_notify_the_teacher_again(self):
        from activity.models import Activity

        self._submit("v1.pdf")
        submission = AssignmentSubmission.objects.get()
        c = _teacher_client(self.teacher_b)
        r = c.post(
            f"/api/assignments/teacher/submissions/{submission.id}/grade/",
            {"marks_obtained": 40})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(
            Activity.objects.filter(
                user=self.teacher_b, type=Activity.TYPE_SUBMISSION).count(),
            1)

    def test_grading_does_not_move_submitted_at(self):
        """submitted_at was auto_now, so any save without update_fields
        rewrote it and could flip On-time to Late."""
        self._submit("v1.pdf")
        submission = AssignmentSubmission.objects.get()
        original = submission.submitted_at

        submission.feedback = "nice"
        submission.save()

        submission.refresh_from_db()
        self.assertEqual(submission.submitted_at, original)


class TeacherEditRoundTripTest(AssignmentScopeFixtureMixin, TestCase):
    """§1 HIGH ×2 — the Edit form is seeded entirely from
    TeacherAssignmentListSerializer, which carried neither `description` (so
    the form initialised it to "" and the teacher retyped the whole brief) nor
    `chapter_id` (so the chapter select opened blank), and
    TeacherAssignmentUpdateSerializer had no `chapter_id` field at all, so
    DRF silently dropped the value the form did POST."""

    @classmethod
    def setUpTestData(cls):
        cls.build_world()
        cls.assignment_b.description = "A four hundred word brief."
        cls.assignment_b.save()
        cls.chapter_2 = Chapter.objects.create(
            subject=cls.subject, title="Geometry", order=1)

    def test_list_row_carries_description_and_chapter(self):
        c = _teacher_client(self.teacher_b)
        row = next(r for r in c.get("/api/assignments/teacher/all/").data
                   if r["id"] == str(self.assignment_b.id))
        self.assertEqual(row["description"], "A four hundred word brief.")
        self.assertEqual(row["chapter_id"], str(self.chapter.id))

    def test_chapter_can_actually_be_changed(self):
        c = _teacher_client(self.teacher_b)
        r = c.patch(f"/api/assignments/teacher/{self.assignment_b.id}/edit/",
                    {"chapter_id": str(self.chapter_2.id)}, format="multipart")
        self.assertEqual(r.status_code, 200, r.content)
        self.assignment_b.refresh_from_db()
        self.assertEqual(self.assignment_b.chapter_id, self.chapter_2.id)

    def test_chapter_cannot_be_moved_to_another_subject(self):
        other_subject = Subject.objects.create(
            course=self.course, name="Science")
        foreign = Chapter.objects.create(
            subject=other_subject, title="Cells", order=0)
        c = _teacher_client(self.teacher_b)
        r = c.patch(f"/api/assignments/teacher/{self.assignment_b.id}/edit/",
                    {"chapter_id": str(foreign.id)}, format="multipart")
        self.assertEqual(r.status_code, 400, r.content)

    def test_max_marks_round_trips(self):
        c = _teacher_client(self.teacher_b)
        r = c.patch(f"/api/assignments/teacher/{self.assignment_b.id}/edit/",
                    {"max_marks": 20}, format="multipart")
        self.assertEqual(r.status_code, 200, r.content)
        self.assignment_b.refresh_from_db()
        self.assertEqual(self.assignment_b.max_marks, 20)


class CustomChapterCreationTest(AssignmentScopeFixtureMixin, TestCase):
    """A teacher creating an assignment can now type a brand-new chapter
    name instead of picking only from the curated list — resolved via
    courses.services.resolve_or_create_chapter()."""

    @classmethod
    def setUpTestData(cls):
        cls.build_world()

    def _create(self, teacher, **fields):
        payload = {
            "batch_id": str(self.batch_b.id),
            "title": "New worksheet",
            "due_date": (timezone.now() + timedelta(days=3)).isoformat(),
            **fields,
        }
        return _teacher_client(teacher).post(
            "/api/assignments/teacher/create/", payload, format="multipart")

    def test_custom_chapter_creates_a_real_chapter(self):
        r = self._create(
            self.teacher_b,
            custom_chapter="Trigonometry", subject_id=str(self.subject.id),
        )
        self.assertEqual(r.status_code, 201, r.content)
        chapter = Chapter.objects.get(subject=self.subject, title="Trigonometry")
        assignment = Assignment.objects.get(id=r.data["id"])
        self.assertEqual(assignment.chapter_id, chapter.id)

    def test_repeat_custom_chapter_name_reuses_the_existing_row(self):
        first = self._create(
            self.teacher_b,
            custom_chapter="Trigonometry", subject_id=str(self.subject.id),
        )
        self.assertEqual(first.status_code, 201, first.content)

        second = self._create(
            self.teacher_b,
            custom_chapter="TRIGONOMETRY", subject_id=str(self.subject.id),
        )
        self.assertEqual(second.status_code, 201, second.content)

        self.assertEqual(
            Chapter.objects.filter(subject=self.subject, title__iexact="Trigonometry").count(),
            1,
        )
        first_assignment = Assignment.objects.get(id=first.data["id"])
        second_assignment = Assignment.objects.get(id=second.data["id"])
        self.assertEqual(first_assignment.chapter_id, second_assignment.chapter_id)

    def test_neither_chapter_id_nor_custom_chapter_is_rejected(self):
        r = self._create(self.teacher_b)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("chapter_id", r.data)

    def test_custom_chapter_for_a_subject_not_taught_in_this_batch_is_rejected(self):
        other_subject = Subject.objects.create(course=self.course, name="Chemistry")
        r = self._create(
            self.teacher_b,
            custom_chapter="Organic Chemistry", subject_id=str(other_subject.id),
        )
        self.assertEqual(r.status_code, 400, r.content)
        # The unauthorized attempt must not leave a stray Chapter behind.
        self.assertFalse(
            Chapter.objects.filter(subject=other_subject, title="Organic Chemistry").exists()
        )
