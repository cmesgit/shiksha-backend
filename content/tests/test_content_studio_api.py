"""Content Studio Phase 1b — activity, restore, page drafts, publish.

Every path here is asserted at /api/content/admin/… deliberately: the handoff
spec and all five of its scaffolds use /admin/content/…, which resolves to
nothing. If someone "fixes" these URLs to match the spec, these tests fail.
"""
from datetime import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from content.models import (
    ContentDraft, ContentRevision, HomeContentBlock, HomeSectionOrder,
    PublishStatus,
)
from content.revisions import record_revision, snapshot_before

User = get_user_model()

DRAFT_URL = "/api/content/admin/pages/home/draft/"
PUBLISH_URL = "/api/content/admin/pages/home/publish/"
ACTIVITY_URL = "/api/content/admin/activity/"


class StudioApiTestCase(TestCase):
    def setUp(self):
        # See the note in the exams tests: the Studio permission caches the
        # feature flag, and the cache does not roll back between tests.
        from django.core.cache import cache

        from content.permissions import IsStudioEditor
        cache.delete(IsStudioEditor.CACHE_KEY)

        self.editor = User.objects.create_user(
            username="ed", email="ed@example.com", password="x", is_staff=True,
        )
        self.other = User.objects.create_user(
            username="ed2", email="ed2@example.com", password="x", is_staff=True,
        )
        self.outsider = User.objects.create_user(
            username="learner", email="l@example.com", password="x",
        )
        self.hero = HomeContentBlock.objects.create(
            section="hero", heading="Learn with ShikshaCom",
        )
        # A migration already seeds one HomeSectionOrder row per section, so
        # creating one here trips the unique constraint on `section`.
        HomeSectionOrder.objects.get_or_create(
            section="hero", defaults={"order": 0},
        )

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c


class PermissionsTest(StudioApiTestCase):
    def test_non_staff_is_refused_everywhere(self):
        c = self.client_for(self.outsider)
        for url in (DRAFT_URL, ACTIVITY_URL, PUBLISH_URL):
            with self.subTest(url=url):
                res = c.get(url) if url != PUBLISH_URL else c.post(url, {}, format="json")
                self.assertEqual(res.status_code, 403, url)

    def test_anonymous_is_refused(self):
        self.assertEqual(APIClient().get(DRAFT_URL).status_code, 401)


class PageDraftTest(StudioApiTestCase):
    def test_get_lists_sections_with_no_edits_initially(self):
        res = self.client_for(self.editor).get(DRAFT_URL)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["page"]["key"], "home")
        self.assertEqual(body["change_count"], 0)
        hero = next(s for s in body["sections"] if s["key"] == "hero")
        self.assertTrue(hero["has_content"])
        self.assertEqual(hero["edited_fields"], [])

    def test_put_stores_a_draft_without_touching_the_live_row(self):
        res = self.client_for(self.editor).put(
            DRAFT_URL,
            {"sections": {"hero": {"heading": "A better heading"}}},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["change_count"], 1)

        self.hero.refresh_from_db()
        self.assertEqual(
            self.hero.heading, "Learn with ShikshaCom",
            "autosaving a draft must never write to the live row",
        )

    def test_edited_fields_drive_the_dot_and_are_derived_not_stored(self):
        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"heading": "X"}}}, format="json")
        body = c.get(DRAFT_URL).json()
        hero = next(s for s in body["sections"] if s["key"] == "hero")
        self.assertEqual(hero["edited_fields"], ["heading"])

    def test_editing_a_field_back_to_the_live_value_clears_the_edit(self):
        """Otherwise the 'unpublished edits' count never returns to zero."""
        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"heading": "X"}}}, format="json")
        res = c.put(
            DRAFT_URL,
            {"sections": {"hero": {"heading": "Learn with ShikshaCom"}}},
            format="json",
        )
        self.assertEqual(res.json()["change_count"], 0)

    def test_two_authors_drafts_do_not_collide(self):
        self.client_for(self.editor).put(
            DRAFT_URL, {"sections": {"hero": {"heading": "Mine"}}}, format="json",
        )
        self.client_for(self.other).put(
            DRAFT_URL, {"sections": {"hero": {"heading": "Theirs"}}}, format="json",
        )
        mine = self.client_for(self.editor).get(DRAFT_URL).json()
        theirs = self.client_for(self.other).get(DRAFT_URL).json()
        self.assertEqual(mine["draft"]["hero"]["heading"], "Mine")
        self.assertEqual(theirs["draft"]["hero"]["heading"], "Theirs")

    def test_non_editable_fields_are_rejected_not_written(self):
        res = self.client_for(self.editor).put(
            DRAFT_URL,
            {"sections": {"hero": {"id": 999, "heading": "ok"}}},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn("hero", res.json().get("rejected", {}))
        self.assertNotIn("id", res.json()["draft"]["hero"])

    def test_unknown_page_is_404(self):
        res = self.client_for(self.editor).get(
            "/api/content/admin/pages/nope/draft/"
        )
        self.assertEqual(res.status_code, 404)

    def test_malformed_body_is_400(self):
        res = self.client_for(self.editor).put(
            DRAFT_URL, {"sections": "not-an-object"}, format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_delete_discards_pending_edits(self):
        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"heading": "X"}}}, format="json")
        c.delete(DRAFT_URL)
        self.assertEqual(c.get(DRAFT_URL).json()["change_count"], 0)


