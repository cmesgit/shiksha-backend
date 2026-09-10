"""Live Ticker Phase 1a — the model layer.

design_handoff_live_ticker. The ticker extends `Announcement` rather than
adding a model, so most of what matters here is that the EXISTING navbar
behaviour is unchanged while the new fields do what the design needs.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from content.models import (
    Announcement, PublishStatus, TickerKind, TickerSlot,
)


def _pub(**kw):
    """A published, currently-live announcement."""
    kw.setdefault("message", "hello")
    kw.setdefault("status", PublishStatus.PUBLISHED)
    kw.setdefault("starts_at", timezone.now() - timedelta(hours=1))
    kw.setdefault("slots", [TickerSlot.NAVBAR])
    return Announcement.objects.create(**kw)


class LiveWindowTest(TestCase):
    """`live()` is the window query the ticker inherits — pin its edges."""

    def test_not_live_before_starts_at(self):
        _pub(starts_at=timezone.now() + timedelta(hours=1))
        self.assertEqual(Announcement.objects.live().count(), 0)

    def test_not_live_after_ends_at(self):
        _pub(starts_at=timezone.now() - timedelta(days=2),
             ends_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(Announcement.objects.live().count(), 0)

    def test_live_with_null_ends_at(self):
        _pub(ends_at=None)
        self.assertEqual(Announcement.objects.live().count(), 1)

    def test_review_status_is_not_live(self):
        """REVIEW is 'being looked at', not 'published'. A ticker item awaiting
        review must never reach a visitor."""
        _pub(status=PublishStatus.REVIEW)
        self.assertEqual(Announcement.objects.live().count(), 0)

    def test_draft_is_not_live(self):
        _pub(status=PublishStatus.DRAFT)
        self.assertEqual(Announcement.objects.live().count(), 0)


class ForSlotTest(TestCase):
    """⚠ The reason `for_slot` casts to text instead of using
    `slots__contains=[slot]`: that lookup raises NotSupportedError on SQLite,
    which is what the test settings run. Measured on both engines 2026-09-10.
    These tests therefore execute the same code path production does."""

    def test_filters_to_the_named_slot(self):
        _pub(message="strip", slots=[TickerSlot.NAVBAR])
        _pub(message="card", slots=[TickerSlot.HERO], kind=TickerKind.MILESTONE)
        self.assertEqual(
            [a.message for a in Announcement.objects.for_slot(TickerSlot.NAVBAR)],
            ["strip"])
        self.assertEqual(
            [a.message for a in Announcement.objects.for_slot(TickerSlot.HERO)],
            ["card"])

    def test_one_item_can_target_several_slots(self):
        _pub(message="both", slots=[TickerSlot.NAVBAR, TickerSlot.HERO],
             kind=TickerKind.MILESTONE)
        self.assertEqual(Announcement.objects.for_slot(TickerSlot.NAVBAR).count(), 1)
        self.assertEqual(Announcement.objects.for_slot(TickerSlot.HERO).count(), 1)

    def test_a_slot_name_cannot_match_inside_another(self):
        """The needle is quoted, so `auth_login` must not satisfy a query for
        `auth_signup` or vice versa — the failure a bare substring match would
        produce, and the one hardest to spot by eye."""
        _pub(message="login-only", slots=[TickerSlot.AUTH_LOGIN],
             kind=TickerKind.MENTOR_SPOTLIGHT)
        self.assertEqual(
            Announcement.objects.for_slot(TickerSlot.AUTH_SIGNUP).count(), 0)
        self.assertEqual(
            Announcement.objects.for_slot(TickerSlot.AUTH_LOGIN).count(), 1)

    def test_it_still_respects_the_live_window(self):
        _pub(message="expired", slots=[TickerSlot.HERO], kind=TickerKind.MILESTONE,
             starts_at=timezone.now() - timedelta(days=2),
             ends_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(Announcement.objects.for_slot(TickerSlot.HERO).count(), 0)

    def test_pinned_sorts_first(self):
        _pub(message="ordinary", slots=[TickerSlot.HERO],
             kind=TickerKind.MILESTONE, order=0)
        _pub(message="pinned", slots=[TickerSlot.HERO],
             kind=TickerKind.MILESTONE, order=9, pinned=True)
        self.assertEqual(
            [a.message for a in Announcement.objects.for_slot(TickerSlot.HERO)],
            ["pinned", "ordinary"])


class SlotValidationTest(TestCase):
    def test_unknown_slot_is_rejected(self):
        a = Announcement(message="x", slots=["nope"])
        with self.assertRaises(ValidationError) as cm:
            a.full_clean()
        self.assertIn("slots", cm.exception.error_dict)

    def test_slots_must_be_a_list(self):
        a = Announcement(message="x", slots={"navbar": True})
        with self.assertRaises(ValidationError) as cm:
            a.full_clean()
        self.assertIn("slots", cm.exception.error_dict)

    def test_every_enum_slot_is_accepted_even_if_its_surface_is_unbuilt(self):
        """Mirrors the ShowcaseCourse.categories rule. Most of these eight
        surfaces do not exist yet; if that made their items unsaveable, the
        queue could not be filled before the screens ship — and later, turning
        a surface off would corrupt every item still tagged with it."""
        a = Announcement(message="x", slots=list(TickerSlot.values),
                         kind=TickerKind.MILESTONE, metric_value="1")
        a.full_clean()  # must not raise

    def test_empty_slots_is_allowed(self):
        Announcement(message="x", slots=[]).full_clean()


class KindValidationTest(TestCase):
    def test_kind_is_not_required_for_a_navbar_only_item(self):
        """The pre-ticker behaviour. Every existing row is navbar-only with no
        kind, and must stay valid without being touched."""
        Announcement(message="x", slots=[TickerSlot.NAVBAR]).full_clean()

    def test_kind_is_required_once_a_card_slot_is_targeted(self):
        a = Announcement(message="x", slots=[TickerSlot.HERO])
        with self.assertRaises(ValidationError) as cm:
            a.full_clean()
        self.assertIn("kind", cm.exception.error_dict)

    def test_blog_post_is_not_a_valid_kind(self):
        """Named in the design Q&A, never drawn, so deliberately not shipped.
        Pinned so re-adding it is a decision and not a drift."""
        self.assertNotIn("blog_post", TickerKind.values)
        self.assertEqual(len(TickerKind.values), 8)


class MetricTest(TestCase):
    def test_a_stored_metric_is_returned_as_typed(self):
        a = _pub(slots=[TickerSlot.HERO], kind=TickerKind.MILESTONE,
                 metric_value="2,400", metric_label="STUDENTS")
        self.assertEqual(a.resolved_metric, ("2,400", "STUDENTS"))

    def test_deadline_derives_days_left_from_ends_at(self):
        a = _pub(slots=[TickerSlot.HERO], kind=TickerKind.DEADLINE,
                 ends_at=timezone.now() + timedelta(days=21, hours=1))
        self.assertEqual(a.resolved_metric, ("21", "DAYS LEFT"))

    def test_deadline_label_is_singular_at_one_day(self):
        a = _pub(slots=[TickerSlot.HERO], kind=TickerKind.DEADLINE,
                 ends_at=timezone.now() + timedelta(days=1, hours=1))
        self.assertEqual(a.resolved_metric, ("1", "DAY LEFT"))

    def test_a_deadline_metric_cannot_be_stored(self):
        """⚠ THE POINT. A stored '21 DAYS LEFT' is wrong tomorrow and nothing
        would ever correct it. Refusing beats accepting-then-ignoring, which
        would show the admin their number vanishing with nothing to debug."""
        a = Announcement(message="x", slots=[TickerSlot.HERO],
                         kind=TickerKind.DEADLINE, metric_value="21",
                         ends_at=timezone.now() + timedelta(days=21))
        with self.assertRaises(ValidationError) as cm:
            a.full_clean()
        self.assertIn("metric_value", cm.exception.error_dict)

    def test_deadline_with_no_end_time_has_no_metric(self):
        a = _pub(slots=[TickerSlot.HERO], kind=TickerKind.DEADLINE, ends_at=None)
        self.assertIsNone(a.resolved_metric)

    def test_glyph_kinds_refuse_a_metric(self):
        for kind in (TickerKind.NEW_COURSE, TickerKind.CURRENT_AFFAIRS,
                     TickerKind.NEW_MENTOR):
            with self.subTest(kind=kind):
                a = Announcement(message="x", slots=[TickerSlot.HERO],
                                 kind=kind, metric_value="5")
                with self.assertRaises(ValidationError) as cm:
                    a.full_clean()
                self.assertIn("metric_value", cm.exception.error_dict)

    def test_no_metric_when_none_was_set(self):
        a = _pub(slots=[TickerSlot.HERO], kind=TickerKind.NEW_COURSE)
        self.assertIsNone(a.resolved_metric)


class BackfillMigrationTest(TestCase):
    """⚠ THE POINT OF THIS TEST. `slots = JSONField(default=list)` applies only
    to rows created after the migration; every row that already exists gets
    `[]`, and `for_slot("navbar")` then matches none of them — silently
    emptying the one ticker slot already live on the public site. A field
    default cannot fix that, so 0036 does it as data.

    Runs the migration's own function against rows in the state dev and prod
    are actually in, the way `global_settings.tests` tests migration 0012 —
    the executor cannot unapply inside the test transaction on SQLite.
    """

    def _run(self, direction="forward"):
        import importlib

        from django.apps import apps as real_apps

        # importlib, because a module name starting with a digit cannot be
        # reached by an import statement.
        mod = importlib.import_module(
            "content.migrations.0036_backfill_announcement_slots")
        fn = mod.set_navbar_slot if direction == "forward" else mod.clear_navbar_slot
        fn(real_apps, None)

    def test_it_makes_the_navbar_slot_explicit(self):
        """The backfill writes down what the fallback already infers.

        Both exist on purpose. The fallback in `for_slot` is what stops a row
        created without slots from vanishing; the backfill is what makes the
        value real, so the Phase 2 admin form shows "navbar" ticked instead of
        nothing, and so the data does not depend on a query-time rule that a
        later refactor could drop.
        """
        a = Announcement.objects.create(message="legacy", slots=[])
        self.assertEqual(Announcement.objects.for_slot(TickerSlot.NAVBAR).count(), 1,
                         "the fallback already covers it")
        self._run()
        a.refresh_from_db()
        self.assertEqual(a.slots, ["navbar"], "now explicit, not inferred")
        self.assertEqual(Announcement.objects.for_slot(TickerSlot.NAVBAR).count(), 1)

    def test_status_defaults_to_published_so_the_backfill_is_immediately_live(self):
        """⚠ `StatusedContentModel.status` defaults to **PUBLISHED**
        (`content/models.py:127-130`), and `starts_at` to now. So a bare
        `create()` is live the instant it is written — there is no draft step
        to catch a mistake. That is why the backfill takes effect immediately
        rather than waiting for anyone to publish, and it is worth knowing
        before the admin form ships in Phase 2."""
        a = Announcement.objects.create(message="legacy", slots=[])
        self.assertEqual(a.status, PublishStatus.PUBLISHED)
        self._run()
        self.assertEqual(
            Announcement.objects.for_slot(TickerSlot.NAVBAR).count(), 1)

    def test_it_does_not_touch_a_row_that_already_chose_its_slots(self):
        a = Announcement.objects.create(
            message="deliberate", slots=[TickerSlot.HERO],
            kind=TickerKind.MILESTONE)
        self._run()
        a.refresh_from_db()
        self.assertEqual(a.slots, ["hero"])

    def test_it_is_idempotent(self):
        a = Announcement.objects.create(message="legacy", slots=[])
        self._run()
        self._run()
        a.refresh_from_db()
        self.assertEqual(a.slots, ["navbar"])

    def test_reverse_leaves_a_richer_row_alone(self):
        plain = Announcement.objects.create(message="p", slots=["navbar"])
        rich = Announcement.objects.create(
            message="r", slots=["navbar", "hero"], kind=TickerKind.MILESTONE)
        self._run("reverse")
        plain.refresh_from_db()
        rich.refresh_from_db()
        self.assertEqual(plain.slots, [])
        self.assertEqual(rich.slots, ["navbar", "hero"])


class ExistingBehaviourUnchangedTest(TestCase):
    """Slot 1 is already live on the public site. Phase 1 must not move it."""

    def test_ends_at_must_still_be_after_starts_at(self):
        now = timezone.now()
        a = Announcement(message="x", starts_at=now, ends_at=now)
        with self.assertRaises(ValidationError) as cm:
            a.full_clean()
        self.assertIn("ends_at", cm.exception.error_dict)

    def test_a_row_created_the_old_way_is_still_valid_and_live(self):
        """No slots, no kind, no metric — exactly what every pre-Phase-1 row
        looks like."""
        a = Announcement.objects.create(
            message="legacy", status=PublishStatus.PUBLISHED,
            starts_at=timezone.now() - timedelta(hours=1),
        )
        a.full_clean()
        self.assertEqual(list(Announcement.objects.live()), [a])

    def test_default_ordering_is_untouched(self):
        self.assertEqual(Announcement._meta.ordering, ["order", "-starts_at"])


# ══════════════════════════════════════════════════════════════════════
#  Phase 1b — serializers and endpoints
# ══════════════════════════════════════════════════════════════════════

class PublicEndpointTest(TestCase):
    """GET /api/content/announcements/[?slot=]"""

    URL = "/api/content/announcements/"

    def setUp(self):
        from django.core.cache import cache
        # The list view memoises on (content version, path, query). Rows
        # created in setUp bump the version, but clearing keeps each test
        # honest about what it is actually asserting.
        cache.clear()

    def test_the_navbar_payload_is_unchanged(self):
        """⚠ THE REGRESSION THAT MATTERS. The site-wide strip has always
        called this endpoint with no parameters. Phase 1 must not change one
        key it reads, one value, or which rows come back. The ticker fields
        are additive — present, but nothing the strip looks at moved."""
        a = _pub(message="hi", link_url="/courses", link_label="See",
                 slots=[TickerSlot.NAVBAR])
        res = self.client.get(self.URL)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(len(body), 1)
        row = body[0]
        for key, expected in [
            ("id", a.id), ("message", "hi"), ("link_url", "/courses"),
            ("link_label", "See"), ("level", "info"),
        ]:
            self.assertEqual(row[key], expected, f"{key} changed")
        self.assertIn("updated_at", row,
                      "the navbar keys its dismissed flag on (id, updated_at)")

    def test_no_slot_param_means_navbar_only(self):
        _pub(message="strip", slots=[TickerSlot.NAVBAR])
        _pub(message="card", slots=[TickerSlot.HERO], kind=TickerKind.MILESTONE)
        self.assertEqual([r["message"] for r in self.client.get(self.URL).json()],
                         ["strip"])

    def test_slot_param_selects_a_surface(self):
        _pub(message="strip", slots=[TickerSlot.NAVBAR])
        _pub(message="card", slots=[TickerSlot.HERO], kind=TickerKind.MILESTONE)
        res = self.client.get(self.URL, {"slot": TickerSlot.HERO})
        self.assertEqual([r["message"] for r in res.json()], ["card"])

    def test_an_unknown_slot_is_a_400_not_an_empty_list(self):
        """An empty list would be indistinguishable from 'nothing scheduled
        here' — the same failure shape as an outage rendering as no content."""
        res = self.client.get(self.URL, {"slot": "nonsense"})
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("slot", res.json())

    def test_slots_is_never_exposed_publicly(self):
        """The server decides placement. Publishing the plan would also leak
        which unreleased surfaces are being staged."""
        _pub(message="x", slots=[TickerSlot.NAVBAR, TickerSlot.HERO],
             kind=TickerKind.MILESTONE)
        self.assertNotIn("slots", self.client.get(self.URL).json()[0])

    def test_a_card_slot_carries_kind_body_and_metric(self):
        _pub(message="2,400 students", slots=[TickerSlot.HERO],
             kind=TickerKind.MILESTONE, body="since April",
             metric_value="2,400", metric_label="STUDENTS")
        row = self.client.get(self.URL, {"slot": TickerSlot.HERO}).json()[0]
        self.assertEqual(row["kind"], "milestone")
        self.assertEqual(row["body"], "since April")
        self.assertEqual(row["metric"], {"value": "2,400", "label": "STUDENTS"})

    def test_a_deadline_metric_is_computed_at_read_time(self):
        _pub(message="closes soon", slots=[TickerSlot.HERO],
             kind=TickerKind.DEADLINE,
             ends_at=timezone.now() + timedelta(days=21, hours=1))
        row = self.client.get(self.URL, {"slot": TickerSlot.HERO}).json()[0]
        self.assertEqual(row["metric"], {"value": "21", "label": "DAYS LEFT"})

    def test_metric_is_null_when_there_is_none(self):
        _pub(message="x", slots=[TickerSlot.HERO], kind=TickerKind.NEW_COURSE)
        self.assertIsNone(
            self.client.get(self.URL, {"slot": TickerSlot.HERO}).json()[0]["metric"])

    def test_the_cache_does_not_serve_one_slot_to_another(self):
        """`list_cache_key` folds the query string in. If it ever stopped,
        the hero would silently render the navbar's content."""
        _pub(message="strip", slots=[TickerSlot.NAVBAR])
        _pub(message="card", slots=[TickerSlot.HERO], kind=TickerKind.MILESTONE)
        first = self.client.get(self.URL).json()            # populates cache
        second = self.client.get(self.URL, {"slot": TickerSlot.HERO}).json()
        self.assertEqual([r["message"] for r in first], ["strip"])
        self.assertEqual([r["message"] for r in second], ["card"])


