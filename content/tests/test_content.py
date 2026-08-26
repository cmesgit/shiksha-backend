# PLACEMENT: backend/content/tests/test_content.py
#
# Run with:  python manage.py test content

import datetime
import shutil
import tempfile
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from content.models import (
    Announcement, BlogPost, ContentTag, CurrentAffair, FAQItem,
    HomeContentBlock, HomeListItem, HomeListVariant, HomeSection,
    PublishStatus, ShowcaseCourse,
)


def make_post(slug="class-9/economics/chapter-1", status=PublishStatus.PUBLISHED,
              publish_at=None, **extra):
    defaults = dict(
        title="Chapter 1: The Story of Village Palampur",
        class_level="9",
        subject="economics",
        chapter_number=1,
        body_html="<h1>Palampur</h1><p>Factors of production.</p>",
        status=status,
        publish_at=publish_at or timezone.now(),
    )
    defaults.update(extra)
    return BlogPost.objects.create(slug=slug, **defaults)


class BlogPostModelTests(TestCase):
    def test_auto_slug_from_taxonomy(self):
        post = BlogPost.objects.create(
            title="Whatever", class_level="10", subject="geography",
            chapter_number=3, body_html="<p>x</p>",
        )
        self.assertEqual(post.slug, "class-10/geography/chapter-3")

    def test_auto_slug_for_general_article(self):
        post = BlogPost.objects.create(
            title="How to prepare for boards", body_html="<p>x</p>",
        )
        self.assertEqual(post.slug, "how-to-prepare-for-boards")

    def test_published_manager_hides_drafts_and_scheduled(self):
        make_post(slug="a/b/c1", chapter_number=None)
        make_post(slug="a/b/c2", chapter_number=None,
                  status=PublishStatus.DRAFT)
        make_post(slug="a/b/c3", chapter_number=None,
                  publish_at=timezone.now() + datetime.timedelta(days=1))
        self.assertEqual(BlogPost.objects.published().count(), 1)

    def test_body_sanitized_unless_trusted(self):
        dirty = '<p onclick="evil()">hi</p><script>alert(1)</script>'
        post = make_post(body_html=dirty)
        self.assertNotIn("<script", post.body_html)
        self.assertNotIn("onclick", post.body_html)
        self.assertIn("hi", post.body_html)

        trusted = make_post(
            slug="class-9/economics/chapter-2", chapter_number=2,
            body_html=dirty, trusted_html=True,
        )
        self.assertIn("<script", trusted.body_html)  # untouched by design

    def test_reading_minutes_computed(self):
        words = " ".join(["word"] * 600)
        post = make_post(body_html=f"<p>{words}</p>")
        self.assertEqual(post.reading_minutes, 3)


class BlogAPITests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.live = make_post()
        make_post(slug="class-9/economics/chapter-2", chapter_number=2,
                  status=PublishStatus.DRAFT)

    def test_list_only_published(self):
        res = self.client.get("/api/content/blogs/")
        self.assertEqual(res.status_code, 200)
        slugs = [row["slug"] for row in res.data["results"]]
        self.assertEqual(slugs, ["class-9/economics/chapter-1"])

    def test_list_filters(self):
        make_post(slug="class-10/science/chapter-1", class_level="10",
                  subject="science", title="Chapter 1: Matter in Our Surroundings")
        res = self.client.get("/api/content/blogs/?class_level=10")
        self.assertEqual(len(res.data["results"]), 1)
        self.assertEqual(res.data["results"][0]["subject"], "science")

        res = self.client.get("/api/content/blogs/?q=palampur")
        self.assertEqual(len(res.data["results"]), 1)

    def test_detail_by_path_slug_and_view_count(self):
        url = "/api/content/blogs/class-9/economics/chapter-1/"
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertIn("<h1>", res.data["body_html"])
        self.live.refresh_from_db()
        self.assertEqual(self.live.view_count, 1)

    def test_detail_404_for_draft(self):
        res = self.client.get("/api/content/blogs/class-9/economics/chapter-2/")
        self.assertEqual(res.status_code, 404)

    def test_list_cache_invalidates_on_edit(self):
        first = self.client.get("/api/content/blogs/")
        self.assertEqual(len(first.data["results"]), 1)
        make_post(slug="class-8/history/chapter-1", class_level="8",
                  subject="history")
        second = self.client.get("/api/content/blogs/")
        self.assertEqual(len(second.data["results"]), 2)


class OtherEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_current_affairs_list_and_detail(self):
        ca = CurrentAffair.objects.create(
            title="Budget highlights", affair_date=timezone.localdate(),
            category="economy", summary="Key points.",
            body_html="<p>Body</p>", status=PublishStatus.PUBLISHED,
        )
        res = self.client.get("/api/content/current-affairs/")
        self.assertEqual(res.data["results"][0]["slug"], ca.slug)
        res = self.client.get(f"/api/content/current-affairs/{ca.slug}/")
        self.assertEqual(res.status_code, 200)

    def test_faq_page_filter(self):
        FAQItem.objects.create(page="home", question="Q1",
                               answer_html="<p>A1</p>", order=0)
        FAQItem.objects.create(page="courses", question="Q2",
                               answer_html="<p>A2</p>", order=0)
        res = self.client.get("/api/content/faqs/?page_key=home")
        self.assertEqual([f["question"] for f in res.data], ["Q1"])

    def test_announcement_live_window(self):
        now = timezone.now()
        Announcement.objects.create(message="live now", starts_at=now)
        Announcement.objects.create(
            message="expired", starts_at=now - datetime.timedelta(days=2),
            ends_at=now - datetime.timedelta(days=1),
        )
        Announcement.objects.create(
            message="future", starts_at=now + datetime.timedelta(days=1),
        )
        res = self.client.get("/api/content/announcements/")
        self.assertEqual([a["message"] for a in res.data], ["live now"])

    def test_showcase_shape(self):
        ShowcaseCourse.objects.create(
            title="Class 10 Foundation", level_label="Foundation",
            price_label="1,500",
            categories=["class8-12"], image_url="https://x/y.jpg",
        )
        res = self.client.get("/api/content/showcase/")
        row = res.data[0]
        self.assertEqual(row["categories"], ["class8-12"])
        self.assertEqual(row["img"], "https://x/y.jpg")


class ImportCommandTests(TestCase):
    def test_import_blog_fragments(self):
        import tempfile
        from pathlib import Path

        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmp:
            frag = Path(tmp) / "class-9" / "economics"
            frag.mkdir(parents=True)
            (frag / "chapter-1.html").write_text(
                "<h1>The Story of Village Palampur</h1>"
                "<p>Farming and non-farm activities.</p>",
                encoding="utf-8",
            )
            call_command("import_blog_fragments", tmp)
            post = BlogPost.objects.get(slug="class-9/economics/chapter-1")
            self.assertEqual(post.class_level, "9")
            self.assertEqual(post.subject, "economics")
            self.assertEqual(post.chapter_number, 1)
            self.assertTrue(post.trusted_html)
            self.assertEqual(post.title, "The Story of Village Palampur")
            self.assertTrue(post.is_live)

            # idempotent: second run skips
            call_command("import_blog_fragments", tmp)
            self.assertEqual(BlogPost.objects.count(), 1)


class HomeListItemImageTests(TestCase):
    """Per-card CMS artwork on HomeListItem (migration 0016).

    Before this, the About hero's sticker row and the Why-Choose card art were
    hardcoded frontend imports, so swapping an image needed a deploy.
    """

    def setUp(self):
        self.client = APIClient()
        cache.clear()

    def test_public_img_falls_back_to_image_url(self):
        HomeListItem.objects.create(
            section=HomeSection.ABOUT_HERO, variant=HomeListVariant.STICKER,
            image_url="https://cdn.example/sticker-1.png", order=0,
        )
        res = self.client.get(
            "/api/content/home-list-items/?section=about_hero"
        )
        self.assertEqual(res.data[0]["img"], "https://cdn.example/sticker-1.png")

    def test_public_img_is_empty_string_when_nothing_set(self):
        # The frontend treats "" as "use my bundled default", so this must not
        # come back as None — `block.img || fallback` has to pick the fallback.
        HomeListItem.objects.create(
            section=HomeSection.ABOUT_WHY, variant=HomeListVariant.NUMBERED,
            title="Interactive Courses", order=0,
        )
        res = self.client.get(
            "/api/content/home-list-items/?section=about_why"
        )
        self.assertEqual(res.data[0]["img"], "")

    def test_uploaded_image_wins_over_image_url(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        # 1x1 transparent GIF — smallest thing ImageField will accept.
        gif = (
            b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
            b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
            b"\x00\x02\x02D\x01\x00;"
        )
        item = HomeListItem.objects.create(
            section=HomeSection.ABOUT_HERO, variant=HomeListVariant.STICKER,
            image_url="https://cdn.example/ignored.png", order=0,
        )
        item.image.save("sticker.gif", SimpleUploadedFile(
            "sticker.gif", gif, content_type="image/gif"), save=True)

        res = self.client.get(
            "/api/content/home-list-items/?section=about_hero"
        )
        img = res.data[0]["img"]
        self.assertIn("sticker", img)
        self.assertNotIn("ignored.png", img)
        item.image.delete(save=False)

    def test_sticker_variant_is_filterable(self):
        HomeListItem.objects.create(
            section=HomeSection.ABOUT_HERO, variant=HomeListVariant.STICKER,
            image_url="https://cdn.example/s.png", order=0,
        )
        HomeListItem.objects.create(
            section=HomeSection.ABOUT_HERO, variant=HomeListVariant.DEFAULT,
            title="not a sticker", order=1,
        )
        res = self.client.get(
            "/api/content/home-list-items/?section=about_hero&variant=sticker"
        )
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]["img"], "https://cdn.example/s.png")


