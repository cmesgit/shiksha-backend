"""Pull runtime and thumbnail for the landing-page demo videos from Bunny.

Run after uploading (or replacing) a clip in the Bunny dashboard:

    python manage.py sync_demo_videos            # sync every configured row
    python manage.py sync_demo_videos --key login
    python manage.py sync_demo_videos --dry-run  # report only, writes nothing

Why this exists rather than a `duration` an admin types in: the design mockup
this feature was built from labelled the two clips "0:40" and "0:28" when the
real recordings were 0:54 and 0:18. Hand-entered durations drift the moment a
clip is re-recorded, and nothing catches it — the label just quietly lies.
Reading it from Bunny means the only way to be wrong is for Bunny to be wrong.

Bunny reports ``length: 0`` until it has finished processing an upload, so a
freshly-uploaded clip syncs to "no duration known" and needs a second run a few
minutes later. That is reported as a skip, not a success — see
``skills/intro_video.py`` which coerces the same 0 for the same reason.

Reads only. This never creates, deletes or uploads anything on Bunny.
"""

from django.core.management.base import BaseCommand

from content.models import DemoVideo
from skills.intro_video import fetch_bunny_video

# Bunny's status codes, from skills/models.py INTRO_VIDEO_STATUS_CHOICES.
STATUS_FINISHED = 4


class Command(BaseCommand):
    help = "Sync landing demo video runtime/thumbnail from Bunny Stream."

    def add_arguments(self, parser):
        parser.add_argument(
            "--key",
            help="Sync only the DemoVideo with this key (e.g. signup).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        qs = DemoVideo.objects.exclude(bunny_video_id="")
        if opts.get("key"):
            qs = qs.filter(key=opts["key"])

        rows = list(qs)
        if not rows:
            self.stdout.write(self.style.WARNING(
                "No demo videos with a bunny_video_id. Add the guid in Django "
                "admin (Content → Landing demo videos) first."
            ))
            return

        synced = skipped = unreachable = 0

        for video in rows:
            data = fetch_bunny_video(video.bunny_video_id)
            if data is None:
                # None means "we learned nothing" — never "the video is gone".
                # Leave the stored values alone rather than blanking a good
                # duration because of a network blip.
                unreachable += 1
                self.stdout.write(self.style.ERROR(
                    f"  {video.key}: Bunny unreachable — left unchanged"
                ))
                continue

            status = data.get("status")
            length = data.get("length") or None
            changed = []

            if length is not None and video.duration_seconds != length:
                video.duration_seconds = length
                changed.append("duration_seconds")

            thumb = data.get("thumbnailFileName", "")
            cdn_host = self._cdn_host()
            if thumb and cdn_host:
                url = f"https://{cdn_host}/{video.bunny_video_id}/{thumb}"
                if video.thumbnail_url != url:
                    video.thumbnail_url = url
                    changed.append("thumbnail_url")

            if not changed:
                skipped += 1
                note = "already up to date"
                if length is None:
                    note = (f"still processing on Bunny (status {status}) — "
                            f"re-run in a few minutes")
                self.stdout.write(f"  {video.key}: {note}")
                continue

            if dry:
                self.stdout.write(self.style.WARNING(
                    f"  {video.key}: would set {', '.join(changed)} "
                    f"(runtime {self._fmt(video.duration_seconds)})"
                ))
            else:
                video.save(update_fields=changed + ["updated_at"])
                self.stdout.write(self.style.SUCCESS(
                    f"  {video.key}: set {', '.join(changed)} "
                    f"(runtime {self._fmt(video.duration_seconds)})"
                ))
            synced += 1

            if status != STATUS_FINISHED:
                self.stdout.write(self.style.WARNING(
                    f"  {video.key}: Bunny status is {status}, not "
                    f"{STATUS_FINISHED} (Finished) — the clip may not play yet"
                ))

        verb = "would update" if dry else "updated"
        self.stdout.write(
            f"\n{verb} {synced}, unchanged {skipped}, unreachable {unreachable}"
        )
        if dry and synced:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))

    @staticmethod
    def _cdn_host():
        from django.conf import settings
        return getattr(settings, "BUNNY_CDN_HOST", "")

    @staticmethod
    def _fmt(seconds):
        if not seconds:
            return "unknown"
        return f"{seconds // 60}:{seconds % 60:02d}"
