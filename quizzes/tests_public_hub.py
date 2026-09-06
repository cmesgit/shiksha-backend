"""Public Quiz Hub read endpoints (design_handoff_public_quiz_hub Phase 5).

The tests that matter most here are the LEAK tests. Every other endpoint in
`quizzes/` requires a role, so a serializer mistake is contained to people who
already have access; these three are served to anonymous visitors, and
`BankQuestionSerializer` two files over exposes `is_correct`. Reaching for it
"just to reuse the shape" would publish the answer key.
"""
from django.core.cache import cache
from django.test import TestCase

from global_settings.models import GlobalSettings
from quizzes.models import Choice, PracticeSet, Question, QuestionTag

SETS = "/api/quizzes/public/sets/"
RAILS = "/api/quizzes/public/rails/"


def make_question(subject, *, exam=None, accepted=True, explanation="Because.",
                  difficulty="medium", text="Which site had a dockyard?"):
    q = Question.objects.create(
        quiz=None, text=text, explanation=explanation, difficulty=difficulty)
    Choice.objects.bulk_create([
        Choice(question=q, text="Lothal", is_correct=True),
        Choice(question=q, text="Harappa", is_correct=False),
    ])
    tags = [subject] + ([exam] if exam else [])
    q.tags.set(tags)
    if accepted:
        q.bank_state = Question.BANK_STATE_ACCEPTED
        q.save(update_fields=["bank_state"])
    return q


class PublicHubTestCase(TestCase):
    def setUp(self):
        # ⚠ IsPublicQuizHubEnabled CACHES the flag for 60s under
        # "quizzes:public_quiz_hub_enabled". Django rolls the DB back between
        # tests but not the cache, so FlagGateTest's `False` survives into
        # every test that runs after it and they all 503 — while passing in
        # isolation, which is the worst way to find out. Clear it here.
        cache.clear()
        g = GlobalSettings.load()
        g.public_quiz_hub_enabled = True
        g.save()
        self.subject = QuestionTag.objects.create(
            kind=QuestionTag.KIND_SUBJECT, label="History",
            status=QuestionTag.STATUS_LIVE)
        self.exam = QuestionTag.objects.create(
            kind=QuestionTag.KIND_EXAM, label="SSC CGL",
            status=QuestionTag.STATUS_LIVE)
        self.q = make_question(self.subject, exam=self.exam)
        self.set = PracticeSet.objects.create(
            title="Ancient India — Quiz 01", subject_tag=self.subject,
            status=PracticeSet.STATUS_PUBLISHED, question_count=10)


class AnonymousAccessTest(PublicHubTestCase):
    """The whole point: /quiz works signed out."""

    def test_an_anonymous_visitor_can_list_sets(self):
        r = self.client.get(SETS)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["count"], 1)

    def test_an_anonymous_visitor_can_open_a_set(self):
        r = self.client.get(f"{SETS}{self.set.slug}/")
        self.assertEqual(r.status_code, 200, r.content)

    def test_an_anonymous_visitor_can_read_the_rails(self):
        r = self.client.get(RAILS)
        self.assertEqual(r.status_code, 200, r.content)


class FlagGateTest(PublicHubTestCase):

    def test_everything_503s_when_the_hub_is_off(self):
        g = GlobalSettings.load()
        g.public_quiz_hub_enabled = False
        g.save()
        cache.clear()
        for url in (SETS, RAILS, f"{SETS}{self.set.slug}/"):
            self.assertEqual(self.client.get(url).status_code, 503, url)


class NoAnswerLeakTest(PublicHubTestCase):
    """⚠ The tests this file exists for."""

    def test_the_detail_response_never_contains_is_correct(self):
        body = self.client.get(f"{SETS}{self.set.slug}/").content.decode()
        self.assertNotIn("is_correct", body)

    def test_the_detail_response_never_contains_the_explanation(self):
        body = self.client.get(f"{SETS}{self.set.slug}/").content.decode()
        self.assertNotIn("explanation", body)
        self.assertNotIn("Because.", body)

    def test_choices_are_served_with_only_id_and_text(self):
        r = self.client.get(f"{SETS}{self.set.slug}/")
        choice = r.data["questions"][0]["choices"][0]
        self.assertEqual(set(choice.keys()), {"id", "text"})


class OnlyPublishableIsServedTest(PublicHubTestCase):

    def test_a_suggested_question_is_never_served(self):
        make_question(self.subject, accepted=False, text="Still suggested?")
        r = self.client.get(f"{SETS}{self.set.slug}/")
        texts = [q["text"] for q in r.data["questions"]]
        self.assertNotIn("Still suggested?", texts)

    def test_an_accepted_question_with_no_explanation_is_never_served(self):
        make_question(self.subject, explanation="", text="No explanation?")
        r = self.client.get(f"{SETS}{self.set.slug}/")
        texts = [q["text"] for q in r.data["questions"]]
        self.assertNotIn("No explanation?", texts)

    def test_a_quiz_owned_question_is_never_served(self):
        """The bank is standalone rows; a teacher's quiz question must not
        leak onto the public site just because it shares a tag."""
        from courses.models import Course, Subject
        from quizzes.models import Quiz
        course = Course.objects.create(title="C")
        subject = Subject.objects.create(course=course, name="S")
        quiz = Quiz.objects.create(subject=subject, title="T")
        owned = Question.objects.create(
            quiz=quiz, text="Owned by a quiz?", explanation="x",
            bank_state=Question.BANK_STATE_ACCEPTED)
        Choice.objects.create(question=owned, text="a", is_correct=True)
        Choice.objects.create(question=owned, text="b", is_correct=False)
        owned.tags.set([self.subject])
        r = self.client.get(f"{SETS}{self.set.slug}/")
        self.assertNotIn("Owned by a quiz?",
                         [q["text"] for q in r.data["questions"]])