class HomeAdminImagePreviewTests(TestCase):
    """The admin form renders `previewUrl={row?.img}`, so the admin
    serializers must resolve `img` too — they previously exposed only the raw
    `image`/`image_url`, so no saved image ever appeared next to the upload
    control.
    """

    def setUp(self):
        self.client = APIClient()
        cache.clear()
        self.staff = get_user_model().objects.create_user(
            username="cms-editor", email="editor@example.com",
            password="pw12345678", is_staff=True,
        )
        self.client.force_authenticate(self.staff)

    def test_admin_list_item_exposes_resolved_img(self):
        HomeListItem.objects.create(
            section=HomeSection.ABOUT_HERO, variant=HomeListVariant.STICKER,
            image_url="https://cdn.example/s.png", order=0,
        )
        res = self.client.get("/api/content/admin/home-list-items/")
        row = res.data["results"][0]
        self.assertEqual(row["img"], "https://cdn.example/s.png")
        # the writable fields must still be there
        self.assertIn("image", row)
        self.assertIn("image_url", row)

    def test_admin_content_block_exposes_resolved_img(self):
        HomeContentBlock.objects.create(
            section=HomeSection.ABOUT_VISION,
            image_url="https://cdn.example/vision.jpg",
        )
        res = self.client.get("/api/content/admin/home-content/")
        row = res.data["results"][0]
        self.assertEqual(row["img"], "https://cdn.example/vision.jpg")

    def test_admin_can_create_a_sticker_row(self):
        res = self.client.post("/api/content/admin/home-list-items/", {
            "section": "about_hero",
            "variant": "sticker",
            "image_url": "https://cdn.example/new.png",
            "order": 2,
        }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["variant"], "sticker")


