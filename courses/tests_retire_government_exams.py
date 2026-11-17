"""Cover for `retire_government_exams`.

The command drops the catch-all "Government Exams" course out of the
competitive nav column and repoints the homepage card it backed at
"SSC Exams". Almost everything worth testing here is a safety property:

1. **Retiring the course alone is not safe.** `PublicFeaturedView` filters on
   `ShowcaseCourse.status`, never on `Course.status`, so a retired course does
   NOT remove or blank its homepage card. It flips `is_coming_soon` to False,
   which turns an inert tile into one offering Enrol / View syllabus buttons
   pointing at a URL that 404s. `test_the_card_still_reads_coming_soon` is the
   regression test for that whole class of mistake, and it asserts through the
   real endpoint rather than the model.

2. **It must refuse once the course stops being an empty placeholder.** The
   precondition is the REASON retiring is safe, not the course's current
   status — so building the course out disables the command instead of letting
   it quietly hide real content.

3. **It must not trample admin curation** it did not write (`level_label`).
"""

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from content.models import PublishStatus, ShowcaseCourse
from courses.models import Course, CourseCategory, Subject
from courses.views import derive_nav_categories


def _run(**kwargs):
    out = StringIO()
    call_command("retire_government_exams", stdout=out, stderr=out, **kwargs)
    return out.getvalue()


class _Fixture(TestCase):
    """The prod shape: two competitive COACHING courses in the same category,
    the older catch-all backing homepage card order 14."""

    def setUp(self):
        self.category = CourseCategory.objects.create(
            name="SSC Exams", slug="ssc", group=CourseCategory.GROUP_COMPETITIVE,
        )
        self.old = Course.objects.create(
            title="Government Exams", slug="government-exams",
            kind=Course.KIND_COACHING, status=Course.STATUS_COMING_SOON, price=0,
        )
        self.old.categories.add(self.category)
        self.new = Course.objects.create(
            title="SSC Exams", slug="ssc-exams",
            kind=Course.KIND_COACHING, status=Course.STATUS_COMING_SOON, price=0,
        )
        self.new.categories.add(self.category)
        self.card = ShowcaseCourse.objects.create(
            title="Government Exams", level_label="SSC · Banking",
            fact_line="Live + Recorded · Launching soon",
            tutor_name="T. Lalhmingthanga", categories=["competitive"],
            icon="book", order=14, course=self.old,
            status=PublishStatus.PUBLISHED,
        )

    def nav_labels(self):
        _tabs, competitive = derive_nav_categories()
        return [link["label"] for link in competitive]


class DryRunTests(_Fixture):
    def test_a_dry_run_writes_nothing(self):
        out = _run()
        self.old.refresh_from_db()
        self.card.refresh_from_db()
        self.assertEqual(self.old.status, Course.STATUS_COMING_SOON)
        self.assertEqual(self.card.course_id, self.old.id)
        self.assertEqual(self.card.level_label, "SSC · Banking")
        self.assertIn("DRY RUN", out)

    def test_a_dry_run_still_reports_the_work(self):
        out = _run()
        self.assertIn("REPOINT", out)
        self.assertIn("RETIRE", out)


class WriteTests(_Fixture):
    def test_the_course_is_retired_but_not_deleted(self):
        _run(yes=True)
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, Course.STATUS_DRAFT)
        # Reversible: the row and its slug survive.
        self.assertTrue(Course.objects.filter(slug="government-exams").exists())

    def test_the_card_is_repointed(self):
        _run(yes=True)
        self.card.refresh_from_db()
        self.assertEqual(self.card.course_id, self.new.id)
        self.assertEqual(self.card.title, "SSC Exams")
        self.assertEqual(self.card.level_label, "SSC")

    def test_the_duplicate_leaves_the_nav_and_the_replacement_stays(self):
        self.assertIn("Government Exams", self.nav_labels())
        _run(yes=True)
        labels = self.nav_labels()
        self.assertNotIn("Government Exams", labels)
        self.assertIn("SSC Exams", labels)

    def test_a_second_run_changes_nothing_further(self):
        _run(yes=True)
        self.card.refresh_from_db()
        stamp = self.card.updated_at

        out = _run(yes=True)
        self.assertIn("Nothing to do", out)
        self.card.refresh_from_db()
        self.old.refresh_from_db()
        self.assertEqual(self.card.course_id, self.new.id)
        self.assertEqual(self.card.updated_at, stamp)
        self.assertEqual(self.old.status, Course.STATUS_DRAFT)
        # And no second card was invented for the same slot.
        self.assertEqual(ShowcaseCourse.objects.filter(order=14).count(), 1)


