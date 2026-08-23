# PLACEMENT: backend/courses/management/commands/seed_featured_cards.py
#
# Creates the 18 homepage 'Featured courses' cards as ShowcaseCourse rows,
# each linked to the real target it represents:
#   - Class 8-12 cards → the already-live CBSE Course for that class+stream
#   - the 2 board tiles → a Board (explore cards)
#   - the 7 competitive cards → the COACHING Course from create_competitive_courses
# Data: _catalog_seed_data.FEATURED_CARD_SEED.
#
# Usage:
#     python manage.py seed_featured_cards            # dry run (default)
#     python manage.py seed_featured_cards --yes       # actually write
#
# Idempotent: deduped by TARGET, not by title —
#   course-linked  → one ShowcaseCourse per course
#   board-linked   → one explore ShowcaseCourse per board
# Re-running never creates a second card for the same target.
#
# Prerequisites (this command CREATES NO Course/Board — it only links to
# existing ones, so it can't duplicate the earlier incident):
#   - import_static_course_content  (the CBSE class 8-12 courses)
#   - seed_boards                    (CBSE/MBSE boards)
#   - create_competitive_courses     (the 7 COACHING courses)
# Any target that is missing is reported and its card is skipped, never faked.

from django.core.management.base import BaseCommand
from django.db import transaction

from content.models import ShowcaseCourse
from courses.models import Board, Course

from ._catalog_seed_data import FEATURED_CARD_SEED


class Command(BaseCommand):
    help = (
        "Seed the 18 homepage ShowcaseCourse cards, linked to real Course/Board "
        "targets. Dry-run by default; pass --yes. Deduped by target."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this flag, only reports what would happen.",
        )

    def _resolve_academic(self, class_level, stream):
        """The already-live, PUBLISHED CBSE Course for this class (+ stream for 11/12).
        Excluding non-published rows matters: unpublished demo/seed CBSE courses for
        this class+stream must never surface on the homepage in place of the real
        (possibly differently-boarded) live course."""
        qs = Course.objects.filter(
            class_level=class_level, board__slug="cbse", status=Course.STATUS_PUBLISHED,
        )
        qs = qs.filter(stream__name=stream) if stream else qs.filter(stream__isnull=True)
        n = qs.count()
        if n == 0:
            return None, f"NO CBSE course for class {class_level}" + (f"/{stream}" if stream else "")
        if n > 1:
            return qs.order_by("created_at").first(), \
                f"WARNING: {n} CBSE courses match class {class_level}/{stream or '-'}, picked earliest"
        return qs.first(), None

    def handle(self, *args, **options):
        dry_run = not options["yes"]
        mode = "DRY RUN — nothing will be written" if dry_run else "WRITE MODE"
        self.stdout.write(self.style.WARNING(f"=== seed_featured_cards: {mode} ==="))

        # This command dedups by TARGET (course/board FK). seed_homepage_defaults
        # creates the same 18 cards WITHOUT any FK — transcribed straight from
        # the frontend's old hardcoded list — so those rows match nothing here
        # and every card would be created a second time, silently doubling the
        # homepage grid. Refuse to run rather than quietly produce 36 cards.
        # Linking the unlinked rows changes what visitors see (a linked card
        # takes its title and price from the real Course), so it is a content
        # decision and deliberately not automated here.
        unlinked = ShowcaseCourse.objects.filter(course__isnull=True, board__isnull=True)
        if unlinked.exists():
            self.stderr.write(self.style.ERROR(
                f"REFUSING TO RUN: {unlinked.count()} ShowcaseCourse row(s) exist "
                f"with no course/board link:\n"
                + "\n".join(f"    id={c.id} order={c.order} {c.title!r}"
                            for c in unlinked.order_by("order")[:20])
                + "\n\nThese are almost certainly seed_homepage_defaults' rows. "
                  "This command would not recognise them and would create a "
                  "duplicate card for every target.\n"
                  "Either link them to their real Course/Board in the admin "
                  "(Content → Showcase), or delete them, then re-run."
            ))
            return

        created = updated = skipped_no_target = 0

        for row in FEATURED_CARD_SEED:
            target = row["target"]
            course = board = None
            label = warn = None

            if "academic" in target:
                class_level, stream = target["academic"]
                course, warn = self._resolve_academic(class_level, stream)
                label = f"Class {class_level}" + (f" {stream.title()}" if stream else "")
            elif "competitive" in target:
                slug = target["competitive"]
                course = Course.objects.filter(slug=slug).first()
                label = f"competitive:{slug}"
                if course is None:
                    warn = f"NO competitive course slug={slug} (run create_competitive_courses)"
            elif "board" in target:
                slug = target["board"]
                board = Board.objects.filter(slug=slug).first()
                label = f"board:{slug}"
                if board is None:
                    warn = f"NO board slug={slug} (run seed_boards)"

            if course is None and board is None:
                skipped_no_target += 1
                self.stdout.write(self.style.WARNING(
                    f"  SKIP    order={row['order']:<2} {label:26} — {warn}"
                ))
                continue
            if warn:
                self.stdout.write(self.style.WARNING(f"          note: {warn}"))

            # Derived, non-curation fields the endpoint computes from the target
            # are intentionally left blank (price_label). title mirrors the
            # target for admin readability but PublicFeaturedView overrides it.
            title = course.title if course else board.name
            fields = dict(
                title=title[:120],
                level_label=row.get("level_label", ""),
                ribbon=row.get("ribbon", ""),
                stars=row.get("stars", 5),
                review_count=row.get("review_count", 0),
                fact_line=row.get("fact_line", ""),
                price_label="",  # derived server-side from the linked target
                tutor_name=row.get("tutor_name", ""),
                is_explore_card=row.get("is_explore_card", False),
                categories=row.get("categories", []),
                gradient_css=row.get("gradient_css", ""),
                icon=row.get("icon", "book"),
                link_path=row.get("link_path", "/courses"),
                link_state=row.get("link_state", {}),
                order=row["order"],
                is_active=True,
                course=course,
                board=board,
            )

            # Dedup by TARGET (never by title).
            if course is not None:
                existing = ShowcaseCourse.objects.filter(course=course).first()
            else:
                existing = ShowcaseCourse.objects.filter(
                    board=board, is_explore_card=True
                ).first()

            if existing is None:
                created += 1
                self.stdout.write(
                    f"  CREATE  order={row['order']:<2} {label:26} → '{title}'  cats={fields['categories']}"
                )
                if not dry_run:
                    with transaction.atomic():
                        card = ShowcaseCourse(**fields)
                        # save() alone skips clean(), so this command could
                        # write categories the model and the admin form both
                        # reject — notably the reserved "all" sentinel, which
                        # this seed data carried until it was stripped.
                        card.full_clean()
                        card.save()
            else:
                changed = [
                    k for k, v in fields.items()
                    if k not in ("course", "board") and getattr(existing, k) != v
                ]
                if changed:
                    updated += 1
                    self.stdout.write(
                        f"  UPDATE  order={row['order']:<2} {label:26} (id={existing.id}; changed: {', '.join(changed)})"
                    )
                    if not dry_run:
                        with transaction.atomic():
                            for k, v in fields.items():
                                setattr(existing, k, v)
                            existing.full_clean()  # same reason as the create path
                            existing.save()
                else:
                    self.stdout.write(
                        f"  SKIP    order={row['order']:<2} {label:26} (id={existing.id}; up to date)"
                    )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"featured cards: created={created} updated={updated} "
            f"skipped_no_target={skipped_no_target} (of {len(FEATURED_CARD_SEED)})"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --yes to write."))
