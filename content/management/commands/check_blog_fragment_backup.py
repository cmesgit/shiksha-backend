# PLACEMENT: backend/content/management/commands/check_blog_fragment_backup.py
#
# Read-only. Verifies that the on-disk fragment tree
# (shiksha-frontend/public/blog-content/, the source import_blog_fragments.py
# imported from) still matches what's stored in the database for every
# legacy chapter — the actual safety net for the block-editor project's
# legacy importer, since BlogPost.body_html_source is NOT a backup (it is
# reassigned from the incoming payload on every save, before sanitization —
# see that field's help_text). Run this before ever pointing the importer at
# a real post:
#
#     python manage.py check_blog_fragment_backup /path/to/blog-content
#
# Exits non-zero if any fragment's content differs from the matching post's
# body_html_source, or if the DB has trusted_html posts with no on-disk
# fragment at all (beyond the one known exception — see below). Makes no
# database writes.

import hashlib
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content.models import BlogPost

# id 115 ("sdsd") was authored directly in the CMS admin, not imported from a
# fragment file — it has no on-disk twin by design, not by accident. Any
# other unmatched post is worth investigating.
KNOWN_NO_FRAGMENT_SLUGS = {"sdsd"}


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Command(BaseCommand):
    help = "Verify the on-disk blog-content fragment tree matches the DB (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("fragment_root", type=str)

    def handle(self, *args, **options):
        root = Path(options["fragment_root"])
        if not root.is_dir():
            raise CommandError(f"Not a directory: {root}")

        fragments = sorted(root.rglob("*.html"))
        if not fragments:
            raise CommandError(f"No .html fragments found under {root}")

        mismatches = []
        missing_in_db = []
        matched_slugs = set()

        for path in fragments:
            slug = str(path.relative_to(root).with_suffix("")).replace("\\", "/")
            matched_slugs.add(slug)
            post = BlogPost.objects.filter(slug=slug, locale="en").first()
            if post is None:
                missing_in_db.append(slug)
                continue
            on_disk = path.read_text(encoding="utf-8")
            if _sha(on_disk) != _sha(post.body_html_source):
                mismatches.append(slug)

        all_slugs = set(
            BlogPost.objects.filter(locale="en").values_list("slug", flat=True)
        )
        missing_on_disk = sorted(
            (all_slugs - matched_slugs) - KNOWN_NO_FRAGMENT_SLUGS
        )

        self.stdout.write(f"Checked {len(fragments)} fragments against the database.\n")

        ok = True
        if mismatches:
            ok = False
            self.stdout.write(self.style.ERROR(
                f"\n{len(mismatches)} fragment(s) differ from the stored post:"
            ))
            for slug in mismatches:
                self.stdout.write(f"  ✗ {slug}")

        if missing_in_db:
            ok = False
            self.stdout.write(self.style.ERROR(
                f"\n{len(missing_in_db)} on-disk fragment(s) have no matching DB post:"
            ))
            for slug in missing_in_db:
                self.stdout.write(f"  ✗ {slug}")

        if missing_on_disk:
            ok = False
            self.stdout.write(self.style.ERROR(
                f"\n{len(missing_on_disk)} DB post(s) have no on-disk fragment "
                f"(beyond the known exception {sorted(KNOWN_NO_FRAGMENT_SLUGS)}):"
            ))
            for slug in missing_on_disk:
                self.stdout.write(f"  ✗ {slug}")

        if ok:
            self.stdout.write(self.style.SUCCESS(
                f"\n✓ All {len(fragments)} fragments match the database byte-for-byte."
            ))
        else:
            raise CommandError("Backup verification failed — see above.")
