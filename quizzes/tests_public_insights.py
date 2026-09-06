"""The hub's signed-in panels and its hero counts
(design_handoff_public_quiz_hub Phases 7 + 8).

What is actually at risk here is HONESTY, not correctness in the usual sense.
Every figure on these panels is a claim made to a learner about their own
work, and the failure mode is a number that looks authoritative and is
wrong — a confident 0% for a subject they never practised, an accuracy that
silently counts blanks as errors, a recommendation for a set that opens onto
an empty paper. Each of those has a test below.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from global_settings.models import GlobalSettings
from quizzes.models import (
    Choice, PracticeSet, PublicAttempt, PublicAttemptAnswer, Question,
    QuestionTag,
)

RAILS = "/api/quizzes/public/rails/"
SUMMARY = "/api/quizzes/public/me/summary/"


def make_question(subject, text):
    q = Question.objects.create(
        quiz=None, text=text, explanation=f"Because of {text}.",
        bank_state=Question.BANK_STATE_ACCEPTED)
    Choice.objects.create(question=q, text="Right", is_correct=True)
    Choice.objects.create(question=q, text="Wrong", is_correct=False)
    q.tags.set([subject])
    return q


class HubTestCase(TestCase):
    def setUp(self):
        # The flag permission caches for 60s in Django's cache, which is NOT
        # rolled back between tests — one test turning it off poisons every
        # later one with a 503 while each still passes in isolation.
        cache.clear()
        g = GlobalSettings.load()
        g.public_quiz_hub_enabled = True
        g.save()
        self.user = get_user_model().objects.create_user(
            username="learner", email="learner@example.com",
            password="pw12345!", is_verified=True)
        self.api = APIClient()
        # Cookie-JWT only: force_login() would leave request.user anonymous
        # and the ownership assertions would pass for the wrong reason.
        self.api.force_authenticate(user=self.user)

    def subject(self, label, *, status=QuestionTag.STATUS_LIVE, color=""):
        return QuestionTag.objects.create(
            kind=QuestionTag.KIND_SUBJECT, label=label, status=status,
            color=color)

    def practice_set(self, subject, title, *, count=2,
                     status=PracticeSet.STATUS_PUBLISHED):
        return PracticeSet.objects.create(
            title=title, subject_tag=subject, status=status,
            question_count=count)

    def submitted_attempt(self, practice_set, questions, outcomes, *,
                          when=None, started=None):
        """Write a finished attempt directly.

        Outcomes are "correct" / "wrong" / "blank". Going through the HTTP
        endpoints would be more end-to-end but cannot produce an attempt
        dated last week, which the streak and recency tests need.
        """
        when = when or timezone.now()
        attempt = PublicAttempt.objects.create(
            practice_set=practice_set, account=self.user,
            total=len(questions), score=outcomes.count("correct"))
        for i, (q, outcome) in enumerate(zip(questions, outcomes)):
            right = q.choices.get(is_correct=True)
            wrong = q.choices.filter(is_correct=False).first()
            picked = {"correct": right, "wrong": wrong,
                      "blank": None}[outcome]
            PublicAttemptAnswer.objects.create(
                attempt=attempt, question=q, order=i,
                selected_choice=picked,
                selected_text=picked.text if picked else "",
                is_correct=(outcome == "correct"))
        PublicAttempt.objects.filter(pk=attempt.pk).update(
            submitted_at=when, started_at=started or when)
        attempt.refresh_from_db()
        return attempt


class SummaryAccessTest(HubTestCase):

    def test_a_guest_is_refused_and_gets_no_shaped_empty_response(self):
        """Hidden, never faked. An anonymous caller must not receive a
        zeroed-out payload it could render as if it were data."""
        r = self.client.get(SUMMARY)
        self.assertIn(r.status_code, (401, 403))

    def test_a_learner_with_no_attempts_is_told_so_rather_than_zeroed(self):
        r = self.api.get(SUMMARY)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.data["has_attempts"])
        # No rings, no chart, no fabricated averages.
        self.assertNotIn("totals", r.data)

    def test_one_learner_never_sees_another_learners_attempts(self):
        other = get_user_model().objects.create_user(
            username="other", email="other@example.com",
            password="pw12345!", is_verified=True)
        history = self.subject("History")
        questions = [make_question(history, f"Q{i}?") for i in range(2)]
        pset = self.practice_set(history, "History 01")
        self.submitted_attempt(pset, questions, ["correct", "correct"])

        api = APIClient()
        api.force_authenticate(user=other)
        self.assertFalse(api.get(SUMMARY).data["has_attempts"])


class SummaryFiguresTest(HubTestCase):

    def test_accuracy_ignores_blanks_but_average_score_does_not(self):
        """The distinction the fixture collapsed. A learner who answers two
        of four and gets both right has 100% accuracy, 50% attempt rate and
        50% average score. Reporting one number for all three would hide
        which problem they actually have."""
        history = self.subject("History")
        questions = [make_question(history, f"Q{i}?") for i in range(4)]
        pset = self.practice_set(history, "History 01", count=4)
        self.submitted_attempt(
            pset, questions, ["correct", "correct", "blank", "blank"])

        totals = self.api.get(SUMMARY).data["totals"]
        self.assertEqual(totals["accuracy"], 100)
        self.assertEqual(totals["attempt_rate"], 50)
        self.assertEqual(totals["average_score"], 50)
        self.assertEqual(totals["questions_answered"], 2)
        self.assertEqual(totals["questions_served"], 4)

    def test_an_answer_whose_choice_was_edited_away_still_counts_as_answered(self):
        """A NULL selected_choice means two different things. If an admin
        replaces a question's choices the learner's pick is SET_NULL, but
        `selected_text` survives — and telling them they skipped a question
        they answered is the bug this guards."""
        history = self.subject("History")
        questions = [make_question(history, f"Q{i}?") for i in range(2)]
        pset = self.practice_set(history, "History 01")
        attempt = self.submitted_attempt(
            pset, questions, ["correct", "wrong"])
        # Simulate the bank editor's delete-all + bulk_create on PATCH.
        Choice.objects.filter(question=questions[1]).delete()
        attempt.refresh_from_db()

        totals = self.api.get(SUMMARY).data["totals"]
        self.assertEqual(totals["questions_answered"], 2)
        self.assertEqual(totals["attempt_rate"], 100)

    def test_a_subject_with_nothing_answered_reports_null_not_zero(self):
        """"No data yet" and "you scored zero" are different statements and
        the caller cannot tell them apart from a bare 0."""
        history = self.subject("History")
        questions = [make_question(history, f"Q{i}?") for i in range(2)]
        pset = self.practice_set(history, "History 01")
        self.submitted_attempt(pset, questions, ["blank", "blank"])

        data = self.api.get(SUMMARY).data
        self.assertIsNone(data["by_subject"][0]["accuracy"])
        self.assertIsNone(data["totals"]["accuracy"])
        # ...and a subject with no usable accuracy cannot be "best".
        self.assertIsNone(data["best_subject"])

    def test_accuracy_is_reported_per_subject(self):
        history, polity = self.subject("History"), self.subject("Polity")
        h_qs = [make_question(history, f"H{i}?") for i in range(2)]
        p_qs = [make_question(polity, f"P{i}?") for i in range(2)]
        self.submitted_attempt(
            self.practice_set(history, "History 01"), h_qs,
            ["correct", "wrong"])
        self.submitted_attempt(
            self.practice_set(polity, "Polity 01"), p_qs,
            ["correct", "correct"])

        data = self.api.get(SUMMARY).data
        by_slug = {s["slug"]: s for s in data["by_subject"]}
        self.assertEqual(by_slug["history"]["accuracy"], 50)
        self.assertEqual(by_slug["polity"]["accuracy"], 100)
        self.assertEqual(data["best_subject"]["slug"], "polity")
        self.assertEqual(data["weak_subject"]["slug"], "history")

    def test_every_subject_gets_a_distinct_chart_colour_even_when_untagged(self):
        """The chart is unreadable if untinted subjects all fall back to one
        colour."""
        for label in ["History", "Polity", "Economy"]:
            subject = self.subject(label)
            questions = [make_question(subject, f"{label}{i}?")
                         for i in range(1)]
            self.submitted_attempt(
                self.practice_set(subject, f"{label} 01", count=1),
                questions, ["correct"])

        colors = [s["color"] for s in self.api.get(SUMMARY).data["by_subject"]]
        self.assertEqual(len(colors), 3)
        self.assertEqual(len(set(colors)), 3)
        self.assertTrue(all(c for c in colors))


class StreakTest(HubTestCase):

    def _attempt_on(self, day_offset):
        history = self.subject(f"History{day_offset}")
        questions = [make_question(history, f"Q{day_offset}?")]
        self.submitted_attempt(
            self.practice_set(history, f"Set {day_offset}", count=1),
            questions, ["correct"],
            when=timezone.now() - timedelta(days=day_offset))

    def test_consecutive_days_count(self):
        for offset in (0, 1, 2):
            self._attempt_on(offset)
        self.assertEqual(
            self.api.get(SUMMARY).data["totals"]["streak_days"], 3)

    def test_a_gap_breaks_the_streak(self):
        for offset in (0, 1, 4, 5):
            self._attempt_on(offset)
        self.assertEqual(
            self.api.get(SUMMARY).data["totals"]["streak_days"], 2)

    def test_practising_yesterday_but_not_yet_today_keeps_the_streak(self):
        """Otherwise the streak reads as broken at 00:01 for someone who
        practised at 23:00 and simply has not practised again yet."""
        for offset in (1, 2):
            self._attempt_on(offset)
        self.assertEqual(
            self.api.get(SUMMARY).data["totals"]["streak_days"], 2)

    def test_an_old_streak_is_not_reported_as_current(self):
        for offset in (6, 7, 8):
            self._attempt_on(offset)
        self.assertEqual(
            self.api.get(SUMMARY).data["totals"]["streak_days"], 0)


class RecentTest(HubTestCase):

    def test_recent_rows_carry_the_real_attempt_id_and_time_spent(self):
        """The Review button opens the actual attempt. The fixture had to
        fabricate a plausible past attempt because it had no id to open."""
        history = self.subject("History")
        questions = [make_question(history, f"Q{i}?") for i in range(2)]
        started = timezone.now() - timedelta(minutes=5)
        attempt = self.submitted_attempt(
            self.practice_set(history, "History 01"), questions,
            ["correct", "wrong"], when=started + timedelta(seconds=180),
            started=started)

        row = self.api.get(SUMMARY).data["recent"][0]
        self.assertEqual(row["attempt_id"], str(attempt.id))
        self.assertEqual(row["score"], 1)
        self.assertEqual(row["total"], 2)
        self.assertEqual(row["seconds_spent"], 180)

    def test_an_unsubmitted_attempt_never_appears(self):
        history = self.subject("History")
        questions = [make_question(history, f"Q{i}?") for i in range(2)]
        pset = self.practice_set(history, "History 01")
        self.submitted_attempt(pset, questions, ["correct", "correct"])
        # Someone opened a set and walked away.
        PublicAttempt.objects.create(
            practice_set=pset, account=self.user, total=2)

        data = self.api.get(SUMMARY).data
        self.assertEqual(len(data["recent"]), 1)
        self.assertEqual(data["totals"]["attempts"], 1)


class RecommendationTest(HubTestCase):

    def test_a_set_already_attempted_is_not_recommended(self):
        history = self.subject("History")
        questions = [make_question(history, f"Q{i}?") for i in range(2)]
        done = self.practice_set(history, "History 01")
        self.practice_set(history, "History 02")
        self.submitted_attempt(done, questions, ["correct", "wrong"])

        slugs = [r["slug"]
                 for r in self.api.get(SUMMARY).data["recommendations"]]
        self.assertIn("history-02", slugs)
        self.assertNotIn(done.slug, slugs)

    def test_a_published_set_that_resolves_to_no_questions_is_not_offered(self):
        """Recommending it would send the learner straight into the start
        endpoint's 409."""
        history = self.subject("History")
        empty_subject = self.subject("Economy")
        questions = [make_question(history, f"Q{i}?") for i in range(2)]
        self.submitted_attempt(
            self.practice_set(history, "History 01"), questions,
            ["wrong", "wrong"])
        self.practice_set(empty_subject, "Economy 01")

        slugs = [r["slug"]
                 for r in self.api.get(SUMMARY).data["recommendations"]]
        self.assertNotIn("economy-01", slugs)

    def test_a_draft_set_is_never_recommended(self):
        history = self.subject("History")
        questions = [make_question(history, f"Q{i}?") for i in range(2)]
        self.submitted_attempt(
            self.practice_set(history, "History 01"), questions,
            ["correct", "wrong"])
        self.practice_set(history, "History Draft",
                          status=PracticeSet.STATUS_DRAFT)

        slugs = [r["slug"]
                 for r in self.api.get(SUMMARY).data["recommendations"]]
        self.assertNotIn("history-draft", slugs)

    def test_the_weakest_subject_is_named_with_its_real_figure(self):
        history, polity = self.subject("History"), self.subject("Polity")
        h_qs = [make_question(history, f"H{i}?") for i in range(2)]
        p_qs = [make_question(polity, f"P{i}?") for i in range(2)]
        self.submitted_attempt(
            self.practice_set(history, "History 01"), h_qs,
            ["wrong", "wrong"])
        self.submitted_attempt(
            self.practice_set(polity, "Polity 01"), p_qs,
            ["correct", "correct"])
        self.practice_set(history, "History 02")

        recs = self.api.get(SUMMARY).data["recommendations"]
        weakest = next(r for r in recs if r["why"] == "Weakest area")
        self.assertEqual(weakest["subject"], "History")
        self.assertIn("0%", weakest["note"])