class HomepageCardStaysCorrectTests(_Fixture):
    """The regression this command exists to avoid."""

    def _card_payload(self):
        res = self.client.get("/api/courses/public/featured/")
        self.assertEqual(res.status_code, 200)
        cards = res.json()["cards"] if isinstance(res.json(), dict) else res.json()
        return next(c for c in cards if c["order"] == 14)

    def test_the_card_still_reads_coming_soon(self):
        before = self._card_payload()
        self.assertTrue(before["is_coming_soon"])

        _run(yes=True)

        after = self._card_payload()
        # Still an inert "Coming Soon" tile, now titled after the live course.
        self.assertTrue(
            after["is_coming_soon"],
            "The card stopped reading Coming Soon — the homepage now offers "
            "Enrol / View syllabus buttons pointing at a course that 404s.",
        )
        self.assertEqual(after["title"], "SSC Exams")
        self.assertEqual(after["course_slug"], "ssc-exams")

    def test_retiring_without_repointing_would_break_the_card(self):
        """Pins WHY the repoint is mandatory, so nobody 'simplifies' the
        command down to a one-line status flip."""
        self.old.status = Course.STATUS_DRAFT
        self.old.save(update_fields=["status"])

        card = self._card_payload()
        self.assertFalse(card["is_coming_soon"])   # the silent flip
        self.assertEqual(card["course_slug"], "government-exams")
        # ...and that slug is now unreachable.
        self.assertEqual(
            self.client.get("/api/courses/public/by-slug/government-exams/").status_code,
            404,
        )


class RefusalTests(_Fixture):
    def test_it_refuses_once_the_course_has_real_content(self):
        Subject.objects.create(course=self.old, name="Quantitative Aptitude")
        with self.assertRaises(CommandError):
            _run(yes=True)
        self.old.refresh_from_db()
        self.card.refresh_from_db()
        self.assertEqual(self.old.status, Course.STATUS_COMING_SOON)
        self.assertEqual(self.card.course_id, self.old.id)

    def test_it_refuses_when_the_replacement_is_missing(self):
        self.new.delete()
        with self.assertRaises(CommandError):
            _run(yes=True)
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, Course.STATUS_COMING_SOON)

    def test_it_refuses_when_the_replacement_is_not_public(self):
        self.new.status = Course.STATUS_DRAFT
        self.new.save(update_fields=["status"])
        with self.assertRaises(CommandError):
            _run(yes=True)
        self.card.refresh_from_db()
        self.assertEqual(self.card.course_id, self.old.id)

    def test_it_refuses_when_the_replacement_is_not_in_the_nav(self):
        self.new.categories.clear()
        with self.assertRaises(CommandError):
            _run(yes=True)
        self.card.refresh_from_db()
        self.assertEqual(self.card.course_id, self.old.id)

    def test_a_refusal_rolls_back_the_repoint(self):
        """The guards run before any write, but the whole run is atomic too —
        a failure part-way must not leave the card pointing at a retired
        course, which is the one state worse than either endpoint."""
        Subject.objects.create(course=self.old, name="Reasoning")
        with self.assertRaises(CommandError):
            _run(yes=True)
        self.card.refresh_from_db()
        self.old.refresh_from_db()
        self.assertEqual(self.card.course_id, self.old.id)
        self.assertEqual(self.old.status, Course.STATUS_COMING_SOON)


class AdminCurationTests(_Fixture):
    def test_an_admin_chosen_level_label_is_kept(self):
        self.card.level_label = "Govt jobs"
        self.card.save(update_fields=["level_label"])

        out = _run(yes=True)

        self.card.refresh_from_db()
        self.assertEqual(self.card.level_label, "Govt jobs")
        self.assertEqual(self.card.course_id, self.new.id)   # still repointed
        self.assertIn("KEPT", out)

    def test_a_card_using_its_own_details_keeps_its_title(self):
        self.card.use_own_details = True
        self.card.save(update_fields=["use_own_details"])

        _run(yes=True)

        self.card.refresh_from_db()
        self.assertEqual(self.card.title, "Government Exams")
        self.assertEqual(self.card.course_id, self.new.id)


class NoCardTests(TestCase):
    """A deployment where the card was already removed by hand."""

    def test_it_still_retires_the_course(self):
        category = CourseCategory.objects.create(
            name="SSC Exams", slug="ssc", group=CourseCategory.GROUP_COMPETITIVE,
        )
        old = Course.objects.create(
            title="Government Exams", slug="government-exams",
            kind=Course.KIND_COACHING, status=Course.STATUS_COMING_SOON, price=0,
        )
        old.categories.add(category)
        new = Course.objects.create(
            title="SSC Exams", slug="ssc-exams",
            kind=Course.KIND_COACHING, status=Course.STATUS_COMING_SOON, price=0,
        )
        new.categories.add(category)

        _run(yes=True)

        old.refresh_from_db()
        self.assertEqual(old.status, Course.STATUS_DRAFT)


class AbsentCourseTests(TestCase):
    """A fresh install that never seeded the catch-all."""

    def test_it_is_a_no_op(self):
        out = _run(yes=True)
        self.assertIn("does not exist", out)
