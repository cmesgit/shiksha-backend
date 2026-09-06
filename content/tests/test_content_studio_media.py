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
        # The Studio permission caches content_studio_enabled. Django rolls the
        # DB back between tests but NOT the cache, so a test that flips the flag
        # off would otherwise leak a cached False into every test after it.
        from django.core.cache import cache

        from content.permissions import IsStudioEditor
        cache.delete(IsStudioEditor.CACHE_KEY)
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

    def _course_migration(self):
        from importlib import import_module
        return import_module("content.migrations.0032_backfill_course_media_usages")

    def test_every_owned_field_is_covered_by_some_backfill(self):
        """The migrations hardcode their own copies (historical models can't
        import the live one). If they drift, the backfill silently misses a
        field — that field's pictures then report "used on 0 pages" forever and
        the 409 delete guard cannot protect them.

        The backfill is split across two migrations because the `courses`
        fields could not be added until 0031 widened `object_id` to hold their
        UUID primary keys. The invariant is the UNION, not either list alone.
        """
        from content.media import OWNED_IMAGE_FIELDS

        covered = [
            *self._migration().OWNED_IMAGE_FIELDS,
            *self._course_migration().COURSE_IMAGE_FIELDS,
        ]
        self.assertEqual(covered, OWNED_IMAGE_FIELDS)

    def test_the_course_backfill_runs_after_object_id_was_widened(self):
        """Course and Board have UUID PKs. Backfilling them against the
        original PositiveIntegerField `object_id` cannot work, so 0032 must
        depend on 0031 — ordering is the only thing making that unreachable."""
        deps = self._course_migration().Migration.dependencies
        self.assertIn(("content", "0031_alter_mediausage_object_id"), deps)

    def test_it_names_the_real_cover_field(self):
        """The handoff spec says BlogPost.cover_image. It is BlogPost.cover —
        naming it wrong makes the backfill find nothing and report success."""
        names = [f for (_, m, f) in self._migration().OWNED_IMAGE_FIELDS if m == "BlogPost"]
        self.assertEqual(names, ["cover"])
        self.assertTrue(hasattr(BlogPost, "cover"))


class MediaPaginationTest(MediaTestCase):
    """`count` used to be len(results) after a hard qs[:200] slice, so the
    library presented itself as complete at exactly 200 and everything past
    that was unreachable — and undeletable — through the UI."""

    def test_count_is_the_real_total_not_the_page_length(self):
        for i in range(7):
            ContentImage.objects.create(file=an_image(), original_name=f"{i}.png")

        body = self.client_for(self.editor).get(
            MEDIA_URL, {"page_size": 3},
        ).json()

        self.assertEqual(len(body["results"]), 3)
        self.assertEqual(body["count"], 7)
        self.assertTrue(body["has_more"])

    def test_later_pages_are_reachable(self):
        for i in range(7):
            ContentImage.objects.create(file=an_image(), original_name=f"{i}.png")
        c = self.client_for(self.editor)

        seen = []
        for page in (1, 2, 3):
            body = c.get(MEDIA_URL, {"page_size": 3, "page": page}).json()
            seen += [a["id"] for a in body["results"]]

        self.assertEqual(len(seen), 7)
        self.assertEqual(len(set(seen)), 7, "pages overlapped")
        self.assertFalse(
            c.get(MEDIA_URL, {"page_size": 3, "page": 3}).json()["has_more"],
        )

    def test_a_junk_page_param_does_not_500(self):
        ContentImage.objects.create(file=an_image(), original_name="a.png")
        res = self.client_for(self.editor).get(
            MEDIA_URL, {"page": "banana", "page_size": "-4"},
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["count"], 1)


