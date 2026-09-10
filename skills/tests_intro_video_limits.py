"""Tests for the intro-clip duration limit and the Bunny status sync.

Two production bugs are pinned here:

1. **Stranded status.** Saving a clip used to hardcode `intro_video_status = 1`
   and nothing ever advanced it, so `intro_video_embed_url()` — which returns
   None below 4 — hid every expert clip that was ever uploaded. The existing
   suite missed this because it set status 4 by hand before asserting.
   `test_save_records_finished_status_from_bunny` is the regression.

2. **No duration limit.** Nothing read Bunny's `length`, so real applicants
   uploaded clips of any length and nothing flagged them.
"""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Role, TeacherProfile, User, UserRole

from .intro_video import MAX_INTRO_VIDEO_SECONDS
from .models import ExpertProfile


def bunny_video(status=4, length=30, thumb="thumb.jpg"):
    return {"status": status, "length": length, "thumbnailFileName": thumb}


class IntroVideoLimitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")
        cls.user = User.objects.create_user(
            username="videxpert", email="videxpert@test.com", password="testpass123",
        )
        UserRole.objects.create(
            user=cls.user, role=cls.teacher_role, is_active=True, is_primary=True,
        )
        cls.teacher_profile = TeacherProfile.objects.create(
            user=cls.user, teacher_type=TeacherProfile.TYPE_GUEST,
        )
        cls.expert = ExpertProfile.objects.create(
            teacher_profile=cls.teacher_profile, headline="Guitar", is_listed=True,
        )
        cls.admin = User.objects.create_user(
            username="vidadmin", email="vidadmin@test.com",
            password="testpass123", is_staff=True,
        )

    def setUp(self):
        self.client_ = APIClient()
        self.client_.force_authenticate(user=self.user)
        self.expert.refresh_from_db()

    def save_video(self, video_id="vid-1"):
        return self.client_.post(
            "/api/skill/teacher/intro-video/save/", {"video_id": video_id}
        )

    # ── the stranded-status regression ──────────────────────────────────

    @patch("skills.intro_video.requests")
    def test_save_records_finished_status_from_bunny(self, mock_requests):
        """A clip Bunny has already finished must come back playable.

        Previously this returned status 1 and a null embed URL forever.
        """
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.json.return_value = bunny_video(status=4, length=42)

        r = self.save_video()

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["intro_video_status"], 4)
        self.assertEqual(r.data["intro_video_duration"], 42)
        self.assertTrue(r.data["intro_video_embed_url"].endswith("/vid-1"))

    @patch("skills.intro_video.requests")
    def test_status_endpoint_advances_a_stranded_clip(self, mock_requests):
        self.expert.intro_video_bunny_id = "vid-stranded"
        self.expert.intro_video_status = 1
        self.expert.save()
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.json.return_value = bunny_video(status=4, length=12)

        r = self.client_.get("/api/skill/teacher/intro-video/status/")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["intro_video_status"], 4)
        self.assertEqual(r.data["intro_video_duration"], 12)
        self.expert.refresh_from_db()
        self.assertEqual(self.expert.intro_video_status, 4)

    @patch("skills.intro_video.requests")
    def test_status_endpoint_does_not_call_bunny_once_settled(self, mock_requests):
        """A finished clip with duration and thumbnail recorded is terminal."""
        self.expert.intro_video_bunny_id = "vid-done"
        self.expert.intro_video_status = 4
        self.expert.intro_video_duration = 20
        self.expert.intro_video_thumbnail_url = "https://cdn.example/vid-done/thumb.jpg"
        self.expert.save()

        r = self.client_.get("/api/skill/teacher/intro-video/status/")

        self.assertEqual(r.status_code, 200)
        mock_requests.get.assert_not_called()

    # ── the duration limit ──────────────────────────────────────────────

    @patch("skills.intro_video.requests")
    def test_save_rejects_a_clip_over_the_limit(self, mock_requests):
        too_long = MAX_INTRO_VIDEO_SECONDS + 35
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.json.return_value = bunny_video(
            status=4, length=too_long
        )

        r = self.save_video("vid-long")

        self.assertEqual(r.status_code, 400)
        self.assertIn(str(too_long), r.data["error"])
        self.assertIn(str(MAX_INTRO_VIDEO_SECONDS), r.data["error"])
        self.expert.refresh_from_db()
        # Nothing was attached, so nothing can leak onto a public surface.
        self.assertEqual(self.expert.intro_video_bunny_id, "")
        self.assertIsNone(self.expert.intro_video_embed_url())

    @patch("skills.intro_video.requests")
    def test_rejected_replacement_keeps_the_previous_clip(self, mock_requests):
        """A failed replacement is a no-op, not a way to lose a good clip."""
        self.expert.intro_video_bunny_id = "vid-good"
        self.expert.intro_video_status = 4
        self.expert.intro_video_duration = 30
        self.expert.intro_video_thumbnail_url = "https://cdn.example/vid-good/t.jpg"
        self.expert.save()

        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.json.return_value = bunny_video(
            status=4, length=MAX_INTRO_VIDEO_SECONDS + 1
        )

        r = self.save_video("vid-toolong")

        self.assertEqual(r.status_code, 400)
        self.expert.refresh_from_db()
        self.assertEqual(self.expert.intro_video_bunny_id, "vid-good")
        self.assertEqual(self.expert.intro_video_status, 4)
        self.assertTrue(self.expert.intro_video_embed_url().endswith("/vid-good"))

    @patch("skills.intro_video.requests")
    def test_clip_exactly_at_the_limit_is_accepted(self, mock_requests):
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.json.return_value = bunny_video(
            status=4, length=MAX_INTRO_VIDEO_SECONDS
        )

        r = self.save_video("vid-exact")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["intro_video_duration"], MAX_INTRO_VIDEO_SECONDS)

    @patch("skills.intro_video.requests")
    def test_limit_applies_later_when_bunny_has_no_length_yet(self, mock_requests):
        """Bunny reports length 0 until it processes the file.

        The clip is accepted on save, then rejected on the poll that first
        sees a real length — it must never resolve to an embed URL.
        """
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.json.return_value = bunny_video(status=2, length=0)
        self.assertEqual(self.save_video("vid-slow").status_code, 200)

        mock_requests.get.return_value.json.return_value = bunny_video(
            status=4, length=MAX_INTRO_VIDEO_SECONDS + 90
        )
        r = self.client_.get("/api/skill/teacher/intro-video/status/")

        self.assertEqual(r.data["intro_video_status"], 5)  # Error
        self.assertIsNone(r.data["intro_video_embed_url"])

    # ── resilience ──────────────────────────────────────────────────────

    @patch("skills.intro_video.requests")
    def test_save_still_works_when_bunny_is_unreachable(self, mock_requests):
        import requests as real_requests
        mock_requests.RequestException = real_requests.RequestException
        mock_requests.get.side_effect = real_requests.RequestException("boom")

        r = self.save_video("vid-offline")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["intro_video_status"], 1)  # Uploaded, pending
        self.expert.refresh_from_db()
        self.assertEqual(self.expert.intro_video_bunny_id, "vid-offline")

    # ── admin moderation visibility ─────────────────────────────────────

    def test_admin_expert_detail_exposes_the_clip_for_moderation(self):
        """The Skill track self-lists, so the admin sees it to moderate it."""
        self.expert.intro_video_bunny_id = "vid-public"
        self.expert.intro_video_status = 4
        self.expert.intro_video_duration = 45
        self.expert.save()

        admin_client = APIClient()
        admin_client.force_authenticate(user=self.admin)
        r = admin_client.get(f"/api/skill/admin/experts/{self.expert.id}/")

        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["intro_video_embed_url"].endswith("/vid-public"))
        self.assertEqual(r.data["intro_video_duration"], 45)

    # ── the repair command ──────────────────────────────────────────────

    @patch("skills.intro_video.requests")
    def test_sync_command_repairs_a_stranded_row(self, mock_requests):
        self.expert.intro_video_bunny_id = "vid-old"
        self.expert.intro_video_status = 1
        self.expert.save()
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.json.return_value = bunny_video(status=4, length=9)

        call_command("sync_intro_videos", stdout=StringIO())

        self.expert.refresh_from_db()
        self.assertEqual(self.expert.intro_video_status, 4)
        self.assertEqual(self.expert.intro_video_duration, 9)

    @patch("skills.intro_video.requests")
    def test_sync_command_dry_run_writes_nothing(self, mock_requests):
        self.expert.intro_video_bunny_id = "vid-old"
        self.expert.intro_video_status = 1
        self.expert.save()
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.json.return_value = bunny_video(status=4, length=9)

        out = StringIO()
        call_command("sync_intro_videos", "--dry-run", stdout=out)

        self.expert.refresh_from_db()
        self.assertEqual(self.expert.intro_video_status, 1)
        self.assertIsNone(self.expert.intro_video_duration)
        self.assertIn("Dry run", out.getvalue())
