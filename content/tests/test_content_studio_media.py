"""Content Studio Phase 4 — the media library.

The point of this phase is a fact nobody could establish before: where a
picture is used. These tests are mostly about that fact staying true when an
image is swapped out or its owner is deleted.
"""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from content.media import sync_usages_for, usage_payload
from content.models import (
    BlogPost, ContentImage, HomeContentBlock, MediaUsage, PublishStatus,
)

User = get_user_model()

MEDIA_URL = "/api/content/admin/media/"

# A one-pixel PNG — small enough to keep the suite fast, real enough that
# ImageField's dimension probing succeeds.
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "05570c1a250000000049454e44ae426082"
)


def an_image(name="pic.png"):
    return SimpleUploadedFile(name, PNG_1PX, content_type="image/png")


class MediaTestCase(TestCase):
    def setUp(self):
        self.editor = User.objects.create_user(
            username="ed", email="ed@example.com", password="x", is_staff=True,
        )
        self.outsider = User.objects.create_user(
            username="l", email="l@example.com", password="x",
        )

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c


class UsageTrackingTest(MediaTestCase):
    def test_attaching_an_image_records_a_usage(self):
        post = BlogPost.objects.create(
            title="P", slug="a/p", status=PublishStatus.PUBLISHED,
            cover=an_image(),
        )
        self.assertEqual(MediaUsage.objects.count(), 1)
        usage = MediaUsage.objects.first()
        self.assertEqual(usage.target, post)
        self.assertEqual(usage.field_name, "cover")

    def test_the_same_picture_on_two_owners_counts_twice(self):
        """'Used on 2 pages' is the whole reason this screen exists."""
        post = BlogPost.objects.create(
            title="P", slug="a/p", status=PublishStatus.PUBLISHED, cover=an_image(),
        )
        asset = MediaUsage.objects.get(object_id=post.pk).asset

        block = HomeContentBlock.objects.create(section="hero")
        block.image.name = asset.file.name
        block.save()
        sync_usages_for(block)

        self.assertEqual(asset.usages.count(), 2)
        self.assertEqual(len(usage_payload(asset)), 2)

    def test_swapping_an_image_out_drops_the_old_usage(self):
        """Without the removal half, the old picture reports a usage forever
        and can never be deleted."""
        post = BlogPost.objects.create(
            title="P", slug="a/p", status=PublishStatus.PUBLISHED,
            cover=an_image("first.png"),
        )
        first = MediaUsage.objects.get(object_id=post.pk).asset

        post.cover = an_image("second.png")
        post.save()

        self.assertEqual(first.usages.count(), 0)
        self.assertEqual(MediaUsage.objects.filter(object_id=post.pk).count(), 1)

    def test_clearing_an_image_drops_the_usage(self):
        post = BlogPost.objects.create(
            title="P", slug="a/p", status=PublishStatus.PUBLISHED, cover=an_image(),
        )
        post.cover = ""
        post.save()
        self.assertEqual(MediaUsage.objects.count(), 0)

    def test_deleting_the_owner_drops_its_usages(self):
        post = BlogPost.objects.create(
            title="P", slug="a/p", status=PublishStatus.PUBLISHED, cover=an_image(),
        )
        post.delete()
        self.assertEqual(MediaUsage.objects.count(), 0)

    def test_usage_survives_a_save_that_does_not_touch_the_image(self):
        post = BlogPost.objects.create(
            title="P", slug="a/p", status=PublishStatus.PUBLISHED, cover=an_image(),
        )
        post.title = "Renamed"
        post.save()
        self.assertEqual(MediaUsage.objects.count(), 1)


class MediaApiTest(MediaTestCase):
    def test_list_reports_usage_count_and_where(self):
        BlogPost.objects.create(
            title="Cover story", slug="a/p", status=PublishStatus.PUBLISHED,
            cover=an_image(),
        )
        body = self.client_for(self.editor).get(MEDIA_URL).json()
        self.assertEqual(body["count"], 1)
        asset = body["results"][0]
        self.assertEqual(asset["usage_count"], 1)
        self.assertEqual(asset["used_in"][0]["title"], "Cover story")
        self.assertEqual(asset["used_in"][0]["field"], "cover")

    def test_upload_creates_an_asset_with_its_real_filename(self):
        res = self.client_for(self.editor).post(
            MEDIA_URL, {"file": an_image("holiday-banner.png")}, format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["name"], "holiday-banner.png")
        self.assertEqual(res.json()["usage_count"], 0)

    def test_upload_without_a_file_is_a_readable_400(self):
        res = self.client_for(self.editor).post(MEDIA_URL, {}, format="multipart")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Choose a picture", res.json()["detail"])

    def test_an_unused_picture_can_be_deleted(self):
        asset = ContentImage.objects.create(file=an_image(), original_name="x.png")
        res = self.client_for(self.editor).delete(f"{MEDIA_URL}{asset.pk}/")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(ContentImage.objects.filter(pk=asset.pk).exists())

    def test_deleting_a_picture_in_use_is_refused_with_what_uses_it(self):
        BlogPost.objects.create(
            title="Cover story", slug="a/p", status=PublishStatus.PUBLISHED,
            cover=an_image(),
        )
        asset = MediaUsage.objects.first().asset

        res = self.client_for(self.editor).delete(f"{MEDIA_URL}{asset.pk}/")
        self.assertEqual(res.status_code, 409, res.content)
        self.assertIn("Cover story", res.json()["detail"])
        self.assertEqual(res.json()["used_in"][0]["title"], "Cover story")
        self.assertTrue(
            ContentImage.objects.filter(pk=asset.pk).exists(),
            "a refused delete must not delete anything",
        )

    def test_search_matches_the_original_filename(self):
        ContentImage.objects.create(file=an_image(), original_name="diwali-hero.png")
        ContentImage.objects.create(file=an_image(), original_name="other.png")
        body = self.client_for(self.editor).get(MEDIA_URL, {"q": "diwali"}).json()
        self.assertEqual([a["name"] for a in body["results"]], ["diwali-hero.png"])

    def test_non_staff_is_refused(self):
        self.assertEqual(
            self.client_for(self.outsider).get(MEDIA_URL).status_code, 403,
        )


class LegacyEditorImagesRouteTest(MediaTestCase):
    def test_the_old_upload_route_still_resolves(self):
        """The blog block editor uploads through admin/editor-images. Widening
        the library must not take that away — they are two views over one
        table, not two libraries."""
        res = self.client_for(self.editor).get("/api/content/admin/editor-images/")
        self.assertEqual(res.status_code, 200, res.content)


class BackfillMigrationTest(MediaTestCase):
    """0023 walks the owning fields and builds the library from what exists."""

    def _migration(self):
        from importlib import import_module
        return import_module("content.migrations.0023_backfill_media_usages")

    def test_owned_fields_list_matches_the_helper(self):
        """The migration hardcodes its own copy (historical models can't import
        the live one). If they drift, the backfill silently misses a field."""
        from content.media import OWNED_IMAGE_FIELDS

        self.assertEqual(self._migration().OWNED_IMAGE_FIELDS, OWNED_IMAGE_FIELDS)

    def test_it_names_the_real_cover_field(self):
        """The handoff spec says BlogPost.cover_image. It is BlogPost.cover —
        naming it wrong makes the backfill find nothing and report success."""
        names = [f for (_, m, f) in self._migration().OWNED_IMAGE_FIELDS if m == "BlogPost"]
        self.assertEqual(names, ["cover"])
        self.assertTrue(hasattr(BlogPost, "cover"))
