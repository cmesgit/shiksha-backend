# PLACEMENT: backend/courses/management/commands/retire_government_exams.py
#
# Retires the catch-all competitive Course "Government Exams"
# (slug government-exams), which duplicates the narrower "SSC Exams",
# "Banking Exams" and "Railway Exams" rows added to the same nav column on
# 2026-09-11, and repoints the homepage card it backs at "SSC Exams".
#
# Usage:
#     python manage.py retire_government_exams            # dry run (default)
#     python manage.py retire_government_exams --yes      # actually write
#
# Idempotent: it discovers its own work from the current state, so a second
# run reports "nothing to do" rather than writing again.
#
# ── Why DRAFT and not just "delete the nav row" ───────────────────────────
# The nav column is DERIVED (courses/views.py derive_nav_categories()) from
# competitive-tagged Courses, so the only way to drop a row is to take the
# Course out of PUBLIC_COURSE_STATUSES. DRAFT rather than ARCHIVED because
# EnrollCourseSummaryView (courses/views.py:54) still serves ARCHIVED courses
# to any authenticated caller that knows the UUID; DRAFT is invisible
# everywhere. Nothing is deleted — the row and its slug survive, so this is
# reversible by flipping the status back.
#
# ── Why the card MUST be repointed in the same transaction ────────────────
# This is the trap that makes "just set it to DRAFT" wrong. PublicFeaturedView
# filters on ShowcaseCourse.status, NOT on Course.status, so retiring the
# course does NOT remove or blank the homepage card. What it does instead:
#
#     is_coming_soon = card.course.status == Course.STATUS_COMING_SOON
#
# DRAFT != COMING_SOON, so the card silently flips from an inert "Coming Soon"
# tile to one that renders a price and an "Enrol" button, and FeaturedCourses'
# `canViewSyllabus: !c.explore && !c.soon && !!(c.courseSlug || c.courseId)`
# turns on a "View syllabus" button. Both navigate to /courses/government-exams,
# which 404s because _public_course_detail_queryset() filters on
# PUBLIC_COURSE_STATUSES. Pointing the card at a live COMING_SOON course keeps
# every one of those derivations correct without needing an override.

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from content.models import ShowcaseCourse
from courses.models import Course, CourseCategory

# The catch-all being retired, and the row that supersedes it on the homepage.
OLD_SLUG = "government-exams"
NEW_SLUG = "ssc-exams"

# The card's stored level_label is rewritten only when it still holds the
# value the seed wrote. "SSC · Banking" describes a catch-all that no longer
# exists — Banking Exams is its own row now — but an admin who has since typed
# their own chip is making a curation decision this command must not overrule.
# Keyed on the legacy form, per the lesson in seed_boards._upgrade_names():
# asserting "the value I mean to replace" is only safe when that value cannot
# also be "the value a human deliberately chose".
LEGACY_LEVEL_LABEL = "SSC · Banking"
NEW_LEVEL_LABEL = "SSC"


class _Rollback(Exception):
    """Raised to undo a dry run.

    transaction.savepoint() is a silent no-op outside an atomic block — it
    returns None in autocommit mode and the matching savepoint_rollback()
    does nothing, which has committed a "dry run" in this codebase before.
    Raising out of transaction.atomic() is the only reliable rollback.
    """


