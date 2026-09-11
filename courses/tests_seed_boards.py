"""Cover for `seed_boards`, which writes the navbar's board rows.

Two properties matter here, and both are safety properties rather than
features:

1. It can never create a second CBSE. The command predates these tests
   because of the 2026-07-27 incident, where a seed matched on slug alone and
   duplicated live rows; matching on slug OR case-insensitive name is what
   fixed it, and nothing was covering that.

2. It only ever writes names it wrote itself. `--apply-curation` renames ~26
   boards from their bare abbreviation ("BSEAP") onto "<abbr> · <state>", and
   the one thing that must not happen is trampling a name a human chose.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from courses.models import Board, Course
from courses.management.commands._catalog_seed_data import (
    BOARD_SEED, LEGACY_NAME_FORMS,
)


def _run(**kwargs):
    out = StringIO()
    call_command("seed_boards", stdout=out, **kwargs)
    return out.getvalue()


class SeedBoardsDryRunTests(TestCase):
    def test_a_dry_run_writes_nothing(self):
        _run()
        self.assertEqual(Board.objects.count(), 0)

    def test_the_write_creates_every_seed_row(self):
        _run(yes=True)
        self.assertEqual(Board.objects.count(), len(BOARD_SEED))

    def test_only_cbse_and_mbse_are_active(self):
        """Everything else must render "Coming Soon"; an accidentally-active
        board is a live nav link into an empty catalog."""
        _run(yes=True)
        active = set(Board.objects.filter(is_active=True).values_list("slug", flat=True))
        self.assertEqual(active, {"cbse", "mbse"})

    def test_a_second_run_creates_nothing(self):
        _run(yes=True)
        _run(yes=True)
        self.assertEqual(Board.objects.count(), len(BOARD_SEED))


class SeedBoardsNeverDuplicatesTests(TestCase):
    """The 2026-07-27 guard."""

    def test_an_existing_cbse_is_matched_not_duplicated(self):
        Board.objects.create(name="CBSE", slug="cbse", board_type="CENTRAL")
        _run(yes=True)
        self.assertEqual(Board.objects.filter(slug="cbse").count(), 1)

    def test_a_differently_slugged_cbse_is_still_matched_by_name(self):
        """Matching on slug alone is exactly the bug that caused the
        incident — the name has to be checked too."""
        Board.objects.create(name="cbse", slug="cbse-legacy", board_type="CENTRAL")
        _run(yes=True)
        self.assertEqual(Board.objects.filter(name__iexact="cbse").count(), 1)

    def test_an_existing_board_keeps_its_is_active(self):
        """Without --apply-curation nothing about a live row may change."""
        Board.objects.create(
            name="MBSE", slug="mbse", board_type="STATE", is_active=True)
        _run(yes=True)
        self.assertTrue(Board.objects.get(slug="mbse").is_active)


class SeedBoardsNameUpgradeTests(TestCase):
    """`--apply-curation` brings legacy names onto "<abbr> · <state>"."""

    def setUp(self):
        # The pre-2026-09 seed's output: bare abbreviations, plus the one row
        # it wrote long-hand without a separator.
        Board.objects.create(name="BSEAP", slug="bseap", board_type="STATE")
        Board.objects.create(name="BSE Odisha", slug="bseodisha", board_type="STATE")
        Board.objects.create(
            name="MBSE", slug="mbse", board_type="STATE", is_active=True)

    def test_nothing_is_renamed_without_the_flag(self):
        _run(yes=True)
        self.assertEqual(Board.objects.get(slug="bseap").name, "BSEAP")

    def test_a_bare_abbreviation_is_upgraded(self):
        _run(yes=True, apply_curation=True)
        self.assertEqual(
            Board.objects.get(slug="bseap").name, "BSEAP · Andhra Pradesh")

    def test_the_legacy_longhand_form_is_upgraded_too(self):
        """"BSE Odisha" is neither the new name nor a bare abbreviation, so a
        naive equality check would have left it behind."""
        _run(yes=True, apply_curation=True)
        self.assertEqual(
            Board.objects.get(slug="bseodisha").name, "BSE · Odisha")

    def test_a_hand_written_name_is_never_trampled(self):
        """The property this whole mechanism exists to preserve."""
        Board.objects.filter(slug="bseap").update(name="Andhra Board (ours)")
        out = _run(yes=True, apply_curation=True)
        self.assertEqual(
            Board.objects.get(slug="bseap").name, "Andhra Board (ours)")
        self.assertIn("KEEP", out)

    def test_the_slug_is_never_touched_by_a_rename(self):
        """Slugs are the `?board=` wire value behind shared links and saved
        homepage CMS link_state rows."""
        before = dict(Board.objects.values_list("id", "slug"))
        _run(yes=True, apply_curation=True)
        for pk, slug in before.items():
            self.assertEqual(Board.objects.get(pk=pk).slug, slug)

    def test_renaming_is_idempotent(self):
        _run(yes=True, apply_curation=True)
        out = _run(yes=True, apply_curation=True)
        self.assertEqual(
            Board.objects.get(slug="mbse").name, "MBSE · Mizoram")
        self.assertIn("upgraded=0", out)


class SeedBoardsDeactivateEmptyTests(TestCase):
    """An active board with no public courses is a link to an empty page.

    Keyed on emptiness, not on a slug list. The version of this that asserted
    "is_active is currently True" before flipping CISCE is what the last test
    here exists to prevent: that check cannot tell "the value I mean to
    replace" from "the value an admin deliberately restored", so re-running
    would have un-launched the board.
    """

    def test_an_empty_active_board_becomes_coming_soon(self):
        Board.objects.create(
            name="CISCE", slug="cisce", board_type="CENTRAL", is_active=True)
        _run(yes=True, apply_curation=True)
        self.assertFalse(Board.objects.get(slug="cisce").is_active)

    def test_a_board_with_a_published_course_is_left_active(self):
        b = Board.objects.create(
            name="CBSE", slug="cbse", board_type="CENTRAL", is_active=True)
        Course.objects.create(
            title="CBSE Class 9", board=b, class_level=9,
            status=Course.STATUS_PUBLISHED)
        _run(yes=True, apply_curation=True)
        self.assertTrue(Board.objects.get(slug="cbse").is_active)

    def test_a_coming_soon_course_also_counts_as_something_to_show(self):
        """COMING_SOON is public — it renders a card with a Notify-me."""
        b = Board.objects.create(
            name="CBSE", slug="cbse", board_type="CENTRAL", is_active=True)
        Course.objects.create(
            title="CBSE Class 9", board=b, class_level=9,
            status=Course.STATUS_COMING_SOON)
        _run(yes=True, apply_curation=True)
        self.assertTrue(Board.objects.get(slug="cbse").is_active)

    def test_a_draft_only_board_is_still_deactivated(self):
        """A DRAFT course is invisible to visitors, so the board still has
        nothing to show."""
        b = Board.objects.create(
            name="CISCE", slug="cisce", board_type="CENTRAL", is_active=True)
        Course.objects.create(
            title="CISCE Class 9", board=b, class_level=9,
            status=Course.STATUS_DRAFT)
        _run(yes=True, apply_curation=True)
        self.assertFalse(Board.objects.get(slug="cisce").is_active)

    def test_nothing_is_deactivated_without_the_flag(self):
        Board.objects.create(
            name="CISCE", slug="cisce", board_type="CENTRAL", is_active=True)
        _run(yes=True)
        self.assertTrue(Board.objects.get(slug="cisce").is_active)

    def test_relaunching_a_board_survives_a_re_run(self):
        """THE regression this rule shape exists for. An admin gives CISCE a
        course and switches it back on; re-running the seeder must not
        silently take the board off the site again."""
        Board.objects.create(
            name="CISCE", slug="cisce", board_type="CENTRAL", is_active=True)
        _run(yes=True, apply_curation=True)                 # -> inactive
        b = Board.objects.get(slug="cisce")
        Course.objects.create(
            title="CISCE Class 9", board=b, class_level=9,
            status=Course.STATUS_PUBLISHED)
        Board.objects.filter(slug="cisce").update(is_active=True)   # it launched

        _run(yes=True, apply_curation=True)
        self.assertTrue(
            Board.objects.get(slug="cisce").is_active,
            "re-running the seeder un-launched a board that now has courses")


class LegacyNameFormsTests(TestCase):
    def test_it_covers_the_three_forms_a_seed_could_have_written(self):
        forms = LEGACY_NAME_FORMS("BSEAP · Andhra Pradesh")
        self.assertEqual(
            forms, {"BSEAP · Andhra Pradesh", "BSEAP", "BSEAP Andhra Pradesh"})

    def test_an_unqualified_name_yields_only_itself(self):
        """National boards carry no state, so there is nothing to upgrade and
        nothing a rename could accidentally match."""
        self.assertEqual(LEGACY_NAME_FORMS("CBSE"), {"CBSE"})

    def test_no_two_seed_rows_share_a_legacy_form(self):
        """If they did, upgrading one board could match another's name."""
        seen = {}
        for slug, name, *_ in BOARD_SEED:
            for form in LEGACY_NAME_FORMS(name):
                self.assertNotIn(
                    form, seen,
                    f"{slug} and {seen.get(form)} both answer to {form!r}")
                seen[form] = slug