class DraftSetTest(PublicHubTestCase):

    def test_a_draft_set_is_not_listed(self):
        PracticeSet.objects.create(
            title="Unfinished", subject_tag=self.subject,
            status=PracticeSet.STATUS_DRAFT)
        r = self.client.get(SETS)
        self.assertEqual([s["title"] for s in r.data["results"]],
                         ["Ancient India — Quiz 01"])

    def test_a_draft_set_404s_rather_than_403s(self):
        """403 would confirm it exists."""
        draft = PracticeSet.objects.create(
            title="Unfinished", subject_tag=self.subject,
            status=PracticeSet.STATUS_DRAFT)
        self.assertEqual(
            self.client.get(f"{SETS}{draft.slug}/").status_code, 404)


class SelectionTest(PublicHubTestCase):

    def test_available_count_reports_what_it_can_actually_serve(self):
        """The card must not advertise 10 and serve 1."""
        r = self.client.get(SETS)
        self.assertEqual(r.data["results"][0]["question_count"], 1)

    def test_the_selection_is_stable_across_requests(self):
        for i in range(6):
            make_question(self.subject, text=f"Q{i}?")
        first = [q["id"] for q in
                 self.client.get(f"{SETS}{self.set.slug}/").data["questions"]]
        second = [q["id"] for q in
                  self.client.get(f"{SETS}{self.set.slug}/").data["questions"]]
        self.assertEqual(first, second)

    def test_question_count_caps_the_paper(self):
        for i in range(9):
            make_question(self.subject, text=f"Q{i}?")
        self.set.question_count = 4
        self.set.save()
        r = self.client.get(f"{SETS}{self.set.slug}/")
        self.assertEqual(len(r.data["questions"]), 4)

    def test_a_different_seed_starts_at_a_different_question(self):
        for i in range(9):
            make_question(self.subject, text=f"Q{i}?")
        self.set.question_count = 3
        self.set.save()
        other = PracticeSet.objects.create(
            title="Second", subject_tag=self.subject, question_count=3, seed=4,
            status=PracticeSet.STATUS_PUBLISHED)
        a = [q["id"] for q in
             self.client.get(f"{SETS}{self.set.slug}/").data["questions"]]
        b = [q["id"] for q in
             self.client.get(f"{SETS}{other.slug}/").data["questions"]]
        self.assertNotEqual(a, b)

    def test_the_exam_tag_narrows_by_AND_not_OR(self):
        """Chained tag filters mean "has both". `tags__in` would mean
        "either", which would pull in every question of the subject."""
        make_question(self.subject, text="Subject only?")   # no exam tag
        self.set.exam_tag = self.exam
        self.set.save()
        r = self.client.get(f"{SETS}{self.set.slug}/")
        self.assertNotIn("Subject only?", [q["text"] for q in r.data["questions"]])

    def test_a_question_is_not_duplicated_by_its_tags(self):
        """Two joins across the tags M2M repeat a row without distinct()."""
        self.set.exam_tag = self.exam
        self.set.save()
        r = self.client.get(f"{SETS}{self.set.slug}/")
        ids = [q["id"] for q in r.data["questions"]]
        self.assertEqual(len(ids), len(set(ids)))


class RailsTest(PublicHubTestCase):

    def test_a_hidden_tag_never_appears(self):
        QuestionTag.objects.create(
            kind=QuestionTag.KIND_SUBJECT, label="Secret",
            status=QuestionTag.STATUS_HIDDEN)
        r = self.client.get(RAILS)
        self.assertNotIn("Secret", [s["label"] for s in r.data["subjects"]])

    def test_a_soon_tag_DOES_appear_so_the_page_can_grey_it_out(self):
        QuestionTag.objects.create(
            kind=QuestionTag.KIND_SUBJECT, label="Reasoning",
            status=QuestionTag.STATUS_SOON)
        r = self.client.get(RAILS)
        row = next(s for s in r.data["subjects"] if s["label"] == "Reasoning")
        self.assertEqual(row["status"], "soon")

    def test_a_live_tag_with_no_questions_degrades_to_soon(self):
        """`live` is a floor, not an override — an admin must not be able to
        make a chip clickable onto an empty grid."""
        QuestionTag.objects.create(
            kind=QuestionTag.KIND_SUBJECT, label="Empty",
            status=QuestionTag.STATUS_LIVE)
        r = self.client.get(RAILS)
        row = next(s for s in r.data["subjects"] if s["label"] == "Empty")
        self.assertEqual(row["status"], "soon")
        self.assertEqual(row["question_count"], 0)

    def test_a_live_tag_with_questions_stays_live(self):
        r = self.client.get(RAILS)
        row = next(s for s in r.data["subjects"] if s["label"] == "History")
        self.assertEqual(row["status"], "live")
        self.assertEqual(row["question_count"], 1)

    def test_the_admins_raw_status_is_not_exposed(self):
        r = self.client.get(RAILS)
        self.assertNotIn("effective_status", r.data["subjects"][0])
        self.assertNotIn("status_downgraded", r.data["subjects"][0])

    def test_counts_are_not_multiplied_by_extra_tag_joins(self):
        """CLAUDE.md's annotate() trap: 2 reported as 6."""
        make_question(self.subject, exam=self.exam, text="Second?")
        r = self.client.get(RAILS)
        row = next(s for s in r.data["subjects"] if s["label"] == "History")
        self.assertEqual(row["question_count"], 2)

    def test_subjects_and_exams_are_returned_separately(self):
        r = self.client.get(RAILS)
        self.assertEqual([s["label"] for s in r.data["subjects"]], ["History"])
        self.assertEqual([e["label"] for e in r.data["exams"]], ["SSC CGL"])