class PagePublishTest(StudioApiTestCase):
    def test_publish_applies_exactly_the_changed_fields(self):
        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"heading": "New heading"}}}, format="json")

        res = c.post(PUBLISH_URL, {}, format="json")
        self.assertEqual(res.status_code, 200, res.content)

        self.hero.refresh_from_db()
        self.assertEqual(self.hero.heading, "New heading")
        self.assertEqual(self.hero.status, PublishStatus.PUBLISHED)

    def test_publish_does_not_disturb_untouched_fields(self):
        self.hero.subheading = "Kept"
        self.hero.save()
        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"heading": "New"}}}, format="json")
        c.post(PUBLISH_URL, {}, format="json")

        self.hero.refresh_from_db()
        self.assertEqual(self.hero.subheading, "Kept")

    def test_publish_consumes_the_draft(self):
        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"heading": "New"}}}, format="json")
        c.post(PUBLISH_URL, {}, format="json")
        self.assertEqual(ContentDraft.objects.count(), 0)
        self.assertEqual(c.get(DRAFT_URL).json()["change_count"], 0)

    def test_publishing_only_publishes_your_own_draft(self):
        self.client_for(self.editor).put(
            DRAFT_URL, {"sections": {"hero": {"heading": "Mine"}}}, format="json",
        )
        self.client_for(self.other).put(
            DRAFT_URL, {"sections": {"hero": {"heading": "Theirs"}}}, format="json",
        )
        self.client_for(self.editor).post(PUBLISH_URL, {}, format="json")

        self.hero.refresh_from_db()
        self.assertEqual(self.hero.heading, "Mine")
        self.assertEqual(
            ContentDraft.objects.filter(author=self.other).count(), 1,
            "the other author's pending edits must survive",
        )

    def test_publish_records_a_revision_of_the_previous_state(self):
        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"heading": "New"}}}, format="json")
        c.post(PUBLISH_URL, {}, format="json")

        rev = ContentRevision.objects.first()
        self.assertEqual(rev.action, ContentRevision.ACTION_PUBLISHED)
        self.assertEqual(rev.snapshot["heading"], "Learn with ShikshaCom")
        self.assertEqual(rev.actor, self.editor)

    def test_publishing_nothing_is_a_400(self):
        res = self.client_for(self.editor).post(PUBLISH_URL, {}, format="json")
        self.assertEqual(res.status_code, 400)


