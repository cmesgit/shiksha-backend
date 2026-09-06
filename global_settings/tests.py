"""
GlobalSettings.razorpay_key_secret is encrypted at rest (EncryptedCharField) —
previously plaintext CharField. Verify: DB column holds ciphertext, ORM
round-trips the plaintext transparently, legacy plaintext rows degrade
gracefully instead of crashing, and the re-encrypt command is idempotent.
"""
from django.db import connection
from django.test import TestCase

from global_settings.models import GlobalSettings
from global_settings.fields import _fernet


class RazorpaySecretEncryptionTest(TestCase):
    SECRET = "rzp_test_super_secret_key_123"

    def _raw_db_value(self):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT razorpay_key_secret FROM global_settings_globalsettings WHERE singleton_id = 1"
            )
            row = cur.fetchone()
            return row[0] if row else None

    def test_stored_value_is_ciphertext_not_plaintext(self):
        gs = GlobalSettings.load()
        gs.razorpay_key_secret = self.SECRET
        gs.save()
        raw = self._raw_db_value()
        self.assertIsNotNone(raw)
        self.assertNotEqual(raw, self.SECRET)
        self.assertNotIn(self.SECRET, raw)

    def test_orm_roundtrip_returns_plaintext(self):
        gs = GlobalSettings.load()
        gs.razorpay_key_secret = self.SECRET
        gs.save()
        reloaded = GlobalSettings.objects.get(pk=1)
        self.assertEqual(reloaded.razorpay_key_secret, self.SECRET)

    def test_blank_secret_stays_blank_and_unset_flag_false(self):
        gs = GlobalSettings.load()
        gs.razorpay_key_secret = ""
        gs.save()
        self.assertEqual(self._raw_db_value(), "")
        reloaded = GlobalSettings.objects.get(pk=1)
        self.assertEqual(reloaded.razorpay_key_secret, "")
        self.assertFalse(bool(reloaded.razorpay_key_secret))

    def test_legacy_plaintext_row_reads_back_without_crashing(self):
        # Simulate a row written before EncryptedCharField existed: raw
        # plaintext in the column, bypassing the field's own encryption.
        GlobalSettings.load()
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE global_settings_globalsettings SET razorpay_key_secret = %s WHERE singleton_id = 1",
                [self.SECRET],
            )
        reloaded = GlobalSettings.objects.get(pk=1)
        self.assertEqual(reloaded.razorpay_key_secret, self.SECRET)

    def test_encrypt_legacy_secrets_command_reencrypts(self):
        from django.core.management import call_command

        GlobalSettings.load()
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE global_settings_globalsettings SET razorpay_key_secret = %s WHERE singleton_id = 1",
                [self.SECRET],
            )
        self.assertEqual(self._raw_db_value(), self.SECRET)  # still plaintext
        call_command("encrypt_legacy_secrets")
        raw_after = self._raw_db_value()
        self.assertNotEqual(raw_after, self.SECRET)
        # And it still decrypts back to the same plaintext via the ORM.
        self.assertEqual(GlobalSettings.objects.get(pk=1).razorpay_key_secret, self.SECRET)

    def test_fernet_key_is_stable_across_calls(self):
        # Two independent encrypt/decrypt round trips must use the same
        # derived key, or restarts would make old secrets unreadable.
        f1 = _fernet()
        token = f1.encrypt(b"hello")
        f2 = _fernet()
        self.assertEqual(f2.decrypt(token), b"hello")


