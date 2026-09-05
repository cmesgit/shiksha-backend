# PLACEMENT: backend/content/tests/test_seed_contact_copy.py
#
#     python manage.py test content.tests.test_seed_contact_copy
#
# The single most important assertion in here is that a DRY RUN WRITES NOTHING.
# transaction.savepoint() is a silent no-op in autocommit mode, so a dry run
# built on savepoint_rollback() commits every row it claims to be discarding —
# this repo has shipped that exact bug (seed_academy_launch). Proving the
# rollback for real is the point of this file.

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from content.models import (
    HomeContentBlock, HomeListItem, HomeListVariant, HomeSection, PublishStatus,
)


def seed(*args):
    out = StringIO()
    call_command("seed_contact_v2_copy", *args, stdout=out, stderr=StringIO())
    return out.getvalue()


class SeedContactCopyTests(TestCase):
    def _live_rows(self):
        """The state production is actually in: the previous page's copy."""
        HomeContentBlock.objects.create(
            section=HomeSection.CONTACT_HERO,
            heading="Contact ShikshaCom",
            subhead="Get in touch with us! Here is how you can reach ShikshaCom.",
            status=PublishStatus.PUBLISHED,
        )
        for order, (icon, title, body) in enumerate([
            ("location", "Head Office", "House No. - 1473A<br>Maruti Vihar"),
            ("location", "Regional Office Address", "Hualngohmun Vengchhak"),
            ("email", "Email", "info@shikshacom.com"),
            ("phone", "Phone", "+0124-4255138 (Haryana)"),
        ]):
            HomeListItem.objects.create(
                section=HomeSection.CONTACT_HERO,
                variant=HomeListVariant.CONTACT_CARD,
                icon=icon, title=title, body=body, order=order,
                status=PublishStatus.PUBLISHED,
            )

    # ── the trap ──────────────────────────────────────────────────

    def test_dry_run_writes_absolutely_nothing(self):
        self._live_rows()
        out = seed()

        self.assertIn("DRY RUN", out)
        block = HomeContentBlock.objects.get(section=HomeSection.CONTACT_HERO)
        self.assertEqual(block.heading, "Contact ShikshaCom")
        self.assertEqual(
            HomeListItem.objects.get(order=1).title, "Regional Office Address"
        )

    def test_dry_run_on_an_empty_cms_creates_no_rows(self):
        """The create path has to roll back too, not just the update path."""
        seed()
        self.assertFalse(HomeContentBlock.objects.exists())
        self.assertFalse(HomeListItem.objects.exists())

    # ── applying ──────────────────────────────────────────────────

    def test_yes_moves_the_live_rows_onto_the_new_copy(self):
        self._live_rows()
        seed("--yes")

        block = HomeContentBlock.objects.get(section=HomeSection.CONTACT_HERO)
        self.assertEqual(block.heading, "We would love to\nhear")
        self.assertEqual(block.heading_secondary, "from you.")
        self.assertEqual(block.eyebrow, "Contact ShikshaCom")
        self.assertIn("a real person from our team", block.subhead)

    def test_the_newline_that_becomes_a_br_survives_the_database(self):
        """The design's <br> is carried as \\n through a CharField. If that did
        not round-trip, the hero would render as one long line."""
        seed("--yes")
        block = HomeContentBlock.objects.get(section=HomeSection.CONTACT_HERO)
        self.assertEqual(block.heading.count("\n"), 1)

    def test_regional_office_gets_its_own_glyph(self):
        """Both offices ship as icon='location' today; the design gives the
        regional one a building. Contact.jsx keys the glyph off this value."""
        self._live_rows()
        seed("--yes")
        self.assertEqual(HomeListItem.objects.get(order=1).icon, "building")
        self.assertEqual(HomeListItem.objects.get(order=1).title, "Regional Office")

    def test_email_card_gets_the_small_note(self):
        self._live_rows()
        seed("--yes")
        self.assertEqual(
            HomeListItem.objects.get(order=2).subtitle,
            "We reply within one working day.",
        )

    def test_runs_from_an_empty_cms(self):
        seed("--yes")
        self.assertEqual(
            HomeListItem.objects.filter(variant=HomeListVariant.CONTACT_CARD).count(), 4
        )

    # ── idempotency ───────────────────────────────────────────────

    def test_second_run_is_a_no_op(self):
        self._live_rows()
        seed("--yes")
        out = seed("--yes")
        self.assertIn("Nothing to do", out)

    def test_rerunning_does_not_duplicate_cards(self):
        self._live_rows()
        seed("--yes")
        seed("--yes")
        self.assertEqual(
            HomeListItem.objects.filter(variant=HomeListVariant.CONTACT_CARD).count(), 4
        )

    # ── what it must not touch ────────────────────────────────────

    def test_an_editors_extra_office_is_reported_not_deleted(self):
        self._live_rows()
        HomeListItem.objects.create(
            section=HomeSection.CONTACT_HERO,
            variant=HomeListVariant.CONTACT_CARD,
            title="Bengaluru Office", body="Somewhere", order=4,
            status=PublishStatus.PUBLISHED,
        )
        out = seed("--yes")
        self.assertIn("Bengaluru Office", out)
        self.assertTrue(HomeListItem.objects.filter(order=4).exists())

    def test_other_sections_are_untouched(self):
        other = HomeContentBlock.objects.create(
            section=HomeSection.ABOUT_HERO, heading="About Us",
            status=PublishStatus.PUBLISHED,
        )
        seed("--yes")
        other.refresh_from_db()
        self.assertEqual(other.heading, "About Us")
