"""Tests for the landing-page demo videos.

The interesting cases here are all about NOT rendering something broken. A
demo video has three independent ways to be half-configured, and each one used
to be a different flavour of "the button is there but nothing plays":

  * published, but no Bunny guid pasted in yet
  * a guid, but BUNNY_LIBRARY_ID unset on the environment
  * a guid, but Bunny hasn't finished processing so length is still 0

The endpoint's contract is that the frontend can trust `list.length` and
`embed_url` without re-deriving any of that, so these assert on what an
anonymous visitor actually receives.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from content.models import DemoVideo, PublishStatus

EMBED = "https://iframe.mediadelivery.net/embed"


@override_settings(BUNNY_LIBRARY_ID="12345", BUNNY_EMBED=EMBED)
class DemoVideoPublicAPITests(TestCase):
    def setUp(self):
        # CachedListAPIView memoises on the content version; without this a
        # row created in one test is served to the next one from cache.
        cache.clear()
        self.client = APIClient()
        self.url = reverse("content:demo-video-list")

    def _make(self, **kw):
        defaults = dict(
            key="signup", title="Sign Up Demo", blurb="Create your account",
            bunny_video_id="guid-signup", order=0,
            bunny_status=DemoVideo.BUNNY_FINISHED,
        )
        return DemoVideo.objects.create(**{**defaults, **kw})

    def test_anonymous_can_read(self):
        self._make()
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)

    def test_serialises_embed_url_not_bound_method(self):
        """`embed_url` is a model METHOD; a plain `fields` entry would
        serialise its repr. Assert on the actual URL, not just truthiness."""
        self._make()
        row = self.client.get(self.url).json()[0]
        self.assertEqual(row["embed_url"], f"{EMBED}/12345/guid-signup")
        self.assertEqual(row["key"], "signup")
        self.assertEqual(row["blurb"], "Create your account")

    def test_bunny_guid_is_not_exposed_as_its_own_field(self):
        self._make()
        self.assertNotIn("bunny_video_id", self.client.get(self.url).json()[0])

    def test_row_without_a_guid_is_withheld(self):
        """Published but not yet uploaded. The frontend gates the whole
        floating button on this list being empty, so a row that arrives with
        no video is a play button that opens an empty player."""
        self._make(bunny_video_id="")
        self.assertEqual(self.client.get(self.url).json(), [])

    def test_row_bunny_has_not_finished_encoding_is_withheld(self):
        """The 2026-09-11 incident: an upload returned success, stored zero
        bytes, and sat at status 2. The row had a guid and was published, so it
        was served — and the homepage offered a demo that opened an empty
        player. A guid proves a slot exists, not that it holds anything."""
        for stuck in (0, 1, 2, 3, 5, 6):
            with self.subTest(bunny_status=stuck):
                cache.clear()
                DemoVideo.objects.all().delete()
                self._make(bunny_status=stuck)
                self.assertEqual(self.client.get(self.url).json(), [])

    def test_row_never_synced_is_withheld(self):
        """`bunny_status` is null until `sync_demo_videos` runs. Unknown has to
        mean hidden, or pasting a guid publishes something unverified."""
        self._make(bunny_status=None)
        self.assertEqual(self.client.get(self.url).json(), [])

    def test_finished_row_is_served(self):
        self._make(bunny_status=DemoVideo.BUNNY_FINISHED)
        self.assertEqual(len(self.client.get(self.url).json()), 1)

    def test_draft_and_archived_are_withheld(self):
        self._make(key="a", status=PublishStatus.DRAFT)
        self._make(key="b", status=PublishStatus.ARCHIVED)
        self._make(key="c", status=PublishStatus.REVIEW)
        self.assertEqual(self.client.get(self.url).json(), [])

    def test_ordering_follows_order_then_id(self):
        self._make(key="login", title="Log In Demo", order=1)
        self._make(key="signup", title="Sign Up Demo", order=0)
        keys = [r["key"] for r in self.client.get(self.url).json()]
        self.assertEqual(keys, ["signup", "login"])

    def test_duration_is_exposed_and_may_be_null(self):
        self._make(duration_seconds=54)
        self.assertEqual(self.client.get(self.url).json()[0]["duration_seconds"], 54)
        cache.clear()
        DemoVideo.objects.update(duration_seconds=None)
        self.assertIsNone(self.client.get(self.url).json()[0]["duration_seconds"])

    @override_settings(BUNNY_LIBRARY_ID=None)
    def test_nothing_is_served_when_the_library_is_unconfigured(self):
        """Without BUNNY_LIBRARY_ID every embed_url() is None, so serving the
        rows would give the frontend a menu of videos that cannot play."""
        self._make()
        self.assertEqual(self.client.get(self.url).json(), [])

    def test_editing_a_row_busts_the_list_cache(self):
        """DemoVideo has to be in content/cache.py's tracked tuple, or an
        admin's edit is invisible for up to LIST_TTL (300s)."""
        video = self._make(title="Old title")
        self.assertEqual(self.client.get(self.url).json()[0]["title"], "Old title")
        video.title = "New title"
        video.save()
        self.assertEqual(self.client.get(self.url).json()[0]["title"], "New title")


