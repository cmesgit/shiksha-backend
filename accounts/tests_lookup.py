"""
#3 — ProfileEmailLookupView must require auth and only ever reflect the caller's
own account (previously AllowAny + arbitrary email → children's-name leak).
"""
from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import User, LearnerProfile


class ProfileLookupAuthTest(TestCase):
    URL = "/api/accounts/profiles/lookup/"

    @classmethod
    def setUpTestData(cls):
        cls.a = User.objects.create_user(username="a", email="a@test.com", password="x")
        LearnerProfile.objects.create(account=cls.a, display_name="Aria", is_default=True)
        # A different account whose child names must NOT leak.
        cls.b = User.objects.create_user(username="b", email="b@test.com", password="x")
        LearnerProfile.objects.create(account=cls.b, display_name="SecretChild", is_default=True)

    def test_unauthenticated_rejected(self):
        res = APIClient().post(self.URL, {"email": "b@test.com"}, format="json")
        self.assertIn(res.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_returns_only_own_profiles_ignoring_email(self):
        c = APIClient(); c.force_authenticate(user=self.a)
        # Ask for account B's email — must still only get A's own profiles.
        res = c.post(self.URL, {"email": "b@test.com"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        names = [p["display_name"] for p in res.data["profiles"]]
        self.assertEqual(names, ["Aria"])
        self.assertNotIn("SecretChild", names)


class LoginThrottleTest(TestCase):
    """The login endpoint must not accept unlimited password guesses.

    LoginRateThrottle existed, was imported, and had a configured rate for a
    long time without being attached to any view — so brute-forcing the real
    LoginView was free. This pins that it is actually wired.
    """

    def setUp(self):
        from django.core.cache import cache
        from accounts.throttles import LoginRateThrottle, LoginAccountRateThrottle

        cache.clear()   # throttle state lives in the cache

        # settings_test disables every throttle scope (they make the suite
        # order-dependent — see config/settings_test.py). Re-enable just these
        # two for this class. @override_settings cannot do it: DRF binds
        # THROTTLE_RATES as a CLASS attribute at import time, so the setting
        # change never reaches the throttle.
        self._saved = dict(LoginRateThrottle.THROTTLE_RATES)
        for cls in (LoginRateThrottle, LoginAccountRateThrottle):
            cls.THROTTLE_RATES["login"] = "20/min"
            cls.THROTTLE_RATES["login_account"] = "10/min"
            # The rate is parsed once in __init__, so nothing else to reset.
        self.addCleanup(self._restore_rates)

    def _restore_rates(self):
        from accounts.throttles import LoginRateThrottle
        LoginRateThrottle.THROTTLE_RATES.clear()
        LoginRateThrottle.THROTTLE_RATES.update(self._saved)

    def _attempt(self, client, email="victim@test.com", ip="203.0.113.9"):
        return client.post(
            "/api/accounts/login/",
            {"email": email, "password": "wrong-password"},
            format="json",
            REMOTE_ADDR=ip,
        )

    def test_repeated_bad_passwords_for_one_account_get_throttled(self):
        from rest_framework.test import APIClient
        c = APIClient()
        statuses = [self._attempt(c).status_code for _ in range(15)]
        self.assertIn(
            429, statuses,
            f"login accepted 15 password guesses without throttling: {statuses}",
        )

    def test_throttle_is_per_account_not_only_per_ip(self):
        # Credential stuffing from MANY IPs against ONE account must still be
        # bounded — that is the case a per-IP-only throttle misses entirely.
        from rest_framework.test import APIClient
        c = APIClient()
        statuses = [
            self._attempt(c, ip=f"198.51.100.{i}").status_code
            for i in range(1, 16)
        ]
        self.assertIn(
            429, statuses,
            f"one account survived 15 guesses from 15 different IPs: {statuses}",
        )
