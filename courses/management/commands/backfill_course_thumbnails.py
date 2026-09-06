# PLACEMENT: backend/courses/management/commands/backfill_course_thumbnails.py
#
# Give every course the picture the homepage already shows for it, so the
# /courses catalog and the homepage's featured grid stop disagreeing.
#
#     python manage.py backfill_course_thumbnails            # dry run (default)
#     python manage.py backfill_course_thumbnails --yes      # actually write
#     python manage.py backfill_course_thumbnails --no-twins # skip stage 2
#
# Why this exists
# ---------------
# Measured against production on 2026-09-06: all 26 courses had
# `Course.thumbnail = NULL`, while 18 of the 19 featured cards rendered a real
# photo. Both surfaces were behaving correctly — the pictures simply lived in
# the wrong place.
#
#   /courses          reads Course.thumbnail, and ONLY that.
#   homepage featured reads Course.thumbnail -> Board.logo -> ShowcaseCourse.image
#                     -> ShowcaseCourse.image_url  (PublicFeaturedView)
#
# So the artwork sat on the showcase CARD, the homepage fell through to it, and
# the catalog — which has no such fallback — had nothing to show and rendered
# the placeholder icon. Every other field (title, price, MRP, discount,
# coming-soon) already agreed exactly; the image was the last mismatch.
#
# Moving the picture onto the Course fixes both surfaces at once *because* the
# featured chain prefers Course.thumbnail: once set, the homepage and the
# catalog resolve to the same file rather than merely similar ones.
#
# Two stages
# ----------
# 1. Card -> course. For each published showcase card that has an image and a
#    linked Course with no thumbnail, copy the image onto the course.
# 2. Twin -> twin (skip with --no-twins). Prod carries the same course under
#    two boards — CBSE "Class 8" is featured, MBSE "Class 8" is not and so has
#    no card to inherit from. Stage 2 copies from a same-titled sibling that
#    now has art, so the catalog is not half-illustrated. The two boards share
#    a photo until an editor uploads distinct art in the CMS.
#
# Safety model (mirrors localize_showcase_images)
# -----------------------------------------------
# * Dry run by default. Nothing is read from storage or written without --yes.
# * CREATE-ONLY: a course that already has a thumbnail is never touched, so
#   this can never clobber artwork an editor uploaded.
# * Idempotent: a second run finds every course already has a thumbnail and
#   reports nothing to do.
# * Cards with `use_own_details` are SKIPPED and reported. That flag means an
#   admin deliberately opted the card out of deriving from its course, so the
#   homepage keeps showing `card.image` regardless. Copying it to the course
#   would put a different picture on the catalog and leave the two surfaces
#   disagreeing again — the exact thing this command exists to end.
#
# ⚠ `Course.thumbnail` is a ProcessedImageField (ResizeToFill 1200x675, WEBP).
# The processing runs in the field's `pre_save`, which only fires for an
# UNCOMMITTED file — i.e. when you ASSIGN to the attribute and then save the
# model. Calling `course.thumbnail.save(...)` writes the raw bytes straight to
# storage and silently skips the resize/re-encode entirely. Assignment is what
# `courses/views.py`'s upload path does, and this must match it.

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from content.models import PublishStatus, ShowcaseCourse
from courses.models import Course

# `thumbnail` is blank=True AND null=True, so "has no picture" is two states.
# Checking only one of them makes the command report success while leaving
# half the catalog bare.
_NO_THUMBNAIL = Q(thumbnail__isnull=True) | Q(thumbnail="")


