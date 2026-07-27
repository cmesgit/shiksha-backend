"""
End-to-end tests for the Settings-surface endpoints added in accounts/settings_views.py
plus the session tracking / revocation they depend on.

Run with:
    DJANGO_SETTINGS_MODULE=config.settings_test .venv/bin/python manage.py test \
        accounts.tests_settings_surface -v2
"""
import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from accounts.models import (
    AccountDeletionRequest,
    LearnerProfile,
    LearningGoal,
    Role,
    UserRole,
    User,
    UserSession,
)

CHROME_WIN = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120 Safari/537.36")
SAFARI_IOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1")

PASSWORD = "correct-horse-battery-9"


class SettingsSurfaceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="aarav", email="aarav@example.com", password=PASSWORD,
        )
        self.user.is_verified = True
        self.user.save(update_fields=["is_verified"])
        # The account holder's own profile, then a child — the shape every real
        # multi-profile account has. Created explicitly because
        # _ensure_default_profile() only auto-creates a SELF profile when the
        # account has none, so seeding the child first would leave no SELF row.
        self.primary = LearnerProfile.objects.create(
            account=self.user, display_name="Aarav",
            relationship="SELF", is_default=True,
        )
        self.child = LearnerProfile.objects.create(
            account=self.user, display_name="Diya", relationship="DEPENDENT",
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def login(self, ua=CHROME_WIN):
        res = self.client.post(
            "/api/accounts/login/",
            {"email": "aarav@example.com", "password": PASSWORD},
            content_type="application/json",
            HTTP_USER_AGENT=ua,
        )
        self.assertEqual(res.status_code, 200, res.content)
        return res

    # ── sessions & devices ───────────────────────────────────────────────

    def test_login_opens_one_session_and_switching_profile_does_not_fork_it(self):
        self.login()
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 1)
        session = UserSession.objects.get(user=self.user)
        self.assertEqual(session.browser_label, "Chrome")
        self.assertEqual(session.platform_label, "Windows")
        self.assertEqual(session.device_kind, "desktop")

        # Selecting a profile re-mints the token. The whole point of the `sid`
        # claim is that this stays ONE device in the sessions list.
        res = self.client.post(
            "/api/accounts/profiles/select/",
            {"profile_id": str(self.primary.id)}, content_type="application/json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 1)

    def test_session_list_flags_the_current_device(self):
        self.login()
        res = self.client.get("/api/accounts/sessions/")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(len(body["sessions"]), 1)
        self.assertTrue(body["sessions"][0]["is_current"])
        self.assertEqual(body["sessions"][0]["device"], "Chrome · Windows")

    def test_revoking_a_session_ends_its_access_immediately(self):
        """The whole promise of the Revoke button: the other device is out NOW,
        not whenever its hour-long access token happens to lapse."""
        # Device A (phone) signs in, then device B (desktop).
        phone = self.client
        self.login(ua=SAFARI_IOS)
        phone_cookies = phone.cookies.copy()

        desktop = self.client_class()
        res = desktop.post(
            "/api/accounts/login/",
            {"email": "aarav@example.com", "password": PASSWORD},
            content_type="application/json", HTTP_USER_AGENT=CHROME_WIN,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 2)

        # Desktop revokes the phone.
        phone_session = UserSession.objects.get(user=self.user, device_kind="mobile")
        res = desktop.post(f"/api/accounts/sessions/{phone_session.id}/revoke/")
        self.assertEqual(res.status_code, 200, res.content)
        phone_session.refresh_from_db()
        self.assertIsNotNone(phone_session.revoked_at)

        # The phone's still-unexpired access cookie must no longer authenticate.
        phone_client = self.client_class()
        phone_client.cookies = phone_cookies
        res = phone_client.get("/api/accounts/sessions/")
        self.assertEqual(res.status_code, 401, "revoked access token still worked")

        # …and it must not be able to refresh its way back in either.
        res = phone_client.post("/api/accounts/refresh/")
        self.assertEqual(res.status_code, 401, "revoked session renewed itself")

    def test_cannot_revoke_the_current_device(self):
        self.login()
        session = UserSession.objects.get(user=self.user)
        res = self.client.post(f"/api/accounts/sessions/{session.id}/revoke/")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json().get("code"), "cannot_revoke_current")

    def test_revoke_others_keeps_the_caller_signed_in(self):
        self.login(ua=SAFARI_IOS)
        desktop = self.client_class()
        desktop.post(
            "/api/accounts/login/",
            {"email": "aarav@example.com", "password": PASSWORD},
            content_type="application/json", HTTP_USER_AGENT=CHROME_WIN,
        )
        res = desktop.post("/api/accounts/sessions/revoke-others/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["revoked"], 1)
        # Caller still works.
        self.assertEqual(desktop.get("/api/accounts/sessions/").status_code, 200)
        self.assertEqual(
            UserSession.objects.filter(user=self.user, revoked_at__isnull=True).count(), 1)

    def test_logout_blacklists_the_refresh_token(self):
        """Regression: LogoutView used to only clear cookies, leaving the
        refresh token usable for its full 7-day life."""
        self.login()
        cookies = self.client.cookies.copy()
        self.assertEqual(self.client.post("/api/accounts/logout/").status_code, 200)

        replay = self.client_class()
        replay.cookies = cookies
        res = replay.post("/api/accounts/refresh/")
        self.assertEqual(res.status_code, 401, "refresh token survived logout")

    # ── learning goals ───────────────────────────────────────────────────

    def test_learning_goal_defaults_then_updates_per_profile(self):
        self.login()
        res = self.client.get("/api/accounts/learning-goals/")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["daily_minutes"], 30)
        self.assertEqual(body["active_days"], [0, 1, 2, 3, 4])
        self.assertEqual(body["streak_days"], 0)  # no quizzes/assignments yet

        res = self.client.patch(
            "/api/accounts/learning-goals/",
            {"daily_minutes": 45, "active_days": [0, 2, 4, 6],
             "reminder_time": "19:30", "reminders_enabled": True},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["daily_minutes"], 45)
        self.assertEqual(res.json()["reminder_time"], "19:30")

        # Scoped to a profile: the child keeps its own goal.
        res = self.client.get(
            f"/api/accounts/learning-goals/?profile_id={self.child.id}")
        self.assertEqual(res.json()["daily_minutes"], 30)
        self.assertEqual(LearningGoal.objects.count(), 2)

    def test_learning_goal_rejects_out_of_range_values(self):
        self.login()
        for payload in ({"daily_minutes": 5}, {"daily_minutes": 500},
                        {"active_days": [9]}, {"reminder_time": "nope"}):
            res = self.client.patch(
                "/api/accounts/learning-goals/", payload,
                content_type="application/json")
            self.assertEqual(res.status_code, 400, f"{payload} was accepted")

    def test_another_accounts_profile_is_not_reachable(self):
        other = User.objects.create_user(
            username="mallory", email="mallory@example.com", password=PASSWORD)
        stranger = LearnerProfile.objects.create(
            account=other, display_name="Not yours")
        self.login()
        res = self.client.get(
            f"/api/accounts/learning-goals/?profile_id={stranger.id}")
        self.assertEqual(res.status_code, 403)

    # ── billing ──────────────────────────────────────────────────────────

    def test_billing_reports_the_real_mode_and_empty_history(self):
        self.login()
        res = self.client.get("/api/accounts/billing/")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        # Defaults: free_trial_enabled=True → effective_mode is "free".
        self.assertEqual(body["mode"], "free")
        self.assertTrue(body["is_free_phase"])
        self.assertEqual(body["access"], [])
        self.assertEqual(body["payments"], [])

    # ── privacy & data ───────────────────────────────────────────────────

    def test_data_export_returns_this_account_only(self):
        self.login()
        res = self.client.post("/api/accounts/data-export/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn("attachment;", res["Content-Disposition"])
        payload = json.loads(res.content)
        self.assertEqual(payload["account"]["email"], "aarav@example.com")
        names = {p["display_name"] for p in payload["profiles"]}
        self.assertIn("Diya", names)

    def test_delete_account_requires_the_password(self):
        self.login()
        res = self.client.post("/api/accounts/delete-account/", {},
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)
        res = self.client.post("/api/accounts/delete-account/",
                               {"password": "wrong"}, content_type="application/json")
        self.assertEqual(res.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_delete_account_deactivates_and_revokes_everything(self):
        self.login()
        res = self.client.post("/api/accounts/delete-account/",
                               {"password": PASSWORD, "reason": "done studying"},
                               content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertFalse(self.user.learner_profiles.filter(is_active=True).exists())

        row = AccountDeletionRequest.objects.get(user=self.user)
        self.assertEqual(row.status, AccountDeletionRequest.STATUS_PENDING)
        self.assertEqual(row.email, "aarav@example.com")
        self.assertGreater(row.purge_after, timezone.now() + timedelta(days=29))

        # Every session closed, including the caller's own.
        self.assertFalse(
            UserSession.objects.filter(user=self.user, revoked_at__isnull=True).exists())

    def test_delete_account_blocked_by_live_paid_access(self):
        from courses.models import Course
        from enrollments.models import Subscription

        self.login()
        course = Course.objects.create(title="Physics XI")
        Subscription.objects.create(
            user=self.user, course=course, status="ACTIVE",
            starts_at=timezone.now() - timedelta(days=1),
            expires_at=timezone.now() + timedelta(days=30),
        )
        res = self.client.post("/api/accounts/delete-account/",
                               {"password": PASSWORD}, content_type="application/json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json().get("code"), "active_subscription")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    # ── choices + profile bio ────────────────────────────────────────────

    def test_choices_endpoint_mirrors_the_model(self):
        self.login()
        res = self.client.get("/api/accounts/choices/")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(
            [c["value"] for c in body["board"]],
            [v for v, _ in LearnerProfile.BOARD_CHOICES],
        )

    def test_profile_bio_round_trips(self):
        """Bio used to live in localStorage, so it vanished on any other
        browser. It must now come back from the server."""
        self.login()
        res = self.client.patch(
            f"/api/accounts/profiles/{self.child.id}/",
            {"bio": "Class 8 · loves chemistry"}, content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)

        res = self.client.get(f"/api/accounts/profiles/{self.child.id}/")
        self.assertEqual(res.json()["bio"], "Class 8 · loves chemistry")

    def test_profile_bio_length_is_enforced(self):
        self.login()
        res = self.client.patch(
            f"/api/accounts/profiles/{self.child.id}/",
            {"bio": "x" * 400}, content_type="application/json")
        self.assertEqual(res.status_code, 400)

    # ── notification preferences (existing endpoint + new language) ───────

    def test_notification_preferences_expose_and_persist_language(self):
        self.login()
        res = self.client.get("/api/notifications/preferences/")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["language"], "en")
        self.assertTrue(any(l["value"] == "hi" for l in body["languages"]))

        res = self.client.put("/api/notifications/preferences/",
                              {"language": "hi", "sms_enabled": False},
                              content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["language"], "hi")
        self.assertFalse(res.json()["sms_enabled"])

        res = self.client.put("/api/notifications/preferences/",
                              {"language": "klingon"},
                              content_type="application/json")
        self.assertEqual(res.status_code, 400)
