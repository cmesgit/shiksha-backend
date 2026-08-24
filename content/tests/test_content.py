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
            stars=5, review_count=214, price_label="1,500",
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
