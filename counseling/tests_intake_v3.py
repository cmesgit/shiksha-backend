"""Tests for the workflow-v3 Step 0 intake fields (audience, level,
location_preference, help_needed, level_answers).

These are the FIRST tests in the counseling app — there was no baseline to
extend. They cover the two things most likely to break something live:

  1. backward compatibility. Every intake on prod predates these columns, so
     a blank `level` must still produce a usable stage via `effective_level`,
     and a GET must not 500 on rows whose JSON fields defaulted.
  2. the JSONField holes. A JSONField accepts anything, so without explicit
     validation a client bug stores a string where the report expects a list
     and the failure surfaces later, on read, somewhere else.
"""

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.models import LearnerProfile
from counseling.models import CounselingIntake, Specialization

User = get_user_model()

INTAKE_URL = "/api/counseling/intake/"


class IntakeV3FieldsTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="v3intake@example.com", username="v3intake", password="x" * 14,
        )
        cls.learner = LearnerProfile.objects.create(
            account=cls.user, display_name="V3 Tester", is_default=True,
            current_class="10", stream="science", board="cbse",
        )
        cls.spec = Specialization.objects.filter(name="Law").first() \
            or Specialization.objects.create(name="Law")

    def setUp(self):
        self.client.force_authenticate(self.user)

    # ── backward compatibility ──────────────────────────────────────────

    def test_pre_v3_intake_loads_and_derives_level_from_class(self):
        """A row created before these columns existed has blank `level` and
        empty JSON defaults. It must still GET cleanly and report a stage."""
        intake = CounselingIntake.objects.create(learner_profile=self.learner)
        self.assertEqual(intake.level, "")

        res = self.client.get(INTAKE_URL)
        self.assertEqual(res.status_code, 200)
        # current_class "10" → the 9-10 step set, without `level` being set.
        self.assertEqual(res.data["effective_level"], "9-10")
        self.assertEqual(res.data["location_preference"], [])
        self.assertEqual(res.data["help_needed"], [])
        self.assertEqual(res.data["level_answers"], {})

    def test_explicit_level_wins_over_the_derived_one(self):
        """A Class 10 learner being assessed for a diploma must not have the
        class override what the wizard actually asked."""
        CounselingIntake.objects.create(learner_profile=self.learner, level="diploma")
        res = self.client.get(INTAKE_URL)
        self.assertEqual(res.data["effective_level"], "diploma")

    def test_effective_level_is_blank_when_class_maps_to_nothing(self):
        self.learner.current_class = ""
        self.learner.save(update_fields=["current_class"])
        CounselingIntake.objects.create(learner_profile=self.learner)
        res = self.client.get(INTAKE_URL)
        self.assertEqual(res.data["effective_level"], "")

    # ── the new fields reach the API ────────────────────────────────────

    def test_step0_fields_round_trip(self):
        """The hand-rolled-dict trap: a field on the model that never reaches
        the serializer looks saved and reads back missing."""
        res = self.client.put(INTAKE_URL, {
            "audience": "child",
            "level": "11-12",
            "location_preference": ["India", "unsure"],
            "help_needed": ["Choose a career path", "Prepare for entrance exams"],
            "level_answers": {"stream": "PCM", "exams": ["JEE Main"]},
            "career_interest_ids": [self.spec.id],
        }, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["audience"], "child")
        self.assertEqual(res.data["level"], "11-12")
        self.assertEqual(res.data["location_preference"], ["india", "unsure"])
        self.assertEqual(res.data["help_needed"],
                         ["Choose a career path", "Prepare for entrance exams"])
        self.assertEqual(res.data["level_answers"]["stream"], "PCM")

        fresh = self.client.get(INTAKE_URL)
        self.assertEqual(fresh.data["level_answers"]["exams"], ["JEE Main"])
        self.assertEqual(fresh.data["effective_level"], "11-12")

    def test_location_preference_is_normalised_and_deduped(self):
        res = self.client.put(INTAKE_URL, {
            "location_preference": ["India", "INDIA", " abroad "],
        }, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["location_preference"], ["india", "abroad"])

    # ── the JSONField holes ─────────────────────────────────────────────

    def test_unknown_location_is_rejected(self):
        res = self.client.put(INTAKE_URL,
                              {"location_preference": ["india", "mars"]},
                              format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("mars", str(res.data["location_preference"]))

    def test_location_preference_must_be_a_list(self):
        res = self.client.put(INTAKE_URL,
                              {"location_preference": "india"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_level_answers_must_be_an_object(self):
        res = self.client.put(INTAKE_URL,
                              {"level_answers": ["not", "a", "dict"]},
                              format="json")
        self.assertEqual(res.status_code, 400)

    def test_invalid_level_choice_is_rejected(self):
        res = self.client.put(INTAKE_URL, {"level": "phd"}, format="json")
        self.assertEqual(res.status_code, 400)

    # ── the abroad gate ─────────────────────────────────────────────────

    def test_abroad_only_is_flagged(self):
        res = self.client.put(INTAKE_URL,
                              {"location_preference": ["abroad"]}, format="json")
        self.assertTrue(res.data["is_abroad_only"])

    def test_abroad_plus_india_is_not_abroad_only(self):
        """The wizard carries on in this case and builds an India report —
        the gate must not swallow a student who picked both."""
        res = self.client.put(INTAKE_URL,
                              {"location_preference": ["abroad", "india"]},
                              format="json")
        self.assertFalse(res.data["is_abroad_only"])

    def test_empty_location_is_not_abroad_only(self):
        res = self.client.put(INTAKE_URL, {"location_preference": []}, format="json")
        self.assertFalse(res.data["is_abroad_only"])