class RailStatsTest(HubTestCase):
    """The hero's counts. The build guide's instruction was to make them real
    or cut them — never ship a number the bank cannot back."""

    def test_stats_count_questions_once_across_overlapping_tags(self):
        """A question carrying both a subject and an exam tag must not be
        counted twice. Summing the per-tag counts would do exactly that."""
        history = self.subject("History")
        ssc = QuestionTag.objects.create(
            kind=QuestionTag.KIND_EXAM, label="SSC",
            status=QuestionTag.STATUS_LIVE)
        q = make_question(history, "Shared?")
        q.tags.set([history, ssc])
        make_question(history, "History only?")

        stats = self.client.get(RAILS).data["stats"]
        self.assertEqual(stats["questions"], 2)

    def test_a_subject_live_over_an_empty_bank_is_not_counted(self):
        """`live` is a floor the admin cannot override upward. The hero must
        agree with the chips a visitor can actually click."""
        self.subject("Economy", status=QuestionTag.STATUS_LIVE)
        history = self.subject("History")
        make_question(history, "Q?")

        body = self.client.get(RAILS).data
        self.assertEqual(body["stats"]["subjects"], 1)
        by_slug = {s["slug"]: s for s in body["subjects"]}
        self.assertEqual(by_slug["economy"]["status"], QuestionTag.STATUS_SOON)

    def test_only_published_sets_are_counted(self):
        history = self.subject("History")
        make_question(history, "Q?")
        self.practice_set(history, "Live one")
        self.practice_set(history, "Hidden one",
                          status=PracticeSet.STATUS_DRAFT)

        self.assertEqual(self.client.get(RAILS).data["stats"]["sets"], 1)

    def test_set_count_is_not_inflated_by_the_question_join(self):
        """Two annotations over different relations multiply each other
        without distinct=True — the "2 subjects report as 6" trap."""
        history = self.subject("History")
        for i in range(3):
            make_question(history, f"Q{i}?")
        self.practice_set(history, "Set A")
        self.practice_set(history, "Set B")
        self.practice_set(history, "Draft one",
                          status=PracticeSet.STATUS_DRAFT)

        subjects = self.client.get(RAILS).data["subjects"]
        row = next(s for s in subjects if s["slug"] == "history")
        self.assertEqual(row["set_count"], 2)
        self.assertEqual(row["question_count"], 3)

    def test_a_tag_without_cover_art_returns_null_not_a_broken_path(self):
        history = self.subject("History")
        make_question(history, "Q?")
        subjects = self.client.get(RAILS).data["subjects"]
        self.assertIsNone(subjects[0]["cover_image"])


