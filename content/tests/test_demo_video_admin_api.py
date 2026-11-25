"""Tests for the React admin's demo-video API (`/api/content/admin/demo-videos/`).

Same rules as the Django-admin upload flow, exposed over DRF: the bytes go
browser -> Bunny, so what is asserted here is everything around that — who may
use it, that a ticket is only ever signed for a guid the server just minted,
and that swapping a clip cannot leave the row advertising the old one's
runtime. Bunny is mocked; `create_bunny_video` and `fetch_bunny_video` are the
only calls that leave the process.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from content.models import DemoVideo, PublishStatus

User = get_user_model()
BASE = "/api/content/admin/demo-videos/"


@override_settings(BUNNY_LIBRARY_ID="12345", BUNNY_API_KEY="key",
                   BUNNY_EMBED="https://iframe.mediadelivery.net/embed")
class DemoVideoAdminAPITests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="editor", email="e@example.com", password="pw12345!",
            is_staff=True)
        self.visitor = User.objects.create_user(
            username="visitor", email="v@example.com", password="pw12345!")
        self.client = APIClient()
        self.client.force_authenticate(self.staff)
        self.video = DemoVideo.objects.create(
            key="signup", title="Sign Up Demo", status=PublishStatus.PUBLISHED,
            bunny_video_id="old-guid", bunny_status=DemoVideo.BUNNY_FINISHED,
            duration_seconds=53, thumbnail_url="https://cdn.example/old.jpg",
            order=0)

    # ── access ───────────────────────────────────────────────────

    def test_anonymous_and_non_staff_are_refused(self):
        anon = APIClient()
        self.assertIn(anon.get(BASE).status_code, (401, 403))
        non_staff = APIClient()
        non_staff.force_authenticate(self.visitor)
        self.assertEqual(non_staff.get(BASE).status_code, 403)
        self.assertEqual(
            non_staff.post(BASE + "upload-slot/", {}, format="json").status_code,
            403)

    # ── list / read shape ────────────────────────────────────────

    def test_list_includes_hidden_rows_and_says_why_each_is_live_or_not(self):
        DemoVideo.objects.create(
            key="draft-one", title="Draft", status=PublishStatus.DRAFT,
            order=5)
        res = self.client.get(BASE)
        self.assertEqual(res.status_code, 200)
        rows = {r["key"]: r for r in res.json()}
        self.assertEqual(set(rows), {"signup", "draft-one"})
        self.assertTrue(rows["signup"]["is_live"])
        self.assertEqual(rows["signup"]["on_the_site"], "Yes")
        self.assertFalse(rows["draft-one"]["is_live"])
        self.assertEqual(rows["draft-one"]["on_the_site"], "No — not published")

    # ── create ───────────────────────────────────────────────────

    def test_create_cannot_set_bunny_derived_fields(self):
        with patch("content.demo_video_bunny.fetch_bunny_video",
                   return_value=None):
            res = self.client.post(BASE, {
                "key": "enroll", "title": "Enroll", "blurb": "b", "order": 2,
                "status": "published", "bunny_video_id": "g-1",
                "bunny_status": 4, "duration_seconds": 999,
                "thumbnail_url": "https://evil.example/x.jpg",
            }, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        row = DemoVideo.objects.get(key="enroll")
        # Bunny could not be reached, so nothing was learned — and the values
        # the caller tried to type in were ignored.
        self.assertIsNone(row.bunny_status)
        self.assertIsNone(row.duration_seconds)
        self.assertEqual(row.thumbnail_url, "")

    def test_duplicate_key_is_rejected(self):
        res = self.client.post(BASE, {"key": "signup", "title": "Again"},
                               format="json")
        self.assertEqual(res.status_code, 400)

    # ── replacing a clip ─────────────────────────────────────────

    def test_changing_the_guid_clears_the_old_clips_derived_values(self):
        with patch("content.demo_video_bunny.fetch_bunny_video",
                   return_value=None):
            res = self.client.patch(
                BASE + "signup/", {"bunny_video_id": "new-guid"},
                format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.video.refresh_from_db()
        self.assertEqual(self.video.bunny_video_id, "new-guid")
        self.assertIsNone(self.video.bunny_status)
        self.assertIsNone(self.video.duration_seconds)
        self.assertEqual(self.video.thumbnail_url, "")
        # Not live until Bunny confirms the new clip finished.
        self.assertFalse(res.json()["is_live"])

    def test_editing_text_does_not_disturb_the_clip(self):
        res = self.client.patch(BASE + "signup/", {"title": "New title"},
                                format="json")
        self.assertEqual(res.status_code, 200)
        self.video.refresh_from_db()
        self.assertEqual(self.video.title, "New title")
        self.assertEqual(self.video.bunny_status, DemoVideo.BUNNY_FINISHED)
        self.assertEqual(self.video.duration_seconds, 53)

    def test_archiving_takes_the_row_off_the_public_list(self):
        res = self.client.patch(BASE + "signup/", {"status": "archived"},
                                format="json")
        self.assertEqual(res.status_code, 200)
        public = APIClient().get("/api/content/demo-videos/")
        self.assertEqual(public.json(), [])

    def test_delete_removes_the_row(self):
        res = self.client.delete(BASE + "signup/")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(DemoVideo.objects.filter(key="signup").exists())

    # ── upload slot ──────────────────────────────────────────────

    @patch("content.demo_video_bunny.create_bunny_video")
    def test_slot_signs_only_a_guid_the_server_just_minted(self, create):
        create.return_value = "server-minted"
        res = self.client.post(
            BASE + "upload-slot/",
            {"video_id": "somebody-elses", "filename": "x.mp4",
             "size": 5_000_000},
            format="json")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["video_id"], "server-minted")
        self.assertEqual(body["library_id"], "12345")
        self.assertTrue(body["signature"])
        self.assertTrue(body["expire"])

    @patch("content.demo_video_bunny.create_bunny_video")
    def test_slot_reports_bunny_being_down_as_502(self, create):
        from content.demo_video_bunny import BunnyUnavailable
        create.side_effect = BunnyUnavailable("Bunny is not answering.")
        res = self.client.post(BASE + "upload-slot/", {}, format="json")
        self.assertEqual(res.status_code, 502)
        self.assertIn("Bunny", res.json()["error"])

    # ── sync ─────────────────────────────────────────────────────

    def test_sync_without_a_video_is_a_no_op_not_an_error(self):
        DemoVideo.objects.create(key="empty", title="Empty")
        res = self.client.post(BASE + "empty/sync/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["reachable"])

    def test_sync_when_bunny_is_unreachable_keeps_stored_values(self):
        with patch("content.demo_video_bunny.fetch_bunny_video",
                   return_value=None):
            res = self.client.post(BASE + "signup/sync/")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["reachable"])
        self.video.refresh_from_db()
        self.assertEqual(self.video.duration_seconds, 53)
