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

    def test_it_puts_pre_ticker_rows_into_the_navbar_slot(self):
        a = Announcement.objects.create(message="legacy", slots=[])
        self.assertEqual(Announcement.objects.for_slot(TickerSlot.NAVBAR).count(), 0,
                         "unreachable before the backfill — the bug 0036 exists for")
        self._run()
        a.refresh_from_db()
        self.assertEqual(a.slots, ["navbar"])
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
