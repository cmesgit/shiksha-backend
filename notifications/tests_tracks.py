# Regression cover for the Academy / Skill Dev notification track split.
#
# What these tests are actually defending
# ───────────────────────────────────────
# 1. A Skill Dev notification must never appear in an Academy-scoped bell
#    (and vice versa), while genuinely cross-track rows (chat/forum/
#    counselling) must appear in BOTH. Getting the neutral direction wrong
#    is the dangerous failure: it silently loses DMs.
# 2. The badge count must match the list. A count that ignores ?track=
#    produces a permanently non-zero badge over an empty list.
# 3. "Mark all read" in one bell must not clear the other one.
# 4. track is derived from the verb, NOT from audience_identity — the two
#    are orthogonal and a dual-track user shares one identity key.
#
# Several tests deliberately assert the NEGATIVE (row absent) — see
# test_neutral_rows_are_not_hidden_by_either_track for the anti-vacuous
# guard that proves the filter isn't just returning nothing.

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .models import Notification
from .services import notify
from . import tracks

User = get_user_model()


class TrackForVerbTest(TestCase):
    """The pure mapping. No DB, no API — just the classification rule."""

    def test_academy_prefixes(self):
        for verb in ("session.approved", "session.reminder_24h",
                     "group.invite", "livestream.started",
                     "assignment.graded", "quiz.posted",
                     "materials.uploaded", "enrollment.approved"):
            self.assertEqual(tracks.track_for_verb(verb), tracks.ACADEMY, verb)

    def test_skill_prefix(self):
        for verb in ("skill.requested", "skill.confirmed", "skill.cancelled",
                     "skill.reschedule_proposed", "skill.paid"):
            self.assertEqual(tracks.track_for_verb(verb), tracks.SKILL, verb)

    def test_cross_track_verbs_are_neutral(self):
        for verb in ("chat.message", "forum.reply", "counseling.booked",
                     "announcement.posted", "support.reply"):
            self.assertEqual(tracks.track_for_verb(verb), tracks.NEUTRAL, verb)

    def test_payments_receipt_is_academy_but_skill_paid_is_not(self):
        # The exact-verb exception must beat the payments.* neutral prefix,
        # and must not drag skill.paid along with it.
        self.assertEqual(tracks.track_for_verb("payments.receipt"), tracks.ACADEMY)
        self.assertEqual(tracks.track_for_verb("payments.failed"), tracks.NEUTRAL)
        self.assertEqual(tracks.track_for_verb("skill.paid"), tracks.SKILL)

    def test_unknown_verb_is_neutral_not_academy(self):
        # The safe direction: an unmapped verb shows in both bells rather
        # than vanishing from one.
        self.assertEqual(tracks.track_for_verb("brandnew.event"), tracks.NEUTRAL)
        self.assertEqual(tracks.track_for_verb(""), tracks.NEUTRAL)
        self.assertEqual(tracks.track_for_verb(None), tracks.NEUTRAL)

    def test_prefix_match_is_dot_anchored(self):
        # "skill." must not match a hypothetical sibling app whose name
        # merely starts with the same letters.
        self.assertEqual(tracks.track_for_verb("skillsomething.x"), tracks.NEUTRAL)

    def test_normalize_rejects_junk(self):
        self.assertEqual(tracks.normalize("ACADEMY"), tracks.ACADEMY)
        self.assertEqual(tracks.normalize(" skill "), tracks.SKILL)
        self.assertEqual(tracks.normalize("nonsense"), tracks.NEUTRAL)
        self.assertEqual(tracks.normalize(None), tracks.NEUTRAL)


class NotifyPersistsTrackTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="learner", email="l@example.com", password="x")

    def test_track_derived_from_verb(self):
        n = notify(recipient=self.user, verb="skill.confirmed", title="t")
        self.assertEqual(n.track, tracks.SKILL)
        n = notify(recipient=self.user, verb="quiz.posted", title="t")
        self.assertEqual(n.track, tracks.ACADEMY)
        n = notify(recipient=self.user, verb="chat.message", title="t")
        self.assertEqual(n.track, tracks.NEUTRAL)

    def test_explicit_track_overrides_the_verb(self):
        n = notify(recipient=self.user, verb="chat.message", title="t",
                   track=tracks.SKILL)
        self.assertEqual(n.track, tracks.SKILL)

    def test_explicit_junk_track_degrades_to_neutral(self):
        # Must not fall through to the verb-derived value either — an
        # explicit-but-invalid track means "caller is confused", and
        # neutral is the non-hiding answer.
        n = notify(recipient=self.user, verb="skill.confirmed", title="t",
                   track="bogus")
        self.assertEqual(n.track, tracks.NEUTRAL)

    def test_track_is_independent_of_audience_identity(self):
        # The load-bearing claim: identity cannot encode track. The same
        # identity key carries rows of both tracks.
        notify(recipient=self.user, verb="skill.confirmed", title="s",
               audience_identity="L:abc")
        notify(recipient=self.user, verb="quiz.posted", title="a",
               audience_identity="L:abc")
        rows = Notification.objects.filter(audience_identity="L:abc")
        self.assertEqual(
            sorted(r.track for r in rows), [tracks.ACADEMY, tracks.SKILL])


