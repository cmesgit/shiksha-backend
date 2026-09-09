"""Cover for the unified teacher resources hub.

The traps this suite exists to catch, all of which are silent — every one of
them produces a page that renders fine and is simply wrong:

  * a teacher seeing another teacher's subject content (scope leak);
  * `mine=true` sweeping up NULL-owner rows and claiming the caller wrote
    content whose author is unrecoverable;
  * the quiz batch filter returning nothing, because Quiz uses an M2M where
    EMPTY means course-wide while the other three use a nullable FK;
  * a quiz reported as a draft because `review_status` was consulted instead of
    `is_assigned` — an unreviewed but assigned quiz IS live to students;
  * a batch filter hiding course-wide content, which is the majority of what a
    batch actually receives.
"""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Role, User, UserRole
from assignments.models import Assignment
from courses.models import Batch, Chapter, Course, Subject, TeachingAssignment
from courses.models_recordings import SessionRecording
from materials.models import StudyMaterial
from quizzes.models import Quiz

URL = "/api/dashboard/teacher/resources/"


def teacher_client(user):
    c = APIClient()
    c.force_authenticate(user=user, token={"context": "teacher"})
    return c


class TeacherResourcesTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")

        cls.mine = User.objects.create_user(
            username="mine", email="mine@test.com", password="x")
        cls.colleague = User.objects.create_user(
            username="colleague", email="colleague@test.com", password="x")
        cls.stranger = User.objects.create_user(
            username="stranger", email="stranger@test.com", password="x")
        for u in (cls.mine, cls.colleague, cls.stranger):
            UserRole.objects.create(
                user=u, role=cls.teacher_role, is_active=True, is_primary=True)

        cls.course = Course.objects.create(title="Physics")
        cls.subject = Subject.objects.create(course=cls.course, name="Mechanics")
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Kinematics")
        cls.batch = Batch.objects.create(
            course=cls.course, name="Batch A", code="A1", year=2026)

        # A subject the caller has nothing to do with — nothing on it may ever
        # appear in their hub.
        cls.other_course = Course.objects.create(title="History")
        cls.other_subject = Subject.objects.create(
            course=cls.other_course, name="Modern")

        # `mine` and `colleague` both teach cls.subject; `stranger` teaches the
        # other one. Course-wide staffing (batch=None) on purpose: it is the
        # common shape and it exercises teacher_scope_filter's OR branches.
        #
        # The colleague is an ASSISTANT, not a second PRIMARY: the
        # uniq_active_primary_per_subject_courselevel constraint permits
        # exactly one active course-level PRIMARY per subject. Co-teaching is
        # expressed by role, and both roles have identical read scope — which
        # is precisely why colleagues' content shows up in this list.
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.mine, is_active=True,
            role=TeachingAssignment.ROLE_PRIMARY)
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.colleague, is_active=True,
            role=TeachingAssignment.ROLE_ASSISTANT)
        TeachingAssignment.objects.create(
            subject=cls.other_subject, teacher=cls.stranger, is_active=True)

        # ---- one row of each type, authored by `mine` ----
        cls.my_material = StudyMaterial.objects.create(
            subject=cls.subject, chapter=cls.chapter,
            title="My notes", uploaded_by=cls.mine)
        cls.due = timezone.now() + timedelta(days=7)
        cls.my_assignment = Assignment.objects.create(
            subject=cls.subject, title="My homework", due_date=cls.due,
            created_by=cls.mine, is_published=True)
        cls.my_quiz = Quiz.objects.create(
            subject=cls.subject, title="My quiz",
            created_by=cls.mine, is_assigned=True)
        cls.my_recording = SessionRecording.objects.create(
            subject=cls.subject, title="My lecture", uploaded_by=cls.mine,
            bunny_video_id="vid-1", status=4, is_published=True)

        # ---- a colleague's material on the SAME subject ----
        cls.their_material = StudyMaterial.objects.create(
            subject=cls.subject, title="Their notes",
            uploaded_by=cls.colleague)

        # ---- an ownerless assignment: the pre-migration state, permanently ----
        cls.orphan_assignment = Assignment.objects.create(
            subject=cls.subject, title="Ownerless homework", due_date=cls.due,
            created_by=None, is_published=True)

        # ---- a stranger's content, on a subject the caller doesn't teach ----
        cls.foreign_material = StudyMaterial.objects.create(
            subject=cls.other_subject, title="Not mine at all",
            uploaded_by=cls.stranger)

    # ------------------------------------------------------------------

    def get(self, user=None, **params):
        res = teacher_client(user or self.mine).get(URL, params)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def titles(self, body):
        return {r["title"] for r in body["results"]}

    # ------------------------------------------------------------------
    # the union itself
    # ------------------------------------------------------------------

    def test_returns_all_four_types_in_one_list(self):
        body = self.get()
        by_type = {r["type"] for r in body["results"]}
        self.assertEqual(
            by_type, {"material", "assignment", "quiz", "recording"})

    def test_totals_count_every_type_even_when_one_is_selected(self):
        """The type chips must keep their counts while a chip is active.

        Computing totals over the type-filtered set would make every chip except
        the selected one read 0 — which looks exactly like "you have none".
        """
        body = self.get(type="quiz")
        self.assertEqual({r["type"] for r in body["results"]}, {"quiz"})
        self.assertEqual(body["totals"]["material"], 2)
        self.assertEqual(body["totals"]["assignment"], 2)
        self.assertEqual(body["totals"]["quiz"], 1)
        self.assertEqual(body["totals"]["recording"], 1)

    def test_unknown_type_falls_back_to_everything(self):
        # A typo must not render as an empty library.
        body = self.get(type="materialz")
        self.assertEqual(len(body["results"]), 6)

    # ------------------------------------------------------------------
    # scope — the leak that matters
    # ------------------------------------------------------------------

    def test_never_returns_content_from_a_subject_the_caller_doesnt_teach(self):
        body = self.get()
        self.assertNotIn("Not mine at all", self.titles(body))

    def test_stranger_sees_only_their_own_subject(self):
        body = self.get(user=self.stranger)
        self.assertEqual(self.titles(body), {"Not mine at all"})

    def test_inactive_teaching_assignment_revokes_access(self):
        TeachingAssignment.objects.filter(
            teacher=self.mine, subject=self.subject
        ).update(is_active=False)
        body = self.get()
        self.assertEqual(body["results"], [])

    def test_duplicate_staffing_rows_do_not_duplicate_content(self):
        """A teacher staffed both course-wide AND on a batch is a legitimate,
        common shape — and without distinct() it doubles every row on the
        subject."""
        TeachingAssignment.objects.create(
            subject=self.subject, teacher=self.mine,
            batch=self.batch, is_active=True,
            role=TeachingAssignment.ROLE_ASSISTANT)
        body = self.get()
        titles = [r["title"] for r in body["results"]]
        self.assertEqual(len(titles), len(set(titles)), titles)

    # ------------------------------------------------------------------
    # ownership
    # ------------------------------------------------------------------

    def test_colleagues_content_is_listed_but_not_mine(self):
        body = self.get()
        rows = {r["title"]: r for r in body["results"]}
        self.assertTrue(rows["My notes"]["is_mine"])
        self.assertIn("Their notes", rows)
        self.assertFalse(rows["Their notes"]["is_mine"])
        self.assertEqual(rows["Their notes"]["owner_id"], str(self.colleague.id))

    def test_mine_filter_excludes_colleagues_and_ownerless_rows(self):
        body = self.get(mine="true")
        self.assertEqual(
            self.titles(body),
            {"My notes", "My homework", "My quiz", "My lecture"},
        )

    def test_ownerless_row_reports_no_owner_and_is_not_mine(self):
        """A NULL author must never be attributed to the caller."""
        body = self.get()
        row = next(r for r in body["results"]
                   if r["title"] == "Ownerless homework")
        self.assertIsNone(row["owner_id"])
        self.assertIsNone(row["owner_name"])
        self.assertFalse(row["is_mine"])

    # ------------------------------------------------------------------
    # status normalization — one vocabulary over four different flags
    # ------------------------------------------------------------------

    def test_material_with_no_publication_flag_reports_live(self):
        row = next(r for r in self.get()["results"]
                   if r["title"] == "My notes")
        self.assertEqual(row["status"], "live")

    def test_unpublished_assignment_is_draft(self):
        Assignment.objects.filter(id=self.my_assignment.id).update(
            is_published=False)
        row = next(r for r in self.get()["results"]
                   if r["title"] == "My homework")
        self.assertEqual(row["status"], "draft")

    def test_unassigned_quiz_is_draft(self):
        Quiz.objects.filter(id=self.my_quiz.id).update(is_assigned=False)
        row = next(r for r in self.get()["results"] if r["title"] == "My quiz")
        self.assertEqual(row["status"], "draft")

    def test_assigned_but_unreviewed_quiz_is_live(self):
        """review_status is the admin's opinion of the questions and does NOT
        gate student visibility. Reporting such a quiz as a draft would tell a
        teacher their class cannot see something the class can see."""
        Quiz.objects.filter(id=self.my_quiz.id).update(
            is_assigned=True, review_status=Quiz.REVIEW_DRAFT)
        row = next(r for r in self.get()["results"] if r["title"] == "My quiz")
        self.assertEqual(row["status"], "live")

    def test_transcoding_recording_is_processing_not_draft(self):
        SessionRecording.objects.filter(id=self.my_recording.id).update(
            status=2, is_published=True)
        row = next(r for r in self.get()["results"]
                   if r["title"] == "My lecture")
        self.assertEqual(row["status"], "processing")

    def test_errored_recording_reports_error(self):
        SessionRecording.objects.filter(id=self.my_recording.id).update(
            status=5)
        row = next(r for r in self.get()["results"]
                   if r["title"] == "My lecture")
        self.assertEqual(row["status"], "error")

    def test_finished_unpublished_recording_is_draft(self):
        SessionRecording.objects.filter(id=self.my_recording.id).update(
            status=4, is_published=False)
        row = next(r for r in self.get()["results"]
                   if r["title"] == "My lecture")
        self.assertEqual(row["status"], "draft")

    def test_status_filter_narrows_the_list(self):
        Assignment.objects.filter(id=self.my_assignment.id).update(
            is_published=False)
        body = self.get(status="draft")
        self.assertEqual(self.titles(body), {"My homework"})

    # ------------------------------------------------------------------
    # batch filtering — where quizzes disagree with everything else
    # ------------------------------------------------------------------

    def test_batch_filter_includes_course_wide_content(self):
        """Course-wide rows ARE delivered to every batch. Filtering to
        `batch=X` alone hides most of what batch X actually has."""
        batched = StudyMaterial.objects.create(
            subject=self.subject, title="Batch A handout",
            batch=self.batch, uploaded_by=self.mine)
        body = self.get(batch_id=str(self.batch.id), type="material")
        self.assertIn(batched.title, self.titles(body))
        self.assertIn("My notes", self.titles(body))  # course-wide

    def test_quiz_with_empty_batches_is_course_wide(self):
        """Quiz.batches EMPTY means course-wide, the inverse of a NULL FK.
        Reusing the FK form here returned nothing at all."""
        self.assertEqual(self.my_quiz.batches.count(), 0)
        body = self.get(batch_id=str(self.batch.id), type="quiz")
        self.assertEqual(self.titles(body), {"My quiz"})

    def test_quiz_scoped_to_another_batch_is_excluded(self):
        other_batch = Batch.objects.create(
            course=self.course, name="Batch B", code="B1", year=2026)
        self.my_quiz.batches.add(other_batch)
        body = self.get(batch_id=str(self.batch.id), type="quiz")
        self.assertEqual(self.titles(body), set())

    def test_batch_none_returns_only_course_wide(self):
        StudyMaterial.objects.create(
            subject=self.subject, title="Batch A handout",
            batch=self.batch, uploaded_by=self.mine)
        body = self.get(batch_id="none", type="material")
        self.assertNotIn("Batch A handout", self.titles(body))
        self.assertIn("My notes", self.titles(body))

    # ------------------------------------------------------------------
    # search, paging, access
    # ------------------------------------------------------------------

    def test_search_matches_across_types(self):
        body = self.get(q="my ")
        self.assertEqual(
            self.titles(body),
            {"My notes", "My homework", "My quiz", "My lecture"},
        )

    def test_count_is_the_unpaginated_total(self):
        body = self.get(limit=2)
        self.assertEqual(len(body["results"]), 2)
        self.assertEqual(body["count"], 6)
        self.assertFalse(body["truncated"])

    def test_offset_walks_the_list_without_repeats(self):
        first = self.get(limit=3)["results"]
        second = self.get(limit=3, offset=3)["results"]
        ids = [r["id"] for r in first] + [r["id"] for r in second]
        self.assertEqual(len(ids), len(set(ids)))

    def test_query_count_does_not_grow_with_the_number_of_rows(self):
        """The whole hub must cost a constant number of queries.

        _row_quiz reads obj.batches.all() for its batch label, and Quiz uses an
        M2M where the other three use a plain FK — so without
        prefetch_related("batches") that read is one query PER QUIZ. Measured
        before the fix: 3 queries for 2 quizzes, i.e. 101 for a teacher with a
        hundred. This asserts the shape (constant), not a magic number: it
        counts once, triples the data, and counts again.
        """
        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                res = teacher_client(self.mine).get(URL)
                self.assertEqual(res.status_code, 200)
            return len(ctx.captured_queries)

        before = count_queries()

        # Triple every type, including quizzes with real batch rows attached —
        # the exact shape that triggered the N+1.
        for i in range(6):
            StudyMaterial.objects.create(
                subject=self.subject, title=f"Bulk material {i}",
                uploaded_by=self.mine)
            Assignment.objects.create(
                subject=self.subject, title=f"Bulk assignment {i}",
                due_date=self.due, created_by=self.mine)
            quiz = Quiz.objects.create(
                subject=self.subject, title=f"Bulk quiz {i}",
                created_by=self.mine, is_assigned=True)
            quiz.batches.add(self.batch)
            SessionRecording.objects.create(
                subject=self.subject, title=f"Bulk recording {i}",
                uploaded_by=self.mine, bunny_video_id=f"bulk-{i}",
                status=4, is_published=True)

        after = count_queries()
        self.assertEqual(
            before, after,
            f"Query count grew from {before} to {after} as rows were added — "
            f"something in the row normalizers is lazy-loading per row.",
        )

    def test_malformed_uuid_filters_do_not_500(self):
        """These feed UUID columns, and the ORM raises Django's own
        ValidationError on junk — which DRF does NOT translate, so the endpoint
        returned a 500 with a stack trace. A stale bookmark, a batch CODE typed
        where its uuid was wanted, or a JS `undefined` in the query string all
        hit it.
        """
        for params in (
            {"subject_id": "abc"},
            {"course_id": "not-a-uuid"},
            {"batch_id": "2026-27"},
            {"subject_id": ""},
        ):
            with self.subTest(params=params):
                res = teacher_client(self.mine).get(URL, params)
                self.assertEqual(res.status_code, 200, res.content)

    def test_a_valid_id_still_filters_when_sent_beside_a_bad_one(self):
        res = teacher_client(self.mine).get(
            URL, {"subject_id": f"garbage,{self.subject.id}", "type": "material"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            {r["title"] for r in res.json()["results"]},
            {"My notes", "Their notes"},
        )

    def test_learner_context_is_refused(self):
        c = APIClient()
        c.force_authenticate(user=self.mine, token={"context": "learner"})
        self.assertEqual(c.get(URL).status_code, 403)

    def test_anonymous_is_refused(self):
        self.assertIn(APIClient().get(URL).status_code, (401, 403))
