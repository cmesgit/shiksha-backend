# PLACEMENT: backend/courses/management/commands/create_competitive_courses.py
#
# Creates the 7 competitive-exam courses as real Course rows
# (kind=COACHING, status=COMING_SOON) carrying the marketing copy + tutor
# names already written in FEATURED_COURSES, and links each to its matching
# competitive-group CourseCategory. Data: _catalog_seed_data.COMPETITIVE_COURSE_SEED.
#
# Usage:
#     python manage.py create_competitive_courses            # dry run (default)
#     python manage.py create_competitive_courses --yes       # actually write
#
# Idempotent: matched by fixed `slug` (and, defensively, by exact title). An
# existing course is left as-is except that its competitive category link is
# ensured. Prerequisite: run seed_course_categories first so the categories
# exist; if a category is missing this reports it and skips the link (never
# invents one).

from django.core.management.base import BaseCommand
from django.db import transaction

from courses.models import Course, CourseCategory

from ._catalog_seed_data import COMPETITIVE_COURSE_SEED


class Command(BaseCommand):
    help = (
        "Create the 7 competitive COACHING courses (status=COMING_SOON) and link "
        "each to its competitive CourseCategory. Dry-run by default; pass --yes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this flag, only reports what would happen.",
        )

    def _description(self, row):
        return (
            f"{row['fact']}\n\n"
            f"{row['title']} — {row['level']} track. "
            f"Mentor: {row['tutor']}. Launching soon on ShikshaCom."
        )

    def handle(self, *args, **options):
        dry_run = not options["yes"]
        mode = "DRY RUN — nothing will be written" if dry_run else "WRITE MODE"
        self.stdout.write(self.style.WARNING(f"=== create_competitive_courses: {mode} ==="))

        created = matched = linked = 0

        for row in COMPETITIVE_COURSE_SEED:
            slug, title = row["slug"], row["title"]

            # Match by fixed slug OR exact title — either identifies the same row.
            existing = Course.objects.filter(slug=slug).first() \
                or Course.objects.filter(title=title, board__isnull=True, stream__isnull=True).first()

            category = CourseCategory.objects.filter(slug=row["category"]).first()
            cat_note = f"category '{row['category']}'" if category else \
                self.style.WARNING(f"category '{row['category']}' NOT FOUND (run seed_course_categories first)")

            if existing is None:
                created += 1
                self.stdout.write(
                    f"  CREATE  {slug:22} '{title}'  kind=COACHING status=COMING_SOON → {cat_note}"
                )
                if not dry_run:
                    with transaction.atomic():
                        course = Course.objects.create(
                            title=title, slug=slug,
                            description=self._description(row),
                            kind=Course.KIND_COACHING,
                            status=Course.STATUS_COMING_SOON,
                            price=0,
                        )
                        if category:
                            course.categories.add(category)
                            linked += 1
                else:
                    if category:
                        linked += 1
            else:
                matched += 1
                already_linked = (
                    category is not None
                    and existing.categories.filter(pk=category.pk).exists()
                )
                if category and not already_linked:
                    self.stdout.write(
                        f"  SKIP    {slug:22} '{title}'  exists (id={existing.id}) — would LINK {cat_note}"
                    )
                    linked += 1
                    if not dry_run:
                        with transaction.atomic():
                            existing.categories.add(category)
                else:
                    self.stdout.write(
                        f"  SKIP    {slug:22} '{title}'  exists (id={existing.id}), link ok — nothing to do"
                    )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"competitive courses: created={created} matched(skipped)={matched} "
            f"category_links_added={linked} (of {len(COMPETITIVE_COURSE_SEED)})"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --yes to write."))
