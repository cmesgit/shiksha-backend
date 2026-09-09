"""Prove the media storage zone actually works, end to end.

WHY THIS EXISTS
---------------
Bunny Edge Storage does not support HEAD: it answers 401 to a HEAD whether or
not the key is valid and whether or not the object exists, while GET/PUT/DELETE
on the same URL with the same key work fine. `BunnyStorage.exists()` and
`.size()` were both built on `requests.head`, so for as long as that backend
has existed:

  · exists() always returned False, so get_available_name() never detected a
    collision and same-named uploads silently overwrote each other;
  · size() always raised, which 500'd every CMS PATCH on a row with an image
    (FullCleanMixin validates the whole row; the image validator reads .size).

None of that produced a log line, a failed deploy, or a 500 anywhere anyone
was looking. It surfaced as "the show/hide toggle does nothing sometimes",
months later, on 18 of prod's 19 showcase cards.

A config check cannot catch that class of bug — the zone and key were both
present and correct. Only a real round trip can, which is what this does:

    write -> exists -> size -> read back -> delete -> confirm gone

Exit code is 1 on any failure, so a deploy script can gate on it.

USAGE
-----
    python manage.py check_media_storage            # round trip, then clean up
    python manage.py check_media_storage --keep     # leave the probe object

Safe to run against production: it writes ONE small object under a dedicated
`_healthcheck/` prefix and deletes it again. It never touches real media.
"""
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Round-trip the configured media storage backend and report what broke."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep", action="store_true",
            help="Don't delete the probe object (for inspecting it by hand).",
        )

    def handle(self, *args, **options):
        backend = type(default_storage).__name__
        module = type(default_storage).__module__
        self.stdout.write(f"backend: {module}.{backend}")

        # A unique name per run, so two concurrent deploys can't collide and a
        # leftover object from a crashed run can never make this pass.
        name = f"_healthcheck/probe-{uuid.uuid4().hex}.txt"
        payload = b"storage healthcheck"
        failures = []

        def step(label, fn):
            try:
                value = fn()
                self.stdout.write(self.style.SUCCESS(f"  ok    {label}: {value}"))
                return value
            except Exception as e:
                failures.append(f"{label}: {type(e).__name__}: {e}")
                self.stdout.write(self.style.ERROR(
                    f"  FAIL  {label}: {type(e).__name__}: {e}"
                ))
                return None

        saved = step("write", lambda: default_storage.save(name, ContentFile(payload)))
        if saved is None:
            raise CommandError(
                "Could not write to storage at all — nothing else is worth "
                "testing. Check BUNNY_STORAGE_* (or the active backend's) "
                "credentials.\n  " + "\n  ".join(failures)
            )

        # These two are the ones that were broken. exists() returning False for
        # an object we literally just wrote is the whole bug, and it is silent.
        found = step("exists (just-written object)", lambda: default_storage.exists(saved))
        if found is False:
            failures.append(
                "exists() returned False for an object that was just written. "
                "get_available_name() relies on this, so uploads sharing a "
                "filename will OVERWRITE each other instead of being suffixed."
            )
            self.stdout.write(self.style.ERROR(
                "        ^ this is silent data loss on upload, not a cosmetic bug"
            ))

        size = step("size", lambda: default_storage.size(saved))
        if size is not None and size != len(payload):
            failures.append(f"size() said {size}, expected {len(payload)}")

        step("read back", lambda: default_storage.open(saved).read()[:len(payload)])

        # A name that cannot exist — exists() must say False for the RIGHT
        # reason, not because it says False for everything.
        step(
            "exists (absent object) -> expect False",
            lambda: default_storage.exists(f"_healthcheck/absent-{uuid.uuid4().hex}.txt"),
        )

        if options["keep"]:
            self.stdout.write(f"  kept  {saved}")
        else:
            step("delete", lambda: default_storage.delete(saved) or "deleted")
            gone = step("exists after delete -> expect False",
                        lambda: default_storage.exists(saved))
            if gone:
                failures.append("object still present after delete()")

        if failures:
            raise CommandError(
                f"{len(failures)} storage problem(s):\n  " + "\n  ".join(failures)
            )
        self.stdout.write(self.style.SUCCESS("media storage round trip OK"))
