# PLACEMENT: backend/backend/materials/management/commands/cleanup_orphan_material_files.py
# DEPLOY:    /app/shiksha-backend/materials/management/commands/cleanup_orphan_material_files.py
#
# (Also create empty __init__.py files at materials/management/ and
#  materials/management/commands/ if they don't exist yet.)
#
# WHY THIS EXISTS
# ───────────────
# The two-step upload flow (POST /files/upload/ → attach via file_ids) leaves
# a MaterialFile row with material=NULL whenever a teacher uploads a file but
# never finishes creating the material (closed the tab, validation failed,
# changed their mind). Nothing ever deleted those rows or their bytes, so
# storage grew forever. This command removes orphans older than a grace
# window (default 24h — long enough that an in-progress upload is never
# swept away).
#
# Run daily, e.g. via cron on the droplet:
#   0 3 * * *  cd /app/shiksha-backend && python manage.py cleanup_orphan_material_files
#
# or wire it into Celery beat alongside the existing scheduled tasks.

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from materials.models import MaterialFile


class Command(BaseCommand):
    help = (
        "Delete MaterialFile rows that were never attached to a StudyMaterial "
        "(material IS NULL) and are older than the grace window, including "
        "their files in storage."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=24,
            help="Grace window in hours before an unattached file is purged "
                 "(default: 24).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be deleted without deleting anything.",
        )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(hours=options["hours"])
        orphans = MaterialFile.objects.filter(
            material__isnull=True, uploaded_at__lt=cutoff
        )

        count = orphans.count()
        if options["dry_run"]:
            for mf in orphans:
                self.stdout.write(f"[dry-run] would delete {mf.id} · {mf.filename()}")
            self.stdout.write(self.style.WARNING(
                f"[dry-run] {count} orphaned file(s) older than "
                f"{options['hours']}h would be deleted."
            ))
            return

        deleted = 0
        for mf in orphans:
            try:
                mf.file.delete(save=False)  # remove bytes from storage first
            except Exception:
                pass  # missing blob must not block the row cleanup
            mf.delete()
            deleted += 1

        self.stdout.write(self.style.SUCCESS(
            f"Deleted {deleted} orphaned material file(s) older than "
            f"{options['hours']}h."
        ))