class UsageLinkTest(MediaTestCase):
    def test_each_usage_carries_a_link_to_where_it_is_used(self):
        """The refusal dialog had one hardcoded destination for every usage, so
        a picture used as a blog cover sent the editor to the homepage tab."""
        from content.models import BlogPost

        asset = ContentImage.objects.create(file=an_image(), original_name="c.png")
        post = BlogPost.objects.create(title="A post", cover=asset.file.name)

        body = self.client_for(self.editor).get(MEDIA_URL).json()
        row = next(a for a in body["results"] if a["id"] == asset.id)
        self.assertEqual(row["usage_count"], 1)
        self.assertEqual(row["used_in"][0]["url"], f"/content/blogs/{post.id}")


class EmbeddedPictureUsageTest(MediaTestCase):
    """A picture used only inside a post's body still counts as used.

    Before this, MediaUsage tracked the four cover/hero FileFields only, so a
    picture embedded in body_html reported "used on 0 pages" and deleted
    cleanly — leaving a broken image on a published post, which is the exact
    breakage the delete guard exists to prevent.
    """

    def _asset(self, name="diagram.png"):
        return ContentImage.objects.create(file=an_image(name), original_name=name)

    def test_a_picture_in_the_body_is_counted_as_used(self):
        from content.models import BlogPost

        asset = self._asset()
        BlogPost.objects.create(
            title="Uses it inline",
            body_html=f'<p>See this</p><img src="/media/{asset.file.name}">',
        )
        asset.refresh_from_db()
        self.assertEqual(asset.usages.count(), 1)
        self.assertEqual(asset.usages.first().field_name, "body_html")

    def test_deleting_it_is_refused_and_names_the_post(self):
        from content.models import BlogPost

        asset = self._asset()
        BlogPost.objects.create(
            title="A published post",
            status=PublishStatus.PUBLISHED,
            body_html=f'<img src="/media/{asset.file.name}">',
        )
        res = self.client_for(self.editor).delete(f"{MEDIA_URL}{asset.pk}/")
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.json()["used_in"][0]["title"], "A published post")

    def test_removing_it_from_the_body_releases_it(self):
        """Without the stale-removal half, a picture could never be deleted
        again once it had been embedded even briefly."""
        from content.models import BlogPost

        asset = self._asset()
        post = BlogPost.objects.create(
            title="Briefly", body_html=f'<img src="/media/{asset.file.name}">',
        )
        self.assertEqual(asset.usages.count(), 1)

        post.body_html = "<p>The picture is gone now.</p>"
        post.save()

        self.assertEqual(asset.usages.count(), 0)
        res = self.client_for(self.editor).delete(f"{MEDIA_URL}{asset.pk}/")
        self.assertEqual(res.status_code, 204, res.content)

    def test_an_absolute_cdn_url_still_matches(self):
        """Bunny rewrites the prefix, so the stored path and the URL in the
        body share only the filename."""
        from content.models import BlogPost

        asset = self._asset("photosynthesis.png")
        base = asset.file.name.rsplit("/", 1)[-1]
        BlogPost.objects.create(
            title="From the CDN",
            body_html=f'<img src="https://cdn.example.net/whatever/{base}">',
        )
        self.assertEqual(asset.usages.count(), 1)

    def test_block_bodies_are_scanned_too(self):
        from content.models import BlogPost

        asset = self._asset()
        BlogPost.objects.create(
            title="Block editor post",
            body_blocks=[{"type": "image", "src": f"/media/{asset.file.name}"}],
        )
        self.assertEqual(
            asset.usages.filter(field_name="body_blocks").count(), 1,
        )

    def test_an_unknown_url_creates_nothing(self):
        """An external or hand-written URL is not a library picture, and
        inventing a row for it would put files nobody uploaded into the
        Pictures screen."""
        from content.models import BlogPost, ContentImage

        before = ContentImage.objects.count()
        BlogPost.objects.create(
            title="External image",
            body_html='<img src="https://example.com/not-ours.png">',
        )
        self.assertEqual(ContentImage.objects.count(), before)


