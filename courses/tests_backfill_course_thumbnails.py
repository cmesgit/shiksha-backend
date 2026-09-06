"""`backfill_course_thumbnails` — give courses the picture the homepage shows.

The bug this closes, measured against production on 2026-09-06: all 26 courses
had `Course.thumbnail = NULL` while 18 of 19 featured cards rendered a real
photo. Both surfaces were behaving correctly — the artwork simply sat on the
showcase CARD, and only the homepage has a fallback chain that reaches it.

    /courses          reads Course.thumbnail, and ONLY that.
    homepage featured reads Course.thumbnail -> Board.logo -> card.image -> ...

So the catalog had nothing to show. Every other field already agreed exactly;
the image was the last mismatch between the two surfaces.
"""
from io import BytesIO, StringIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase

from content.models import PublishStatus, ShowcaseCourse

from .models import Board, Course


def an_image(name="pic.png"):
    """A real (if tiny) photo.

    NOT the 1x1 PNG used elsewhere in this suite: `Course.thumbnail` is a
    ProcessedImageField whose ResizeToFill(1200, 675) has to actually run, and
    upscaling a single pixel that far raises "broken data stream" inside
    pilkit. 64x36 is small enough to keep the suite fast and real enough to
    survive the resize.
    """
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (64, 36), (40, 120, 90)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def run(*args):
    out = StringIO()
    call_command("backfill_course_thumbnails", *args, stdout=out, stderr=out)
    return out.getvalue()


