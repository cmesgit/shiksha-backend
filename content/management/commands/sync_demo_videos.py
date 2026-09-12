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

The rules themselves live in ``content/demo_video_bunny.py``, shared with the
upload flow on the admin change form — which attaches a guid and then asks the
same question this command asks. Only the reporting below is the command's own.
"""

from django.core.management.base import BaseCommand

from content.demo_video_bunny import sync_demo_video
from content.models import DemoVideo

# One definition, on the model, because the list endpoint gates on it too.
STATUS_FINISHED = DemoVideo.BUNNY_FINISHED


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
            # save=False on a dry run sets the fields in memory so they can be
            # reported, and writes nothing.
            changed, data = sync_demo_video(video, save=not dry)
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

            if not changed:
                skipped += 1
                self.stdout.write(f"  {video.key}: already up to date")
            elif dry:
                synced += 1
                self.stdout.write(self.style.WARNING(
                    f"  {video.key}: would set {', '.join(changed)} "
                    f"(runtime {self._fmt(video.duration_seconds)})"
                ))
            else:
                synced += 1
                # Already written by sync_demo_video(save=True) above.
                self.stdout.write(self.style.SUCCESS(
                    f"  {video.key}: set {', '.join(changed)} "
                    f"(runtime {self._fmt(video.duration_seconds)})"
                ))

            # Keyed on the real condition, not on whether anything changed.
            # It used to hang off the `not changed` branch, so the first sync
            # of an unfinished clip — which always writes bunny_status — said
            # nothing about it being unplayable.
            if status != STATUS_FINISHED:
                self.stdout.write(self.style.WARNING(
                    f"  {video.key}: still processing on Bunny (status "
                    f"{status}, not {STATUS_FINISHED} Finished) — this clip is "
                    f"HIDDEN from the site until it is. Re-run in a few "
                    f"minutes; if it is stuck at 2 with storageSize 0 the "
                    f"upload stored nothing, so upload the clip again from "
                    f"the row in Django admin."
                ))

        verb = "would update" if dry else "updated"
        self.stdout.write(
            f"\n{verb} {synced}, unchanged {skipped}, unreachable {unreachable}"
        )
        if dry and synced:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))

    @staticmethod
    def _fmt(seconds):
        if not seconds:
            return "unknown"
        return f"{seconds // 60}:{seconds % 60:02d}"
