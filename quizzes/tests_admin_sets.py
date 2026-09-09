"""Admin CRUD for public practice sets (public Quiz Hub, Phase 5b)."""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from global_settings.models import GlobalSettings
from quizzes.models import Choice, PracticeSet, PublicAttempt, Question, QuestionTag

SETS = "/api/quizzes/admin/sets/"


def make_question(subject, text="Q?", difficulty="medium"):
    q = Question.objects.create(
        quiz=None, text=text, explanation="Because.", difficulty=difficulty,
        bank_state=Question.BANK_STATE_ACCEPTED)
    Choice.objects.create(question=q, text="a", is_correct=True)
    Choice.objects.create(question=q, text="b", is_correct=False)
    q.tags.set([subject])
    return q


class AdminSetTestCase(TestCase):
    def setUp(self):
        cache.clear()
        g = GlobalSettings.load()
        g.public_quiz_hub_enabled = True
        g.save()
        U = get_user_model()
        self.admin = U.objects.create_user(
            username="a", email="a@example.com", password="x", is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)
        self.subject = QuestionTag.objects.create(
            kind=QuestionTag.KIND_SUBJECT, label="History")
        self.exam = QuestionTag.objects.create(
            kind=QuestionTag.KIND_EXAM, label="SSC CGL")
        self.q = make_question(self.subject)

    def payload(self, **over):
        body = {"title": "History 01", "subject_tag": str(self.subject.id),
                "question_count": 10, "minutes": 10, "status": "draft"}
        body.update(over)
        return body


class CrudTest(AdminSetTestCase):

    def test_an_admin_can_create_a_set(self):
        r = self.client.post(SETS, self.payload(), format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["subject"], "History")
        self.assertEqual(r.data["slug"], "history-01")

    def test_available_count_reflects_the_bank_not_the_target(self):
        r = self.client.post(SETS, self.payload(question_count=10),
                             format="json")
        self.assertEqual(r.data["available_count"], 1)

    def test_an_admin_can_edit_and_publish(self):
        created = self.client.post(SETS, self.payload(), format="json").data
        r = self.client.patch(f"{SETS}{created['id']}/",
                              {"status": "published"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["status"], "published")

    def test_the_slug_is_not_re_derived_on_rename(self):
        """Renaming must not break links already shared."""
        created = self.client.post(SETS, self.payload(), format="json").data
        r = self.client.patch(f"{SETS}{created['id']}/",
                              {"title": "Totally different"}, format="json")
        self.assertEqual(r.data["slug"], "history-01")

    def test_a_set_with_no_attempts_can_be_deleted(self):
        created = self.client.post(SETS, self.payload(), format="json").data
        self.assertEqual(
            self.client.delete(f"{SETS}{created['id']}/").status_code, 204)


class PublishGuardTest(AdminSetTestCase):
    """An empty published set is a card that opens onto nothing."""

    def test_publishing_a_set_that_matches_nothing_is_refused(self):
        empty = QuestionTag.objects.create(
            kind=QuestionTag.KIND_SUBJECT, label="Reasoning")
        r = self.client.post(
            SETS, self.payload(subject_tag=str(empty.id), status="published"),
            format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("empty set", str(r.data))

    def test_the_same_set_saves_fine_as_a_draft(self):
        empty = QuestionTag.objects.create(
            kind=QuestionTag.KIND_SUBJECT, label="Reasoning")
        r = self.client.post(
            SETS, self.payload(subject_tag=str(empty.id)), format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_the_guard_is_evaluated_on_the_merged_result(self):
        """Changing difficulty alone on a published set must be checked
        against the combination that will actually be stored."""
        created = self.client.post(
            SETS, self.payload(status="published"), format="json").data
        r = self.client.patch(f"{SETS}{created['id']}/",
                              {"difficulty": "hard"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_a_matching_difficulty_still_publishes(self):
        created = self.client.post(
            SETS, self.payload(status="published"), format="json").data
        r = self.client.patch(f"{SETS}{created['id']}/",
                              {"difficulty": "medium"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)


class TagKindTest(AdminSetTestCase):

    def test_an_exam_tag_cannot_be_used_as_the_subject(self):
        r = self.client.post(
            SETS, self.payload(subject_tag=str(self.exam.id)), format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("not a subject", str(r.data))

    def test_a_subject_tag_cannot_be_used_as_the_exam(self):
        r = self.client.post(
            SETS, self.payload(exam_tag=str(self.subject.id)), format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("not an exam", str(r.data))


class DeleteGuardTest(AdminSetTestCase):

    def test_a_set_with_attempts_is_refused_with_409(self):
        created = self.client.post(SETS, self.payload(), format="json").data
        PublicAttempt.objects.create(
            practice_set=PracticeSet.objects.get(pk=created["id"]), total=1)
        r = self.client.delete(f"{SETS}{created['id']}/")
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.data["attempt_count"], 1)

    def test_the_set_survives_the_refusal(self):
        created = self.client.post(SETS, self.payload(), format="json").data
        PublicAttempt.objects.create(
            practice_set=PracticeSet.objects.get(pk=created["id"]), total=1)
        self.client.delete(f"{SETS}{created['id']}/")
        self.assertTrue(PracticeSet.objects.filter(pk=created["id"]).exists())


class PermissionTest(AdminSetTestCase):

    def test_a_non_admin_is_refused(self):
        U = get_user_model()
        plain = U.objects.create_user(
            username="p", email="p@example.com", password="x")
        c = APIClient()
        c.force_authenticate(user=plain)
        self.assertEqual(c.get(SETS).status_code, 403)

    def test_an_anonymous_caller_is_refused(self):
        self.assertEqual(APIClient().get(SETS).status_code, 401)

    def test_it_503s_when_the_hub_is_off(self):
        g = GlobalSettings.load()
        g.public_quiz_hub_enabled = False
        g.save()
        cache.clear()
        self.assertEqual(self.client.get(SETS).status_code, 503)