class SetOrderingAndSearchTest(HubTestCase):
    """Sort and search happen on the SERVER because the list is paginated.
    In the browser they would silently apply to the loaded page only."""

    SETS = "/api/quizzes/public/sets/"

    def setUp(self):
        super().setUp()
        self.history = self.subject("History")
        for i in range(3):
            make_question(self.history, f"Q{i}?")
        self.easy = PracticeSet.objects.create(
            title="Ancient India", subject_tag=self.history,
            status=PracticeSet.STATUS_PUBLISHED, question_count=2,
            difficulty=Question.DIFFICULTY_EASY, minutes=20)
        self.hard = PracticeSet.objects.create(
            title="Medieval India", subject_tag=self.history,
            status=PracticeSet.STATUS_PUBLISHED, question_count=2,
            difficulty=Question.DIFFICULTY_HARD, minutes=5)

    def slugs(self, **params):
        r = self.client.get(self.SETS, params)
        self.assertEqual(r.status_code, 200, r.content)
        return [row["slug"] for row in r.data["results"]]

    def test_easiest_first_is_not_alphabetical_on_the_difficulty_string(self):
        """Ordering the raw column gives easy → hard → medium."""
        self.assertEqual(self.slugs(ordering="easy")[0], self.easy.slug)
        self.assertEqual(self.slugs(ordering="hard")[0], self.hard.slug)

    def test_shortest_first_uses_minutes(self):
        self.assertEqual(self.slugs(ordering="short")[0], self.hard.slug)

    def test_search_spans_title_and_subject(self):
        self.assertEqual(self.slugs(q="Medieval"), [self.hard.slug])
        self.assertEqual(len(self.slugs(q="History")), 2)

    def test_an_unknown_ordering_falls_back_rather_than_erroring(self):
        self.assertEqual(len(self.slugs(ordering="nonsense")), 2)
