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
    """design_handoff_public_quiz_hub — the flag, after Phase 9.

    ⚠ THIS CONTRACT INVERTED IN PHASE 9. Through Phases 0–8 the flag shipped
    OFF and this class asserted that, so a half-built hub could not reach a
    visitor mid-rebuild. The hub has now shipped, migration 0012 flips the
    default to True, and the flag's job changed from launch gate to KILL
    SWITCH.

    What still matters, and is still asserted below: only an admin can move
    it, and the key is present in `feature_flags` whatever its value.
    """

    URL = "/api/admin/settings/"

    def _client(self, user):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_ships_on_by_default_after_phase_9(self):
        self.assertTrue(GlobalSettings.load().public_quiz_hub_enabled)

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

    def test_non_admin_cannot_turn_it_off(self):
        """The direction that matters flipped with the default: the risk is no
        longer a stranger launching the hub early, it is a stranger taking a
        live public page down."""
        from accounts.models import User

        GlobalSettings.load()
        student = User.objects.create_user(
            username="student3", email="student3@example.com", password="x",
        )
        res = self._client(student).patch(
            self.URL, {"public_quiz_hub_enabled": False}, format="json",
        )
        self.assertEqual(res.status_code, 403, res.content)
        self.assertTrue(GlobalSettings.objects.get(pk=1).public_quiz_hub_enabled)

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
        self.assertTrue(flags["public_quiz_hub_enabled"])

    def test_the_phase_9_migration_flips_a_row_that_already_exists(self):
        """⚠ THE POINT OF THIS TEST. Changing a model default only affects
        rows CREATED afterwards, and GlobalSettings is a singleton whose row
        already exists on dev and prod holding False. Without the data step in
        migration 0012, "the default is now True" would be true of a fresh
        database and of nothing that is actually deployed — the hub would stay
        dark everywhere it matters and look like a broken flag.

        Running the migration end to end here is not possible on SQLite — the
        executor cannot unapply inside the test's transaction — so this calls
        the migration's own data function against a row that says False, which
        is exactly the situation on dev and prod.
        """
        import importlib

        from django.apps import apps as real_apps

        # importlib, because a module whose name starts with a digit cannot
        # be reached by an import statement.
        _mod = importlib.import_module(
            "global_settings.migrations.0012_public_quiz_hub_on_by_default")

        GlobalSettings.load()
        GlobalSettings.objects.update(public_quiz_hub_enabled=False)

        _mod.turn_on(real_apps, None)

        self.assertTrue(GlobalSettings.objects.get(pk=1).public_quiz_hub_enabled)


class LiveTickerFeatureFlagTest(TestCase):
    """design_handoff_live_ticker Phase 0 groundwork — the CMS promo ticker
    ships behind one admin-controlled flag, OFF, and nothing consumes it yet.

    A REAL gate, like content_studio_enabled: every one of the eight ticker
    slots checks it, and the rollout phase flips the default. Pinned here so a
    later phase cannot quietly ship a half-built ticker on by default.

    ⚠ This is the FIRST flag to be published both authenticated (feature_flags
    on /accounts/me/) and anonymous (/api/public-config/). It has to be: the
    Auth · login and Auth · signup slots render before anyone has logged in.
    Both halves are asserted below, because dropping either one is a silent
    failure on exactly one set of screens.
    """

    URL = "/api/admin/settings/"

    def _client(self, user):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_is_on_by_default_since_phase_9(self):
        """⚠ THIS CONTRACT INVERTED IN PHASE 9 (migration 0015). Through
        Phases 0–8 the flag shipped OFF as a launch gate; now all eight slots
        have shipped and it is a KILL SWITCH. Turning it off renders no
        ticker anywhere and leaves the navbar strip exactly as it was."""
        self.assertTrue(GlobalSettings.load().live_ticker_enabled)

    def test_admin_can_flip_it_via_patch(self):
        """Without the field in GlobalSettingsSerializer.Meta.fields the PATCH
        returns 200 and silently discards it — which reads as a dead switch in
        the admin UI, not as an error."""
        from accounts.models import User

        admin = User.objects.create_user(
            username="admin4", email="admin4@example.com", password="x", is_staff=True,
        )
        res = self._client(admin).patch(
            self.URL, {"live_ticker_enabled": True}, format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()["live_ticker_enabled"])
        self.assertTrue(GlobalSettings.objects.get(pk=1).live_ticker_enabled)

    def test_non_admin_cannot_turn_it_off(self):
        """The direction that matters flipped with the default: the risk is no
        longer a stranger launching the ticker early, it is a stranger taking
        live surfaces down."""
        from accounts.models import User

        GlobalSettings.load()
        student = User.objects.create_user(
            username="student4", email="student4@example.com", password="x",
        )
        res = self._client(student).patch(
            self.URL, {"live_ticker_enabled": False}, format="json",
        )
        self.assertEqual(res.status_code, 403, res.content)
        self.assertTrue(GlobalSettings.objects.get(pk=1).live_ticker_enabled)

    def test_exposed_read_only_in_feature_flags_on_me(self):
        """The key must be present whatever its value — an absent key and a
        set one are different bugs, and each app's AuthContext defaults
        differently when it is missing."""
        from accounts.models import User

        user = User.objects.create_user(
            username="learner4", email="learner4@example.com", password="x",
        )
        res = self._client(user).get("/api/accounts/me/")
        self.assertEqual(res.status_code, 200, res.content)
        flags = res.json()["feature_flags"]
        self.assertIn("live_ticker_enabled", flags)
        self.assertTrue(flags["live_ticker_enabled"])

    def test_the_phase_9_migration_flips_a_row_that_already_exists(self):
        """⚠ THE POINT. Changing a model default only affects rows CREATED
        afterwards, and GlobalSettings is a singleton whose row already exists
        on dev and prod holding False. Without the data step in 0015, "the
        default is now True" would be true of a fresh database and of nothing
        that is actually deployed."""
        import importlib

        from django.apps import apps as real_apps

        mod = importlib.import_module(
            "global_settings.migrations.0015_live_ticker_on_by_default")
        GlobalSettings.load()
        GlobalSettings.objects.update(live_ticker_enabled=False)
        mod.turn_on(real_apps, None)
        self.assertTrue(GlobalSettings.objects.get(pk=1).live_ticker_enabled)

    def test_an_anonymous_visitor_can_read_it(self):
        """⚠ THE POINT OF THIS TEST. feature_flags requires a login, and two of
        the eight slots are the login and signup screens themselves. If this
        flag were authenticated-only, those two slots would be ungated for
        every visitor who can actually see them — the gate would appear to work
        everywhere it was tested and fail exactly where it wasn't."""
        from rest_framework.test import APIClient

        # load() first — the singleton row does not exist until something
        # creates it, and .filter(pk=1).update() on no rows is a silent no-op.
        GlobalSettings.load()
        GlobalSettings.objects.filter(pk=1).update(live_ticker_enabled=True)
        body = APIClient().get("/api/public-config/").json()
        self.assertTrue(body["live_ticker_enabled"])
        GlobalSettings.objects.filter(pk=1).update(live_ticker_enabled=False)
        self.assertFalse(
            APIClient().get("/api/public-config/").json()["live_ticker_enabled"])


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
        # An EXACT set, deliberately: this is the tripwire that makes adding a
        # public flag a conscious act. Widening it to assertIn/issubset would
        # let the next field onto the anonymous endpoint silently.
        # admission_abroad_enabled was added 2026-11-12 (public because
        # /admission/abroad is a logged-out page — see PublicConfigView).
        self.assertEqual(
            set(body),
            {"public_quiz_hub_enabled", "live_ticker_enabled",
             "admission_abroad_enabled"},
        )
        blob = str(body)
        for secret in ("rzp_live", "okaxis", "ShikshaCom", "ops@shikshacom.com"):
            self.assertNotIn(secret, blob)

    def test_it_is_read_only(self):
        """No PATCH/POST handler — the admin endpoint is the only writer."""
        self.assertEqual(
            self._client().patch(self.URL, {"public_quiz_hub_enabled": True},
                                 format="json").status_code, 405)


