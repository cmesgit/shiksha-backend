# PLACEMENT: backend/content/tests/test_blog_blocks.py
#
# Phase 1 (backend) of the blog block-editor project — see
# shared/src/blogBlocks/schema.js for the block contract this validates
# against, and /home/hruaia/.claude/plans/luminous-launching-sparrow.md for
# the full architecture.
#
# Run with:  python manage.py test content.tests.test_blog_blocks

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from content.blocks import blocks_to_text, validate_blocks, validate_theme
from content.models import BlogPost, BlogRevision, PublishStatus


def make_post(slug="class-9/economics/chapter-1", **extra):
    defaults = dict(
        title="Chapter 1: The Story of Village Palampur",
        class_level="9",
        subject="economics",
        chapter_number=1,
        body_html="<h1>Palampur</h1><p>Factors of production.</p>",
        status=PublishStatus.PUBLISHED,
        publish_at=timezone.now(),
    )
    defaults.update(extra)
    return BlogPost.objects.create(slug=slug, **defaults)


def staff_client():
    user = get_user_model().objects.create_user(
        username="editor", password="x", is_staff=True,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


class BlocksModuleTests(TestCase):
    """content/blocks.py in isolation — no DB needed."""

    def test_validate_blocks_accepts_known_types(self):
        blocks = [{"t": "divider"}, {"t": "rich_text", "html": "<p>hi</p>"}]
        self.assertEqual(validate_blocks(blocks), blocks)

    def test_validate_blocks_rejects_unknown_type(self):
        with self.assertRaises(Exception):
            validate_blocks([{"t": "not_a_real_block"}])

    def test_validate_blocks_rejects_non_list(self):
        with self.assertRaises(Exception):
            validate_blocks({"t": "divider"})

    def test_validate_blocks_rejects_missing_t_key(self):
        with self.assertRaises(Exception):
            validate_blocks([{"kind": "divider"}])

    def test_validate_theme_accepts_hex(self):
        theme = {"accent": "#4f46e5", "ink": "#fff"}
        self.assertEqual(validate_theme(theme), theme)

    def test_validate_theme_rejects_non_hex(self):
        with self.assertRaises(Exception):
            validate_theme({"accent": "red"})

    def test_validate_theme_rejects_unknown_token(self):
        with self.assertRaises(Exception):
            validate_theme({"not_a_token": "#ffffff"})

    def test_validate_theme_rejects_xml_breaking_value(self):
        # The exact DOMPurify SAFE_FOR_XML trap the client-side normalizer
        # guards against (see schema.js's normalizeTheme) — reject it here
        # too so a broken theme is a 400 at save time, not a silent gap.
        with self.assertRaises(Exception):
            validate_theme({"accent": "]> </style"})

    def test_blocks_to_text_extracts_strings_recursively(self):
        blocks = [
            {"t": "hero", "title": "Motion", "stats": [{"label": "Subject", "value": "Physics"}]},
            {"t": "faq_group", "items": [{"q": "What?", "a": "<strong>This</strong>."}]},
        ]
        text = blocks_to_text(blocks)
        self.assertIn("Motion", text)
        self.assertIn("Physics", text)
        self.assertIn("What?", text)
        self.assertNotIn("<strong>", text)  # tags stripped, text kept
        self.assertIn("This", text)

    def test_blocks_to_text_non_list_is_empty(self):
        self.assertEqual(blocks_to_text("not a list"), "")
        self.assertEqual(blocks_to_text(None), "")


class BlogPostBlocksModelTests(TestCase):
    def test_defaults_are_blank_not_none(self):
        post = make_post(slug="a/b/c1", chapter_number=None)
        self.assertEqual(post.body_blocks, [])
        self.assertEqual(post.body_theme, {})

    def test_full_clean_accepts_empty_blocks_and_theme(self):
        # The exact trap that hit ShowcaseCourse.categories — JSONField
        # default=list/dict WITHOUT blank=True makes full_clean() reject []
        # as blank on every create. Both new fields have blank=True; this
        # proves it actually works rather than just being present in source.
        post = BlogPost(
            title="x", class_level="9", subject="economics", chapter_number=9,
            body_html="<p>x</p>", slug="a/b/blank-check",
        )
        post.full_clean()  # must not raise

    def test_reading_minutes_prefers_blocks_over_html_when_present(self):
        # body_html deliberately understates the word count; if reading_minutes
        # were still computed from it, this test would see 1, not 3.
        blocks = [{"t": "rich_text", "html": " ".join(["word"] * 600)}]
        post = make_post(
            slug="a/b/c2", chapter_number=2,
            body_html="<p>short</p>", body_blocks=blocks,
        )
        self.assertEqual(post.reading_minutes, 3)

    def test_reading_minutes_falls_back_to_html_when_blocks_empty(self):
        post = make_post(
            slug="a/b/c3", chapter_number=3,
            body_html=f"<p>{' '.join(['word'] * 400)}</p>", body_blocks=[],
        )
        self.assertEqual(post.reading_minutes, 2)


class BlogPostPublicSerializerTests(TestCase):
    def test_detail_response_includes_blocks_and_theme(self):
        theme = {"accent": "#123456"}
        blocks = [{"t": "divider", "mark": "hex"}]
        make_post(body_blocks=blocks, body_theme=theme)
        resp = APIClient().get(
            reverse("content:blog-detail", args=["class-9/economics/chapter-1"])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["body_blocks"], blocks)
        self.assertEqual(resp.data["body_theme"], theme)


class BlogPostAdminValidationTests(TestCase):
    def setUp(self):
        self.client, self.user = staff_client()

    def test_create_rejects_unknown_block_type(self):
        resp = self.client.post(
            reverse("content:admin-blog-list"),
            data={
                "title": "New post", "class_level": "9", "subject": "economics",
                "chapter_number": 5, "body_html": "<p>x</p>",
                "body_blocks": [{"t": "not_a_real_block"}],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("body_blocks", resp.data)

    def test_create_rejects_non_hex_theme(self):
        resp = self.client.post(
            reverse("content:admin-blog-list"),
            data={
                "title": "New post", "class_level": "9", "subject": "economics",
                "chapter_number": 6, "body_html": "<p>x</p>",
                "body_theme": {"accent": "javascript:alert(1)"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("body_theme", resp.data)

    def test_create_accepts_valid_blocks_and_theme(self):
        resp = self.client.post(
            reverse("content:admin-blog-list"),
            data={
                # slug is included (even empty) deliberately: BlogPost's
                # UniqueConstraint on (slug, locale) makes DRF auto-attach a
                # UniqueTogetherValidator, whose enforce_required_fields()
                # unconditionally demands the KEY be present in the payload
                # on create — ignoring extra_kwargs={"required": False} —
                # unless the key is present at all (any value, including "").
                # BlogEditor.jsx's toApiFields() always sends the key, so
                # this isn't live-broken today, but a future direct API
                # caller (e.g. a bulk importer script) that omits the key
                # entirely would 400 here even though the model itself
                # auto-generates the slug in save().
                "title": "New post", "slug": "", "class_level": "9",
                "subject": "economics",
                "chapter_number": 7, "body_html": "<p>x</p>",
                "body_blocks": [{"t": "divider", "mark": "hex"}],
                "body_theme": {"accent": "#4f46e5"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["body_blocks"], [{"t": "divider", "mark": "hex"}])


class BlogRevisionTests(TestCase):
    def setUp(self):
        self.client, self.user = staff_client()
        self.post = make_post(
            body_html="<p>original</p>",
            body_blocks=[{"t": "rich_text", "html": "original"}],
        )

    def test_update_creates_a_revision_of_the_pre_update_state(self):
        self.assertEqual(BlogRevision.objects.filter(post=self.post).count(), 0)

        resp = self.client.patch(
            reverse("content:admin-blog-detail", args=[self.post.id]),
            data={"body_html": "<p>edited</p>", "body_blocks": []},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        revisions = BlogRevision.objects.filter(post=self.post)
        self.assertEqual(revisions.count(), 1)
        rev = revisions.first()
        # The revision holds the OLD content, not the new — this is what
        # makes it a usable undo path (unlike body_html_source).
        self.assertIn("original", rev.body_html)
        self.assertEqual(rev.body_blocks, [{"t": "rich_text", "html": "original"}])
        self.assertEqual(rev.created_by, self.user)

        self.post.refresh_from_db()
        self.assertIn("edited", self.post.body_html)

    def test_revision_reason_is_recorded(self):
        self.client.patch(
            reverse("content:admin-blog-detail", args=[self.post.id]),
            data={"body_html": "<p>edited</p>", "revision_reason": "test import"},
            format="json",
        )
        rev = BlogRevision.objects.get(post=self.post)
        self.assertEqual(rev.reason, "test import")

    def test_revisions_action_lists_history_newest_first(self):
        self.client.patch(
            reverse("content:admin-blog-detail", args=[self.post.id]),
            data={"body_html": "<p>edit 1</p>", "revision_reason": "first edit"},
            format="json",
        )
        self.client.patch(
            reverse("content:admin-blog-detail", args=[self.post.id]),
            data={"body_html": "<p>edit 2</p>", "revision_reason": "second edit"},
            format="json",
        )
        resp = self.client.get(
            reverse("content:admin-blog-revisions", args=[self.post.id])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)
        # Meta.ordering = ["-created_at"] — the snapshot taken during the
        # SECOND patch (which captured "edit 1", tagged "second edit") is
        # newest and must come first.
        self.assertEqual(resp.data[0]["reason"], "second edit")
        self.assertEqual(resp.data[1]["reason"], "first edit")
        self.assertIn("created_by", resp.data[0])
        self.assertEqual(resp.data[0]["created_by"], "editor")

    def test_restore_revision_puts_old_content_back_and_snapshots_current_first(self):
        self.client.patch(
            reverse("content:admin-blog-detail", args=[self.post.id]),
            data={"body_html": "<p>edited</p>", "body_blocks": []},
            format="json",
        )
        rev = BlogRevision.objects.get(post=self.post)  # the "original" snapshot
        self.assertEqual(BlogRevision.objects.filter(post=self.post).count(), 1)

        resp = self.client.post(
            reverse("content:admin-blog-restore-revision",
                    kwargs={"pk": self.post.id, "revision_id": rev.id}),
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        self.post.refresh_from_db()
        self.assertIn("original", self.post.body_html)
        self.assertEqual(self.post.body_blocks, [{"t": "rich_text", "html": "original"}])

        # Restoring is itself an update, so it must ALSO snapshot what it's
        # about to overwrite ("edited") — undoing an undo must stay possible.
        self.assertEqual(BlogRevision.objects.filter(post=self.post).count(), 2)
        newest = BlogRevision.objects.filter(post=self.post).order_by("-created_at").first()
        self.assertIn("edited", newest.body_html)


class CheckBlogFragmentBackupCommandTests(TestCase):
    def _write_fragment(self, root, rel_path, content):
        path = Path(root) / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_passes_when_disk_matches_db(self):
        with tempfile.TemporaryDirectory() as root:
            make_post(
                slug="class-9/economics/chapter-1",
                body_html="<p>hi</p>", trusted_html=True,
            )
            self._write_fragment(root, "class-9/economics/chapter-1.html", "<p>hi</p>")
            call_command("check_blog_fragment_backup", root)  # must not raise

    def test_fails_on_content_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            make_post(
                slug="class-9/economics/chapter-1",
                body_html="<p>hi</p>", trusted_html=True,
            )
            self._write_fragment(
                root, "class-9/economics/chapter-1.html", "<p>DIFFERENT</p>",
            )
            with self.assertRaises(CommandError):
                call_command("check_blog_fragment_backup", root)

    def test_fails_when_disk_fragment_has_no_db_post(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_fragment(root, "class-9/economics/chapter-9.html", "<p>hi</p>")
            with self.assertRaises(CommandError):
                call_command("check_blog_fragment_backup", root)

    def test_known_exception_slug_is_not_flagged_as_missing_on_disk(self):
        with tempfile.TemporaryDirectory() as root:
            make_post(slug="sdsd", chapter_number=None, body_html="<p>hi</p>")
            self._write_fragment(root, "class-9/economics/chapter-1.html", "<p>hi</p>")
            make_post(
                slug="class-9/economics/chapter-1",
                body_html="<p>hi</p>", trusted_html=True,
            )
            call_command("check_blog_fragment_backup", root)  # must not raise despite "sdsd"