class SeedAboutImagesCommandTests(TestCase):
    """`seed_about_images` materialises About2.jsx's bundled artwork into the
    CMS. Mirrors seed_homepage_defaults' safety model: dry run by default,
    create-only, idempotent.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, **kw):
        from django.core.management import call_command
        out = StringIO()
        with override_settings(MEDIA_ROOT=self.tmp):
            call_command("seed_about_images", stdout=out, stderr=out, **kw)
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        HomeContentBlock.objects.create(section=HomeSection.ABOUT_VISION)
        out = self._run()
        self.assertIn("Dry run", out)
        self.assertEqual(
            HomeListItem.objects.filter(variant=HomeListVariant.STICKER).count(), 0
        )
        self.assertFalse(
            HomeContentBlock.objects.get(section=HomeSection.ABOUT_VISION).image
        )

    def test_yes_creates_the_five_stickers_in_design_order(self):
        out = self._run(yes=True)
        self.assertIn("Done.", out)
        rows = list(
            HomeListItem.objects
            .filter(section=HomeSection.ABOUT_HERO,
                    variant=HomeListVariant.STICKER)
            .order_by("order")
        )
        self.assertEqual(len(rows), 5)
        # the row is deliberately 5,2,3,4,1 — not sorted
        self.assertEqual(
            [r.image.name.rsplit("/", 1)[-1].split(".")[0][:9] for r in rows],
            ["sticker_5", "sticker_2", "sticker_3", "sticker_4", "sticker_1"],
        )
        self.assertEqual([r.order for r in rows], [0, 1, 2, 3, 4])

    def test_attaches_block_photos_when_blocks_exist(self):
        HomeContentBlock.objects.create(section=HomeSection.ABOUT_VISION)
        HomeContentBlock.objects.create(section=HomeSection.ABOUT_VALUES)
        self._run(yes=True)
        vision = HomeContentBlock.objects.get(section=HomeSection.ABOUT_VISION)
        values = HomeContentBlock.objects.get(section=HomeSection.ABOUT_VALUES)
        self.assertIn("meet", vision.image.name)
        self.assertIn("studio", values.image.name)

    def test_warns_instead_of_crashing_when_block_missing(self):
        # A fresh DB has no about_vision row at all; the command must say so
        # rather than blowing up or silently creating a partial block.
        out = self._run(yes=True)
        self.assertIn("no about_vision block yet", out)
        self.assertFalse(
            HomeContentBlock.objects.filter(section=HomeSection.ABOUT_VISION).exists()
        )

    def test_is_idempotent_and_never_clobbers_an_editors_upload(self):
        HomeContentBlock.objects.create(section=HomeSection.ABOUT_VISION)
        self._run(yes=True)
        before = HomeContentBlock.objects.get(
            section=HomeSection.ABOUT_VISION).image.name

        out = self._run(yes=True)
        self.assertIn("already has an image, skipped", out)
        self.assertIn("scope skipped", out)
        # no duplicate stickers, and the existing file untouched
        self.assertEqual(
            HomeListItem.objects.filter(variant=HomeListVariant.STICKER).count(), 5
        )
        self.assertEqual(
            HomeContentBlock.objects.get(
                section=HomeSection.ABOUT_VISION).image.name,
            before,
        )


class LocalizeShowcaseImagesCommandTests(TestCase):
    """`localize_showcase_images` pulls externally-hosted card artwork into our
    own media storage, so the homepage stops hotlinking third-party CDNs.

    Every featured card on production resolved its thumbnail to an
    images.unsplash.com URL that arrived with the seed data. These tests pin
    the safety model (dry run by default, create-only, idempotent) and the
    refusal paths, since the URLs come from the database.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.card = ShowcaseCourse.objects.create(
            title="Class 10 Foundation", level_label="Foundation",
            categories=["class8-12"],
            image_url="https://images.unsplash.com/photo-123?w=800",
        )

    def _run(self, payload=b"\x89PNG\r\n\x1a\n" + b"0" * 64,
             ctype="image/png", **kw):
        """Run the command with the network stubbed out."""
        from unittest import mock
        from django.core.management import call_command

        out = StringIO()

        class FakeResp:
            headers = {"Content-Type": ctype}

            def raise_for_status(self):
                pass

            def iter_content(self, _n):
                yield payload

        with override_settings(MEDIA_ROOT=self.tmp), \
                mock.patch("requests.get", return_value=FakeResp()) as get:
            call_command("localize_showcase_images", stdout=out, stderr=out, **kw)
        return out.getvalue(), get

    def test_dry_run_writes_nothing_and_makes_no_request(self):
        out, get = self._run()
        self.card.refresh_from_db()
        self.assertFalse(self.card.image)
        self.assertTrue(self.card.image_url, "image_url must survive a dry run")
        get.assert_not_called()
        self.assertIn("Dry run", out)

    def test_yes_downloads_attaches_and_clears_the_external_url(self):
        out, get = self._run(yes=True)
        self.card.refresh_from_db()
        self.assertTrue(self.card.image, "expected an uploaded file")
        self.assertIn("class-10-foundation", self.card.image.name)
        self.assertEqual(
            self.card.image_url, "",
            "image_url must be cleared so the thumbnail chain uses card.image",
        )
        get.assert_called_once()

    def test_is_idempotent(self):
        self._run(yes=True)
        name = ShowcaseCourse.objects.get(pk=self.card.pk).image.name
        out, get = self._run(yes=True)
        get.assert_not_called()
        self.assertEqual(ShowcaseCourse.objects.get(pk=self.card.pk).image.name, name)
        # A localized card now has BOTH an uploaded image and an empty
        # image_url, so it is caught by the create-only guard before the
        # "nothing to localize" branch. Either message means no re-download;
        # assert the one that actually fires so this test would notice if the
        # guard order ever changed.
        self.assertIn("already has an uploaded image", out)

    def test_existing_upload_is_never_clobbered(self):
        from django.core.files.base import ContentFile
        self.card.image.save("mine.png", ContentFile(b"editor upload"), save=True)
        self.card.image_url = "https://images.unsplash.com/photo-123?w=800"
        self.card.save()
        out, get = self._run(yes=True)
        get.assert_not_called()
        self.assertIn("already has an uploaded image", out)
        self.assertEqual(ShowcaseCourse.objects.get(pk=self.card.pk).image.read(),
                         b"editor upload")

    def test_non_image_content_type_is_refused_and_url_kept(self):
        out, _ = self._run(ctype="text/html", yes=True)
        self.card.refresh_from_db()
        self.assertFalse(self.card.image)
        self.assertTrue(self.card.image_url, "a failed card must keep rendering")
        self.assertIn("not an image", out)

    def test_non_http_url_is_skipped(self):
        self.card.image_url = "file:///etc/passwd"
        self.card.save()
        out, get = self._run(yes=True)
        get.assert_not_called()
        self.assertIn("not an http(s) URL", out)


