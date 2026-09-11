# PLACEMENT: backend/courses/management/commands/seed_boards.py
#
# Creates the 31 Board rows the public navbar needs from
# _catalog_seed_data.BOARD_SEED: India's three national boards (CBSE, CISCE,
# NIOS) and the 28 state boards. All except CBSE and MBSE are seeded
# is_active=False → the nav renders an inert "Coming Soon" row and the catalog
# renders a locked chip with a Notify-me capture.
#
# Usage:
#     python manage.py seed_boards                            # dry run
#     python manage.py seed_boards --yes                      # create missing
#     python manage.py seed_boards --yes --apply-curation     # + edit existing
#
# --apply-curation is what changes rows that ALREADY EXIST, and is off by
# default because the rest of this command never mutates one. It does two
# things, each keyed on a condition rather than on a list of slugs so that
# re-running can never undo a later human decision:
#   * _upgrade_names()          — legacy "BSEAP" → "BSEAP · Andhra Pradesh"
#   * _deactivate_empty_boards() — an active board with no public courses is
#                                  a nav link to an empty catalog
#
# CRITICAL — the 2026-07-27 incident guard: CBSE and MBSE almost certainly
# already exist as real Board rows (import_static_course_content requires
# them). This command matches every seed row against BOTH slug AND
# case-insensitive name BEFORE creating anything, so it can never create a
# second CBSE/MBSE (or any) board. Rows flagged pre_existing are only ever
# reported, never written, even if somehow absent.
#
# Idempotent: existing boards (by slug or name) get display_order backfilled if
# it is still 0. Without --apply-curation, board_type / is_active / name on an
# already-live board are left ALONE. New boards are created per BOARD_SEED.

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from courses.models import Board, Course

from ._catalog_seed_data import BOARD_SEED, LEGACY_NAME_FORMS

# Mirrors courses.views.PUBLIC_COURSE_STATUSES. Defined here from the model
# rather than imported, so a management command does not have to drag the
# whole DRF view module (and its import graph) in behind it.
PUBLIC_COURSE_STATUSES = [Course.STATUS_PUBLISHED, Course.STATUS_COMING_SOON]