class ActivityAndRestoreTest(StudioApiTestCase):
    def test_activity_groups_by_day(self):
        record_revision(self.hero, ContentRevision.ACTION_UPDATED, actor=self.editor)
        body = self.client_for(self.editor).get(ACTIVITY_URL).json()
        self.assertEqual(len(body["days"]), 1)
        item = body["days"][0]["items"][0]
        self.assertEqual(item["kind"], "homecontentblock")
        self.assertEqual(item["actor"], self.editor.email)

    def test_undo_reverts_a_published_change(self):
        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"heading": "New"}}}, format="json")
        c.post(PUBLISH_URL, {}, format="json")
        self.hero.refresh_from_db()
        self.assertEqual(self.hero.heading, "New")

        rev_id = self.client_for(self.editor).get(ACTIVITY_URL).json()[
            "days"][0]["items"][0]["id"]
        res = c.post(f"/api/content/admin/revisions/{rev_id}/restore/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.content)

        self.hero.refresh_from_db()
        self.assertEqual(self.hero.heading, "Learn with ShikshaCom")

    def test_restore_adds_history_rather_than_removing_it(self):
        before = snapshot_before(self.hero)
        self.hero.heading = "Changed"
        self.hero.save()
        rev = record_revision(
            self.hero, ContentRevision.ACTION_UPDATED,
            actor=self.editor, snapshot=before,
        )
        count = ContentRevision.objects.count()

        self.client_for(self.editor).post(
            f"/api/content/admin/revisions/{rev.pk}/restore/", {}, format="json",
        )
        self.assertEqual(ContentRevision.objects.count(), count + 1)
        self.assertTrue(ContentRevision.objects.filter(pk=rev.pk).exists())

    def test_restoring_onto_a_deleted_row_is_410_not_500(self):
        rev = record_revision(self.hero, ContentRevision.ACTION_UPDATED)
        self.hero.delete()
        res = self.client_for(self.editor).post(
            f"/api/content/admin/revisions/{rev.pk}/restore/", {}, format="json",
        )
        self.assertEqual(res.status_code, 410)


class PublicSiteUnaffectedTest(StudioApiTestCase):
    def test_drafting_does_not_change_what_the_public_endpoint_returns(self):
        """The whole point of ContentDraft: the live site sees nothing until
        publish."""
        before = self.client.get("/api/content/home-content/").json()

        self.client_for(self.editor).put(
            DRAFT_URL, {"sections": {"hero": {"heading": "Unpublished"}}},
            format="json",
        )
        after = self.client.get("/api/content/home-content/").json()
        self.assertEqual(before, after)


SEARCH_URL = "/api/content/admin/search/"


class StudioSearchTest(StudioApiTestCase):
    def setUp(self):
        super().setUp()
        from content.models import BlogPost, ContentTag, FAQItem, PublishStatus as PS
        from courses.models import CourseCategory

        BlogPost.objects.create(
            title="Photosynthesis explained", slug="bio/photosynthesis",
            status=PS.PUBLISHED,
        )
        FAQItem.objects.create(
            question="How does photosynthesis work?", answer_html="<p>Light.</p>",
        )
        ContentTag.objects.create(name="photosynthesis")
        CourseCategory.objects.create(name="Photosynthesis Prep", group="boards")
        self.hero.heading = "Photosynthesis for Class 9"
        self.hero.save()

    def test_finds_every_content_type_in_one_query(self):
        res = self.client_for(self.editor).get(SEARCH_URL, {"q": "photosynthesis"})
        self.assertEqual(res.status_code, 200, res.content)
        kinds = {r["kind"] for r in res.json()["results"]}
        self.assertEqual(
            kinds, {"post", "answer", "page", "label"},
            "the palette must reach across posts, answers, pages and labels",
        )

    def test_labels_span_two_django_apps(self):
        """ContentTag lives in `content`, CourseCategory in `courses`."""
        res = self.client_for(self.editor).get(SEARCH_URL, {"q": "photosynthesis"})
        wheres = {
            r["where"] for r in res.json()["results"] if r["kind"] == "label"
        }
        self.assertIn("Blog tag", wheres)
        self.assertTrue(any("Course category" in w for w in wheres))

    def test_short_queries_return_nothing_rather_than_everything(self):
        res = self.client_for(self.editor).get(SEARCH_URL, {"q": "p"})
        self.assertEqual(res.json()["results"], [])

    def test_missing_query_is_not_an_error(self):
        res = self.client_for(self.editor).get(SEARCH_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["results"], [])

    def test_results_carry_an_admin_route_not_an_api_path(self):
        res = self.client_for(self.editor).get(SEARCH_URL, {"q": "photosynthesis"})
        for r in res.json()["results"]:
            with self.subTest(kind=r["kind"]):
                self.assertFalse(
                    r["url"].startswith("/api/"),
                    "url is where the palette navigates, not what it fetches",
                )

    def test_non_staff_is_refused(self):
        res = self.client_for(self.outsider).get(SEARCH_URL, {"q": "photosynthesis"})
        self.assertEqual(res.status_code, 403)


INBOX_URL = "/api/content/admin/inbox/"
CALENDAR_URL = "/api/content/admin/calendar/"


class InboxTest(StudioApiTestCase):
    def _groups(self, body):
        return {g["key"]: g["items"] for g in body["groups"]}

    def test_empty_inbox_still_returns_all_three_groups(self):
        """The screen renders three headings; it should not have to guess
        which ones the server decided to omit."""
        body = self.client_for(self.editor).get(INBOX_URL).json()
        self.assertEqual(
            [g["key"] for g in body["groups"]],
            ["publishing_today", "awaiting_you", "stale_drafts"],
        )
        self.assertEqual(body["total"], 0)

    def test_items_in_review_are_awaiting_you(self):
        self.hero.status = PublishStatus.REVIEW
        self.hero.save()
        body = self.client_for(self.editor).get(INBOX_URL).json()
        items = self._groups(body)["awaiting_you"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Learn with ShikshaCom")
        self.assertEqual(items[0]["state"], "review")

    def test_a_fresh_draft_is_not_stale(self):
        self.hero.status = PublishStatus.DRAFT
        self.hero.save()
        body = self.client_for(self.editor).get(INBOX_URL).json()
        self.assertEqual(self._groups(body)["stale_drafts"], [])

    def test_an_old_draft_is_stale(self):
        from datetime import timedelta

        from django.utils import timezone

        from content.models import HomeContentBlock

        self.hero.status = PublishStatus.DRAFT
        self.hero.save()
        # auto_now on updated_at means this has to be written past the ORM.
        HomeContentBlock.objects.filter(pk=self.hero.pk).update(
            updated_at=timezone.now() - timedelta(days=30),
        )
        body = self.client_for(self.editor).get(INBOX_URL).json()
        items = self._groups(body)["stale_drafts"]
        self.assertEqual(len(items), 1)
        self.assertIn("30 days", items[0]["reason"])

    def test_scheduled_post_shows_under_publishing_today(self):
        from django.utils import timezone

        from content.models import BlogPost

        BlogPost.objects.create(
            title="Goes live today", slug="x/today",
            status=PublishStatus.PUBLISHED, publish_at=timezone.now(),
        )
        body = self.client_for(self.editor).get(INBOX_URL).json()
        items = self._groups(body)["publishing_today"]
        self.assertEqual([i["title"] for i in items], ["Goes live today"])

    def test_every_item_carries_a_deep_link(self):
        self.hero.status = PublishStatus.REVIEW
        self.hero.save()
        body = self.client_for(self.editor).get(INBOX_URL).json()
        for group in body["groups"]:
            for item in group["items"]:
                self.assertTrue(item["url"].startswith("/content"))

    def test_non_staff_is_refused(self):
        self.assertEqual(
            self.client_for(self.outsider).get(INBOX_URL).status_code, 403,
        )


class CalendarTest(StudioApiTestCase):
    def test_returns_a_cell_for_every_day_in_range(self):
        body = self.client_for(self.editor).get(
            CALENDAR_URL, {"from": "2026-08-24", "to": "2026-08-30"},
        ).json()
        self.assertEqual(len(body["days"]), 7)
        self.assertEqual(body["days"][0]["date"], "2026-08-24")

    def test_empty_days_are_present_not_omitted(self):
        body = self.client_for(self.editor).get(
            CALENDAR_URL, {"from": "2026-08-24", "to": "2026-08-26"},
        ).json()
        self.assertTrue(all("items" in d for d in body["days"]))

    def test_a_scheduled_post_lands_on_its_day(self):
        from django.utils import timezone

        from content.models import BlogPost

        when = timezone.make_aware(datetime(2026, 8, 25, 9, 0))
        BlogPost.objects.create(
            title="Tuesday piece", slug="x/tue",
            status=PublishStatus.PUBLISHED, publish_at=when,
        )
        body = self.client_for(self.editor).get(
            CALENDAR_URL, {"from": "2026-08-24", "to": "2026-08-30"},
        ).json()
        day = next(d for d in body["days"] if d["date"] == "2026-08-25")
        self.assertEqual([i["title"] for i in day["items"]], ["Tuesday piece"])

    def test_a_runaway_range_is_capped_not_scanned(self):
        body = self.client_for(self.editor).get(
            CALENDAR_URL, {"from": "2020-01-01", "to": "2039-01-01"},
        ).json()
        self.assertLessEqual(len(body["days"]), 93)

    def test_reversed_range_is_tolerated(self):
        body = self.client_for(self.editor).get(
            CALENDAR_URL, {"from": "2026-08-30", "to": "2026-08-24"},
        ).json()
        self.assertEqual(body["from"], "2026-08-24")

    def test_garbage_dates_fall_back_to_this_week(self):
        body = self.client_for(self.editor).get(
            CALENDAR_URL, {"from": "not-a-date"},
        ).json()
        self.assertEqual(len(body["days"]), 7)


CHECKLIST_URL = "/api/content/admin/pages/home/checklist/"


class ChecklistTest(StudioApiTestCase):
    def _draft(self, fields):
        return self.client_for(self.editor).put(
            DRAFT_URL, {"sections": {"hero": fields}}, format="json",
        )

    def test_nothing_touched_means_nothing_to_publish(self):
        body = self.client_for(self.editor).get(CHECKLIST_URL).json()
        self.assertTrue(body["nothing_to_publish"])
        self.assertFalse(body["can_publish"])

    def test_a_good_edit_can_be_published(self):
        self._draft({"heading": "Learn with real teachers"})
        body = self.client_for(self.editor).get(CHECKLIST_URL).json()
        self.assertTrue(body["can_publish"], body)
        self.assertEqual(body["blocking"], 0)

    def test_an_emptied_heading_blocks(self):
        """The check runs against the DRAFT. Checking the live row would pass
        a pending edit that empties the heading — the exact mistake this is for."""
        self._draft({"heading": ""})
        body = self.client_for(self.editor).get(CHECKLIST_URL).json()
        self.assertEqual(body["blocking"], 1)
        self.assertFalse(body["can_publish"])

    def test_a_button_with_no_destination_blocks(self):
        self._draft({"heading": "A perfectly fine heading", "cta_primary_label": "Join now"})
        body = self.client_for(self.editor).get(CHECKLIST_URL).json()
        ids = [c["id"] for s in body["sections"] for c in s["checks"]]
        self.assertIn("cta_main", ids)
        self.assertEqual(body["blocking"], 1)

    def test_a_destination_with_no_words_only_warns(self):
        self._draft({"heading": "A perfectly fine heading", "cta_primary_href": "/courses"})
        body = self.client_for(self.editor).get(CHECKLIST_URL).json()
        self.assertEqual(body["blocking"], 0)
        self.assertGreaterEqual(body["warnings"], 1)

    def test_a_very_long_heading_warns_but_does_not_block(self):
        self._draft({"heading": "x" * 120})
        body = self.client_for(self.editor).get(CHECKLIST_URL).json()
        self.assertEqual(body["blocking"], 0)
        self.assertGreaterEqual(body["warnings"], 1)

    def test_untouched_sections_are_not_checked(self):
        """A pre-existing problem elsewhere must not block an unrelated edit,
        or the button can never be pressed."""
        HomeContentBlock.objects.create(section="why_shiksha", heading="")
        self._draft({"heading": "A perfectly fine heading"})
        body = self.client_for(self.editor).get(CHECKLIST_URL).json()
        self.assertEqual([s["key"] for s in body["sections"]], ["hero"])
        self.assertTrue(body["can_publish"])


class PublishRespectsChecklistTest(StudioApiTestCase):
    def test_publish_is_refused_when_a_check_blocks(self):
        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"heading": ""}}}, format="json")
        res = c.post(PUBLISH_URL, {}, format="json")

        self.assertEqual(res.status_code, 409, res.content)
        self.assertIn("blocking", res.json())
        self.hero.refresh_from_db()
        self.assertEqual(
            self.hero.heading, "Learn with ShikshaCom",
            "a refused publish must not have written anything",
        )

    def test_the_draft_survives_a_refused_publish(self):
        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"heading": ""}}}, format="json")
        c.post(PUBLISH_URL, {}, format="json")
        self.assertEqual(
            ContentDraft.objects.count(), 1,
            "losing the pending edit on a refusal would be data loss",
        )

    def test_a_warning_alone_still_publishes(self):
        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"heading": "x" * 120}}}, format="json")
        res = c.post(PUBLISH_URL, {}, format="json")
        self.assertEqual(res.status_code, 200, res.content)


