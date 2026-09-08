"""
Tests for GET /api/skill/languages/ — the languages the roster actually teaches in.

Same reason the locations endpoint exists: the frontend hardcoded three
languages (Mizo, English, Hindi), so an expert teaching in Manipuri was
reachable by search but invisible to the language filter.

The property worth pinning hardest is the dedup: `languages` is written from a
free-text comma string, so the same language genuinely does arrive spelled
several ways, and a filter offering both "Manipuri" and "manipuri" splits one
group of teachers across two options that each look half-empty.
"""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, TeacherProfile
from .models import ExpertProfile


def make_expert(n, *, languages, listed=True):
    user = User.objects.create_user(
        username=f"lang{n}", email=f"lang{n}@test.com", password="testpass123",
    )
    tp = TeacherProfile.objects.create(user=user)
    return ExpertProfile.objects.create(
        teacher_profile=tp, headline=f"Expert {n}",
        is_listed=listed, languages=languages,
    )


class DirectoryLanguagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_expert(1, languages=["English", "Mizo"])
        make_expert(2, languages=["english", "Hindi"])       # casing differs
        make_expert(3, languages=["  Manipuri  "])           # padded
        make_expert(4, languages=["Bengali"], listed=False)  # not listed
        make_expert(5, languages=[])                         # none given
        make_expert(6, languages=["", "   "])                # blank entries

    def setUp(self):
        self.client = APIClient()
        # The view caches for an hour and LocMemCache is not reset between
        # tests, so a payload built by an earlier test would leak into this one.
        cache.clear()

    def test_is_public(self):
        self.assertEqual(self.client.get("/api/skill/languages/").status_code, 200)

    def test_returns_languages_beyond_the_hardcoded_three(self):
        """The whole point: Manipuri was unreachable before this endpoint."""
        langs = self.client.get("/api/skill/languages/").json()["languages"]
        self.assertIn("Manipuri", langs)

    def test_case_variants_collapse_to_one_option(self):
        langs = self.client.get("/api/skill/languages/").json()["languages"]
        lowered = [l.casefold() for l in langs]
        self.assertEqual(lowered.count("english"), 1)

    def test_values_are_stripped(self):
        langs = self.client.get("/api/skill/languages/").json()["languages"]
        self.assertIn("Manipuri", langs)
        self.assertNotIn("  Manipuri  ", langs)

    def test_blank_entries_are_dropped(self):
        langs = self.client.get("/api/skill/languages/").json()["languages"]
        self.assertNotIn("", langs)
        self.assertTrue(all(l.strip() for l in langs))

    def test_sorted_case_insensitively(self):
        langs = self.client.get("/api/skill/languages/").json()["languages"]
        self.assertEqual(langs, sorted(langs, key=str.casefold))

    def test_unlisted_experts_are_excluded(self):
        """Offering their language would be a filter that always returns nothing."""
        langs = self.client.get("/api/skill/languages/").json()["languages"]
        self.assertNotIn("Bengali", langs)

    def test_every_offered_language_matches_somebody(self):
        """A language offered by the rail but matching nobody is a dead end —
        exactly what the hardcoded list was for everyone outside its three."""
        langs = self.client.get("/api/skill/languages/").json()["languages"]
        self.assertTrue(langs, "fixture should produce at least one language")
        for lang in langs:
            res = self.client.get("/api/skill/teachers/", {"lang": lang})
            self.assertGreaterEqual(
                res.json()["count"], 1,
                msg=f"language {lang!r} is offered but matches nobody",
            )

    def test_non_list_languages_column_does_not_break_the_endpoint(self):
        """The column is user-written JSON and NOT NULL, so the shape that can
        actually go wrong is a bare JSON string rather than a list. Iterating
        that would yield characters; a bad row must not take the filter rail
        down or fill it with single letters."""
        ExpertProfile.objects.filter(headline="Expert 5").update(languages="English")
        cache.clear()
        res = self.client.get("/api/skill/languages/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Manipuri", res.json()["languages"])