class DemoVideoEmbedUrlTests(TestCase):
    """`embed_url()` guards the environment, because BUNNY_LIBRARY_ID has no
    default in settings_base and is None when unset — which would otherwise
    build a real-looking iframe src pointing at `.../None/<guid>`."""

    @override_settings(BUNNY_LIBRARY_ID=None, BUNNY_EMBED=EMBED)
    def test_none_when_library_id_unset(self):
        video = DemoVideo(key="signup", title="t", bunny_video_id="guid")
        self.assertIsNone(video.embed_url())

    @override_settings(BUNNY_LIBRARY_ID="12345", BUNNY_EMBED=EMBED)
    def test_none_when_no_guid(self):
        self.assertIsNone(DemoVideo(key="signup", title="t").embed_url())

    @override_settings(BUNNY_LIBRARY_ID="12345", BUNNY_EMBED=EMBED)
    def test_url_is_unsigned(self):
        """Deliberately unsigned: the list endpoint is cached for 300s and a
        signed URL expires in 4h, so a signed URL would eventually be served
        past its own expiry. Assert there is no token, so switching to signed
        URLs has to be a deliberate change that breaks this test."""
        video = DemoVideo(key="signup", title="t", bunny_video_id="guid")
        url = video.embed_url()
        self.assertNotIn("token=", url)
        self.assertNotIn("expires=", url)


class SyncDemoVideosCommandTests(TestCase):
    """The command owns duration and thumbnail. The mockup this feature came
    from hardcoded 0:40/0:28 for clips that were really 0:54/0:18 — reading
    from Bunny is what stops a label drifting from its file."""

    def setUp(self):
        self.video = DemoVideo.objects.create(
            key="signup", title="Sign Up Demo", bunny_video_id="guid-signup",
        )

    def _run(self, **kw):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("sync_demo_videos", stdout=out, stderr=out, **kw)
        return out.getvalue()

    @patch("content.management.commands.sync_demo_videos.fetch_bunny_video")
    def test_records_bunny_status_even_when_unfinished(self, fetch):
        """The list endpoint gates on this, so a row that never finishes must
        be storable as "not finished" rather than left null and ambiguous."""
        fetch.return_value = {"status": 2, "length": 0}
        self._run()
        self.video.refresh_from_db()
        self.assertEqual(self.video.bunny_status, 2)
        self.assertFalse(self.video.is_playable)

    @patch("content.management.commands.sync_demo_videos.fetch_bunny_video")
    def test_status_regression_hides_a_previously_live_clip(self, fetch):
        """A clip that was Finished and later is not must go back to hidden,
        not keep serving on a stale value."""
        DemoVideo.objects.update(bunny_status=4, duration_seconds=54)
        fetch.return_value = {"status": 5, "length": 54}   # Error
        self._run()
        self.video.refresh_from_db()
        self.assertEqual(self.video.bunny_status, 5)
        self.assertFalse(self.video.is_playable)

    @override_settings(BUNNY_CDN_HOST="cdn.example.net")
    @patch("content.management.commands.sync_demo_videos.fetch_bunny_video")
    def test_writes_duration_and_thumbnail(self, fetch):
        fetch.return_value = {
            "status": 4, "length": 54, "thumbnailFileName": "thumb.jpg",
        }
        self._run()
        self.video.refresh_from_db()
        self.assertEqual(self.video.bunny_status, 4)
        self.assertEqual(self.video.duration_seconds, 54)
        self.assertEqual(
            self.video.thumbnail_url,
            "https://cdn.example.net/guid-signup/thumb.jpg",
        )

    @patch("content.management.commands.sync_demo_videos.fetch_bunny_video")
    def test_length_zero_means_unknown_not_zero(self, fetch):
        """Bunny reports length 0 until processing finishes. Storing that
        would publish a '0:00' label on a real video."""
        fetch.return_value = {"status": 3, "length": 0}
        out = self._run()
        self.video.refresh_from_db()
        self.assertIsNone(self.video.duration_seconds)
        self.assertIn("still processing", out)

    @patch("content.management.commands.sync_demo_videos.fetch_bunny_video")
    def test_unreachable_bunny_leaves_existing_values_alone(self, fetch):
        """None means 'we learned nothing', never 'the video is gone'. A
        network blip must not blank a good duration."""
        DemoVideo.objects.update(duration_seconds=54)
        fetch.return_value = None
        out = self._run()
        self.video.refresh_from_db()
        self.assertEqual(self.video.duration_seconds, 54)
        self.assertIn("unreachable", out)

    @patch("content.management.commands.sync_demo_videos.fetch_bunny_video")
    def test_dry_run_writes_nothing(self, fetch):
        fetch.return_value = {"status": 4, "length": 54}
        out = self._run(dry_run=True)
        self.video.refresh_from_db()
        self.assertIsNone(self.video.duration_seconds)
        self.assertIn("Dry run", out)

    @patch("content.management.commands.sync_demo_videos.fetch_bunny_video")
    def test_skips_rows_with_no_guid(self, fetch):
        DemoVideo.objects.update(bunny_video_id="")
        out = self._run()
        fetch.assert_not_called()
        self.assertIn("No demo videos", out)