class TrackScopedApiTest(TestCase):
    """The endpoints, through the real URL conf and permission stack."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="dual", email="d@example.com", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        self.academy = notify(recipient=self.user, verb="quiz.posted",
                              title="Academy quiz")
        self.skill = notify(recipient=self.user, verb="skill.confirmed",
                            title="Skill session confirmed")
        self.neutral = notify(recipient=self.user, verb="chat.message",
                              title="A direct message")

    def _titles(self, response):
        return {r["title"] for r in response.data["results"]}

    def test_academy_scope_hides_skill_but_keeps_neutral(self):
        r = self.client.get("/api/notifications/", {"track": "academy"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._titles(r), {"Academy quiz", "A direct message"})

    def test_skill_scope_hides_academy_but_keeps_neutral(self):
        r = self.client.get("/api/notifications/", {"track": "skill"})
        self.assertEqual(self._titles(r), {"Skill session confirmed",
                                           "A direct message"})

    def test_neutral_rows_are_not_hidden_by_either_track(self):
        # Anti-vacuous guard. If the filter were accidentally returning an
        # empty queryset, the two tests above would still "pass" their
        # absence assertions; this one fails loudly.
        for track in ("academy", "skill"):
            r = self.client.get("/api/notifications/", {"track": track})
            self.assertIn("A direct message", self._titles(r), track)
            self.assertEqual(len(r.data["results"]), 2, track)

    def test_no_track_param_returns_everything(self):
        r = self.client.get("/api/notifications/")
        self.assertEqual(len(r.data["results"]), 3)

    def test_invalid_track_is_ignored_not_empty(self):
        # Degrading to "show everything" beats degrading to "show nothing":
        # a typo in a client must not blank the bell.
        r = self.client.get("/api/notifications/", {"track": "acadmy"})
        self.assertEqual(len(r.data["results"]), 3)

    def test_badge_matches_the_scoped_list(self):
        r = self.client.get("/api/notifications/", {"track": "academy"})
        self.assertEqual(r.data["unread_count"], 2)   # academy + neutral
        self.assertEqual(len(r.data["results"]), 2)

    def test_track_unread_counts_are_exact_not_scoped(self):
        r = self.client.get("/api/notifications/", {"track": "academy"})
        # "general" must NOT be folded into the other two — the peek needs
        # the count of rows this bell is genuinely not showing.
        self.assertEqual(r.data["track_unread"],
                         {"academy": 1, "skill": 1, "general": 1})

    def test_unread_count_endpoint_honours_track(self):
        r = self.client.get("/api/notifications/unread-count/",
                            {"track": "skill"})
        self.assertEqual(r.data["unread_count"], 2)   # skill + neutral
        self.assertEqual(r.data["track_unread"]["skill"], 1)

    def test_mark_all_read_scoped_to_one_track_spares_the_other(self):
        r = self.client.post("/api/notifications/read/", {"track": "academy"},
                             format="json")
        self.assertEqual(r.status_code, 200)
        self.skill.refresh_from_db()
        self.academy.refresh_from_db()
        self.neutral.refresh_from_db()
        self.assertFalse(self.skill.is_read, "skill bell was wrongly cleared")
        self.assertTrue(self.academy.is_read)
        # Neutral rows are visible in the Academy bell, so clearing that
        # bell legitimately clears them.
        self.assertTrue(self.neutral.is_read)

    def test_mark_all_read_without_track_still_clears_everything(self):
        self.client.post("/api/notifications/read/", {}, format="json")
        self.assertEqual(
            Notification.objects.filter(recipient=self.user, is_read=False).count(), 0)

    def test_track_is_exposed_on_the_serialized_row(self):
        r = self.client.get("/api/notifications/")
        by_title = {n["title"]: n for n in r.data["results"]}
        self.assertEqual(by_title["Skill session confirmed"]["track"], "skill")
        self.assertEqual(by_title["Academy quiz"]["track"], "academy")
        self.assertEqual(by_title["A direct message"]["track"], "")

    def test_track_scope_composes_with_identity_scope(self):
        # Both axes at once: the child-B row must stay hidden even though
        # it is in the requested track.
        notify(recipient=self.user, verb="quiz.posted", title="Child A quiz",
               audience_identity="L:child-a")
        notify(recipient=self.user, verb="quiz.posted", title="Child B quiz",
               audience_identity="L:child-b")
        r = self.client.get("/api/notifications/",
                            {"track": "academy", "identity": "L:child-a"})
        titles = self._titles(r)
        self.assertIn("Child A quiz", titles)
        self.assertNotIn("Child B quiz", titles)
        self.assertNotIn("Skill session confirmed", titles)
        self.assertIn("A direct message", titles)   # account-wide + neutral
