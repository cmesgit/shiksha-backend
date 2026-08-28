"""Content Studio — status, revisions and drafts.

`is_active` was dropped in content/0024, so the tests that policed the two
fields agreeing went with it. What remains is what still matters: status is
readable and writable everywhere the UI needs it, revisions record and restore,
and drafts are per-author.
"""
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from content.models import (
    Announcement, ContentDraft, ContentRevision, FAQItem, HomeListItem,
    PublishStatus, ShowcaseCourse,
)
from content.revisions import (
    record_revision, restore_revision, snapshot_before, snapshot_of,
)

User = get_user_model()


class StatusIsActiveSyncTest(TestCase):
    """Status behaves as the single source of truth for visibility."""

    def _faq(self, **kw):
        return FAQItem.objects.create(
            question="How do I enrol?", answer_html="<p>Click enrol.</p>", **kw
        )

    def test_new_row_defaults_to_published(self):
        faq = self._faq()
        self.assertEqual(faq.status, PublishStatus.PUBLISHED)
        self.assertTrue(faq.is_live)

    def test_review_is_not_live(self):
        faq = self._faq()
        faq.status = PublishStatus.REVIEW
        faq.save()
        faq.refresh_from_db()
        self.assertFalse(faq.is_live)



class PublicQuerysetsStillWorkTest(TestCase):
    """The public site reads `status`. These pin what visitors get."""

    def test_archived_rows_leave_the_public_faq_list(self):
        live = FAQItem.objects.create(question="A?", answer_html="<p>a</p>")
        hidden = FAQItem.objects.create(question="B?", answer_html="<p>b</p>")
        hidden.status = PublishStatus.ARCHIVED
        hidden.save()

        visible = list(FAQItem.objects.filter(status=PublishStatus.PUBLISHED))
        self.assertIn(live, visible)
        self.assertNotIn(hidden, visible)

    def test_announcement_manager_still_filters_correctly(self):
        """Announcement keeps AnnouncementQuerySet — the new base must not
        replace `objects`, or live_now() disappears."""
        self.assertTrue(hasattr(Announcement.objects, "live"))
        Announcement.objects.create(message="Live one")
        self.assertEqual(Announcement.objects.live().count(), 1)

        hidden = Announcement.objects.create(message="Hidden one")
        hidden.status = PublishStatus.DRAFT
        hidden.save()
        self.assertEqual(
            Announcement.objects.live().count(), 1,
            "a draft announcement must drop out of live()",
        )


class RecordRevisionTest(TestCase):
    def setUp(self):
        # The Studio permission caches content_studio_enabled. Django rolls the
        # DB back between tests but NOT the cache, so a test that flips the flag
        # off would otherwise leak a cached False into every test after it.
        from django.core.cache import cache

        from content.permissions import IsStudioEditor
        cache.delete(IsStudioEditor.CACHE_KEY)
        self.user = User.objects.create_user(
            username="ed", email="ed@example.com", password="x",
        )
        self.faq = FAQItem.objects.create(
            question="Original?", answer_html="<p>original</p>",
        )

    def test_snapshot_is_json_safe(self):
        """Dates and file fields must survive the trip into a JSONField."""
        snap = snapshot_of(self.faq)
        import json
        json.dumps(snap)  # would raise if a datetime leaked through
        self.assertEqual(snap["question"], "Original?")

    def test_records_the_state_before_the_change(self):
        before = snapshot_before(self.faq)
        self.faq.question = "Changed?"
        self.faq.save()
        rev = record_revision(
            self.faq, ContentRevision.ACTION_UPDATED,
            actor=self.user, snapshot=before,
        )
        self.assertEqual(rev.snapshot["question"], "Original?")
        self.assertEqual(rev.actor, self.user)
        self.assertEqual(rev.target, self.faq)

    def test_prunes_to_the_retention_limit_per_object(self):
        keep = ContentRevision.RETENTION_PER_OBJECT
        for i in range(keep + 8):
            record_revision(self.faq, ContentRevision.ACTION_UPDATED)
        ct = ContentType.objects.get_for_model(FAQItem)
        self.assertEqual(
            ContentRevision.objects.filter(
                content_type=ct, object_id=self.faq.pk
            ).count(),
            keep,
        )

    def test_pruning_is_scoped_to_one_object(self):
        """A busy row must not age out a quiet row's history."""
        other = FAQItem.objects.create(question="Quiet?", answer_html="<p>q</p>")
        record_revision(other, ContentRevision.ACTION_CREATED)
        for _ in range(ContentRevision.RETENTION_PER_OBJECT + 5):
            record_revision(self.faq, ContentRevision.ACTION_UPDATED)
        ct = ContentType.objects.get_for_model(FAQItem)
        self.assertEqual(
            ContentRevision.objects.filter(
                content_type=ct, object_id=other.pk
            ).count(),
            1,
        )


class RestoreRevisionTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="ed2", email="ed2@example.com", password="x",
        )
        self.faq = FAQItem.objects.create(
            question="Original?", answer_html="<p>original</p>",
        )

    def test_restore_reapplies_the_snapshot(self):
        before = snapshot_before(self.faq)
        self.faq.question = "Changed?"
        self.faq.save()
        rev = record_revision(
            self.faq, ContentRevision.ACTION_UPDATED, snapshot=before,
        )

        restore_revision(rev, actor=self.user)
        self.faq.refresh_from_db()
        self.assertEqual(self.faq.question, "Original?")

    def test_restore_writes_a_new_revision_rather_than_deleting_one(self):
        before = snapshot_before(self.faq)
        self.faq.question = "Changed?"
        self.faq.save()
        rev = record_revision(
            self.faq, ContentRevision.ACTION_UPDATED, snapshot=before,
        )
        count_before = ContentRevision.objects.count()

        restore_revision(rev, actor=self.user)

        self.assertEqual(ContentRevision.objects.count(), count_before + 1)
        self.assertTrue(ContentRevision.objects.filter(pk=rev.pk).exists())
        newest = ContentRevision.objects.first()
        self.assertEqual(newest.action, ContentRevision.ACTION_RESTORED)

    def test_undo_of_an_undo_is_allowed(self):
        before = snapshot_before(self.faq)
        self.faq.question = "Changed?"
        self.faq.save()
        rev = record_revision(
            self.faq, ContentRevision.ACTION_UPDATED, snapshot=before,
        )

        restore_revision(rev, actor=self.user)          # back to "Original?"
        undo_rev = ContentRevision.objects.first()      # snapshot = "Changed?"
        restore_revision(undo_rev, actor=self.user)     # forward again

        self.faq.refresh_from_db()
        self.assertEqual(self.faq.question, "Changed?")

    def test_restore_of_a_deleted_row_returns_none(self):
        rev = record_revision(self.faq, ContentRevision.ACTION_UPDATED)
        self.faq.delete()
        self.assertIsNone(restore_revision(rev, actor=self.user))


class ContentDraftTest(TestCase):
    def setUp(self):
        self.a = User.objects.create_user(
            username="a", email="a@example.com", password="x",
        )
        self.b = User.objects.create_user(
            username="b", email="b@example.com", password="x",
        )
        self.faq = FAQItem.objects.create(
            question="Shared?", answer_html="<p>shared</p>",
        )
        self.ct = ContentType.objects.get_for_model(FAQItem)

    def test_two_authors_drafts_on_one_object_do_not_collide(self):
        ContentDraft.objects.create(
            content_type=self.ct, object_id=self.faq.pk, author=self.a,
            payload={"question": "A's take?"},
        )
        ContentDraft.objects.create(
            content_type=self.ct, object_id=self.faq.pk, author=self.b,
            payload={"question": "B's take?"},
        )
        self.assertEqual(
            ContentDraft.objects.filter(
                content_type=self.ct, object_id=self.faq.pk
            ).count(),
            2,
        )

    def test_one_author_gets_one_draft_per_object(self):
        from django.db import IntegrityError, transaction

        ContentDraft.objects.create(
            content_type=self.ct, object_id=self.faq.pk, author=self.a,
            payload={"question": "first"},
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ContentDraft.objects.create(
                    content_type=self.ct, object_id=self.faq.pk, author=self.a,
                    payload={"question": "second"},
                )

    def test_change_count_drives_the_unpublished_edits_chip(self):
        draft = ContentDraft.objects.create(
            content_type=self.ct, object_id=self.faq.pk, author=self.a,
            payload={"question": "q", "answer_html": "<p>a</p>", "order": 3},
        )
        self.assertEqual(draft.change_count, 3)

    def test_draft_holds_only_changed_fields_not_the_whole_row(self):
        """Publishing applies the payload onto the row as it is at publish
        time, so a field someone else changed meanwhile survives."""
        draft = ContentDraft.objects.create(
            content_type=self.ct, object_id=self.faq.pk, author=self.a,
            payload={"question": "Only this changed?"},
        )
        self.assertNotIn("answer_html", draft.payload)
        self.assertEqual(draft.target, self.faq)


class BackfillMigrationTest(TestCase):
    """0021's backfill list, pinned.

    The forward/reverse functions used to be exercised against live models —
    a shortcut that stopped working when `is_active` was dropped in 0024. What
    still matters, and is still checkable, is that the migration named every
    model: one left out would have silently gone live on deploy.
    """

    def _migration(self):
        from importlib import import_module
        return import_module(
            "content.migrations.0021_backfill_status_from_is_active"
        )

    def test_forward_covers_every_migrated_model(self):
        """If a model gains status but is left out of MODELS, its hidden rows
        silently go live on deploy."""
        self.assertEqual(
            set(self._migration().MODELS),
            {
                "FAQItem", "Announcement", "ShowcaseCourse",
                "HomeContentBlock", "HomeListItem", "HomeFloater",
            },
        )


class AdminSerializersExposeStatusTest(TestCase):
    """Every model that gained `status` must expose it on its admin serializer.

    This codebase has been bitten 6+ times by a field existing on the model but
    never reaching the screen, because the endpoint is a hand-rolled dict or the
    serializer's `fields` list was never updated. Phase 6 builds draft UI on
    exactly these six, so pin it now rather than discovering it there.
    """

    def test_status_is_readable_on_all_six(self):
        from content import admin_serializers as s

        pairs = [
            (s.FAQItemAdminSerializer, "FAQItem"),
            (s.AnnouncementAdminSerializer, "Announcement"),
            (s.ShowcaseCourseAdminSerializer, "ShowcaseCourse"),
            (s.HomeContentBlockAdminSerializer, "HomeContentBlock"),
            (s.HomeListItemAdminSerializer, "HomeListItem"),
            (s.HomeFloaterAdminSerializer, "HomeFloater"),
        ]
        for serializer, name in pairs:
            with self.subTest(model=name):
                self.assertIn(
                    "status", serializer().fields,
                    f"{name}'s admin serializer does not expose `status` — the "
                    f"draft/review UI would have nothing to read or write.",
                )

    def test_status_is_writable_so_the_ui_can_set_a_draft(self):
        from content.admin_serializers import HomeContentBlockAdminSerializer

        field = HomeContentBlockAdminSerializer().fields["status"]
        self.assertFalse(
            field.read_only,
            "a read-only status would make the review workflow unusable",
        )