class LinkTargetsTest(StudioApiTestCase):
    URL = "/api/content/admin/link-targets/"

    def test_offers_real_site_pages(self):
        body = self.client_for(self.editor).get(self.URL).json()
        values = [o["value"] for g in body["groups"] for o in g["options"]]
        self.assertIn("/courses", values)

    def test_includes_course_categories(self):
        from courses.models import CourseCategory
        CourseCategory.objects.create(name="Class 9", group="class8-12")
        body = self.client_for(self.editor).get(self.URL).json()
        labels = [o["label"] for g in body["groups"] for o in g["options"]]
        self.assertIn("Class 9", labels)

    def test_non_staff_is_refused(self):
        self.assertEqual(self.client_for(self.outsider).get(self.URL).status_code, 403)


class DraftExposesLiveValuesTest(StudioApiTestCase):
    """The fields column renders from `values`. If the endpoint omits it,
    every input in the editor is silently blank — the hand-rolled-response
    trap this codebase keeps hitting."""

    def test_sections_carry_their_live_field_values(self):
        self.hero.eyebrow = "For Class 8-12"
        self.hero.save()
        body = self.client_for(self.editor).get(DRAFT_URL).json()
        hero = next(s for s in body["sections"] if s["key"] == "hero")
        self.assertEqual(hero["values"]["heading"], "Learn with ShikshaCom")
        self.assertEqual(hero["values"]["eyebrow"], "For Class 8-12")

    def test_values_cover_every_editable_field(self):
        """A field the editor can write must also be a field it can read."""
        from content.studio_views import PAGES, _editable_fields

        body = self.client_for(self.editor).get(DRAFT_URL).json()
        hero = next(s for s in body["sections"] if s["key"] == "hero")
        missing = _editable_fields(PAGES["home"]["model"]) - set(hero["values"])
        self.assertEqual(missing, set(), f"not readable in the editor: {missing}")

    def test_values_are_json_safe(self):
        import json
        body = self.client_for(self.editor).get(DRAFT_URL).json()
        json.dumps(body)  # an ImageFieldFile would raise


