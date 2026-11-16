"""Redirects — design_handoff_admin_counsellor, Admin v2 Phase 5.

The problem these exist for is documented in the workspace CLAUDE.md:
`Course.slug` is derived from the title on first save and NEVER re-derived on
rename, so renaming a course silently kills every shared link to it.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from content.models import Redirect

RESOLVE = "/api/content/redirects/resolve/"
ADMIN = "/api/content/admin/redirects/"


def _admin():
    u = User.objects.create_user(username="rdadmin", email="rdadmin@e.com",
                                 password="x", is_staff=True)
    c = APIClient(); c.force_authenticate(user=u); return c


class NormaliseTest(TestCase):
    """One spelling per path, or a lookup misses on punctuation alone."""

    def test_casing_trailing_slash_and_query_all_collapse(self):
        for raw in ["/A/B", "/a/b/", "a/b", "/a/b?utm=x", "/A/B/#top"]:
            self.assertEqual(Redirect.normalise(raw), "/a/b", raw)

    def test_root_survives(self):
        self.assertEqual(Redirect.normalise("/"), "/")


class ResolveTest(TestCase):
    def setUp(self):
        Redirect.objects.create(from_path="/courses/old", to_path="/courses/new")

    def test_public_and_unauthenticated(self):
        """⚠ Every visitor on a dead link runs this. Requiring a session
        would make it useless for exactly the case it exists for."""
        res = APIClient().get(RESOLVE, {"path": "/courses/old"})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["to"], "/courses/new")

    def test_unknown_path_is_404_not_an_empty_200(self):
        """The SPA renders its 404 on this; a 200 with no destination would
        have to be special-cased by every caller."""
        self.assertEqual(APIClient().get(RESOLVE, {"path": "/nope"}).status_code, 404)

    def test_matching_ignores_case_and_trailing_slash(self):
        for variant in ["/Courses/Old", "/courses/old/", "/courses/old?ref=x"]:
            self.assertEqual(
                APIClient().get(RESOLVE, {"path": variant}).status_code, 200, variant)

    def test_inactive_rules_do_not_fire(self):
        Redirect.objects.filter(from_path="/courses/old").update(is_active=False)
        self.assertEqual(APIClient().get(RESOLVE, {"path": "/courses/old"}).status_code, 404)

    def test_a_chain_is_followed_to_the_end(self):
        """Renaming twice is normal. A visitor should land on the live page,
        not on the intermediate name that is also dead."""
        Redirect.objects.create(from_path="/courses/new", to_path="/courses/newest")
        self.assertEqual(
            APIClient().get(RESOLVE, {"path": "/courses/old"}).json()["to"],
            "/courses/newest")

    def test_a_loop_terminates_instead_of_hanging(self):
        """⚠ Two rules edited independently can close a loop that neither
        validation pass saw. The resolver must stop, not spin."""
        Redirect.objects.create(from_path="/courses/new", to_path="/courses/old")
        res = APIClient().get(RESOLVE, {"path": "/courses/old"})
        self.assertEqual(res.status_code, 200, res.content)

    def test_hits_are_counted_on_the_rule_that_matched(self):
        APIClient().get(RESOLVE, {"path": "/courses/old"})
        APIClient().get(RESOLVE, {"path": "/courses/old"})
        row = Redirect.objects.get(from_path="/courses/old")
        self.assertEqual(row.hits, 2)
        self.assertIsNotNone(row.last_hit_at)

    def test_offsite_destination_is_returned_verbatim(self):
        """A full URL must keep its scheme, case and query — normalising it
        would break the link it is meant to preserve."""
        Redirect.objects.create(
            from_path="/partner", to_path="https://Example.com/a?b=C")
        self.assertEqual(
            APIClient().get(RESOLVE, {"path": "/partner"}).json()["to"],
            "https://Example.com/a?b=C")


class AdminCrudTest(TestCase):
    def test_non_admin_cannot_list_or_create(self):
        u = User.objects.create_user(username="plain", email="plain@e.com", password="x")
        c = APIClient(); c.force_authenticate(user=u)
        self.assertEqual(c.get(ADMIN).status_code, 403)
        self.assertEqual(c.post(ADMIN, {"from_path": "/a", "to_path": "/b"}).status_code, 403)

    def test_anonymous_cannot_enumerate_rules(self):
        self.assertIn(APIClient().get(ADMIN).status_code, (401, 403))

    def test_create_normalises_both_paths(self):
        c = _admin()
        res = c.post(ADMIN, {"from_path": "Courses/Old/", "to_path": "/Courses/New/"})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["from_path"], "/courses/old")
        self.assertEqual(res.json()["to_path"], "/courses/new")

    def test_self_redirect_is_refused(self):
        res = _admin().post(ADMIN, {"from_path": "/a", "to_path": "/a/"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("to_path", res.json())

    def test_redirecting_the_site_root_is_refused(self):
        res = _admin().post(ADMIN, {"from_path": "/", "to_path": "/home"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("from_path", res.json())

    def test_a_loop_through_an_existing_rule_is_refused(self):
        """⚠ THE ONE PEOPLE ACTUALLY CREATE: B→A already exists, and someone
        adds A→B. Checking only one level deep would let this through."""
        Redirect.objects.create(from_path="/b", to_path="/a")
        res = _admin().post(ADMIN, {"from_path": "/a", "to_path": "/b"})
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("to_path", res.json())

    def test_duplicate_from_path_is_refused(self):
        Redirect.objects.create(from_path="/dup", to_path="/x")
        self.assertEqual(
            _admin().post(ADMIN, {"from_path": "/DUP/", "to_path": "/y"}).status_code, 400)

    def test_patch_and_delete(self):
        c = _admin()
        rid = c.post(ADMIN, {"from_path": "/p", "to_path": "/q"}).json()["id"]
        self.assertEqual(c.patch(f"{ADMIN}{rid}/", {"is_active": False},
                                 format="json").json()["is_active"], False)
        self.assertEqual(c.delete(f"{ADMIN}{rid}/").status_code, 204)
        self.assertFalse(Redirect.objects.filter(pk=rid).exists())

    def test_search_spans_both_paths(self):
        Redirect.objects.create(from_path="/alpha", to_path="/beta")
        c = _admin()
        self.assertEqual(c.get(ADMIN, {"q": "beta"}).json()["count"], 1)
        self.assertEqual(c.get(ADMIN, {"q": "alpha"}).json()["count"], 1)