def _slugish(text, fallback="course"):
    keep = [c.lower() if c.isalnum() else "-" for c in (text or "")]
    out = "".join(keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return (out or fallback)[:60]


class Command(BaseCommand):
    help = ("Copy showcase-card artwork onto the courses it belongs to, so the "
            "catalog and the homepage show the same picture (dry run unless --yes).")

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this the command only reports.",
        )
        parser.add_argument(
            "--no-twins", action="store_true",
            help="Skip stage 2 (sharing a photo with a same-titled course on "
                 "another board). Those courses keep the placeholder.",
        )

    def handle(self, *args, **opts):
        write = opts["yes"]
        do_twins = not opts["no_twins"]

        copied = shared = skipped = failed = 0
        # Dry-run only: title -> course that stage 1 would have given art to,
        # and the set of those courses' pks.
        pending = {}
        pending_pks = set()

        with transaction.atomic():
            # ---- Stage 1: showcase card -> its linked course ----------------
            self.stdout.write(self.style.MIGRATE_HEADING(
                "Stage 1 — showcase card artwork onto its linked course"
            ))
            cards = (
                ShowcaseCourse.objects
                .filter(status=PublishStatus.PUBLISHED)
                .select_related("course")
                .order_by("order", "id")
            )
            for card in cards:
                label = f"card #{card.id} {card.title or '(untitled)'}"
                course = card.course
                if course is None:
                    self.stdout.write(f"  = {label}: no linked course, skipped")
                    skipped += 1
                    continue
                if card.use_own_details:
                    self.stdout.write(self.style.WARNING(
                        f"  ~ {label}: use_own_details is set — the homepage keeps "
                        f"showing the card's own picture, so copying it to "
                        f"'{course.title}' would NOT make the two surfaces agree. Skipped."
                    ))
                    skipped += 1
                    continue
                if not card.image:
                    self.stdout.write(f"  = {label}: card has no uploaded image, skipped")
                    skipped += 1
                    continue
                if course.thumbnail:
                    self.stdout.write(
                        f"  = {label}: '{course.title}' already has a thumbnail, left alone"
                    )
                    skipped += 1
                    continue

                self.stdout.write(f"  + {label} -> course '{course.title}'")
                if not write:
                    # Remember what stage 1 WOULD have done, so the dry run's
                    # stage 2 reports the twins it would really reach. Without
                    # this, a dry run finds no donors (nothing was written) and
                    # claims every twin keeps the placeholder — the opposite of
                    # what --yes would do.
                    pending.setdefault(course.title.strip().lower(), course)
                    pending_pks.add(course.pk)
                    copied += 1
                    continue
                if self._copy(card.image, course, "card image"):
                    copied += 1
                else:
                    failed += 1

            # ---- Stage 2: share with same-titled courses on other boards ----
            if do_twins:
                self.stdout.write(self.style.MIGRATE_HEADING(
                    "Stage 2 — share that photo with same-titled courses on other boards"
                ))
                # Re-query: stage 1 has just given some courses a thumbnail and
                # those are exactly the donors stage 2 needs. On a dry run
                # nothing was written, so fold in what stage 1 would have done.
                donors = dict(pending)
                for c in Course.objects.exclude(thumbnail="").exclude(thumbnail__isnull=True):
                    donors.setdefault(c.title.strip().lower(), c)

                needy = Course.objects.filter(_NO_THUMBNAIL).order_by("title")
                for course in needy:
                    # On a real run stage 1's courses have a thumbnail now and
                    # are already out of `needy`. On a dry run they are not, so
                    # without this they get reported twice — once (wrongly) as
                    # "keeps the placeholder", once as their own donor.
                    if course.pk in pending_pks:
                        continue
                    donor = donors.get(course.title.strip().lower())
                    if donor is None or donor.pk == course.pk:
                        self.stdout.write(
                            f"  = '{course.title}': no same-titled course has a photo, "
                            f"keeps the placeholder"
                        )
                        skipped += 1
                        continue
                    self.stdout.write(
                        f"  + '{course.title}' <- shares with the same title on another board"
                    )
                    if not write:
                        shared += 1
                        continue
                    if self._copy(donor.thumbnail, course, "twin course"):
                        shared += 1
                    else:
                        failed += 1

            # ---- Report what is still bare ---------------------------------
            bare = Course.objects.filter(_NO_THUMBNAIL).count() if write else None

        verb = "would copy" if not write else "copied"
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {copied} from cards, {shared} shared with twins; "
            f"{skipped} skipped, {failed} failed."
        ))
        if bare:
            self.stdout.write(self.style.WARNING(
                f"{bare} course(s) still have no picture and will render the "
                f"placeholder on /courses. Upload art for them in the CMS."
            ))
        if not write:
            self.stdout.write(self.style.WARNING(
                "Dry run — nothing was written. Re-run with --yes to apply."
            ))

    def _copy(self, source_field, course, what):
        """Read `source_field`'s bytes and put them on `course.thumbnail`."""
        try:
            source_field.open("rb")
            try:
                payload = source_field.read()
            finally:
                source_field.close()
        except Exception as exc:  # noqa: BLE001 — report and keep going
            self.stderr.write(self.style.ERROR(
                f"    x could not read the {what}: {exc}"
            ))
            return False
        if not payload:
            self.stderr.write(self.style.ERROR(f"    x the {what} is empty"))
            return False

        # .webp because the field re-encodes to WEBP; naming it anything else
        # leaves a file whose extension lies about its contents.
        filename = f"{_slugish(course.title)}-{course.pk}.webp"
        try:
            # ASSIGNMENT, not thumbnail.save() — see the module docstring.
            course.thumbnail = ContentFile(payload, name=filename)
            course.save(update_fields=["thumbnail"])
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f"    x could not save: {exc}"))
            return False
        self.stdout.write(self.style.SUCCESS(
            f"    v {len(payload) // 1024} kB -> {course.thumbnail.name}"
        ))
        return True
