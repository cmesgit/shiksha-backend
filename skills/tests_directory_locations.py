"""
Tests for GET /api/skill/locations/ — the directory's real state/district sets.

This endpoint exists because the frontend used to hardcode Mizoram's eight
districts, so the page's "verified experts from across India" copy described a
reach the location filter could not deliver. These tests pin the two properties
that make that claim true: the list comes from the data, and it is not limited
to any one state.
"""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, TeacherProfile
from .models import ExpertProfile


def make_expert(n, *, state, district, listed=True):
    user = User.objects.create_user(
        username=f"loc{n}", email=f"loc{n}@test.com", password="testpass123",
    )
    tp = TeacherProfile.objects.create(user=user)
    return ExpertProfile.objects.create(
        teacher_profile=tp, headline=f"Expert {n}",
        is_listed=listed, state=state, district=district,
    )


class DirectoryLocationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_expert(1, state="Mizoram", district="Aizawl")
        make_expert(2, state="Mizoram", district="Lunglei")
        make_expert(3, state="Mizoram", district="Aizawl")      # duplicate district
        make_expert(4, state="Assam", district="Guwahati")
        make_expert(5, state="Delhi", district="New Delhi")
        make_expert(6, state="", district="Shillong")           # state left blank
        make_expert(7, state="Kerala", district="Kochi", listed=False)  # not listed
        make_expert(8, state="Punjab", district="")             # no district

    def setUp(self):
        self.client = APIClient()
        # The view caches for an hour and LocMemCache is not reset between
        # tests, so a payload built by an earlier test would leak into this one.
        cache.clear()

    def test_is_public(self):
        self.assertEqual(self.client.get("/api/skill/locations/").status_code, 200)

    def test_returns_districts_beyond_the_launch_state(self):
        """The whole point: not Mizoram-only."""
        data = self.client.get("/api/skill/locations/").json()
        self.assertIn("Guwahati", data["districts"])
        self.assertIn("New Delhi", data["districts"])
        states = [s["state"] for s in data["states"]]
        self.assertIn("Assam", states)
        self.assertIn("Delhi", states)

    def test_districts_are_deduplicated_and_sorted(self):
        data = self.client.get("/api/skill/locations/").json()
        self.assertEqual(data["districts"], sorted(set(data["districts"])))
        self.assertEqual(data["districts"].count("Aizawl"), 1)

    def test_states_carry_their_own_districts_for_cascading(self):
        data = self.client.get("/api/skill/locations/").json()
        mizoram = next(s for s in data["states"] if s["state"] == "Mizoram")
        self.assertEqual(mizoram["districts"], ["Aizawl", "Lunglei"])

    def test_unlisted_experts_are_excluded(self):
        """An unlisted expert is invisible in the directory, so offering their
        district would be a filter that always returns nothing."""
        data = self.client.get("/api/skill/locations/").json()
        self.assertNotIn("Kochi", data["districts"])
        self.assertNotIn("Kerala", [s["state"] for s in data["states"]])

    def test_expert_with_no_district_is_skipped(self):
        data = self.client.get("/api/skill/locations/").json()
        self.assertNotIn("", data["districts"])
        self.assertNotIn("Punjab", [s["state"] for s in data["states"]])

    def test_blank_state_is_bucketed_not_dropped(self):
        """The district is still a usable filter value even with no state."""
        data = self.client.get("/api/skill/locations/").json()
        self.assertIn("Shillong", data["districts"])
        other = next(s for s in data["states"] if s["state"] == "Other")
        self.assertEqual(other["districts"], ["Shillong"])

    def test_district_filter_actually_matches_the_offered_value(self):
        """Every district offered must return at least the expert it came from
        — otherwise the filter is a dead end, which is what the hardcoded list
        was for everyone outside Mizoram."""
        data = self.client.get("/api/skill/locations/").json()
        for district in data["districts"]:
            res = self.client.get("/api/skill/teachers/", {"district": district})
            self.assertGreaterEqual(
                res.json()["count"], 1,
                msg=f"district {district!r} is offered but matches nobody",
            )

    def test_state_filter_is_accepted(self):
        """`state` has always been an accepted param but no client sent it."""
        res = self.client.get("/api/skill/teachers/", {"state": "Assam"})
        self.assertEqual(res.status_code, 200)
        names = res.json()["count"]
        self.assertGreaterEqual(names, 1)