class AdmissionAbroadFlagTest(TestCase):
    """`admission_abroad_enabled` — the public gate on /admission/abroad.

    Third flag on the anonymous allowlist, and for the same reason as the
    other two: the directory is a logged-out marketing page, so a flag
    delivered on /accounts/me/ would be missing for exactly the people who
    see it. The gate would test green everywhere and fail where it mattered.
    """

    URL = "/api/public-config/"

    def _client(self):
        from rest_framework.test import APIClient
        return APIClient()

    def test_it_ships_off(self):
        """⚠ Ships OFF deliberately. The screen's data is cited and dated, but
        the study-abroad content around it is not finished — the country
        guides still need a human pass and the source names live immigration
        instruments with no date stamp. Defaulting ON would publish that."""
        self.assertFalse(GlobalSettings.load().admission_abroad_enabled)

    def test_an_anonymous_visitor_can_read_it(self):
        body = self._client().get(self.URL).json()
        self.assertIn("admission_abroad_enabled", body)
        self.assertFalse(body["admission_abroad_enabled"])

    def test_it_reflects_the_current_value(self):
        # load() first: the singleton row does not exist until something
        # creates it, and .filter(pk=1).update() on no rows is a silent no-op
        # that would make this test pass while asserting nothing.
        GlobalSettings.load()
        GlobalSettings.objects.filter(pk=1).update(admission_abroad_enabled=True)
        self.assertTrue(self._client().get(self.URL).json()["admission_abroad_enabled"])
        GlobalSettings.objects.filter(pk=1).update(admission_abroad_enabled=False)
        self.assertFalse(self._client().get(self.URL).json()["admission_abroad_enabled"])

    def test_an_admin_can_flip_it(self):
        """The whole point of the flag: turned on from the admin, no deploy."""
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        admin = get_user_model().objects.create_user(
            username="flagadmin", email="flagadmin@example.com",
            password="pw12345!", is_staff=True,
        )
        c = APIClient()
        c.force_authenticate(admin)
        res = c.patch("/api/admin/settings/",
                      {"admission_abroad_enabled": True}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(GlobalSettings.load().admission_abroad_enabled)
        # and it must reach the anonymous channel, not just the admin one
        self.assertTrue(
            APIClient().get(self.URL).json()["admission_abroad_enabled"])

    def test_the_allowlist_still_withholds_secrets(self):
        """Adding a third name must not widen the response to the whole
        settings serializer, which holds payment credentials."""
        body = self._client().get(self.URL).json()
        for leaked in ("razorpay_key_id", "razorpay_key_secret",
                       "razorpay_secret_set"):
            self.assertNotIn(leaked, body)