class QuizV2FeatureFlagsTest(TestCase):
    """design_handoff_quiz_system Phase 0 groundwork — both flags default OFF
    and are readable/writable only via the admin settings endpoint. Nothing
    in this phase consumes them yet; this pins the contract Phase 1+ builds on.
    """

    URL = "/api/admin/settings/"

    def test_shipped_defaults_on_a_fresh_row(self):
        # quiz_v2_enabled ON (Phase 10, migration 0008 — a record that v2 is
        # live, not a gate); AI drafting still OFF and admin-controlled.
        gs = GlobalSettings.load()
        self.assertTrue(gs.quiz_v2_enabled)
        self.assertFalse(gs.ai_question_drafting_enabled)

    def _client(self, user):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_admin_can_flip_both_flags_via_patch(self):
        from accounts.models import User

        admin = User.objects.create_user(
            username="admin", email="admin@example.com", password="x", is_staff=True,
        )
        res = self._client(admin).patch(
            self.URL,
            {"quiz_v2_enabled": True, "ai_question_drafting_enabled": True},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()["quiz_v2_enabled"])
        self.assertTrue(res.json()["ai_question_drafting_enabled"])
        reloaded = GlobalSettings.objects.get(pk=1)
        self.assertTrue(reloaded.quiz_v2_enabled)
        self.assertTrue(reloaded.ai_question_drafting_enabled)

    def test_non_admin_gets_403(self):
        from accounts.models import User

        GlobalSettings.load()  # ensure the singleton row exists to check afterwards
        non_admin = User.objects.create_user(
            username="student", email="student@example.com", password="x",
        )
        res = self._client(non_admin).patch(
            self.URL,
            {"quiz_v2_enabled": True, "ai_question_drafting_enabled": True},
            format="json",
        )
        self.assertEqual(res.status_code, 403, res.content)
        # Unchanged from the shipped defaults — the point is that a non-admin
        # PATCH wrote nothing, not that the values are false.
        reloaded = GlobalSettings.objects.get(pk=1)
        self.assertTrue(reloaded.quiz_v2_enabled)
        self.assertFalse(reloaded.ai_question_drafting_enabled)


class ContentStudioFeatureFlagTest(TestCase):
    """design_handoff_content_studio Phase 0 groundwork — the restructured CMS
    ships behind one admin-controlled flag, OFF, and nothing consumes it yet.

    Unlike quiz_v2_enabled (which ended up recording a shipped state rather
    than gating anything), this one is a REAL gate: Phases 2-8 each check it,
    and Phase 9 flips the default. Pinning that here so a later phase can't
    quietly ship the Studio on by default mid-rebuild.
    """

    URL = "/api/admin/settings/"

    def _client(self, user):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_is_on_by_default_since_phase_9(self):
        """It shipped OFF through the rebuild; Phase 9 turned it on once the
        Studio covered everything the retired screens could do."""
        self.assertTrue(GlobalSettings.load().content_studio_enabled)

    def test_admin_can_flip_it_via_patch(self):
        from accounts.models import User

        admin = User.objects.create_user(
            username="admin2", email="admin2@example.com", password="x", is_staff=True,
        )
        res = self._client(admin).patch(
            self.URL, {"content_studio_enabled": True}, format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()["content_studio_enabled"])
        self.assertTrue(GlobalSettings.objects.get(pk=1).content_studio_enabled)

    def test_non_admin_cannot_turn_it_on(self):
        from accounts.models import User

        GlobalSettings.load()
        student = User.objects.create_user(
            username="student2", email="student2@example.com", password="x",
        )
        GlobalSettings.objects.filter(pk=1).update(content_studio_enabled=False)
        res = self._client(student).patch(
            self.URL, {"content_studio_enabled": True}, format="json",
        )
        self.assertEqual(res.status_code, 403, res.content)
        self.assertFalse(GlobalSettings.objects.get(pk=1).content_studio_enabled)