class Command(BaseCommand):
    help = (
        "Retire the duplicate 'Government Exams' course (status=DRAFT) and "
        "repoint its homepage showcase card at 'SSC Exams'. Dry-run by "
        "default; pass --yes to write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this flag, only reports what would happen.",
        )

    # ── guards ────────────────────────────────────────────────────────────

    def _blocking_dependents(self, course):
        """Rows that make `course` real content rather than an unbuilt shell.

        The precondition for retiring this course is the REASON it is safe to
        retire: it is an empty placeholder that duplicates a more specific
        row. Keyed on emptiness rather than on "status is still COMING_SOON",
        so the moment somebody builds this course out — subjects, a batch,
        an enrolment, a notify-me signup — the command stops applying instead
        of quietly retiring work that has since been done.

        Showcase cards are excluded: repointing them is this command's job.
        """
        blocking = []
        for rel in course._meta.related_objects:
            if rel.related_model is ShowcaseCourse:
                continue
            accessor = rel.get_accessor_name()
            try:
                related = getattr(course, accessor)
            except Exception:
                # A reverse one-to-one with no row raises RelatedObjectDoesNotExist.
                continue
            count = related.count() if hasattr(related, "count") else (1 if related else 0)
            if count:
                blocking.append((rel.related_model._meta.label, accessor, count))
        return blocking

    # ── main ──────────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        dry_run = not options["yes"]
        mode = "DRY RUN — nothing will be written" if dry_run else "WRITE MODE"
        self.stdout.write(self.style.WARNING(f"=== retire_government_exams: {mode} ==="))

        try:
            with transaction.atomic():
                wrote = self._apply()
                if dry_run:
                    raise _Rollback
        except _Rollback:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Dry run — re-run with --yes to write."))
            return

        self.stdout.write("")
        if wrote:
            self.stdout.write(self.style.SUCCESS("Done. Course and ShowcaseCourse both bump "
                                                 "courses_version(), so the nav and featured "
                                                 "caches have already self-invalidated."))
        else:
            self.stdout.write(self.style.SUCCESS("Nothing to do — already in the target state."))

    def _apply(self):
        """Returns True if anything was (or would be) changed."""
        old = Course.objects.filter(slug=OLD_SLUG).first()
        new = Course.objects.filter(slug=NEW_SLUG).first()

        if old is None:
            self.stdout.write(f"  course '{OLD_SLUG}' does not exist — nothing to retire.")
            return False

        # The replacement has to be publicly visible, or repointing the card
        # would aim it at something no visitor can reach — swapping a
        # duplicate nav row for a broken homepage tile.
        if new is None:
            raise CommandError(
                f"REFUSING: replacement course '{NEW_SLUG}' does not exist. "
                f"Run `seed_course_categories --yes` then "
                f"`create_competitive_courses --yes` first."
            )
        if new.status not in (Course.STATUS_PUBLISHED, Course.STATUS_COMING_SOON):
            raise CommandError(
                f"REFUSING: replacement course '{NEW_SLUG}' has status={new.status}, "
                f"which is not publicly visible. The homepage card would point at "
                f"a course no visitor can open."
            )
        if not new.categories.filter(group=CourseCategory.GROUP_COMPETITIVE).exists():
            raise CommandError(
                f"REFUSING: replacement course '{NEW_SLUG}' carries no competitive "
                f"CourseCategory, so it is not in the nav column either "
                f"(derive_nav_categories filters on categories__group)."
            )

        blocking = self._blocking_dependents(old)
        if blocking:
            self.stdout.write(self.style.ERROR(
                f"REFUSING: '{OLD_SLUG}' is no longer an empty placeholder:"
            ))
            for label, accessor, count in blocking:
                self.stdout.write(self.style.ERROR(f"    {count:>4} x {label} (via .{accessor})"))
            raise CommandError(
                "Someone has built this course out since it was flagged as a "
                "duplicate. Retiring it would hide real content — resolve by hand."
            )

        changed = False

        # ── 1. repoint the homepage card(s) ───────────────────────────────
        cards = list(ShowcaseCourse.objects.filter(course=old).order_by("order"))
        if not cards:
            already = ShowcaseCourse.objects.filter(course=new).count()
            self.stdout.write(
                f"  card      no ShowcaseCourse points at '{OLD_SLUG}'"
                + (f" ({already} already point at '{NEW_SLUG}')" if already else "")
            )
        for card in cards:
            changed = True
            fields = ["course"]
            card.course = new

            # `title` is only admin-facing while use_own_details is False
            # (PublicFeaturedView derives the public title from the course),
            # but leaving it reading "Government Exams" would misidentify the
            # card in the Studio list and in seed_featured_cards' output.
            if not card.use_own_details and card.title != new.title:
                self.stdout.write(
                    f"  card      order={card.order} title {card.title!r} -> {new.title!r}"
                )
                card.title = new.title
                fields.append("title")

            if card.level_label == LEGACY_LEVEL_LABEL:
                self.stdout.write(
                    f"  card      order={card.order} level_label "
                    f"{card.level_label!r} -> {NEW_LEVEL_LABEL!r}"
                )
                card.level_label = NEW_LEVEL_LABEL
                fields.append("level_label")
            elif card.level_label != NEW_LEVEL_LABEL:
                self.stdout.write(self.style.WARNING(
                    f"  card      order={card.order} level_label is {card.level_label!r}, "
                    f"not the seeded {LEGACY_LEVEL_LABEL!r} — KEPT (admin-chosen)."
                ))

            self.stdout.write(
                f"  REPOINT   order={card.order} card -> course '{NEW_SLUG}' "
                f"(was '{OLD_SLUG}'); coming_soon_override={card.coming_soon_override!r}, "
                f"so the badge now follows {NEW_SLUG} (status={new.status})"
            )
            card.save(update_fields=fields + ["updated_at"])

        # ── 2. retire the course ──────────────────────────────────────────
        if old.status == Course.STATUS_DRAFT:
            self.stdout.write(f"  course    '{OLD_SLUG}' already DRAFT — nothing to do")
        else:
            changed = True
            self.stdout.write(
                f"  RETIRE    course '{OLD_SLUG}' status {old.status} -> "
                f"{Course.STATUS_DRAFT} (drops it from the nav column and the catalog)"
            )
            old.status = Course.STATUS_DRAFT
            old.save(update_fields=["status"])

        return changed
