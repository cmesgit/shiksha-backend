# Cover for the Academy / Skill Dev track filter on GET /activity/feed/.
#
# The bell in all three dashboards reads this endpoint (NOT
# /api/notifications/), so this is the filter that actually decides whether
# a Skill Dev booking shows up inside Academy chrome. The notifications
# table has its own parallel cover in notifications/tests_tracks.py.
#
# A skill row is identified by its generic FK pointing at
# skills.SkillSession — the same probe the serializer's is_skill_session
# uses. These tests assert the two agree, because a disagreement would mean
# the row is filtered into one bell and then rendered with the other's icon
# and deep link.

import uuid

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.auth_flow import CTX_TEACHER
from skills.models import SkillSession

from .models import Activity

User = get_user_model()


class ActivityTrackFilterTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="t", email="t@example.com", password="x")
        self.client = APIClient()
        # Teacher context needs no learner profile, which keeps this test
        # about the track filter rather than about profile resolution.
        self.client.force_authenticate(self.user, token={"context": CTX_TEACHER})

        self.skill_ct = ContentType.objects.get_for_model(SkillSession)
        self.academy_ct = ContentType.objects.get_for_model(Activity)

        self.skill_row = self._activity("Skill booking", self.skill_ct)
        self.academy_row = self._activity("Academy class", self.academy_ct)

    def _activity(self, title, ct, is_read=False):
        return Activity.objects.create(
            user=self.user,
            audience=Activity.AUDIENCE_TEACHER,
            type=Activity.TYPE_SESSION,
            title=title,
            content_type=ct,
            object_id=uuid.uuid4(),
            is_read=is_read,
        )

    def _titles(self, response):
        return {r["title"] for r in response.data["results"]}

    def test_no_track_param_returns_both(self):
        r = self.client.get("/api/activity/feed/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._titles(r), {"Skill booking", "Academy class"})

    def test_academy_scope_excludes_skill_rows(self):
        r = self.client.get("/api/activity/feed/", {"track": "academy"})
        self.assertEqual(self._titles(r), {"Academy class"})

    def test_skill_scope_excludes_academy_rows(self):
        r = self.client.get("/api/activity/feed/", {"track": "skill"})
        self.assertEqual(self._titles(r), {"Skill booking"})

    def test_invalid_track_is_ignored_rather_than_blanking_the_bell(self):
        r = self.client.get("/api/activity/feed/", {"track": "acadmy"})
        self.assertEqual(len(r.data["results"]), 2)

    def test_serializer_track_agrees_with_the_filter(self):
        # If these ever disagree, a row gets filtered into one bell and
        # rendered with the other bell's icon/deep-link.
        r = self.client.get("/api/activity/feed/")
        by_title = {x["title"]: x for x in r.data["results"]}
        self.assertEqual(by_title["Skill booking"]["track"], "skill")
        self.assertTrue(by_title["Skill booking"]["is_skill_session"])
        self.assertEqual(by_title["Academy class"]["track"], "academy")
        self.assertFalse(by_title["Academy class"]["is_skill_session"])

    def test_cross_track_unread_counts_the_hidden_track_only(self):
        r = self.client.get("/api/activity/feed/", {"track": "academy"})
        self.assertEqual(r.data["cross_track_unread"], 1)   # the skill row

        r = self.client.get("/api/activity/feed/", {"track": "skill"})
        self.assertEqual(r.data["cross_track_unread"], 1)   # the academy row

    def test_cross_track_unread_ignores_already_read_rows(self):
        self.skill_row.is_read = True
        self.skill_row.save(update_fields=["is_read"])
        r = self.client.get("/api/activity/feed/", {"track": "academy"})
        self.assertEqual(r.data["cross_track_unread"], 0)

    def test_cross_track_unread_is_zero_when_nothing_is_hidden(self):
        # No ?track= means the bell shows everything; claiming N unread
        # "elsewhere" would be a lie.
        r = self.client.get("/api/activity/feed/")
        self.assertEqual(r.data["cross_track_unread"], 0)

    def test_mark_all_read_scoped_to_academy_spares_the_skill_bell(self):
        r = self.client.post("/api/activity/feed/read-all/",
                             {"track": "academy"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.skill_row.refresh_from_db()
        self.academy_row.refresh_from_db()
        self.assertFalse(self.skill_row.is_read,
                         "clearing Academy wrongly cleared the Skill bell")
        self.assertTrue(self.academy_row.is_read)

    def test_mark_all_read_scoped_to_skill_spares_the_academy_bell(self):
        self.client.post("/api/activity/feed/read-all/",
                         {"track": "skill"}, format="json")
        self.skill_row.refresh_from_db()
        self.academy_row.refresh_from_db()
        self.assertTrue(self.skill_row.is_read)
        self.assertFalse(self.academy_row.is_read)

    def test_mark_all_read_without_track_still_clears_everything(self):
        self.client.post("/api/activity/feed/read-all/", {}, format="json")
        self.assertEqual(
            Activity.objects.filter(user=self.user, is_read=False).count(), 0)
