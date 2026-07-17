# PLACEMENT: backend/content/tests/test_content.py
#
# Run with:  python manage.py test content

import datetime

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from content.models import (
    Announcement, BlogPost, ContentTag, CurrentAffair, FAQItem,
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
