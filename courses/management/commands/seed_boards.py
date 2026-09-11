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
# --apply-curation is what changes rows that already exist (BOARD_CURATION:
# the MBSE rename and the CISCE deactivation). It is deliberately off by
# default — see _curate()'s docstring.
#
# CRITICAL — the 2026-07-27 incident guard: CBSE and MBSE almost certainly
# already exist as real Board rows (import_static_course_content requires
# them). This command matches every seed row against BOTH slug AND
# case-insensitive name BEFORE creating anything, so it can never create a
# second CBSE/MBSE (or any) board. Rows flagged pre_existing are only ever
# reported, never written, even if somehow absent.
#
# Idempotent: existing boards (by slug or name) get display_order backfilled if
# it is still 0; board_type / is_active on already-live boards are left ALONE
# (they may have been curated in admin). New boards are created inactive.

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from courses.models import Board

from ._catalog_seed_data import BOARD_CURATION, BOARD_SEED


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
                "Also apply BOARD_CURATION — the explicit, per-slug edits to "
                "boards that ALREADY EXIST. Off by default: the rest of this "
                "command never mutates an existing row, and that guard is "
                "load-bearing."
            ),
        )

    def _curate(self, dry_run):
        """Apply BOARD_CURATION: a fixed allow-list of edits to existing rows.

        Separate from the seeding loop above, and opt-in, because that loop's
        entire safety property is "an existing board is only ever reported,
        never written" — the guard that stopped the 2026-07-27 duplicate-CBSE
        incident. Rather than soften it into a general "update to match seed"
        pass (which would let any future BOARD_SEED typo rewrite live rows),
        the only mutations allowed are the ones named one-by-one in
        BOARD_CURATION, keyed on slug.

        Each entry asserts the CURRENT value before writing. That makes the
        command idempotent (a second run reports "already applied" instead of
        re-writing) and, more importantly, means an admin who has since
        changed the field by hand is reported and left alone rather than
        silently overwritten.
        """
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("--- curation of existing rows ---"))
        applied = already = skipped = 0

        for slug, field, expect, new, why in BOARD_CURATION:
            board = Board.objects.filter(slug=slug).first()
            if board is None:
                skipped += 1
                self.stdout.write(self.style.WARNING(
                    f"  MISS    {slug:11} — no such board; nothing to curate."
                ))
                continue

            current = getattr(board, field)
            if current == new:
                already += 1
                self.stdout.write(
                    f"  OK      {slug:11} {field}={new!r} — already applied."
                )
                continue
            if current != expect:
                # Somebody changed this by hand. Report, do not clobber.
                skipped += 1
                self.stdout.write(self.style.WARNING(
                    f"  SKIP    {slug:11} {field} is {current!r}, expected "
                    f"{expect!r} before setting {new!r}. Left alone — this row "
                    f"was edited outside this command."
                ))
                continue

            applied += 1
            self.stdout.write(
                f"  CURATE  {slug:11} {field}: {current!r} → {new!r}\n"
                f"          why: {why}"
            )
            if not dry_run:
                with transaction.atomic():
                    setattr(board, field, new)
                    board.save(update_fields=[field])

        self.stdout.write(self.style.SUCCESS(
            f"curation: applied={applied} already_applied={already} "
            f"skipped={skipped} (of {len(BOARD_CURATION)})"
        ))

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
                # Expected to exist but doesn't — report loudly, still create it
                # (active) rather than silently leaving the live board missing.
                self.stdout.write(self.style.WARNING(
                    f"  WARN    {slug:11} {name:12} — expected pre-existing but NOT FOUND; "
                    f"would CREATE (active)."
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
            self._curate(dry_run)
        else:
            pending = [
                f"{slug}.{field}" for slug, field, expect, new, _why in BOARD_CURATION
                if (b := Board.objects.filter(slug=slug).first()) is not None
                and getattr(b, field) == expect
            ]
            if pending:
                self.stdout.write(self.style.WARNING(
                    f"{len(pending)} curation edit(s) NOT applied "
                    f"({', '.join(pending)}) — pass --apply-curation to include them."
                ))

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --yes to write."))
