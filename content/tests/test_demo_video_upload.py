"""Tests for uploading a landing demo clip from the Django admin.

The bytes go browser → Bunny, so there is no file upload to test here. What
these assert is everything *around* that: who may mint a signed ticket, that a
ticket is only ever signed for a guid the server itself just created, and that
attaching a new clip cannot leave the row advertising the old one's runtime.

Bunny is mocked throughout — `create_bunny_video` and `fetch_bunny_video` are
the only two calls that leave the process, both patched at
`content.demo_video_bunny` where the views import them from.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from content.models import DemoVideo, PublishStatus

User = get_user_model()


@override_settings(BUNNY_LIBRARY_ID="12345", BUNNY_API_KEY="key",
                   BUNNY_EMBED="https://iframe.mediadelivery.net/embed")
class DemoVideoUploadAdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="boss", email="admin@example.com", password="pw12345!")
        self.video = DemoVideo.objects.create(
            key="login", title="Log In Demo", status=PublishStatus.PUBLISHED)
        self.client.force_login(self.admin)

    def _url(self, name):
        return reverse(f"admin:content_demovideo_{name}", args=[self.video.pk])

    # ── minting a slot ───────────────────────────────────────────

    @patch("content.demo_video_bunny.create_bunny_video")
    def test_slot_returns_a_ticket_for_a_freshly_created_video(self, create):
        create.return_value = "new-guid"
        res = self.client.post(self._url("upload_slot"),
                               {"filename": "log in.mp4", "size": 5_000_000},
                               content_type="application/json")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["video_id"], "new-guid")
        self.assertEqual(body["library_id"], "12345")
        # The browser needs both halves or Bunny rejects the TUS handshake.
        self.assertTrue(body["signature"])
        self.assertTrue(body["expire"])

    @patch("content.demo_video_bunny.create_bunny_video")
    def test_slot_never_signs_a_client_supplied_guid(self, create):
        """The whole access-control story for this endpoint.

        `skills` needs a PendingIntroVideoUpload row to prove the caller owns
        the guid it wants signed. Here a guid simply cannot be supplied — the
        only one that can be signed is the one this call just minted — so a
        caller naming someone else's video gets a ticket for a new empty slot,
        never for theirs.
        """
        create.return_value = "server-minted"
        res = self.client.post(
            self._url("upload_slot"),
            {"video_id": "somebody-elses-video", "filename": "x.mp4"},
            content_type="application/json")
        self.assertEqual(res.json()["video_id"], "server-minted")

    @patch("content.demo_video_bunny.create_bunny_video")
    def test_bunny_refusing_is_reported_as_502_in_words(self, create):
        from content.demo_video_bunny import BunnyUnavailable
        create.side_effect = BunnyUnavailable("Could not reach the video service.")
        res = self.client.post(self._url("upload_slot"), {},
                               content_type="application/json")
        self.assertEqual(res.status_code, 502)
        self.assertIn("Could not reach", res.json()["error"])

    # ── permissions ──────────────────────────────────────────────

    def test_staff_without_change_permission_cannot_upload(self):
        """`admin_view` only proves the caller is staff.

        A read-only content admin must not be able to repoint a video on the
        public homepage by calling the endpoint directly.
        """
        weak = User.objects.create_user(
            username="weak", email="weak@example.com", password="pw12345!",
            is_staff=True)
        self.client.force_login(weak)
        for name in ("upload_slot", "attach", "state"):
            res = self.client.post(self._url(name), {"video_id": "x"},
                                   content_type="application/json")
            # 403 specifically, not a login redirect: `admin_view` lets this
            # caller through (they ARE staff) and the per-model check is what
            # stops them. A 302 here would mean the permission check never ran.
            self.assertEqual(res.status_code, 403, name)

    def test_anonymous_is_bounced_to_the_admin_login(self):
        self.client.logout()
        res = self.client.post(self._url("upload_slot"), {},
                               content_type="application/json")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/login", res["Location"])

    def test_get_is_refused_on_the_write_endpoints(self):
        for name in ("upload_slot", "attach"):
            self.assertEqual(self.client.get(self._url(name)).status_code, 405)

    # ── attaching ────────────────────────────────────────────────

    @patch("content.demo_video_bunny.fetch_bunny_video")
    def test_attach_clears_the_previous_clips_derived_fields(self, fetch):
        """A runtime outliving the clip it described is worse than none.

        The menu would keep advertising 0:54 for a video that is now something
        else, and the next sync is the only thing that would ever catch it.
        """
        DemoVideo.objects.filter(pk=self.video.pk).update(
            bunny_video_id="old-guid", bunny_status=4, duration_seconds=54,
            thumbnail_url="https://cdn.example.net/old-guid/t.jpg")
        fetch.return_value = {"status": 2, "length": 0}   # still processing

        res = self.client.post(self._url("attach"), {"video_id": "fresh-guid"},
                               content_type="application/json")

        self.assertEqual(res.status_code, 200)
        self.video.refresh_from_db()
        self.assertEqual(self.video.bunny_video_id, "fresh-guid")
        self.assertIsNone(self.video.duration_seconds)
        self.assertEqual(self.video.thumbnail_url, "")
        self.assertFalse(self.video.is_playable)

    @patch("content.demo_video_bunny.fetch_bunny_video")
    def test_attach_reports_why_the_clip_is_not_live_yet(self, fetch):
        fetch.return_value = {"status": 2, "length": 0}
        body = self.client.post(self._url("attach"), {"video_id": "g"},
                                content_type="application/json").json()
        self.assertFalse(body["finished"])
        self.assertEqual(body["status_label"], "Processing")
        self.assertIn("No —", body["on_the_site"])

    @patch("content.demo_video_bunny.fetch_bunny_video")
    def test_attach_of_a_finished_clip_is_live_immediately(self, fetch):
        fetch.return_value = {"status": 4, "length": 53}
        body = self.client.post(self._url("attach"), {"video_id": "g"},
                                content_type="application/json").json()
        self.assertTrue(body["finished"])
        self.assertEqual(body["on_the_site"], "Yes")
        self.assertEqual(body["runtime"], "0:53")

    def test_attach_requires_a_video_id(self):
        res = self.client.post(self._url("attach"), {"video_id": "  "},
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)

    @patch("content.demo_video_bunny.fetch_bunny_video")
    def test_attach_is_recorded_in_the_admin_log(self, fetch):
        """Repointing a homepage video is a content change like any other —
        it should show up in the row's History, not happen invisibly."""
        from django.contrib.admin.models import LogEntry

        fetch.return_value = {"status": 4, "length": 53}
        self.client.post(self._url("attach"), {"video_id": "g"},
                         content_type="application/json")
        entry = LogEntry.objects.filter(object_id=str(self.video.pk)).first()
        self.assertIsNotNone(entry)
        self.assertIn("g", entry.change_message)

    # ── polling ──────────────────────────────────────────────────

    @patch("content.demo_video_bunny.fetch_bunny_video")
    def test_state_picks_up_a_finished_encode(self, fetch):
        DemoVideo.objects.filter(pk=self.video.pk).update(
            bunny_video_id="g", bunny_status=2)
        fetch.return_value = {"status": 4, "length": 53}
        body = self.client.get(self._url("state")).json()
        self.assertTrue(body["finished"])
        self.video.refresh_from_db()
        self.assertEqual(self.video.bunny_status, 4)

    @patch("content.demo_video_bunny.fetch_bunny_video")
    def test_bunny_being_unreachable_is_not_a_broken_clip(self, fetch):
        """None from Bunny means "we learned nothing", never "it is gone".

        The stored state must survive, and the widget must be told the answer
        is stale rather than shown a state the server never actually learned.
        """
        DemoVideo.objects.filter(pk=self.video.pk).update(
            bunny_video_id="g", bunny_status=4, duration_seconds=53)
        fetch.return_value = None

        body = self.client.get(self._url("state")).json()

        self.assertFalse(body["reachable"])
        self.assertEqual(body["duration_seconds"], 53)
        self.video.refresh_from_db()
        self.assertEqual(self.video.bunny_status, 4)

    @patch("content.demo_video_bunny.fetch_bunny_video")
    def test_state_does_not_call_bunny_for_a_row_with_no_clip(self, fetch):
        body = self.client.get(self._url("state")).json()
        fetch.assert_not_called()
        self.assertEqual(body["video_id"], "")
        self.assertEqual(body["on_the_site"], "No — no video uploaded")

    # ── the change form itself ───────────────────────────────────

    def test_change_form_renders_the_panel_with_its_endpoints(self):
        res = self.client.get(
            reverse("admin:content_demovideo_change", args=[self.video.pk]))
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn('class="dv-upload"', html)
        self.assertIn(self._url("upload_slot"), html)
        self.assertIn("content/tus.min.js", html)
        # The paste-a-guid path predates this widget and still has to work.
        self.assertIn('name="bunny_video_id"', html)

    def test_add_form_says_to_save_first(self):
        """There is no row to attach a guid to yet, and a file picker that
        cannot work is worse than a sentence explaining why."""
        res = self.client.get(reverse("admin:content_demovideo_add"))
        self.assertEqual(res.status_code, 200)
        self.assertIn("Save this row first", res.content.decode())