class CmsImageValidatorTests(TestCase):
    """The About-hero field accepted a 4096x4096 / 12.7 MB PNG off a phone,
    with nothing to reject or resize it. These pin the guard-rails."""

    @staticmethod
    def _image(width=800, height=400, fmt="PNG"):
        from io import BytesIO

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (width, height), (10, 120, 90)).save(buf, format=fmt)
        buf.seek(0)
        ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "BMP": "bmp"}[fmt]
        return SimpleUploadedFile(f"t.{ext}", buf.read(), content_type=f"image/{ext}")

    def test_accepts_a_reasonable_web_image(self):
        from content.validators import validate_cms_image
        validate_cms_image(self._image())  # must not raise

    def test_rejects_an_over_large_dimension(self):
        from django.core.exceptions import ValidationError

        from content.validators import validate_cms_image
        with self.assertRaises(ValidationError) as cm:
            validate_cms_image(self._image(4096, 4096))
        self.assertIn("4096x4096", " ".join(cm.exception.messages))

    def test_rejects_a_disallowed_format(self):
        from django.core.exceptions import ValidationError

        from content.validators import validate_cms_image
        with self.assertRaises(ValidationError) as cm:
            validate_cms_image(self._image(fmt="BMP"))
        self.assertIn("not accepted", " ".join(cm.exception.messages))

    def test_rejects_a_file_over_the_byte_ceiling(self):
        from django.core.exceptions import ValidationError

        from content.validators import CmsImageValidator
        tiny_ceiling = CmsImageValidator(max_bytes=200)
        with self.assertRaises(ValidationError) as cm:
            tiny_ceiling(self._image())
        self.assertIn("the limit is", " ".join(cm.exception.messages))

    def test_rejects_something_that_is_not_an_image(self):
        from django.core.exceptions import ValidationError
        from django.core.files.uploadedfile import SimpleUploadedFile

        from content.validators import validate_cms_image
        with self.assertRaises(ValidationError) as cm:
            validate_cms_image(SimpleUploadedFile("x.png", b"not an image"))
        self.assertIn("could not be read", " ".join(cm.exception.messages))

    def test_leaves_the_file_readable_for_the_rest_of_the_save(self):
        """The validator opens the upload to read its size; if it does not
        rewind, whatever saves the file next writes zero bytes."""
        from content.validators import validate_cms_image
        f = self._image()
        validate_cms_image(f)
        self.assertEqual(f.tell(), 0)
        self.assertTrue(f.read(8).startswith(b"\x89PNG"))

    def test_the_field_actually_carries_the_validator(self):
        """Guards against the validator existing but never being wired up."""
        from content.validators import CmsImageValidator
        for model, field in (
            (HomeContentBlock, "image"),
            (HomeListItem, "image"),
            (ShowcaseCourse, "image"),
        ):
            vs = model._meta.get_field(field).validators
            self.assertTrue(
                any(isinstance(v, CmsImageValidator) for v in vs),
                f"{model.__name__}.{field} is missing CmsImageValidator",
            )