class SectionOrderTest(StudioApiTestCase):
    """The section list claims "the order here is the order visitors see".
    That has to be true before drag-to-reorder means anything."""

    def test_sections_come_back_in_visitor_order(self):
        from content.models import HomeSectionOrder

        HomeSectionOrder.objects.filter(section="hero").update(order=50)
        first = HomeSectionOrder.objects.exclude(section="hero").order_by("order").first()
        HomeSectionOrder.objects.filter(pk=first.pk).update(order=0)

        body = self.client_for(self.editor).get(DRAFT_URL).json()
        ordered = [s["key"] for s in body["sections"] if s["order"] is not None]
        self.assertEqual(ordered[0], first.section)
        self.assertLess(ordered.index(first.section), ordered.index("hero"))

    def test_sections_with_no_order_row_sort_last(self):
        body = self.client_for(self.editor).get(DRAFT_URL).json()
        orders = [s["order"] for s in body["sections"]]
        seen_none = False
        for o in orders:
            if o is None:
                seen_none = True
            else:
                self.assertFalse(
                    seen_none,
                    "a placed section must never sort after an unplaced one",
                )

    def test_reorder_endpoint_demands_the_complete_set(self):
        """A stale tab sending a partial list must not silently drop a section
        off the homepage."""
        res = self.client_for(self.editor).post(
            "/api/content/admin/home-section-order/reorder/",
            {"sections": ["hero"]}, format="json",
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("missing", res.json())

    def test_reorder_persists_and_the_editor_reflects_it(self):
        from content.models import HomeSectionOrder

        keys = list(
            HomeSectionOrder.objects.order_by("order").values_list("section", flat=True)
        )
        flipped = [keys[1], keys[0], *keys[2:]]
        res = self.client_for(self.editor).post(
            "/api/content/admin/home-section-order/reorder/",
            {"sections": flipped}, format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)

        body = self.client_for(self.editor).get(DRAFT_URL).json()
        ordered = [s["key"] for s in body["sections"] if s["order"] is not None]
        self.assertEqual(ordered[:2], flipped[:2])


class DraftValidationTest(StudioApiTestCase):
    """A draft may not carry a value the column can't hold.

    sqlite truncates silently; prod is Postgres, which raises DataError from
    inside the atomic publish. The rollback takes `draft.delete()` with it, so
    the bad draft survives and every later publish fails the same way — the
    page becomes permanently unpublishable. These tests pin the refusal.
    """

    def test_over_length_value_is_rejected_at_draft_time(self):
        c = self.client_for(self.editor)
        res = c.put(
            DRAFT_URL,
            {"sections": {"hero": {"eyebrow": "z" * 500}}},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertIn("hero", body.get("rejected", {}))
        self.assertIn("eyebrow", body["rejected"]["hero"])
        # and nothing was stored
        self.assertEqual(body["change_count"], 0)
        self.assertFalse(ContentDraft.objects.exists())

    def test_publish_refuses_a_bad_value_with_400_not_500(self):
        """A draft written before this guard existed must not 500 forever."""
        from django.contrib.contenttypes.models import ContentType

        ContentDraft.objects.create(
            content_type=ContentType.objects.get_for_model(HomeContentBlock),
            object_id=self.hero.id,
            author=self.editor,
            payload={"eyebrow": "z" * 500},
        )
        res = self.client_for(self.editor).post(PUBLISH_URL, {}, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("eyebrow", res.json()["invalid"]["hero"])
        self.hero.refresh_from_db()
        self.assertEqual(self.hero.eyebrow, "")

    def test_section_is_not_draft_editable(self):
        """`section` is UNIQUE — a draft that sets it locks publishing."""
        res = self.client_for(self.editor).put(
            DRAFT_URL,
            {"sections": {"hero": {"section": "why_shiksha"}}},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn("not editable", res.json()["rejected"]["hero"])
        self.assertEqual(res.json()["change_count"], 0)

    def test_json_field_round_trips_instead_of_flattening_to_empty_string(self):
        self.hero.extra = {"foo": "bar"}
        self.hero.save(update_fields=["extra"])
        body = self.client_for(self.editor).get(DRAFT_URL).json()
        hero = next(s for s in body["sections"] if s["key"] == "hero")
        self.assertEqual(hero["values"]["extra"], {"foo": "bar"})


class ListItemCapabilityTest(StudioApiTestCase):
    """The editor must only offer the list panel where rows are actually shown.

    Five sections' public components take only the content block and never read
    `items`, so a row saved against them was accepted, reported as saved, and
    then invisible on the live site forever.
    """

    def _sections(self):
        body = self.client_for(self.editor).get(DRAFT_URL).json()
        return {s["key"]: s for s in body["sections"]}

    def test_sections_that_render_rows_support_them(self):
        sections = self._sections()
        for key in ("why_shiksha", "browse_categories", "collaborate",
                    "contact_hero", "about_values"):
            with self.subTest(section=key):
                self.assertTrue(sections[key]["supports_list_items"])

    def test_sections_that_ignore_rows_do_not(self):
        sections = self._sections()
        for key in ("hero", "featured_courses", "faq", "cta", "courses_hero"):
            with self.subTest(section=key):
                self.assertFalse(sections[key]["supports_list_items"])

    def test_the_two_with_content_elsewhere_say_where(self):
        sections = self._sections()
        self.assertEqual(
            sections["featured_courses"]["list_source"]["url"], "/content/cards",
        )
        self.assertEqual(
            sections["faq"]["list_source"]["url"], "/content/questions",
        )
        # Hero genuinely has no repeatable content anywhere — no pointer.
        self.assertIsNone(sections["hero"]["list_source"])

    def test_every_section_carries_the_flag(self):
        """A missing key would make the editor treat it as falsey and silently
        hide a panel that should be there."""
        for key, s in self._sections().items():
            with self.subTest(section=key):
                self.assertIn("supports_list_items", s)
                self.assertIsInstance(s["supports_list_items"], bool)


class SectionCapabilityTest(StudioApiTestCase):
    """Everything the editor needs to reach parity with the legacy tab.

    Each of these was owned only by the old Homepage Content screen, which is
    what kept it alive: badge slots, the card-count cap, and the id needed to
    toggle a section's place on the page.
    """

    def _sections(self):
        body = self.client_for(self.editor).get(DRAFT_URL).json()
        return {s["key"]: s for s in body["sections"]}

    def test_only_three_sections_have_badge_slots(self):
        sections = self._sections()
        self.assertEqual(sections["hero"]["floater_slots"], ["cap", "book", "play"])
        self.assertEqual(sections["collaborate"]["floater_slots"], ["top", "bottom"])
        self.assertEqual(
            sections["why_choose"]["floater_slots"], ["b_tl", "b_tr", "b_bl"],
        )
        # everything else has none, so the panel renders nothing
        for key in ("why_shiksha", "featured_courses", "faq", "contact_hero"):
            with self.subTest(section=key):
                self.assertEqual(sections[key]["floater_slots"], [])

    def test_slots_come_from_the_model_not_a_frontend_copy(self):
        """A slot maps 1:1 to a pre-tested CSS position, so an invented one
        renders nowhere. The editor must not keep its own list."""
        from content.models import HomeFloater

        for key, s in self._sections().items():
            with self.subTest(section=key):
                self.assertEqual(
                    s["floater_slots"],
                    HomeFloater.SLOT_CHOICES_BY_SECTION.get(key, []),
                )

    def test_only_featured_courses_caps_its_cards(self):
        sections = self._sections()
        self.assertTrue(sections["featured_courses"]["has_card_cap"])
        for key in ("hero", "why_shiksha", "faq", "cta"):
            with self.subTest(section=key):
                self.assertFalse(sections[key]["has_card_cap"])

    def test_the_cap_round_trips_through_the_draft_as_a_dict(self):
        """It lives in `extra`, a JSONField. Flattening that to "" made the
        editor unable to carry it at all."""
        # The cap lives on this section's own block, so it has to exist.
        HomeContentBlock.objects.create(
            section="featured_courses", heading="Explore our",
        )
        c = self.client_for(self.editor)
        res = c.put(
            DRAFT_URL,
            {"sections": {"featured_courses": {"extra": {"max_cards": 0}}}},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertNotIn("rejected", res.json())
        self.assertEqual(
            res.json()["draft"]["featured_courses"]["extra"], {"max_cards": 0},
        )

    def test_placed_sections_carry_the_order_row_id(self):
        """Without it the editor could show the hidden state but not change
        it, which is what kept the legacy tab alive."""
        hero = self._sections()["hero"]
        self.assertIsNotNone(hero["order_id"])
        self.assertEqual(hero["order_id"], HomeSectionOrder.objects.get(
            section="hero").id)


class PublishStatusIntentTest(StudioApiTestCase):
    """Publishing must not un-hide a section the editor deliberately hid.

    The checklist tells the editor "Publishing saves your changes but the
    section still won't show". Force-setting PUBLISHED made that copy a lie.
    """

    def test_archived_section_stays_archived_through_a_publish(self):
        self.hero.status = PublishStatus.ARCHIVED
        self.hero.save(update_fields=["status"])

        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"subhead": "typo fixed"}}},
              format="json")
        res = c.post(PUBLISH_URL, {}, format="json")

        self.assertEqual(res.status_code, 200, res.content)
        self.hero.refresh_from_db()
        self.assertEqual(self.hero.subhead, "typo fixed")
        self.assertEqual(self.hero.status, PublishStatus.ARCHIVED)

    def test_a_drafted_status_is_honoured(self):
        c = self.client_for(self.editor)
        c.put(DRAFT_URL,
              {"sections": {"hero": {"status": PublishStatus.ARCHIVED}}},
              format="json")
        res = c.post(PUBLISH_URL, {}, format="json")

        self.assertEqual(res.status_code, 200, res.content)
        self.hero.refresh_from_db()
        self.assertEqual(self.hero.status, PublishStatus.ARCHIVED)

    def test_publish_still_defaults_to_published(self):
        self.hero.status = PublishStatus.DRAFT
        self.hero.save(update_fields=["status"])
        c = self.client_for(self.editor)
        c.put(DRAFT_URL, {"sections": {"hero": {"subhead": "live now"}}},
              format="json")
        c.post(PUBLISH_URL, {}, format="json")
        self.hero.refresh_from_db()
        self.assertEqual(self.hero.status, PublishStatus.PUBLISHED)


class RevisionRecordingTest(StudioApiTestCase):
    """Every write a person makes through the admin API leaves history.

    Before this, `record_revision` had ONE production call site — the page
    editor's publish — so editing an FAQ, notice, card, affair, list item,
    badge or tag wrote nothing, the History feed stayed empty, and
    ACTION_CREATED/UPDATED/HIDDEN/DELETED were dead constants.
    """

    def _revisions_for(self, obj):
        from django.contrib.contenttypes.models import ContentType
        return ContentRevision.objects.filter(
            content_type=ContentType.objects.get_for_model(obj.__class__),
            object_id=obj.pk,
        ).order_by("id")

    def test_creating_an_answer_is_recorded(self):
        res = self.client_for(self.editor).post(
            "/api/content/admin/faqs/",
            {"question": "Is it recorded?", "answer_html": "<p>Yes.</p>",
             "page": "general"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)

        from content.models import FAQItem
        faq = FAQItem.objects.get(pk=res.json()["id"])
        revs = self._revisions_for(faq)
        self.assertEqual([r.action for r in revs], [ContentRevision.ACTION_CREATED])
        self.assertEqual(revs[0].actor, self.editor)

    def test_editing_an_answer_records_the_previous_state(self):
        from content.models import FAQItem
        faq = FAQItem.objects.create(
            question="Before", answer_html="<p>Old.</p>", page="general",
        )
        res = self.client_for(self.editor).patch(
            f"/api/content/admin/faqs/{faq.pk}/", {"question": "After"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)

        revs = self._revisions_for(faq)
        self.assertEqual([r.action for r in revs], [ContentRevision.ACTION_UPDATED])
        # the snapshot is the state BEFORE the edit — that is what makes undo work
        self.assertEqual(revs[0].snapshot["question"], "Before")

    def test_a_status_change_reads_as_published_or_hidden_not_updated(self):
        from content.models import FAQItem
        faq = FAQItem.objects.create(
            question="Q", answer_html="<p>A</p>", page="general",
            status=PublishStatus.DRAFT,
        )
        c = self.client_for(self.editor)
        c.patch(f"/api/content/admin/faqs/{faq.pk}/",
                {"status": PublishStatus.PUBLISHED}, format="json")
        c.patch(f"/api/content/admin/faqs/{faq.pk}/",
                {"status": PublishStatus.ARCHIVED}, format="json")

        self.assertEqual(
            [r.action for r in self._revisions_for(faq)],
            [ContentRevision.ACTION_PUBLISHED, ContentRevision.ACTION_HIDDEN],
        )

    def test_deleting_is_recorded_with_its_last_state(self):
        from content.models import FAQItem
        faq = FAQItem.objects.create(
            question="Doomed", answer_html="<p>A</p>", page="general",
        )
        pk = faq.pk
        res = self.client_for(self.editor).delete(
            f"/api/content/admin/faqs/{pk}/",
        )
        self.assertEqual(res.status_code, 204, res.content)
        self.assertFalse(FAQItem.objects.filter(pk=pk).exists())

        from django.contrib.contenttypes.models import ContentType
        rev = ContentRevision.objects.get(
            content_type=ContentType.objects.get_for_model(FAQItem),
            object_id=pk,
        )
        self.assertEqual(rev.action, ContentRevision.ACTION_DELETED)
        self.assertEqual(rev.snapshot["question"], "Doomed")

    def test_seeding_directly_writes_no_history(self):
        """The whole reason this is not a post_save signal: a migration or
        seed run must not fill the feed with entries nobody caused."""
        from content.models import FAQItem
        faq = FAQItem.objects.create(
            question="Seeded", answer_html="<p>A</p>", page="general",
        )
        self.assertEqual(self._revisions_for(faq).count(), 0)


class RestoreKeepsRelationsTest(StudioApiTestCase):
    def test_restoring_a_post_brings_its_tags_back(self):
        """`concrete_fields` excludes M2M, so undo used to restore a post's
        text and permanently lose its tags — while the snapshot held them
        the whole time."""
        from content.models import BlogPost, ContentTag
        from content.revisions import restore_revision, snapshot_of

        tag = ContentTag.objects.create(name="ncert")
        post = BlogPost.objects.create(title="With tags")
        post.tags.set([tag])

        snapshot = snapshot_of(post)
        self.assertIn("tags", snapshot, "the serializer should capture M2M")

        rev = record_revision(
            post, ContentRevision.ACTION_UPDATED, actor=self.editor,
            snapshot=snapshot,
        )

        post.tags.clear()
        post.title = "Stripped"
        post.save()

        restored = restore_revision(rev, actor=self.editor)
        self.assertEqual(restored.title, "With tags")
        self.assertEqual([t.name for t in restored.tags.all()], ["ncert"])

    def test_a_tag_deleted_since_the_snapshot_does_not_break_the_restore(self):
        from content.models import BlogPost, ContentTag
        from content.revisions import restore_revision, snapshot_of

        keep = ContentTag.objects.create(name="keep")
        gone = ContentTag.objects.create(name="gone")
        post = BlogPost.objects.create(title="Two tags")
        post.tags.set([keep, gone])

        rev = record_revision(
            post, ContentRevision.ACTION_UPDATED, actor=self.editor,
            snapshot=snapshot_of(post),
        )
        gone.delete()
        post.tags.clear()

        restored = restore_revision(rev, actor=self.editor)
        self.assertEqual([t.name for t in restored.tags.all()], ["keep"])


class StudioFlagGateTest(StudioApiTestCase):
    """`content_studio_enabled` has to actually gate something.

    It was described as a real gate from the start, but nothing enforced it:
    turning it off hid the Studio nav while leaving every Studio endpoint open
    to any staff user. A flag that only hides its own front door is not a gate.
    """

    STUDIO_URLS = [
        DRAFT_URL,
        ACTIVITY_URL,
        "/api/content/admin/media/",
        "/api/content/admin/labels/",
        "/api/content/admin/inbox/",
        "/api/content/admin/calendar/",
        "/api/content/admin/exams/readiness/",
    ]

    def _set_flag(self, value):
        from django.core.cache import cache
        from global_settings.models import GlobalSettings

        from content.permissions import IsStudioEditor

        gs = GlobalSettings.load()
        gs.content_studio_enabled = value
        gs.save(update_fields=["content_studio_enabled"])
        # The permission caches the flag for a minute; a test flipping it wants
        # the change now, not on the next TTL boundary.
        cache.delete(IsStudioEditor.CACHE_KEY)

    def test_studio_endpoints_answer_normally_when_the_flag_is_on(self):
        self._set_flag(True)
        c = self.client_for(self.editor)
        for url in self.STUDIO_URLS:
            with self.subTest(url=url):
                self.assertEqual(c.get(url).status_code, 200, url)

    def test_every_studio_endpoint_is_refused_when_the_flag_is_off(self):
        self._set_flag(False)
        c = self.client_for(self.editor)
        for url in self.STUDIO_URLS:
            with self.subTest(url=url):
                self.assertEqual(c.get(url).status_code, 403, url)

    def test_the_flag_does_not_switch_off_the_rest_of_the_cms(self):
        """Blog Posts still runs on the older admin viewsets. The flag governs
        the Studio, not the whole CMS."""
        self._set_flag(False)
        c = self.client_for(self.editor)
        self.assertEqual(c.get("/api/content/admin/blogs/").status_code, 200)
        self.assertEqual(c.get("/api/content/admin/faqs/").status_code, 200)

    def test_writes_are_gated_too_not_just_reads(self):
        self._set_flag(False)
        res = self.client_for(self.editor).put(
            DRAFT_URL, {"sections": {"hero": {"heading": "Sneaky"}}},
            format="json",
        )
        self.assertEqual(res.status_code, 403, res.content)
        self.hero.refresh_from_db()
        self.assertEqual(self.hero.heading, "Learn with ShikshaCom")
