"""
#3 — ProfileEmailLookupView must require auth and only ever reflect the caller's
own account (previously AllowAny + arbitrary email → children's-name leak).
"""
from django.test import TestCase
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