class BackfillCourseThumbnailsTest(TestCase):
    def _card_on(self, course, **kw):
        kw.setdefault("title", "card")
        kw.setdefault("order", 0)
        kw.setdefault("status", PublishStatus.PUBLISHED)
        kw.setdefault("image", an_image())
        return ShowcaseCourse.objects.create(course=course, **kw)

    # -- the default is a report, not a write ----------------------------

    def test_a_dry_run_writes_nothing(self):
        course = Course.objects.create(title="Class 8")
        self._card_on(course)

        out = run()

        course.refresh_from_db()
        self.assertFalse(course.thumbnail, "dry run must not write")
        self.assertIn("Dry run", out)

    def test_a_dry_run_still_reports_the_twins_it_would_reach(self):
        """Stage 2's donors are what stage 1 just wrote. On a dry run nothing
        was written, so without folding in stage 1's intent the dry run claims
        every twin keeps the placeholder — the opposite of what --yes does."""
        cbse = Board.objects.create(name="CBSE")
        mbse = Board.objects.create(name="MBSE")
        featured = Course.objects.create(title="Class 8", board=cbse)
        Course.objects.create(title="Class 8", board=mbse)
        self._card_on(featured)

        out = run()

        self.assertIn("shares with the same title", out)
        self.assertIn("would copy 1 from cards, 1 shared", out)

    def test_a_dry_run_does_not_report_the_same_course_twice(self):
        """Stage 1's courses still look bare to stage 2 on a dry run, so they
        were reported a second time as their own would-be donor — once as
        "keeps the placeholder" and once as a copy. The counts were wrong and
        the report contradicted itself. Caught by running it, not by a test."""
        cbse = Board.objects.create(name="CBSE")
        mbse = Board.objects.create(name="MBSE")
        featured = Course.objects.create(title="Class 8", board=cbse)
        Course.objects.create(title="Class 8", board=mbse)
        self._card_on(featured)

        out = run()

        self.assertNotIn("keeps the placeholder", out)
        self.assertIn("0 skipped", out)

    # -- stage 1: card -> its linked course -------------------------------

    def test_it_copies_the_cards_picture_onto_the_course(self):
        course = Course.objects.create(title="Class 8")
        self._card_on(course)

        run("--yes")

        course.refresh_from_db()
        self.assertTrue(course.thumbnail, "the catalog reads this field")

    def test_the_copy_is_re_encoded_by_the_fields_processor(self):
        """`Course.thumbnail` is a ProcessedImageField (1200x675 WEBP). That
        processing only runs for an UNCOMMITTED file, i.e. on assignment then
        save. Calling `thumbnail.save(...)` writes raw bytes and silently skips
        the resize, leaving a file whose .webp name lies about its contents."""
        course = Course.objects.create(title="Class 8")
        self._card_on(course)

        run("--yes")

        course.refresh_from_db()
        self.assertTrue(course.thumbnail.name.endswith(".webp"))
        self.assertEqual((course.thumbnail.width, course.thumbnail.height), (1200, 675))

    def test_both_surfaces_then_resolve_to_the_same_file(self):
        """The whole point. The featured chain prefers Course.thumbnail, so
        once it is set the homepage and the catalog stop merely showing
        similar pictures and show the identical one."""
        from django.core.cache import cache
        from rest_framework.test import APIClient

        course = Course.objects.create(title="Class 8", status=Course.STATUS_PUBLISHED)
        self._card_on(course)
        run("--yes")
        cache.clear()

        client = APIClient()
        card = client.get("/api/courses/public/featured/").data["cards"][0]
        row = next(
            r for r in client.get("/api/courses/public/catalog/").data
            if r["id"] == str(course.id)
        )

        self.assertTrue(card["thumbnail"])
        self.assertEqual(card["thumbnail"], row["thumbnail"])

    # -- create-only, and idempotent --------------------------------------

    def test_it_never_overwrites_artwork_that_is_already_there(self):
        course = Course.objects.create(title="Class 8", thumbnail=an_image("editors.png"))
        before = course.thumbnail.name
        self._card_on(course)

        run("--yes")

        course.refresh_from_db()
        self.assertEqual(course.thumbnail.name, before)

    def test_a_second_run_finds_nothing_to_do(self):
        course = Course.objects.create(title="Class 8")
        self._card_on(course)
        run("--yes")
        course.refresh_from_db()
        first = course.thumbnail.name

        out = run("--yes")

        course.refresh_from_db()
        self.assertEqual(course.thumbnail.name, first)
        self.assertIn("copied 0 from cards", out)

    # -- the use_own_details carve-out ------------------------------------

    def test_a_card_that_opted_out_of_its_link_is_skipped(self):
        """`use_own_details` means the homepage keeps showing the CARD's
        picture whatever the course has. Copying it to the course would put a
        different picture on the catalog and leave the two surfaces
        disagreeing — the exact thing this command exists to end."""
        course = Course.objects.create(title="Class 8")
        self._card_on(course, use_own_details=True)

        out = run("--yes")

        course.refresh_from_db()
        self.assertFalse(course.thumbnail)
        self.assertIn("use_own_details", out)

    def test_a_card_with_no_uploaded_image_is_skipped(self):
        course = Course.objects.create(title="Class 8")
        self._card_on(course, image=None)

        run("--yes")

        course.refresh_from_db()
        self.assertFalse(course.thumbnail)

    # -- stage 2: the twin on the other board ------------------------------

    def test_a_same_titled_course_on_another_board_shares_the_photo(self):
        """Prod carries each class under two boards. Only the CBSE one has a
        featured card, so without this the catalog is half-illustrated."""
        cbse = Board.objects.create(name="CBSE")
        mbse = Board.objects.create(name="MBSE")
        featured = Course.objects.create(title="Class 8", board=cbse)
        twin = Course.objects.create(title="Class 8", board=mbse)
        self._card_on(featured)

        run("--yes")

        twin.refresh_from_db()
        self.assertTrue(twin.thumbnail, "the MBSE twin must not stay bare")

    def test_no_twins_leaves_the_other_board_alone(self):
        cbse = Board.objects.create(name="CBSE")
        mbse = Board.objects.create(name="MBSE")
        featured = Course.objects.create(title="Class 8", board=cbse)
        twin = Course.objects.create(title="Class 8", board=mbse)
        self._card_on(featured)

        run("--yes", "--no-twins")

        twin.refresh_from_db()
        self.assertFalse(twin.thumbnail)

    def test_a_course_with_no_same_titled_sibling_is_reported_not_invented(self):
        Course.objects.create(title="Lonely course")

        out = run("--yes")

        self.assertIn("keeps the placeholder", out)
        self.assertIn("still have no picture", out)

    # -- the library learns about it --------------------------------------

    def test_the_copied_picture_lands_in_the_media_library(self):
        """A course thumbnail is now an owned image field, so the post_save
        signal must register it — otherwise the CMS still cannot see the most
        load-bearing picture on the site."""
        from content.models import MediaUsage

        course = Course.objects.create(title="Class 8")
        self._card_on(course)

        run("--yes")

        usage = MediaUsage.objects.get(field_name="thumbnail")
        self.assertEqual(usage.object_id, str(course.pk))
        self.assertEqual(usage.target, course)