class ContactPageCmsTests(TestCase):
    """The /contact page's heading, blurb and four detail cards were hardcoded
    in Contact.jsx, so a changed phone number needed a frontend deploy. They
    are now a content block plus `contact_card` list items on the
    `contact_hero` section."""

    def setUp(self):
        cache.clear()
        self.block = HomeContentBlock.objects.create(
            section=HomeSection.CONTACT_HERO,
            heading="Contact ShikshaCom",
            subhead="Get in touch with us!",
            is_active=True,
        )
        for i, (icon, title, body) in enumerate([
            ("location", "Head Office", "House No. 1<br />Gurgaon"),
            ("email", "Email", "info@shikshacom.com"),
            ("phone", "Phone", "+0124-4255138 (Haryana)"),
        ]):
            HomeListItem.objects.create(
                section=HomeSection.CONTACT_HERO,
                variant=HomeListVariant.CONTACT_CARD,
                icon=icon, title=title, body=body, order=i, is_active=True,
            )

    def test_the_contact_section_is_a_valid_choice(self):
        """Guards against the section existing in seed data but not the enum,
        which would make every row fail validation."""
        self.assertIn(
            "contact_hero", dict(HomeSection.choices),
        )
        self.assertIn(
            "contact_card", dict(HomeListVariant.choices),
        )

    def test_block_is_served_on_the_public_endpoint(self):
        r = self.client.get("/api/content/home-content/", {"section": "contact_hero"})
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["heading"], "Contact ShikshaCom")

    def test_cards_are_served_in_order_with_their_line_breaks(self):
        r = self.client.get("/api/content/home-list-items/", {"section": "contact_hero"})
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        self.assertEqual([x["title"] for x in rows], ["Head Office", "Email", "Phone"])
        # <br> survives the restricted inline allowlist — an address needs it.
        self.assertIn("<br", rows[0]["body"])

    def test_a_card_can_be_added_without_a_deploy(self):
        """The whole point of list items over four fixed slots."""
        HomeListItem.objects.create(
            section=HomeSection.CONTACT_HERO,
            variant=HomeListVariant.CONTACT_CARD,
            icon="location", title="Third Office", body="Shillong", order=3,
            is_active=True,
        )
        cache.clear()
        rows = self.client.get(
            "/api/content/home-list-items/", {"section": "contact_hero"}
        ).json()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[-1]["title"], "Third Office")

    def test_an_inactive_card_is_not_served(self):
        card = HomeListItem.objects.get(title="Phone")
        card.status = PublishStatus.DRAFT
        card.save()
        cache.clear()
        rows = self.client.get(
            "/api/content/home-list-items/", {"section": "contact_hero"}
        ).json()
        self.assertNotIn("Phone", [x["title"] for x in rows])

    def test_a_queryset_update_on_is_active_no_longer_hides_a_card(self):
        """⚠ The public views read `status` now, not `is_active`.

        StatusedContentModel.save() keeps the two in step, but a queryset
        .update() bypasses save() entirely — so writing is_active that way
        leaves status untouched and the card stays visible. This is not a bug
        to fix by re-reading is_active; it is the reason the read sites moved.
        Nothing in the codebase writes is_active this way except this test.
        """
        HomeListItem.objects.filter(title="Phone").update(is_active=False)
        cache.clear()
        rows = self.client.get(
            "/api/content/home-list-items/", {"section": "contact_hero"}
        ).json()
        self.assertIn(
            "Phone", [x["title"] for x in rows],
            "a .update() that skips save() must not be expected to hide a card",
        )

    def test_seed_data_matches_what_the_frontend_hardcodes(self):
        """The seeded copy must be the page's real current text, or seeding
        would silently change the live page instead of just making it editable."""
        from content.management.commands._homepage_seed_data import (
            CONTACT_BLOCKS, CONTACT_LIST_ITEMS,
        )
        self.assertEqual(CONTACT_BLOCKS[0]["heading"], "Contact ShikshaCom")
        titles = [i["title"] for i in CONTACT_LIST_ITEMS]
        self.assertEqual(
            titles, ["Head Office", "Regional Office Address", "Email", "Phone"],
        )
        self.assertTrue(all(i["variant"] == "contact_card" for i in CONTACT_LIST_ITEMS))
