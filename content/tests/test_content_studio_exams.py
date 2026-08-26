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

    def exam(self, title, *, kind=None, categorised=True):
        course = Course.objects.create(title=title, **({"kind": kind} if kind else {}))
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
