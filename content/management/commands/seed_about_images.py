# PLACEMENT: backend/content/management/commands/seed_about_images.py
#
# Materialises the /about page's HARDCODED images into real CMS rows, so the
# artwork becomes admin-swappable like its copy already is.
#
#     python manage.py seed_about_images           # dry run (default)
#     python manage.py seed_about_images --yes     # actually write
#
# Why this exists
# ---------------
# seed_homepage_defaults did this for every *word* on the page. The images
# were left behind: About2.jsx imported seven files straight out of
# src/assets/, so swapping the Vision photo or a hero sticker meant a code
# change and a frontend deploy. Migration 0016 added image/image_url to
# HomeListItem (HomeContentBlock already had them) and About2.jsx now reads
# `img` on both, falling back to those same bundled files. This command fills
# the CMS in with what is currently on screen, so an editor opens the section
# and sees the real image sitting there to replace, not an empty field.
#
# Trade-off worth knowing before running this on production
# --------------------------------------------------------
# The bundled imports are content-hashed by Vite and served immutably from
# the same origin as the page. Seeded rows are served from MEDIA instead
# (BunnyCDN when BUNNY_STORAGE_* is configured, local disk otherwise), which
# is an extra origin and one more thing that can 404. Because About2.jsx
# falls back per-image, a missing seeded file degrades to the bundled asset
# rather than breaking the page — but the page is strictly faster if you
# DON'T run this and only attach images you actually want to change. Running
# it buys discoverability in the CMS, not performance.
#
# Safety model (same as seed_homepage_defaults)
# ---------------------------------------------
# * Dry run by default. Nothing is written without --yes.
# * CREATE-ONLY by default: a block that already has an image, or a sticker
#   scope that already has rows, is left completely alone — this can never
#   clobber artwork an editor uploaded. Pass --update to overwrite.
# * Idempotent: re-running writes nothing and can never duplicate a sticker.

from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand
from django.db import transaction

from content.models import (
    HomeContentBlock, HomeListItem, HomeListVariant, HomeSection,
)

SEED_DIR = Path(__file__).resolve().parent.parent.parent / "seed_assets" / "about"

# section -> filename, for the two large photos that live on a content block.
BLOCK_IMAGES = {
    HomeSection.ABOUT_VISION: "meet.jpeg",
    HomeSection.ABOUT_VALUES: "studio.jpeg",
}

# The About hero's sticker row, in the order About2.jsx lists them. This is
# deliberately NOT sticker_1..5 — it is the order the row was designed in, and
# reordering it changes the page.
STICKERS = [
    "sticker_5.png",
    "sticker_2.png",
    "sticker_3.png",
    "sticker_4.png",
    "sticker_1.png",
]


class Command(BaseCommand):
    help = "Seed the /about page's hardcoded images into the CMS (dry run unless --yes)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this the command only reports.",
        )
        parser.add_argument(
            "--update", action="store_true",
            help="Also overwrite images that are already set (default is "
                 "create-only, which never touches an editor's upload).",
        )

    def handle(self, *args, **opts):
        write = opts["yes"]
        update = opts["update"]

        missing = [n for n in [*BLOCK_IMAGES.values(), *STICKERS]
                   if not (SEED_DIR / n).is_file()]
        if missing:
            self.stderr.write(self.style.ERROR(
                f"Missing seed asset(s) in {SEED_DIR}: {', '.join(missing)}"
            ))
            return

        created = updated = skipped = 0

        with transaction.atomic():
            # ── the two content-block photos ──
            for section, filename in BLOCK_IMAGES.items():
                block = HomeContentBlock.objects.filter(section=section).first()
                if block is None:
                    self.stdout.write(self.style.WARNING(
                        f"  ! no {section} block yet — run seed_homepage_defaults "
                        f"first, then re-run this ({filename} not attached)"
                    ))
                    skipped += 1
                    continue
                if block.image and not update:
                    self.stdout.write(f"  = {section}: already has an image, skipped")
                    skipped += 1
                    continue
                verb = "update" if block.image else "attach"
                self.stdout.write(f"  {'~' if block.image else '+'} {section}: {verb} {filename}")
                if write:
                    with open(SEED_DIR / filename, "rb") as fh:
                        block.image.save(filename, File(fh), save=True)
                    updated += 1 if verb == "update" else 0
                    created += 1 if verb == "attach" else 0

            # ── the hero sticker row ──
            existing = HomeListItem.objects.filter(
                section=HomeSection.ABOUT_HERO,
                variant=HomeListVariant.STICKER,
            )
            if existing.exists() and not update:
                self.stdout.write(
                    f"  = about_hero stickers: {existing.count()} row(s) already "
                    f"exist, scope skipped"
                )
                skipped += existing.count()
            else:
                if update and existing.exists():
                    self.stdout.write(
                        f"  ~ about_hero stickers: replacing {existing.count()} row(s)"
                    )
                    if write:
                        for row in existing:
                            row.image.delete(save=False)
                        existing.delete()
                for i, filename in enumerate(STICKERS):
                    self.stdout.write(f"  + about_hero sticker {i}: {filename}")
                    if write:
                        row = HomeListItem.objects.create(
                            section=HomeSection.ABOUT_HERO,
                            variant=HomeListVariant.STICKER,
                            order=i,
                        )
                        with open(SEED_DIR / filename, "rb") as fh:
                            row.image.save(filename, File(fh), save=True)
                        created += 1

            if not write:
                # Nothing was written, but block.image.save() above would have
                # been inside this atomic block — bail out explicitly so a
                # future edit that forgets a `if write:` can't leak a write.
                transaction.set_rollback(True)

        if write:
            self.stdout.write(self.style.SUCCESS(
                f"Done. created={created} updated={updated} skipped={skipped}"
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "Dry run — nothing written. Re-run with --yes to apply."
            ))
