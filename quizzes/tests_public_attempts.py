"""Public Quiz Hub attempts (design_handoff_public_quiz_hub Phase 6).

Three properties carry real risk and each has tests below:

1. The review reveals the answer key, so it must be unreachable before
   submission and unreachable for somebody else's signed-in attempt.
2. The paper is SNAPSHOT at start. A PracticeSet's membership is a live query
   (Phase 5), so without the snapshot a learner could be reviewed on
   questions they were never asked.
3. A score is frozen. An admin fixing an answer key later must not silently
   rewrite a result somebody already saw.
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from global_settings.models import GlobalSettings
from quizzes.models import (
    Choice, PracticeSet, PublicAttempt, Question, QuestionTag,
)

SETS = "/api/quizzes/public/sets/"
ATTEMPTS = "/api/quizzes/public/attempts/"


def make_question(subject, text, *, correct="Right", wrong="Wrong"):
    q = Question.objects.create(
        quiz=None, text=text, explanation=f"Because {correct}.",
        bank_state=Question.BANK_STATE_ACCEPTED)
    Choice.objects.create(question=q, text=correct, is_correct=True)
    Choice.objects.create(question=q, text=wrong, is_correct=False)
    q.tags.set([subject])
    return q


class AttemptTestCase(TestCase):
    def setUp(self):
        cache.clear()
        g = GlobalSettings.load()
        g.public_quiz_hub_enabled = True
        g.save()
        self.subject = QuestionTag.objects.create(
            kind=QuestionTag.KIND_SUBJECT, label="History",
            status=QuestionTag.STATUS_LIVE)
        self.qs = [make_question(self.subject, f"Q{i}?") for i in range(3)]
        self.set = PracticeSet.objects.create(
            title="History 01", subject_tag=self.subject,
            status=PracticeSet.STATUS_PUBLISHED, question_count=3)

    def start(self):
        r = self.client.post(f"{SETS}{self.set.slug}/attempts/")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data["attempt_id"], r.data

    def correct_id(self, question):
        return str(question.choices.get(is_correct=True).id)

    def wrong_id(self, question):
        return str(question.choices.filter(is_correct=False).first().id)


class AnonymousRoundTripTest(AttemptTestCase):

    def test_an_anonymous_visitor_can_start_answer_and_review(self):
        attempt_id, started = self.start()
        self.assertEqual(started["total"], 3)
        self.assertEqual(len(started["questions"]), 3)

        answers = [{"question": str(q.id), "choice": self.correct_id(q)}
                   for q in self.qs]
        r = self.client.post(f"{ATTEMPTS}{attempt_id}/submit/",
                             {"answers": answers},
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["score"], 3)
        self.assertEqual(r.data["total"], 3)

    def test_the_started_paper_never_contains_the_answer(self):
        _, started = self.start()
        body = str(started)
        self.assertNotIn("is_correct", body)
        self.assertNotIn("explanation", body)

    def test_the_attempt_is_stored_with_no_account(self):
        attempt_id, _ = self.start()
        self.assertIsNone(PublicAttempt.objects.get(pk=attempt_id).account_id)

    def test_a_wrong_answer_scores_zero_and_is_marked_wrong(self):
        attempt_id, _ = self.start()
        answers = [{"question": str(q.id), "choice": self.wrong_id(q)}
                   for q in self.qs]
        r = self.client.post(f"{ATTEMPTS}{attempt_id}/submit/",
                             {"answers": answers},
                             content_type="application/json")
        self.assertEqual(r.data["score"], 0)
        self.assertTrue(all(a["is_correct"] is False for a in r.data["answers"]))


class BlankAnswerTest(AttemptTestCase):
    """The state QuizAttempt structurally cannot hold."""

    def test_an_omitted_question_is_recorded_as_blank_not_an_error(self):
        attempt_id, _ = self.start()
        r = self.client.post(f"{ATTEMPTS}{attempt_id}/submit/",
                             {"answers": []}, content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["score"], 0)
        self.assertTrue(all(a["was_blank"] for a in r.data["answers"]))

    def test_an_explicit_null_choice_is_blank(self):
        attempt_id, _ = self.start()
        answers = [{"question": str(q.id), "choice": None} for q in self.qs]
        r = self.client.post(f"{ATTEMPTS}{attempt_id}/submit/",
                             {"answers": answers},
                             content_type="application/json")
        self.assertTrue(all(a["was_blank"] for a in r.data["answers"]))

    def test_a_partly_finished_paper_still_scores(self):
        attempt_id, _ = self.start()
        answers = [{"question": str(self.qs[0].id),
                    "choice": self.correct_id(self.qs[0])}]
        r = self.client.post(f"{ATTEMPTS}{attempt_id}/submit/",
                             {"answers": answers},
                             content_type="application/json")
        self.assertEqual(r.data["score"], 1)
        blanks = [a for a in r.data["answers"] if a["was_blank"]]
        self.assertEqual(len(blanks), 2)


class ReviewIsGatedTest(AttemptTestCase):
    """⚠ The review carries the answer key."""

    def test_the_review_409s_before_submission(self):
        attempt_id, _ = self.start()
        r = self.client.get(f"{ATTEMPTS}{attempt_id}/")
        self.assertEqual(r.status_code, 409, r.content)

    def test_the_review_never_leaks_before_submission(self):
        attempt_id, _ = self.start()
        body = self.client.get(f"{ATTEMPTS}{attempt_id}/").content.decode()
        self.assertNotIn("correct_choice_id", body)
        self.assertNotIn("Because", body)

    def test_the_review_carries_the_key_after_submission(self):
        attempt_id, _ = self.start()
        self.client.post(f"{ATTEMPTS}{attempt_id}/submit/",
                         {"answers": []}, content_type="application/json")
        r = self.client.get(f"{ATTEMPTS}{attempt_id}/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(all(a["correct_choice_id"] for a in r.data["answers"]))
        self.assertTrue(all(a["explanation"] for a in r.data["answers"]))

    def test_resubmitting_is_refused(self):
        """Otherwise: submit, read the key, resubmit for full marks."""
        attempt_id, _ = self.start()
        body = {"answers": []}
        self.client.post(f"{ATTEMPTS}{attempt_id}/submit/", body,
                         content_type="application/json")
        again = self.client.post(f"{ATTEMPTS}{attempt_id}/submit/", body,
                                 content_type="application/json")
        self.assertEqual(again.status_code, 409, again.content)


class OwnershipTest(AttemptTestCase):
    """⚠ Auth here is cookie-JWT (accounts.authentication.CookieJWTAuthentication),
    NOT Django sessions — `force_login` leaves request.user anonymous and the
    attempt silently records account=None, which makes these tests pass for
    the wrong reason. Use APIClient.force_authenticate, as quizzes/tests.py
    does throughout."""

    def setUp(self):
        super().setUp()
        U = get_user_model()
        self.owner = U.objects.create_user(
            username="owner", email="owner@example.com", password="x")
        self.other = U.objects.create_user(
            username="other", email="other@example.com", password="x")

    def as_user(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def start_as(self, client):
        r = client.post(f"{SETS}{self.set.slug}/attempts/")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data["attempt_id"]

    def test_a_signed_in_attempt_is_linked_to_the_account(self):
        attempt_id = self.start_as(self.as_user(self.owner))
        self.assertEqual(
            PublicAttempt.objects.get(pk=attempt_id).account_id, self.owner.id)

    def test_another_account_cannot_read_it_even_with_the_id(self):
        owner = self.as_user(self.owner)
        attempt_id = self.start_as(owner)
        owner.post(f"{ATTEMPTS}{attempt_id}/submit/", {"answers": []},
                   format="json")
        self.assertEqual(
            self.as_user(self.other).get(f"{ATTEMPTS}{attempt_id}/").status_code,
            404)

    def test_an_anonymous_visitor_cannot_read_a_signed_in_attempt(self):
        owner = self.as_user(self.owner)
        attempt_id = self.start_as(owner)
        owner.post(f"{ATTEMPTS}{attempt_id}/submit/", {"answers": []},
                   format="json")
        self.assertEqual(
            self.client.get(f"{ATTEMPTS}{attempt_id}/").status_code, 404)

    def test_the_owner_can_read_their_own_attempt(self):
        """The guard must not lock out the person it belongs to."""
        owner = self.as_user(self.owner)
        attempt_id = self.start_as(owner)
        owner.post(f"{ATTEMPTS}{attempt_id}/submit/", {"answers": []},
                   format="json")
        self.assertEqual(
            owner.get(f"{ATTEMPTS}{attempt_id}/").status_code, 200)


class SnapshotTest(AttemptTestCase):
    """⚠ Why starting an attempt is a POST."""

    def test_the_review_shows_the_questions_served_not_the_sets_current_ones(self):
        attempt_id, started = self.start()
        served = {q["id"] for q in started["questions"]}
        # Curation moves on mid-attempt: three new questions land, and one of
        # the originals is pulled back to `suggested`.
        for i in range(3):
            make_question(self.subject, f"New{i}?")
        self.qs[0].bank_state = Question.BANK_STATE_SUGGESTED
        self.qs[0].save(update_fields=["bank_state"])

        self.client.post(f"{ATTEMPTS}{attempt_id}/submit/", {"answers": []},
                         content_type="application/json")
        r = self.client.get(f"{ATTEMPTS}{attempt_id}/")
        reviewed = {a["question_id"] for a in r.data["answers"]}
        self.assertEqual(reviewed, served)

    def test_total_is_frozen_at_the_number_served(self):
        attempt_id, _ = self.start()
        for i in range(5):
            make_question(self.subject, f"New{i}?")
        self.client.post(f"{ATTEMPTS}{attempt_id}/submit/", {"answers": []},
                         content_type="application/json")
        self.assertEqual(
            PublicAttempt.objects.get(pk=attempt_id).total, 3)


class ScoreIsFrozenTest(AttemptTestCase):

    def test_editing_the_answer_key_later_does_not_change_a_past_score(self):
        attempt_id, _ = self.start()
        answers = [{"question": str(q.id), "choice": self.correct_id(q)}
                   for q in self.qs]
        self.client.post(f"{ATTEMPTS}{attempt_id}/submit/", {"answers": answers},
                         content_type="application/json")

        # An admin decides the other option was right all along.
        q = self.qs[0]
        q.choices.update(is_correct=False)
        q.choices.filter(text="Wrong").update(is_correct=True)

        r = self.client.get(f"{ATTEMPTS}{attempt_id}/")
        self.assertEqual(r.data["score"], 3)

    def test_replacing_the_choices_keeps_the_review_readable(self):
        """The admin bank editor replaces choices WHOLESALE on PATCH. The FK
        goes null; `selected_text` is why the review still reads."""
        attempt_id, _ = self.start()
        answers = [{"question": str(self.qs[0].id),
                    "choice": self.correct_id(self.qs[0])}]
        self.client.post(f"{ATTEMPTS}{attempt_id}/submit/", {"answers": answers},
                         content_type="application/json")
        self.qs[0].choices.all().delete()
        Choice.objects.create(question=self.qs[0], text="Fresh", is_correct=True)

        r = self.client.get(f"{ATTEMPTS}{attempt_id}/")
        row = next(a for a in r.data["answers"]
                   if a["question_id"] == str(self.qs[0].id))
        self.assertEqual(row["selected_text"], "Right")
        self.assertIsNone(row["selected_choice_id"])
        # Still not mistaken for a skipped question.
        self.assertFalse(row["was_blank"])


class CrossQuestionChoiceTest(AttemptTestCase):

    def test_a_choice_from_another_question_does_not_score(self):
        """Otherwise a client could post any correct choice id it knows."""
        attempt_id, _ = self.start()
        answers = [{"question": str(self.qs[0].id),
                    "choice": self.correct_id(self.qs[1])}]
        r = self.client.post(f"{ATTEMPTS}{attempt_id}/submit/",
                             {"answers": answers},
                             content_type="application/json")
        self.assertEqual(r.data["score"], 0)


class EmptySetTest(AttemptTestCase):

    def test_starting_a_set_with_no_ready_questions_is_refused(self):
        empty_subject = QuestionTag.objects.create(
            kind=QuestionTag.KIND_SUBJECT, label="Empty")
        empty = PracticeSet.objects.create(
            title="Nothing here", subject_tag=empty_subject,
            status=PracticeSet.STATUS_PUBLISHED)
        r = self.client.post(f"{SETS}{empty.slug}/attempts/")
        self.assertEqual(r.status_code, 409, r.content)

    def test_a_draft_set_cannot_be_attempted(self):
        draft = PracticeSet.objects.create(
            title="Draft", subject_tag=self.subject,
            status=PracticeSet.STATUS_DRAFT)
        self.assertEqual(
            self.client.post(f"{SETS}{draft.slug}/attempts/").status_code, 404)


class AttemptCountTest(AttemptTestCase):

    def test_only_submitted_attempts_are_counted(self):
        self.start()                       # started, never submitted
        attempt_id, _ = self.start()
        self.client.post(f"{ATTEMPTS}{attempt_id}/submit/", {"answers": []},
                         content_type="application/json")
        r = self.client.get(SETS)
        self.assertEqual(r.data["results"][0]["attempt_count"], 1)


class QuestionDeleteGuardTest(AttemptTestCase):

    def test_a_question_with_public_attempts_is_reported_as_in_use(self):
        """Without this the admin sees "safe to delete" and the PROTECT on
        PublicAttemptAnswer.question turns a friendly 409 into a 500."""
        from quizzes.serializers import _bank_question_usage
        attempt_id, _ = self.start()
        self.client.post(f"{ATTEMPTS}{attempt_id}/submit/", {"answers": []},
                         content_type="application/json")
        usage = _bank_question_usage(self.qs[0])
        self.assertEqual(usage.get("public_attempt_answers"), 1)


class FlagGateTest(AttemptTestCase):

    def test_starting_an_attempt_503s_when_the_hub_is_off(self):
        g = GlobalSettings.load()
        g.public_quiz_hub_enabled = False
        g.save()
        cache.clear()
        r = self.client.post(f"{SETS}{self.set.slug}/attempts/")
        self.assertEqual(r.status_code, 503, r.content)
