"""Put HomeSectionOrder into the canonical homepage running order.

WHY THIS EXISTS
---------------
The homepage order is fully CMS-driven: ShikshaHome.jsx paints DEFAULT_ORDER
first, then GET /content/home-section-order/ replaces it wholesale. So editing
DEFAULT_ORDER in the frontend changes the first paint and nothing else — on any
database that has already been seeded (every real environment; migration
content/0008 does the seeding), the stored rows win a moment later and the
homepage snaps back to the old order.

This command is the other half: it moves the stored rows. Run it once per
environment after deploying the matching frontend.

    manage.py reorder_homepage_sections --dry-run   # report only, changes nothing
    manage.py reorder_homepage_sections             # apply

It is idempotent — running it twice is a no-op the second time.

WHAT IT DOES NOT DO
-------------------
It never changes `is_visible`. Hiding or showing a section is an editorial
decision made in the admin page editor, and silently un-hiding something an
admin deliberately hid would be a worse bug than a wrong order. A hidden
section still gets its `order` corrected, so it lands in the right place if it
is ever shown again.

Sections present in the database but absent from CANONICAL_ORDER are appended
after it in their existing relative order rather than dropped, so a section
added later cannot be silently deleted from the homepage by an old copy of
this command.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from content.models import HomeSectionOrder


class _DryRun(Exception):
    """Unwinds the transaction on --dry-run.

    Raising is the ONLY reliable rollback here. transaction.savepoint() returns
    None outside an atomic block (autocommit), which makes the matching
    savepoint_rollback() a silent no-op — a --dry-run that commits everything it
    claims to be discarding. This project has been bitten by exactly that.
    """


# The running order the site is designed around. Keep in step with
# ShikshaHome.jsx's DEFAULT_ORDER and content.models.HOMEPAGE_SECTIONS.
CANONICAL_ORDER = [
    "hero",
    "teachers_students",
    "collaborate",
    "featured_courses",
    "browse_categories",
    "resources",
    "why_choose",
    "why_shiksha",
    "faq",
    "cta",
    "ticker_band",
]


class Command(BaseCommand):
    help = "Reorder HomeSectionOrder rows into the canonical homepage sequence."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and roll back without writing.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        try:
            with transaction.atomic():
                changed = self._apply()
                if dry_run:
                    raise _DryRun
        except _DryRun:
            self.stdout.write(self.style.WARNING("\nDry run — rolled back, nothing written."))
            return

        if changed:
            self.stdout.write(self.style.SUCCESS(f"\nReordered {changed} section(s)."))
        else:
            self.stdout.write(self.style.SUCCESS("\nAlready in canonical order — nothing to do."))

    def _apply(self):
        rows = {r.section: r for r in HomeSectionOrder.objects.all()}
        if not rows:
            self.stdout.write(
                self.style.WARNING(
                    "No HomeSectionOrder rows exist. The homepage is running on "
                    "the frontend's DEFAULT_ORDER; nothing to reorder."
                )
            )
            return 0

        # Known sections first, in canonical order; anything unrecognised keeps
        # its relative position at the end rather than being dropped.
        sequence = [s for s in CANONICAL_ORDER if s in rows]
        extra = [s for s in rows if s not in CANONICAL_ORDER]
        if extra:
            extra.sort(key=lambda s: rows[s].order)
            self.stdout.write(
                self.style.WARNING(
                    "Not in CANONICAL_ORDER, appended at the end: " + ", ".join(extra)
                )
            )
        sequence += extra

        missing = [s for s in CANONICAL_ORDER if s not in rows]
        if missing:
            self.stdout.write(
                self.style.WARNING(
                    "No row in the database (skipped): " + ", ".join(missing)
                )
            )

        to_update, changed = [], 0
        for index, section in enumerate(sequence):
            row = rows[section]
            hidden = "" if row.is_visible else "  [hidden]"
            if row.order != index:
                self.stdout.write(f"  {section:<20} {row.order} -> {index}{hidden}")
                row.order = index
                to_update.append(row)
                changed += 1
            else:
                self.stdout.write(f"  {section:<20} {row.order} (unchanged){hidden}")

        if to_update:
            HomeSectionOrder.objects.bulk_update(to_update, ["order"])
        return changed