class CourseArtworkInTheLibraryTest(MediaTestCase):
    """Course and Board pictures belong in the same library as everything else.

    `Course.thumbnail` is the picture BOTH public surfaces read — /courses
    reads it directly and the homepage's featured grid prefers it ahead of the
    showcase card's own image — yet the library had never heard of it. It
    reported no usage count and the 409 delete guard could not protect it.

    These owners have UUID primary keys, which is why `MediaUsage.object_id`
    had to stop being a PositiveIntegerField before any of this could work.
    """

    @staticmethod
    def a_photo(name="pic.png"):
        """NOT the module's 1x1 PNG. `Course.thumbnail` and `Board.logo` are
        ProcessedImageFields whose ResizeToFill(1200, 675) actually runs, and
        upscaling a single pixel that far raises "broken data stream" inside
        pilkit."""
        from io import BytesIO

        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (64, 36), (40, 120, 90)).save(buf, format="PNG")
        return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")

    def _course(self, title="Class 8", **kw):
        from courses.models import Course
        return Course.objects.create(title=title, **kw)

    def test_a_course_thumbnail_is_recorded_as_a_usage(self):
        course = self._course(thumbnail=self.a_photo())

        usage = MediaUsage.objects.get(field_name="thumbnail")
        self.assertEqual(usage.target, course)
        self.assertEqual(usage.object_id, str(course.pk))

    def test_object_id_holds_a_uuid_not_an_integer(self):
        """The regression this whole change turns on: a UUID primary key does
        not fit a PositiveIntegerField, so before 0031 this could not be
        recorded at all."""
        course = self._course(thumbnail=self.a_photo())

        usage = MediaUsage.objects.get(field_name="thumbnail")
        self.assertEqual(usage.object_id, str(course.pk))
        self.assertIn("-", usage.object_id, "a UUID, not a stringified int")

    def test_deleting_a_picture_a_course_uses_is_refused_by_name(self):
        self._course(title="Class 11 Science", thumbnail=self.a_photo())
        asset = MediaUsage.objects.get(field_name="thumbnail").asset

        res = self.client_for(self.editor).delete(f"{MEDIA_URL}{asset.pk}/")

        self.assertEqual(res.status_code, 409, res.content)
        self.assertIn("Class 11 Science", res.json()["detail"])
        self.assertEqual(res.json()["used_in"][0]["url"], "/courses")
        self.assertTrue(ContentImage.objects.filter(pk=asset.pk).exists())

    def test_a_board_usage_is_named_by_its_name_field(self):
        """Board's label field is `name`, not `title`. Without `name` in the
        candidate list the dialog reads 'Board #<uuid>', which tells an editor
        nothing about what they would break."""
        from courses.models import Board
        Board.objects.create(name="MBSE", logo=self.a_photo())

        asset = MediaUsage.objects.get(field_name="logo").asset
        self.assertEqual(usage_payload(asset)[0]["title"], "MBSE")

    def test_swapping_a_course_thumbnail_releases_the_old_picture(self):
        course = self._course(thumbnail=self.a_photo("first.png"))
        first = MediaUsage.objects.get(field_name="thumbnail").asset

        course.thumbnail = self.a_photo("second.png")
        course.save()

        self.assertEqual(first.usages.count(), 0, "the old one must be released")
        self.assertEqual(MediaUsage.objects.filter(field_name="thumbnail").count(), 1)

    def test_deleting_the_course_releases_its_picture(self):
        """`_on_owner_deleted` filters on object_id. Passing a raw UUID there
        matches nothing, which would strand the row and make the picture
        permanently undeletable."""
        course = self._course(thumbnail=self.a_photo())
        asset = MediaUsage.objects.get(field_name="thumbnail").asset

        course.delete()

        self.assertEqual(asset.usages.count(), 0)
        res = self.client_for(self.editor).delete(f"{MEDIA_URL}{asset.pk}/")
        self.assertEqual(res.status_code, 204, res.content)