class PublicQuizHubFeatureFlagTest(TestCase):
    """design_handoff_public_quiz_hub Phase 0 groundwork.

    The public /quiz rebuild and the admin question-bank authoring behind it
    ship behind one admin-controlled flag, OFF, and nothing consumes it yet.

    Like content_studio_enabled and unlike quiz_v2_enabled, this is a REAL
    gate. Phase 9 flips the default; until then a half-built hub must never
    reach a visitor. The default assertion below is what stops a later phase
    from quietly turning it on mid-rebuild.
    """

    URL = "/api/admin/settings/"

    def _client(self, user):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_ships_off_by_default(self):
        self.assertFalse(GlobalSettings.load().public_quiz_hub_enabled)

    def test_admin_can_flip_it_via_patch(self):
        from accounts.models import User

        admin = User.objects.create_user(
            username="admin3", email="admin3@example.com", password="x", is_staff=True,
        )
        res = self._client(admin).patch(
            self.URL, {"public_quiz_hub_enabled": True}, format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()["public_quiz_hub_enabled"])
        self.assertTrue(GlobalSettings.objects.get(pk=1).public_quiz_hub_enabled)

    def test_non_admin_cannot_turn_it_on(self):
        from accounts.models import User

        GlobalSettings.load()
        student = User.objects.create_user(
            username="student3", email="student3@example.com", password="x",
        )
        res = self._client(student).patch(
            self.URL, {"public_quiz_hub_enabled": True}, format="json",
        )
        self.assertEqual(res.status_code, 403, res.content)
        self.assertFalse(GlobalSettings.objects.get(pk=1).public_quiz_hub_enabled)

    def test_exposed_read_only_in_feature_flags_on_me(self):
        """Every app reads flags off /accounts/me/, so the key must be present
        even while False — an absent key and a False one are different bugs to
        debug, and each AuthContext defaults differently when it is missing."""
        from accounts.models import User

        user = User.objects.create_user(
            username="learner3", email="learner3@example.com", password="x",
        )
        res = self._client(user).get("/api/accounts/me/")
        self.assertEqual(res.status_code, 200, res.content)
        flags = res.json()["feature_flags"]
        self.assertIn("public_quiz_hub_enabled", flags)
        self.assertFalse(flags["public_quiz_hub_enabled"])


class PublicConfigViewTest(TestCase):
    """The anonymous flag allowlist behind /api/public-config/.

    The marketing site is browsable by guests, so the Quiz Hub's switch has to
    be readable without a login. The risk that creates is over-exposure, which
    is what most of these tests are about.
    """

    URL = "/api/public-config/"

    def _client(self):
        from rest_framework.test import APIClient
        return APIClient()

    def test_an_anonymous_visitor_can_read_it(self):
        res = self._client().get(self.URL)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn("public_quiz_hub_enabled", res.json())

    def test_it_reflects_the_current_value(self):
        GlobalSettings.load()
        GlobalSettings.objects.filter(pk=1).update(public_quiz_hub_enabled=True)
        self.assertTrue(self._client().get(self.URL).json()["public_quiz_hub_enabled"])
        GlobalSettings.objects.filter(pk=1).update(public_quiz_hub_enabled=False)
        self.assertFalse(self._client().get(self.URL).json()["public_quiz_hub_enabled"])

    def test_it_leaks_nothing_beyond_the_allowlist(self):
        """THE reason this view hand-builds its dict instead of using
        GlobalSettingsSerializer. That model carries the Razorpay key id, the
        platform UPI payee name and VPA, the contact email and every live
        session limit. A serializer dump here publishes all of it to anyone
        with curl, and it would look completely innocuous in review."""
        GlobalSettings.objects.filter(pk=1).update(
            razorpay_key_id="rzp_live_SHOULD_NOT_LEAK",
            upi_id="shiksha@okaxis",
            upi_payee_name="ShikshaCom",
            platform_email="ops@shikshacom.com",
        )
        body = self._client().get(self.URL).json()
        self.assertEqual(set(body), {"public_quiz_hub_enabled"})
        blob = str(body)
        for secret in ("rzp_live", "okaxis", "ShikshaCom", "ops@shikshacom.com"):
            self.assertNotIn(secret, blob)

    def test_it_is_read_only(self):
        """No PATCH/POST handler — the admin endpoint is the only writer."""
        self.assertEqual(
            self._client().patch(self.URL, {"public_quiz_hub_enabled": True},
                                 format="json").status_code, 405)
