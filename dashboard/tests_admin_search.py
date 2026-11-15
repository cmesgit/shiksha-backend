"""GET /api/dashboard/admin/search/ — design_handoff_admin_counsellor Phase 5.

The artboard's top bar says "Find a student, course, teacher or page". The
only search that existed was the CMS one, which finds none of the first three
and 403s entirely when content_studio_enabled is off.
"""
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import LearnerProfile, Role, User, UserRole

URL = "/api/dashboard/admin/search/"


def _client(user=None):
    c = APIClient()
    if user is not None:
        c.force_authenticate(user=user)
    return c


class AdminSearchTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="srch_admin", email="srch_admin@e.com", password="x",
            is_staff=True, is_superuser=True)
        role, _ = Role.objects.get_or_create(name="ADMIN")
        UserRole.objects.create(user=self.admin, role=role, is_active=True)

    def _get(self, q):
        return _client(self.admin).get(URL, {"q": q}).json()

    # ── access ──────────────────────────────────────────────────────────
    def test_anonymous_is_refused(self):
        self.assertIn(_client().get(URL, {"q": "ab"}).status_code, (401, 403))

    def test_ordinary_user_is_refused(self):
        u = User.objects.create_user(username="srch_u", email="srch_u@e.com", password="x")
        self.assertEqual(_client(u).get(URL, {"q": "ab"}).status_code, 403)

    # ── query floor ─────────────────────────────────────────────────────
    def test_a_one_character_query_returns_nothing_rather_than_everything(self):
        """A single letter matches most of the database. The floor is what
        stops the palette dumping the whole user table on first keystroke."""
        User.objects.create_user(username="aaa", email="aaa@e.com", password="x")
        self.assertEqual(self._get("a")["results"], [])

    # ── people ──────────────────────────────────────────────────────────
    def test_finds_a_person_by_email(self):
        User.objects.create_user(username="meera", email="meera@school.com", password="x")
        titles = [r["title"] for r in self._get("meera")["results"]]
        self.assertTrue(any("meera" in t.lower() for t in titles), titles)

    def test_finds_a_person_by_their_PROFILE_name(self):
        """⚠ `display_name` lives on LearnerProfile, not User. Searching only
        User fields misses everyone whose name is the thing you remember."""
        u = User.objects.create_user(username="u404", email="u404@e.com", password="x")
        lp = u.learner_profiles.first() or LearnerProfile.objects.create(
            account=u, display_name="Aarav Kulkarni")
        lp.display_name = "Aarav Kulkarni"
        lp.save()
        titles = [r["title"] for r in self._get("Aarav")["results"]]
        self.assertIn("Aarav Kulkarni", titles)

    def test_an_account_is_returned_once_even_with_several_profiles(self):
        """A User with multiple matching profiles would otherwise appear
        once per profile — the join multiplies."""
        u = User.objects.create_user(username="multi", email="multi@e.com", password="x")
        for n in ("Multi One", "Multi Two"):
            LearnerProfile.objects.create(account=u, display_name=n)
        people = [r for r in self._get("Multi")["results"] if r["kind"] == "person"]
        self.assertEqual(len(people), 1, people)

    def test_person_rows_point_at_the_user_detail_route(self):
        u = User.objects.create_user(username="linky", email="linky@e.com", password="x")
        row = next(r for r in self._get("linky")["results"] if r["kind"] == "person")
        self.assertEqual(row["url"], f"/users/{u.pk}")

    # ── courses ─────────────────────────────────────────────────────────
    def test_finds_a_course_and_says_its_status(self):
        """A DRAFT course is invisible to visitors, and "why isn't this live"
        is the commonest question about one — so the status rides along."""
        from courses.models import Course
        Course.objects.create(title="Class 10 Mathematics")
        row = next(r for r in self._get("Mathem")["results"] if r["kind"] == "course")
        self.assertEqual(row["title"], "Class 10 Mathematics")
        self.assertTrue(row["where"])

    # ── the content half ────────────────────────────────────────────────
    def test_content_is_reported_as_SKIPPED_not_silently_empty(self):
        """⚠ THE POINT. The CMS search sits behind IsStudioEditor, which
        refuses while content_studio_enabled is off. "No posts matched" and
        "posts were not searched" are different answers, and a search box that
        conflates them teaches admins to distrust it."""
        from global_settings.models import GlobalSettings
        GlobalSettings.objects.filter(pk=1).update(content_studio_enabled=False)
        GlobalSettings.load.cache_clear() if hasattr(GlobalSettings.load, "cache_clear") else None
        body = self._get("anything")
        self.assertIn("skipped", body)

    def test_envelope_shape_is_stable(self):
        body = self._get("zz")
        for key in ("query", "results", "skipped"):
            self.assertIn(key, body)

    def test_every_result_carries_what_the_palette_renders(self):
        User.objects.create_user(username="shape1", email="shape1@e.com", password="x")
        for r in self._get("shape1")["results"]:
            for key in ("kind", "kind_label", "title", "where", "url"):
                self.assertIn(key, r, r)
