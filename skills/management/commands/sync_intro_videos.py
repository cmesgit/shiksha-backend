"""Re-sync intro-clip state from Bunny for experts and listings.

Why this exists: the save endpoints used to write `intro_video_status = 1`
("Uploaded") and never advance it. The expert-level status endpoint that
would have advanced it had no caller in any frontend, so every expert clip
ever uploaded stayed at 1 forever — and `intro_video_embed_url()` returns
None below status 4, which made all of them invisible on the public
directory, the expert profile page and the student app, while Bunny reported
every one of them finished and healthy.

The save path now syncs on write, so new clips are fine. This command repairs
the rows already in the database, and backfills `intro_video_duration` for
clips that predate the field.

    manage.py sync_intro_videos --dry-run     # report only
    manage.py sync_intro_videos               # write
    manage.py sync_intro_videos --all         # re-check even settled rows

Safe to re-run: every write is derived from Bunny's current answer, and rows
Bunny cannot be reached for are left exactly as they are.
"""
from django.core.management.base import BaseCommand

from skills.intro_video import (
    MAX_INTRO_VIDEO_SECONDS,
    fetch_bunny_video,
    needs_sync,
    sync_intro_video,
)
from skills.listing_models import SkillListing
from skills.models import ExpertProfile


class Command(BaseCommand):
    help = "Re-sync ExpertProfile/SkillListing intro-video state from Bunny."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing.",
        )
        parser.add_argument(
            "--all", action="store_true",
            help="Re-check every clip, including ones already settled at Finished.",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        force = opts["all"]

        for model, label in ((ExpertProfile, "expert"), (SkillListing, "listing")):
            rows = model.objects.exclude(intro_video_bunny_id="")
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n{label}s with a clip: {rows.count()}"
            ))
            for obj in rows:
                if not force and not needs_sync(obj):
                    self.stdout.write(f"  {obj.pk}  settled (status 4) — skipped")
                    continue

                before = (obj.intro_video_status, obj.intro_video_duration)

                if dry:
                    # Read-only path: ask Bunny, report, write nothing. A
                    # savepoint would be a silent no-op in autocommit, so
                    # this simply never calls save() rather than pretending
                    # to roll one back.
                    data = fetch_bunny_video(obj.intro_video_bunny_id)
                    if data is None:
                        self.stdout.write(self.style.WARNING(
                            f"  {obj.pk}  Bunny unreachable — would leave {before}"
                        ))
                        continue
                    length = data.get("length") or None
                    after = (data.get("status", 0), length)
                    note = ""
                    if length and length > MAX_INTRO_VIDEO_SECONDS:
                        after = (5, length)
                        note = f"  ← OVER LIMIT ({length}s > {MAX_INTRO_VIDEO_SECONDS}s)"
                    verb = "would change" if after != before else "unchanged"
                    self.stdout.write(f"  {obj.pk}  {verb}: {before} → {after}{note}")
                    continue

                changed, error = sync_intro_video(obj)
                after = (obj.intro_video_status, obj.intro_video_duration)
                if error:
                    self.stdout.write(self.style.WARNING(
                        f"  {obj.pk}  {before} → {after}  {error}"
                    ))
                elif changed:
                    self.stdout.write(self.style.SUCCESS(
                        f"  {obj.pk}  {before} → {after}"
                    ))
                else:
                    self.stdout.write(f"  {obj.pk}  unchanged {before}")

        if dry:
            self.stdout.write(self.style.WARNING("\nDry run — nothing was written."))