class CourseThumbnailFromTheLibraryTest(TestCase):
    """Picking an existing CMS picture for a course.

    The point is ONE library: the course must POINT AT the picture the CMS
    already holds, not get a private copy of it. A copy would put a second
    physical file in storage and a second row in the Pictures screen for one
    picture — the "two libraries" outcome the media work exists to avoid.
    """

    URL = "/api/courses/admin/courses/"

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        User = get_user_model()
        self.admin = User.objects.create_user(
            username="a", email="a@example.com", password="x",
            is_staff=True, is_superuser=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _asset(self):
        from content.models import ContentImage
        return ContentImage.objects.create(
            file=an_image("library-pic.png"), original_name="library-pic.png",
        )

    def test_picking_a_library_picture_points_at_the_same_file(self):
        from content.models import ContentImage

        asset = self._asset()
        course = Course.objects.create(title="Class 8")
        before = ContentImage.objects.count()

        res = self.client.patch(
            f"{self.URL}{course.id}/", {"thumbnail_asset_id": asset.id}, format="json",
        )

        self.assertEqual(res.status_code, 200, res.content)
        course.refresh_from_db()
        self.assertEqual(course.thumbnail.name, asset.file.name)
        self.assertEqual(
            ContentImage.objects.count(), before,
            "picking must not mint a second library row for one picture",
        )

    def test_the_library_then_reports_the_course_as_a_user_of_it(self):
        from content.models import MediaUsage

        asset = self._asset()
        course = Course.objects.create(title="Class 8")

        self.client.patch(
            f"{self.URL}{course.id}/", {"thumbnail_asset_id": asset.id}, format="json",
        )

        usage = MediaUsage.objects.get(field_name="thumbnail")
        self.assertEqual(usage.asset_id, asset.id)
        self.assertEqual(usage.target, course)

    def test_deleting_that_library_picture_is_then_refused(self):
        """The whole reason usages are tracked: an editor must not be able to
        blank a live course's card from the Pictures screen."""
        asset = self._asset()
        course = Course.objects.create(title="Class 11 Science")
        self.client.patch(
            f"{self.URL}{course.id}/", {"thumbnail_asset_id": asset.id}, format="json",
        )

        res = self.client.delete(f"/api/content/admin/media/{asset.id}/")

        self.assertEqual(res.status_code, 409, res.content)
        self.assertIn("Class 11 Science", res.json()["detail"])

    def test_an_unknown_asset_id_is_a_readable_400(self):
        course = Course.objects.create(title="Class 8")
        res = self.client.patch(
            f"{self.URL}{course.id}/", {"thumbnail_asset_id": 999999}, format="json",
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("media library", str(res.json()))

    def test_an_empty_asset_id_clears_the_picture(self):
        asset = self._asset()
        course = Course.objects.create(title="Class 8")
        self.client.patch(
            f"{self.URL}{course.id}/", {"thumbnail_asset_id": asset.id}, format="json",
        )

        self.client.patch(
            f"{self.URL}{course.id}/", {"thumbnail_asset_id": ""}, format="json",
        )

        course.refresh_from_db()
        self.assertFalse(course.thumbnail)

    def test_omitting_the_key_leaves_the_picture_alone(self):
        """"absent" and "empty" must not mean the same thing, or every unrelated
        edit to a course would silently wipe its picture."""
        asset = self._asset()
        course = Course.objects.create(title="Class 8")
        self.client.patch(
            f"{self.URL}{course.id}/", {"thumbnail_asset_id": asset.id}, format="json",
        )

        self.client.patch(f"{self.URL}{course.id}/", {"title": "Renamed"}, format="json")

        course.refresh_from_db()
        self.assertEqual(course.thumbnail.name, asset.file.name)

    def test_a_real_file_upload_still_works(self):
        """The library path is additive — it must not take away the plain
        upload the course form has always had."""
        course = Course.objects.create(title="Class 8")
        res = self.client.patch(
            f"{self.URL}{course.id}/",
            {"thumbnail": an_image("fresh.png")}, format="multipart",
        )
        self.assertEqual(res.status_code, 200, res.content)
        course.refresh_from_db()
        self.assertTrue(course.thumbnail)
        self.assertEqual((course.thumbnail.width, course.thumbnail.height), (1200, 675))
