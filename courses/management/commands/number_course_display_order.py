# PLACEMENT: backend/courses/management/commands/number_course_display_order.py
#
#     python manage.py number_course_display_order          # dry run (default)
#     python manage.py number_course_display_order --yes     # actually write
#
# Why this exists
# ---------------
# `Course.display_order` defaults to 0 and nothing ever set it, so every course
# on production sits at 0. That makes the field useless in both directions: the
# admin course tables show a column of zeros, and because the list falls back to
# title within a tie, setting one course to 1 shoves it to the BOTTOM of an
# otherwise alphabetical list rather than moving it one place. An editor types a
# number, the wrong thing happens, and the feature reads as broken.
#
# This numbers the existing courses in the order they are already displayed, so
# nothing visibly moves — it just replaces "everything is 0" with a real
# sequence that can then be nudged.
#
# Numbered in steps of 10 on purpose. To move a course between two others an
# editor can type a value in the gap (35 to land between 30 and 40) instead of
# renumbering the whole board. The admin UI's up/down arrows keep the steps
# tidy; the gaps are for the type-a-number path.
#
# Scoped per board, because that is the unit the admin actually browses: each
# board's list starts again at 10. Courses with no board are numbered as their
# own group.
#
# Safety model (same as the other seed/backfill commands here)
# -----------------------------------------------------------
# * Dry run by default; nothing is written without --yes.
# * Only touches courses whose display_order is currently 0 unless --renumber is
#   passed, so it can never quietly undo ordering someone has already set.
# * Idempotent: a second run finds nothing at 0 and does nothing.

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from courses.models import Course

STEP = 10


class Command(BaseCommand):
    help = ("Give courses a real display_order sequence instead of all-zeros "
            "(dry run unless --yes).")

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this the command only reports.",
        )
        parser.add_argument(
            "--renumber", action="store_true",
            help="Also renumber courses that already have a non-zero order "
                 "(default leaves any existing ordering alone).",
        )

    def handle(self, *args, **opts):
        write = opts["yes"]
        renumber = opts["renumber"]

        # Read in the same order the admin list uses, so the numbers we assign
        # match what an editor is currently looking at.
        courses = list(
            Course.objects.select_related("board").order_by("display_order", "title")
        )
        by_board = defaultdict(list)
        for c in courses:
            by_board[c.board_id].append(c)

        planned, skipped = [], 0

        for board_id, group in by_board.items():
            label = group[0].board.name if group[0].board else "(no board)"
            self.stdout.write(f"\n{label} — {len(group)} course(s)")
            position = 0
            for c in group:
                position += STEP
                if c.display_order == position:
                    self.stdout.write(f"  = {c.title}: already {position}")
                    skipped += 1
                    continue
                if c.display_order != 0 and not renumber:
                    self.stdout.write(
                        f"  = {c.title}: has order {c.display_order} already, left alone"
                    )
                    skipped += 1
                    continue
                self.stdout.write(f"  + {c.title}: {c.display_order} -> {position}")
                planned.append((c, position))

        if write and planned:
            with transaction.atomic():
                for c, position in planned:
                    c.display_order = position
                    c.save(update_fields=["display_order"])

        if write:
            self.stdout.write(self.style.SUCCESS(
                f"\nDone. numbered={len(planned)} unchanged={skipped}"
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f"\nDry run — {len(planned)} course(s) would be numbered, "
                f"{skipped} left alone. Re-run with --yes to apply."
            ))
