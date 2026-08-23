# PLACEMENT: backend/courses/management/commands/link_showcase_cards.py
#
#     python manage.py link_showcase_cards           # dry run (default)
#     python manage.py link_showcase_cards --yes     # actually write
#
# Attaches the homepage "Featured courses" cards to the real Course/Board rows
# they represent.
#
# Why this is separate from seed_featured_cards
# ---------------------------------------------
# seed_featured_cards CREATES cards for targets that have none. These cards
# already exist: seed_homepage_defaults transcribed them out of the frontend's
# old hardcoded list, so they carry literal titles and prices and no FK at all.
# Running seed_featured_cards against them would not recognise them and would
# create a second card per target. This command adopts them in place instead.
#
# What linking actually changes (PublicFeaturedView, courses/views.py)
# --------------------------------------------------------------------
# A linked card stops using its own stored title/price/thumbnail and derives
# them from the target on every request:
#   title        <- course.title / board.name
#   price_label  <- course.price, or "Free" when the course costs nothing
#   coming_soon  <- course.status == COMING_SOON
#   thumbnail    <- course.thumbnail / board.logo, falling back to the card's
# So this is a visible content change, not a silent migration. Run the dry run
# and read the before/after table before passing --yes.
#
# Matching is by `order`, which is the one stable join here: both this seed
# list and seed_homepage_defaults' cards derive their order from the same
# original frontend array, and unlike title it is not something an editor is
# expected to rewrite.

from django.core.management.base import BaseCommand
from django.db import transaction

from content.models import ShowcaseCourse
from courses.models import Board, Course

from ._catalog_seed_data import FEATURED_CARD_SEED


class Command(BaseCommand):
    help = (
        "Link the existing homepage showcase cards to their real Course/Board "
        "rows. Dry-run by default; pass --yes. Only touches unlinked cards."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this flag, only reports the change.",
        )

    def _resolve(self, target):
        """Return (course, board, note). Never guesses: an ambiguous or missing
        target yields (None, None, reason) and its card is left unlinked."""
        if "academic" in target:
            class_level, stream = target["academic"]
            qs = Course.objects.filter(
                class_level=class_level, board__slug="cbse",
                status=Course.STATUS_PUBLISHED,
            )
            qs = qs.filter(stream__name=stream) if stream else qs.filter(stream__isnull=True)
            n = qs.count()
            label = f"class {class_level}" + (f"/{stream}" if stream else "")
            if n == 0:
                return None, None, f"no published CBSE course for {label}"
            if n > 1:
                return None, None, f"AMBIGUOUS: {n} CBSE courses match {label}"
            return qs.first(), None, None

        if "competitive" in target:
            slug = target["competitive"]
            c = Course.objects.filter(slug=slug).first()
            return (c, None, None) if c else (None, None, f"no course slug={slug}")

        if "board" in target:
            slug = target["board"]
            b = Board.objects.filter(slug=slug).first()
            return (None, b, None) if b else (None, None, f"no board slug={slug}")

        return None, None, "unrecognised target"

    @staticmethod
    def _preview(course, board):
        if course:
            price = "Free" if not course.price else f"₹{course.price // 100:,}/month"
            if course.status == Course.STATUS_COMING_SOON:
                price = "Coming Soon"
            return course.title, price
        return board.name, "(no price shown)"

    def handle(self, *args, **options):
        dry_run = not options["yes"]
        self.stdout.write(self.style.WARNING(
            "=== link_showcase_cards: "
            + ("DRY RUN — nothing will be written" if dry_run else "WRITE MODE")
            + " ==="
        ))
        self.stdout.write("")
        self.stdout.write(f"{'ord':>3}  {'BEFORE (card as stored)':44}  ->  AFTER (derived from target)")
        self.stdout.write("-" * 108)

        linked = already = skipped = 0

        for row in FEATURED_CARD_SEED:
            order = row["order"]
            card = ShowcaseCourse.objects.filter(order=order).first()
            if card is None:
                skipped += 1
                self.stdout.write(self.style.WARNING(f"{order:>3}  no card at this order — skipped"))
                continue

            if card.course_id or card.board_id:
                already += 1
                self.stdout.write(self.style.HTTP_NOT_MODIFIED(
                    f"{order:>3}  {card.title[:44]:44}  ->  already linked, untouched"
                ))
                continue

            course, board, note = self._resolve(row["target"])
            if course is None and board is None:
                skipped += 1
                self.stdout.write(self.style.ERROR(
                    f"{order:>3}  {card.title[:44]:44}  ->  LEFT UNLINKED — {note}"
                ))
                continue

            new_title, new_price = self._preview(course, board)
            before = f"{card.title} / {card.price_label or '—'}"
            self.stdout.write(f"{order:>3}  {before[:44]:44}  ->  {new_title} / {new_price}")

            linked += 1
            if not dry_run:
                with transaction.atomic():
                    card.course = course
                    card.board = board
                    card.save(update_fields=["course", "board", "updated_at"])

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"linked={linked}  already_linked={already}  left_unlinked={skipped}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --yes to write."))
        elif linked:
            self.stdout.write(self.style.WARNING(
                "Cards now derive title/price/thumbnail from their target. The "
                "public featured endpoint is cached — allow the TTL to lapse "
                "before judging the live page."
            ))
