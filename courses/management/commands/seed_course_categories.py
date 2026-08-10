# PLACEMENT: backend/courses/management/commands/seed_course_categories.py
#
# Seeds the CourseCategory taxonomy that powers the homepage tab filters and
# the competitive-exam tracks. Data lives in _catalog_seed_data.CATEGORY_SEED.
#
# Usage (matches import_static_course_content's convention):
#     python manage.py seed_course_categories            # dry run (default)
#     python manage.py seed_course_categories --yes       # actually write
#
# Idempotent: matched by `slug`. An existing category is updated in place
# (name/group/blurb/icon/display_order/is_active) — never duplicated.

from django.core.management.base import BaseCommand
from django.db import transaction

from courses.models import CourseCategory

from ._catalog_seed_data import CATEGORY_SEED


class Command(BaseCommand):
    help = (
        "Seed CourseCategory rows (boards / class8-12 / competitive groups). "
        "Dry-run by default; pass --yes to write. Idempotent by slug."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this flag, only reports what would happen.",
        )

    def handle(self, *args, **options):
        dry_run = not options["yes"]
        mode = "DRY RUN — nothing will be written" if dry_run else "WRITE MODE"
        self.stdout.write(self.style.WARNING(f"=== seed_course_categories: {mode} ==="))

        created = updated = unchanged = 0

        for row in CATEGORY_SEED:
            slug = row["slug"]
            fields = dict(
                name=row["name"], group=row["group"],
                blurb=row.get("blurb", ""), icon=row.get("icon", ""),
                display_order=row.get("display_order", 0),
                is_active=row.get("is_active", True),
            )
            existing = CourseCategory.objects.filter(slug=slug).first()

            if existing is None:
                created += 1
                self.stdout.write(f"  CREATE  [{fields['group']:11}] {slug}  ({fields['name']})")
                if not dry_run:
                    with transaction.atomic():
                        CourseCategory.objects.create(slug=slug, **fields)
            else:
                changed = [k for k, v in fields.items() if getattr(existing, k) != v]
                if changed:
                    updated += 1
                    self.stdout.write(
                        f"  UPDATE  [{fields['group']:11}] {slug}  (changed: {', '.join(changed)})"
                    )
                    if not dry_run:
                        with transaction.atomic():
                            for k, v in fields.items():
                                setattr(existing, k, v)
                            existing.save()
                else:
                    unchanged += 1
                    self.stdout.write(f"  SKIP    [{fields['group']:11}] {slug}  (already up to date)")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"categories: created={created} updated={updated} unchanged={unchanged} "
            f"(of {len(CATEGORY_SEED)})"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --yes to write."))