class AdminEndpointTest(TestCase):
    """PUT/POST /api/content/admin/announcements/ — the Phase 2 form's API."""

    URL = "/api/content/admin/announcements/"

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.core.cache import cache

        from content.permissions import IsStudioEditor
        # The Studio permission caches content_studio_enabled, and Django rolls
        # back the DB but not the cache between tests.
        cache.delete(IsStudioEditor.CACHE_KEY)
        cache.clear()
        self.editor = get_user_model().objects.create_user(
            username="ticker-ed", email="ticker-ed@example.com",
            password="x", is_staff=True,
        )

    def _client(self):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(user=self.editor)
        return c

    def test_slots_round_trip(self):
        res = self._client().post(self.URL, {
            "message": "card", "slots": ["hero", "footer"], "kind": "milestone",
            "metric_value": "2,400", "metric_label": "STUDENTS",
        }, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["slots"], ["hero", "footer"])
        self.assertEqual(Announcement.objects.get().slots, ["hero", "footer"])

    def test_an_unknown_slot_is_a_400_not_a_500(self):
        """FullCleanMixin is what turns Announcement.clean() into a readable
        field error. Without it this is an uncaught ValidationError — a 500."""
        res = self._client().post(self.URL, {
            "message": "x", "slots": ["nope"],
        }, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("slots", res.json())

    def test_a_card_slot_without_a_kind_is_a_400(self):
        res = self._client().post(self.URL, {
            "message": "x", "slots": ["hero"],
        }, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("kind", res.json())

    def test_a_stored_deadline_metric_is_a_400(self):
        res = self._client().post(self.URL, {
            "message": "x", "slots": ["hero"], "kind": "deadline",
            "metric_value": "21",
            "ends_at": (timezone.now() + timedelta(days=21)).isoformat(),
        }, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("metric_value", res.json())

    def test_ends_at_before_starts_at_is_still_a_400(self):
        now = timezone.now()
        res = self._client().post(self.URL, {
            "message": "x", "starts_at": now.isoformat(), "ends_at": now.isoformat(),
        }, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("ends_at", res.json())

    def test_the_admin_sees_img_and_every_ticker_field(self):
        """The Phase 2 form needs `img` for its thumbnail — the mixin supplies
        only the resolver, so a missing `img = SerializerMethodField()`
        declaration would drop it silently."""
        _pub(message="x", slots=[TickerSlot.HERO], kind=TickerKind.MILESTONE,
             image_url="https://cdn.example.com/a.png", metric_value="9")
        row = self._client().get(self.URL).json()
        row = (row["results"] if isinstance(row, dict) else row)[0]
        for key in ("slots", "kind", "body", "image", "image_url", "img",
                    "pinned", "metric_value", "metric_label", "status"):
            self.assertIn(key, row, f"admin serializer is missing {key}")
        self.assertEqual(row["img"], "https://cdn.example.com/a.png")

    def test_a_draft_ticker_item_never_reaches_the_public_endpoint(self):
        self._client().post(self.URL, {
            "message": "unpublished", "slots": ["hero"], "kind": "milestone",
            "status": "draft",
        }, format="json")
        res = self.client.get("/api/content/announcements/", {"slot": "hero"})
        self.assertEqual(res.json(), [])


class EmptySlotsFallsBackToNavbarTest(TestCase):
    """⚠ CAUGHT BY AN EXISTING TEST, NOT A NEW ONE.

    `content.tests.test_content.OtherEndpointTests.test_announcement_live_window`
    creates announcements with no `slots` and expects them on the public
    endpoint. It failed the moment `for_slot` started filtering, because the
    field defaults to `[]`.

    Migration 0036 backfills the rows that existed at migration time, but
    nothing stops a NEW row being created without slots — the Django admin, a
    fixture, a management command, a seeding script. Each would save fine and
    then be invisible on the live strip with no error raised anywhere. So an
    empty `slots` is treated as navbar-only, which is also what the field's
    own help_text promises.
    """

    def test_a_row_with_no_slots_is_on_the_navbar(self):
        Announcement.objects.create(
            message="untargeted", status=PublishStatus.PUBLISHED,
            starts_at=timezone.now() - timedelta(hours=1),
        )
        self.assertEqual(
            [a.message for a in Announcement.objects.for_slot(TickerSlot.NAVBAR)],
            ["untargeted"])

    def test_a_row_with_no_slots_is_NOT_on_a_card_slot(self):
        """The fallback is navbar-only. An untargeted row must not leak onto
        the homepage hero — it has no kind, so there is no card to render."""
        Announcement.objects.create(
            message="untargeted", status=PublishStatus.PUBLISHED,
            starts_at=timezone.now() - timedelta(hours=1),
        )
        for slot in TickerSlot.values:
            if slot == TickerSlot.NAVBAR:
                continue
            with self.subTest(slot=slot):
                self.assertEqual(Announcement.objects.for_slot(slot).count(), 0)

    def test_it_reaches_the_public_endpoint(self):
        from django.core.cache import cache
        cache.clear()
        Announcement.objects.create(
            message="untargeted", status=PublishStatus.PUBLISHED,
            starts_at=timezone.now() - timedelta(hours=1),
        )
        res = self.client.get("/api/content/announcements/")
        self.assertEqual([r["message"] for r in res.json()], ["untargeted"])


# ══════════════════════════════════════════════════════════════════════
#  Phase 4 — the homepage band as a real, orderable section
# ══════════════════════════════════════════════════════════════════════

class TickerBandSectionTest(TestCase):
    """The band is a `HomeSection`, so an admin positions it with the same
    drag-reorder as everything else instead of it living at a hardcoded index.
    """

    def test_it_is_a_homepage_section(self):
        from content.models import HOMEPAGE_SECTIONS, HomeSection
        self.assertIn(HomeSection.TICKER_BAND, HOMEPAGE_SECTIONS)

    def test_it_does_not_offer_a_list_item_panel(self):
        """Its cards come from the ticker queue, not HomeListItem. Offering
        the panel would let an editor fill in rows that can never render —
        the exact trap SECTIONS_WITH_LIST_ITEMS was introduced to close."""
        from content.models import (
            LIST_CONTENT_ELSEWHERE, SECTIONS_WITH_LIST_ITEMS, HomeSection,
        )
        self.assertNotIn(HomeSection.TICKER_BAND, SECTIONS_WITH_LIST_ITEMS)
        # ...but it says where the content DOES come from, rather than just
        # hiding the panel and leaving the editor to guess.
        self.assertIn(HomeSection.TICKER_BAND, LIST_CONTENT_ELSEWHERE)
        self.assertEqual(
            LIST_CONTENT_ELSEWHERE[HomeSection.TICKER_BAND]["url"],
            "/content/ticker")

    def test_the_value_fits_the_column(self):
        """`section` is max_length=24 across four models; a longer key would
        need an AlterField on all of them."""
        from content.models import HomeSection
        self.assertLessEqual(len(HomeSection.TICKER_BAND.value), 24)

    def test_the_migration_appends_an_order_row_without_renumbering(self):
        """⚠ THE POINT. Without a HomeSectionOrder row the band can never
        render — ShikshaHome.jsx builds its list from the order endpoint — and
        `reorder/` 400s on a set that omits it. Appending rather than
        inserting is what stops an existing arrangement being reshuffled."""
        import importlib

        from django.apps import apps as real_apps

        from content.models import HomeSectionOrder

        HomeSectionOrder.objects.all().delete()
        HomeSectionOrder.objects.create(section="hero", order=0)
        HomeSectionOrder.objects.create(section="cta", order=1)

        mod = importlib.import_module(
            "content.migrations.0039_ticker_band_section_order")
        mod.add_row(real_apps, None)

        row = HomeSectionOrder.objects.get(section="ticker_band")
        self.assertEqual(row.order, 2, "should append, not insert")
        self.assertTrue(row.is_visible)
        # the pre-existing arrangement is untouched
        self.assertEqual(HomeSectionOrder.objects.get(section="hero").order, 0)
        self.assertEqual(HomeSectionOrder.objects.get(section="cta").order, 1)

    def test_the_migration_is_idempotent(self):
        import importlib

        from django.apps import apps as real_apps

        from content.models import HomeSectionOrder

        mod = importlib.import_module(
            "content.migrations.0039_ticker_band_section_order")
        mod.add_row(real_apps, None)
        mod.add_row(real_apps, None)
        self.assertEqual(
            HomeSectionOrder.objects.filter(section="ticker_band").count(), 1)

    def test_the_band_slot_is_addressable(self):
        _pub(message="band card", slots=[TickerSlot.HOME_BAND],
             kind=TickerKind.MILESTONE, metric_value="9")
        self.assertEqual(
            Announcement.objects.for_slot(TickerSlot.HOME_BAND).count(), 1)


class ImageDownscaleTest(TestCase):
    """A ticker picture is downscaled and recompressed on upload.

    ⚠ The point: `validate_cms_image` CAPS uploads at 2560px/5MB but never
    shrank them, so a phone photo was served at full weight into a 168px
    card — the most expensive image on the homepage doing the least work.
    """

    def _png(self, w, h):
        from io import BytesIO
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (w, h), (10, 120, 90)).save(buf, format="PNG")
        return SimpleUploadedFile("big.png", buf.getvalue(), content_type="image/png")

    def _open(self, a):
        from PIL import Image
        a.image.open()
        return Image.open(a.image)

    def test_a_wide_upload_is_downscaled_and_converted(self):
        a = Announcement.objects.create(message="x", image=self._png(2400, 1200))
        with self._open(a) as im:
            self.assertEqual(im.width, 1600, "should cap at TICKER_MAX_WIDTH")
            self.assertEqual(im.height, 800, "aspect ratio must be preserved")
            self.assertEqual((im.format or "").upper(), "WEBP")

    def test_a_small_upload_is_not_enlarged(self):
        a = Announcement.objects.create(message="x", image=self._png(400, 300))
        with self._open(a) as im:
            self.assertEqual(im.width, 400, "must never upscale")
            self.assertEqual(im.height, 300)

    def test_resaving_does_not_recompress(self):
        """⚠ Guarded on _committed. Without it, every unrelated edit — the
        row's on/off switch, say — would re-open and re-encode the same
        image, losing quality on each save."""
        a = Announcement.objects.create(message="x", image=self._png(2400, 1200))
        first = a.image.name
        a.status = PublishStatus.DRAFT
        a.save()
        a.refresh_from_db()
        self.assertEqual(a.image.name, first, "the file should not be rewritten")

    def test_an_item_with_no_image_saves_normally(self):
        a = Announcement.objects.create(message="x")
        self.assertFalse(a.image)
