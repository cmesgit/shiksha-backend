"""Backfill `Document.file_hash` for uploads that predate the field.

The duplicate-upload guard keys on a SHA-256 of the file's bytes. Rows created
before that field existed carry a blank hash and are excluded from the
uniqueness constraint, so without this command a user who uploaded a file
before the change could still upload it a second time — once.

This reads every file, which on a real deployment means an HTTP GET per
document against Bunny Edge Storage, so it is a command rather than a data
migration: a migration that has to fetch remote media can hang a deploy, and
`migrate` is not where you want a network dependency.

    backfill_document_hashes --dry-run     # report only, writes nothing
    backfill_document_hashes               # write the hashes

Idempotent — it only looks at rows whose hash is still blank, so re-running it
after a partial failure picks up where it stopped. Rows whose file is missing
from storage are reported and left blank; that is a pre-existing data problem,
not something this command should paper over.
"""

import hashlib

from django.core.management.base import BaseCommand

from documents.models import Document


class Command(BaseCommand):
    help = "Compute SHA-256 file hashes for documents uploaded before the field existed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be written without saving anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        pending = Document.objects.filter(file_hash="").exclude(file="").order_by("pk")
        total = pending.count()

        if not total:
            self.stdout.write(self.style.SUCCESS("Nothing to do — no unhashed documents with a file."))
            return

        self.stdout.write(f"{total} document(s) need a hash." + (" (dry run)" if dry_run else ""))

        hashed = skipped = 0
        seen = {}          # (owner_id, hash) -> first document id, to report collisions
        collisions = []

        for doc in pending.iterator():
            try:
                digest = hashlib.sha256()
                with doc.file.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        digest.update(chunk)
                value = digest.hexdigest()
            except Exception as exc:                     # missing/unreadable file
                skipped += 1
                self.stderr.write(self.style.WARNING(
                    f"  skip #{doc.pk} {doc.title!r}: {exc.__class__.__name__}: {exc}"))
                continue

            key = (doc.owner_id, value)
            if not doc.is_removed:
                if key in seen:
                    # Writing this would violate the uniqueness constraint. Report
                    # it and leave the later copy blank rather than crashing the
                    # run or silently deleting somebody's document.
                    collisions.append((seen[key], doc.pk, doc.title))
                    continue
                seen[key] = doc.pk

            if not dry_run:
                Document.objects.filter(pk=doc.pk).update(file_hash=value)
            hashed += 1

        self.stdout.write(self.style.SUCCESS(
            f"{'Would hash' if dry_run else 'Hashed'} {hashed} document(s); {skipped} skipped."))

        if collisions:
            self.stdout.write(self.style.WARNING(
                f"\n{len(collisions)} pre-existing duplicate(s) left unhashed "
                f"(same owner already has these bytes under an earlier document):"))
            for first, dup, title in collisions:
                self.stdout.write(f"  #{dup} {title!r} duplicates #{first}")
            self.stdout.write(
                "These are real duplicates that got in before the guard existed. "
                "Delete the extra copy to let the owner re-upload cleanly.")
