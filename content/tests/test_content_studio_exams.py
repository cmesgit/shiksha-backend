"""Content Studio Phase 8 — competitive exam readiness.

The screen's whole value is that its numbers are true. If it says an exam has
zero subjects, there are zero subjects — so these tests are mostly about the
counts being real counts, and about the competitive check catching courses that
only one of the two discriminators would find.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from content.models import ShowcaseCourse
from courses.models import Chapter, Course, CourseCategory, Subject

User = get_user_model()

URL = "/api/content/admin/exams/readiness/"


class ExamReadinessTest(TestCase):
    def setUp(self):
        # The Studio permission caches content_studio_enabled. Django rolls the
        # DB back between tests but NOT the cache, so a test that flips the flag
        # off would otherwise leak a cached False into every test after it.
        from django.core.cache import cache

        from content.permissions import IsStudioEditor
        cache.delete(IsStudioEditor.CACHE_KEY)
        self.editor = User.objects.create_user(
            username="ed", email="ed@example.com", password="x", is_staff=True,
        )
        self.outsider = User.objects.create_user(
            username="l", email="l@example.com", password="x",
        )
        self.competitive = CourseCategory.objects.create(
            name="Competitive", group="competitive",
        )

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def exam(self, title, *, kind=None, categorised=True, status=None):
        # Course.status defaults to DRAFT, which now means "not in the navbar".
        # A real competitive exam is COMING_SOON, so that is the default here;
        # the visibility tests pass an explicit status.
        course = Course.objects.create(
            title=title,
            status=status or Course.STATUS_COMING_SOON,
            **({"kind": kind} if kind else {}),
        )
        if categorised:
            course.categories.add(self.competitive)
        return course

    def body(self):
        return self.client_for(self.editor).get(URL).json()

    # ── what counts as an exam ────────────────────────────────────

    def test_finds_a_course_linked_only_by_category(self):
        """create_competitive_courses skips the category link with a warning
        when categories were never seeded, so `kind` alone misses some — and
        courses categorised without the kind set miss the other way."""
        self.exam("UPSC Prelims")
        self.assertEqual([e["name"] for e in self.body()["exams"]], ["UPSC Prelims"])

    def test_finds_a_coaching_course_with_no_category(self):
        self.exam("SSC CGL", kind="COACHING", categorised=False)
        self.assertEqual([e["name"] for e in self.body()["exams"]], ["SSC CGL"])

    def test_ignores_an_ordinary_school_course(self):
        school = CourseCategory.objects.create(name="Class 9", group="class8-12")
        Course.objects.create(title="Class 9 Science").categories.add(school)
        self.assertEqual(self.body()["exams"], [])

    def test_each_exam_appears_once(self):
        """A course matching BOTH discriminators must not be listed twice."""
        self.exam("UPSC Prelims", kind="COACHING")
        self.assertEqual(len(self.body()["exams"]), 1)

    # ── the counts are real ───────────────────────────────────────

    def test_a_bare_exam_reports_honest_zeros(self):
        """The acceptance criterion: it says zero, because that is the truth."""
        self.exam("UPSC Prelims")
        exam = self.body()["exams"][0]
        for key in ("has_card", "subject_count", "chapter_count",
                    "material_count", "quiz_count"):
            self.assertEqual(exam[key], 0, key)
        self.assertEqual(exam["state"], "coming_soon")
        self.assertTrue(all(not s["done"] for s in exam["steps"]))

    def test_subject_and_chapter_counts_are_not_inflated_by_joins(self):
        """Counting subjects and chapters in one annotate() without distinct
        multiplies the rows — 2 subjects x 3 chapters would report 6 subjects."""
        course = self.exam("UPSC Prelims")
        for name in ("History", "Polity"):
            subject = Subject.objects.create(course=course, name=name)
            for i in range(3):
                Chapter.objects.create(subject=subject, title=f"{name} {i}")

        exam = self.body()["exams"][0]
        self.assertEqual(exam["subject_count"], 2)
        self.assertEqual(exam["chapter_count"], 6)

    def test_has_card_reflects_a_linked_showcase_card(self):
        course = self.exam("UPSC Prelims")
        exam = self.body()["exams"][0]
        self.assertEqual(exam["has_card"], 0)

        ShowcaseCourse.objects.create(
            title="UPSC", level_label="Coaching", course=course,
        )
        self.assertEqual(self.body()["exams"][0]["has_card"], 1)

    def test_state_needs_both_subjects_and_material(self):
        """Derived server-side from the counts, never stored — a stored flag
        would drift the moment someone added a subject elsewhere."""
        course = self.exam("UPSC Prelims")
        Subject.objects.create(course=course, name="History")

        exam = self.body()["exams"][0]
        self.assertEqual(exam["subject_count"], 1)
        self.assertEqual(
            exam["state"], "coming_soon",
            "subjects alone is not enough — there is nothing to read yet",
        )

    # ── what the screen renders from ──────────────────────────────

    def test_summary_counts_match_the_exam_list(self):
        self.exam("UPSC Prelims")
        self.exam("SSC CGL")
        body = self.body()
        self.assertEqual(body["summary"]["total"], 2)
        self.assertEqual(body["summary"]["with_subjects"], 0)
        self.assertEqual(body["summary"]["coming_soon"], 2)
        self.assertEqual(body["summary"]["live"], 0)

    def test_suggests_the_one_furthest_along(self):
        bare = self.exam("SSC CGL")
        started = self.exam("UPSC Prelims")
        Subject.objects.create(course=started, name="History")

        body = self.body()
        self.assertEqual(body["suggested_id"], str(started.id))
        self.assertEqual(
            body["exams"][0]["name"], "UPSC Prelims",
            "the most-progressed exam sorts first",
        )
        self.assertNotEqual(body["suggested_id"], str(bare.id))

    def test_pipeline_labels_match_the_step_keys(self):
        self.exam("UPSC Prelims")
        body = self.body()
        self.assertEqual(
            body["pipeline"],
            [s["label"] for s in body["exams"][0]["steps"]],
        )

    def test_scheduling_is_reported_unavailable(self):
        """Quiz has no start/availability date at all, so the setup rail marks
        that step blocked rather than inviting an impossible action."""
        self.assertFalse(self.body()["scheduling_available"])

    def test_edit_url_points_at_the_existing_course_editor(self):
        """`Add content` deep-links into Courses.jsx rather than rebuilding
        subject/chapter editing inside the CMS."""
        course = self.exam("UPSC Prelims")
        self.assertEqual(
            self.body()["exams"][0]["edit_url"], f"/courses?course={course.id}",
        )

    def test_non_staff_is_refused(self):
        self.assertEqual(self.client_for(self.outsider).get(URL).status_code, 403)


class ExamReadinessQueryCountTest(ExamReadinessTest):
    """The screen loads every exam at once, so a per-exam query is an N+1 that
    grows with the product. It was 3 queries per exam before this was pinned:
    two count() calls plus _is_competitive's category lookup."""

    def test_query_count_does_not_grow_with_the_number_of_exams(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for i in range(3):
            self.exam(f"Exam {i}")

        # One warm-up request first. IsStudioEditor reads content_studio_enabled
        # and caches it, so the very first request of the process pays an extra
        # query the second does not — a one-off constant, not per-exam growth,
        # but enough to make the two samples below incomparable.
        self.client_for(self.editor).get(URL)

        with CaptureQueriesContext(connection) as few:
            self.client_for(self.editor).get(URL)

        for i in range(3, 15):
            self.exam(f"Exam {i}")
        with CaptureQueriesContext(connection) as many:
            body = self.client_for(self.editor).get(URL).json()

        self.assertEqual(len(body["exams"]), 15)
        self.assertEqual(
            len(many), len(few),
            f"query count grew from {len(few)} to {len(many)} as exams went "
            f"3 -> 15; something is querying per exam again",
        )

    def test_blurb_reads_the_field_that_exists(self):
        """Course.description, not short_description. getattr's default made
        every blurb silently blank while the screen looked fine."""
        course = self.exam("UPSC Prelims")
        course.description = "The civil services preliminary examination."
        course.save()
        self.assertEqual(
            self.body()["exams"][0]["blurb"],
            "The civil services preliminary examination.",
        )


class ExamNavbarVisibilityTest(ExamReadinessTest):
    """A DRAFT or ARCHIVED course is not in the navbar, whatever its kind says.

    Prod carries two such rows — a stray "hy" and a duplicate "NEET" — and
    counting them as live made the screen claim nine published exams when
    seven were. Reporting it is the fix; deleting the rows is not, because both
    turned out to own real related data.
    """

    def test_a_draft_exam_is_not_counted_as_in_the_navbar(self):
        live = self.exam("UPSC Prelims")
        live.status = Course.STATUS_COMING_SOON
        live.save()
        junk = self.exam("hy")
        junk.status = Course.STATUS_DRAFT
        junk.save()

        body = self.body()
        self.assertEqual(body["summary"]["total"], 2)
        self.assertEqual(body["summary"]["in_navbar"], 1)
        self.assertEqual(body["summary"]["not_published"], 1)

        by_name = {e["name"]: e for e in body["exams"]}
        self.assertTrue(by_name["UPSC Prelims"]["in_navbar"])
        self.assertFalse(by_name["hy"]["in_navbar"])

    def test_an_archived_duplicate_is_listed_but_not_as_live(self):
        dupe = self.exam("NEET")
        dupe.status = Course.STATUS_ARCHIVED
        dupe.save()
        body = self.body()
        self.assertEqual([e["name"] for e in body["exams"]], ["NEET"])
        self.assertFalse(body["exams"][0]["in_navbar"])
        self.assertEqual(body["exams"][0]["course_status"], Course.STATUS_ARCHIVED)

    def test_an_unpublished_exam_is_never_the_suggested_one(self):
        """Sending someone to finish an exam nobody can reach is wasted work."""
        junk = self.exam("hy")
        junk.status = Course.STATUS_DRAFT
        junk.save()
        self.assertIsNone(self.body()["suggested_id"])

        real = self.exam("UPSC Prelims")
        real.status = Course.STATUS_COMING_SOON
        real.save()
        self.assertEqual(self.body()["suggested_id"], str(real.id))

    def test_coming_soon_counts_only_what_visitors_can_reach(self):
        """Counting unpublished rows here claimed 12 exams said "Coming soon"
        while only 10 were on the site."""
        self.exam("UPSC Prelims")
        junk = self.exam("hy", status=Course.STATUS_DRAFT)
        body = self.body()
        self.assertEqual(body["summary"]["total"], 2)
        self.assertEqual(body["summary"]["coming_soon"], 1)
        self.assertEqual(body["summary"]["in_navbar"], 1)
