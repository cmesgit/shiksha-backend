"""Term-plan pacing.

🔴 These assertions are about a judgement shown on a STUDENT'S OWN dashboard.
"BEHIND" is not a neutral label, so the cases that matter most here are the
ones where the honest answer is "no opinion": no plan, a half-filled plan, an
empty subject. Each of those must return None, not a status.
"""

from datetime import date

from django.test import SimpleTestCase

from courses.term_plan import (
    MIN_PLANNED_RATIO,
    STATUS_AHEAD,
    STATUS_BEHIND,
    STATUS_ON_TRACK,
    current_chapter,
    expected_fraction,
    pacing,
)

TODAY = date(2026, 11, 16)


def plan(*days):
    """Planned dates relative to TODAY; None stays None."""
    return [None if d is None else date(2026, 11, 16 - d) if d >= 0
            else date(2026, 11, 16 - d) for d in days]


class ExpectedFractionTests(SimpleTestCase):
    def test_no_chapters_has_no_expectation(self):
        self.assertIsNone(expected_fraction([], today=TODAY))

    def test_no_planned_dates_means_NO_PLAN_not_zero(self):
        # 🔴 The whole point. An undated syllabus is not a syllabus that is
        # 0% due — returning 0.0 here would brand every such class "BEHIND".
        self.assertIsNone(expected_fraction([None] * 10, today=TODAY))

    def test_a_half_filled_plan_is_not_a_plan(self):
        # 2 of 10 dated is a form someone started, not a term plan.
        dates = [date(2026, 1, 1), date(2026, 1, 2)] + [None] * 8
        self.assertLess(2 / 10, MIN_PLANNED_RATIO)
        self.assertIsNone(expected_fraction(dates, today=TODAY))

    def test_past_dates_count_as_due(self):
        dates = [date(2026, 11, 1)] * 5 + [date(2026, 12, 1)] * 5
        self.assertAlmostEqual(expected_fraction(dates, today=TODAY), 0.5)

    def test_undated_chapters_still_count_in_the_denominator(self):
        # 6 dated and all past, 4 undated. Expected is 6/10, NOT 6/6 —
        # dividing by the dated ones alone would demand 100% and call an
        # on-schedule class "behind".
        dates = [date(2026, 11, 1)] * 6 + [None] * 4
        self.assertGreaterEqual(6 / 10, MIN_PLANNED_RATIO)
        self.assertAlmostEqual(expected_fraction(dates, today=TODAY), 0.6)

    def test_a_date_of_today_is_already_due(self):
        self.assertAlmostEqual(
            expected_fraction([TODAY] * 4, today=TODAY), 1.0)


class PacingTests(SimpleTestCase):
    def all_past(self, n):
        return [date(2026, 11, 1)] * n

    def test_no_plan_yields_no_status_and_no_tick(self):
        status, expected = pacing(3, 10, [None] * 10, today=TODAY)
        self.assertIsNone(status)
        self.assertIsNone(expected)

    def test_empty_subject_yields_nothing(self):
        self.assertEqual(pacing(0, 0, [], today=TODAY), (None, None))

    def test_behind_when_actual_trails_expected(self):
        # expected 1.0, actual 0.2
        status, expected = pacing(2, 10, self.all_past(10), today=TODAY)
        self.assertEqual(status, STATUS_BEHIND)
        self.assertAlmostEqual(expected, 1.0)

    def test_ahead_when_actual_leads_expected(self):
        # 5 of 10 due, 9 covered.
        dates = self.all_past(5) + [date(2026, 12, 25)] * 5
        status, expected = pacing(9, 10, dates, today=TODAY)
        self.assertEqual(status, STATUS_AHEAD)
        self.assertAlmostEqual(expected, 0.5)

    def test_on_track_when_actual_matches_expected(self):
        dates = self.all_past(5) + [date(2026, 12, 25)] * 5
        status, _ = pacing(5, 10, dates, today=TODAY)
        self.assertEqual(status, STATUS_ON_TRACK)

    def test_small_slips_stay_on_track(self):
        # One chapter adrift in twelve must not be branded BEHIND. 1/12 is
        # 0.0833, so ON_TRACK_TOLERANCE has to exceed it — this test is what
        # caught the original 0.08 missing its own intent.
        dates = self.all_past(6) + [date(2026, 12, 25)] * 6
        status, _ = pacing(5, 12, dates, today=TODAY)
        self.assertEqual(status, STATUS_ON_TRACK)

    def test_expected_is_a_fraction_usable_as_a_tick_position(self):
        # 3 of 4 dated = 0.75, clear of MIN_PLANNED_RATIO. (An earlier version
        # of this test used 2 of 4 = 0.5, which is BELOW the threshold and
        # correctly returned None — the test was wrong, not the code.)
        _, expected = pacing(1, 4, self.all_past(3) + [None], today=TODAY)
        self.assertIsNotNone(expected)
        self.assertGreaterEqual(expected, 0.0)
        self.assertLessEqual(expected, 1.0)


class CurrentChapterTests(SimpleTestCase):
    def test_first_uncovered_by_order(self):
        chapters = [
            {"title": "Two", "order": 2, "is_covered": False},
            {"title": "One", "order": 1, "is_covered": True},
            {"title": "Three", "order": 3, "is_covered": False},
        ]
        self.assertEqual(current_chapter(chapters)["title"], "Two")

    def test_a_finished_subject_has_no_current_chapter(self):
        # 🔴 Not "the last one". A finished subject showing "Now: Ch 13"
        # forever reads as a class stuck on its final chapter.
        chapters = [{"title": "One", "order": 1, "is_covered": True}]
        self.assertIsNone(current_chapter(chapters))

    def test_no_chapters_is_safe(self):
        self.assertIsNone(current_chapter([]))