class Command(BaseCommand):
    help = (
        "Seed missing Board rows from BOARD_OPTIONS (inactive except CBSE/MBSE). "
        "Dry-run by default; pass --yes to write. Matches by slug OR name first "
        "so CBSE/MBSE are never duplicated."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this flag, only reports what would happen.",
        )
        parser.add_argument(
            "--apply-curation", action="store_true",
            help=(
                "Also upgrade legacy board NAMES and mark any active board "
                "with no public courses as Coming Soon. Off by default: the "
                "rest of this command never mutates an existing row, and "
                "that guard is load-bearing."
            ),
        )

    def _upgrade_names(self, dry_run):
        """Bring existing boards onto the "<abbr> · <state>" naming.

        A board created by the pre-2026-09 seed is called "BSEAP", which
        identifies nothing once the mobile drawer flattens both board tabs
        into one list — and MBSE/MBOSE are one letter apart. Only names that
        exactly match a LEGACY_NAME_FORMS string are touched, so a board an
        admin has renamed by hand is reported and left as it is.

        Slugs are never touched: they are the `?board=` wire value.
        """
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("--- name upgrades on existing rows ---"))
        upgraded = already = custom = 0

        for slug, name, _bt, _act, _pre in BOARD_SEED:
            board = Board.objects.filter(slug=slug).first()
            if board is None:
                continue  # the seeding loop above will have created it
            if board.name == name:
                already += 1
                continue
            if board.name not in LEGACY_NAME_FORMS(name):
                custom += 1
                self.stdout.write(self.style.WARNING(
                    f"  KEEP    {slug:11} {board.name!r} is not a name this "
                    f"command wrote — left alone (would have been {name!r})."
                ))
                continue

            upgraded += 1
            self.stdout.write(f"  RENAME  {slug:11} {board.name!r} → {name!r}")
            if not dry_run:
                with transaction.atomic():
                    board.name = name
                    board.save(update_fields=["name"])

        self.stdout.write(self.style.SUCCESS(
            f"names: upgraded={upgraded} already_correct={already} "
            f"left_as_customised={custom}"
        ))

    def _deactivate_empty_boards(self, dry_run):
        """Mark an ACTIVE board that has no public courses as Coming Soon.

        An active board is a clickable nav entry and an unlocked catalog chip.
        With no PUBLISHED or COMING_SOON course behind it, both lead to an
        empty page — prod's CISCE row was exactly that, and dev's junk "sds"
        board still is. Inactive is the honest state: the nav renders an inert
        "Coming Soon" row and the catalog chip becomes a Notify-me capture
        (BoardNotifyRequest) instead of a dead end.

        Stated as a REASON rather than a list of slugs on purpose. An earlier
        draft asserted "is_active is currently True" before flipping CISCE,
        which a test showed would re-deactivate it on the next run the day it
        genuinely launched — "the value I mean to replace" and "the value an
        admin restored" are the same value. Keyed on emptiness instead, the
        rule stops applying to a board the moment it has a course, so it can
        never un-launch anything.

        CBSE and MBSE both carry courses and are untouched by construction.
        """
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "--- boards active with nothing to show ---"))

        empty = [
            b for b in Board.objects.filter(is_active=True)
            if not b.courses.filter(status__in=PUBLIC_COURSE_STATUSES).exists()
        ]
        if not empty:
            self.stdout.write(
                "  none — every active board has at least one public course.")
            return

        for b in empty:
            self.stdout.write(
                f"  SOON    {b.slug:11} {b.name!r} is active with 0 public "
                f"courses → is_active=False"
            )
            if not dry_run:
                with transaction.atomic():
                    b.is_active = False
                    b.save(update_fields=["is_active"])

        self.stdout.write(self.style.SUCCESS(
            f"deactivated: {len(empty)} empty active board(s)"))

    def handle(self, *args, **options):
        dry_run = not options["yes"]
        mode = "DRY RUN — nothing will be written" if dry_run else "WRITE MODE"
        self.stdout.write(self.style.WARNING(f"=== seed_boards: {mode} ==="))

        created = matched = backfilled = 0

        for order, (slug, name, board_type, is_active, pre_existing) in enumerate(BOARD_SEED):
            # Match-before-create on slug OR case-insensitive name. This is the
            # single most important line in this command — it is what prevents a
            # duplicate CBSE/MBSE (the shape of the earlier 1,055-row near-miss).
            existing = Board.objects.filter(
                Q(slug=slug) | Q(name__iexact=name)
            ).first()

            if existing is not None:
                matched += 1
                note = "pre-existing/live" if pre_existing else "already present"
                extra = ""
                # Only ever backfill a still-default display_order; never touch
                # board_type / is_active / name on an already-curated row.
                if existing.display_order == 0 and order != 0:
                    extra = f" (would set display_order={order})"
                    backfilled += 1
                    if not dry_run:
                        with transaction.atomic():
                            existing.display_order = order
                            existing.save(update_fields=["display_order"])
                self.stdout.write(
                    f"  SKIP    {slug:11} {name:12} — {note} (id={existing.id}){extra}"
                )
                continue

            if pre_existing:
                # Expected to exist but doesn't — report loudly, still create
                # it rather than silently leaving the live board missing.
                #
                # Report the flag actually being written, not a literal: CISCE
                # is pre_existing (it is a real prod row) AND seeds inactive,
                # so a hardcoded "(active)" here described the opposite of
                # what the create below does.
                self.stdout.write(self.style.WARNING(
                    f"  WARN    {slug:11} {name:12} — expected pre-existing but NOT FOUND; "
                    f"would CREATE ({'active' if is_active else 'inactive'})."
                ))

            created += 1
            flag = "active" if is_active else "inactive"
            self.stdout.write(
                f"  CREATE  {slug:11} {name:12} type={board_type:7} {flag} display_order={order}"
            )
            if not dry_run:
                with transaction.atomic():
                    Board.objects.create(
                        name=name, slug=slug, board_type=board_type,
                        is_active=is_active, display_order=order,
                    )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"boards: created={created} matched(skipped)={matched} "
            f"display_order_backfilled={backfilled} (of {len(BOARD_SEED)})"
        ))

        if options["apply_curation"]:
            self._upgrade_names(dry_run)
            self._deactivate_empty_boards(dry_run)
        else:
            stale = [
                slug for slug, name, _bt, _a, _p in BOARD_SEED
                if (b := Board.objects.filter(slug=slug).first()) is not None
                and b.name != name and b.name in LEGACY_NAME_FORMS(name)
            ]
            empty = [
                b.slug for b in Board.objects.filter(is_active=True)
                if not b.courses.filter(status__in=PUBLIC_COURSE_STATUSES).exists()
            ]
            if stale or empty:
                self.stdout.write(self.style.WARNING(
                    f"NOT applied without --apply-curation: "
                    f"{len(stale)} name upgrade(s), "
                    f"{len(empty)} empty active board(s)"
                    + (f" ({', '.join(empty)})" if empty else "")
                ))

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --yes to write."))
