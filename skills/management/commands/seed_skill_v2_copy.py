"""
Move the Skill Development directory's CMS copy onto the v2 design's wording.

    python manage.py seed_skill_v2_copy          # dry run (default)
    python manage.py seed_skill_v2_copy --yes    # actually write

WHY THIS EXISTS
---------------
`/skill/browse` was rebuilt from a design handoff. The page reads its hero and
teach-banner copy from `SkillMarketingBlock` and falls back to hardcoded
strings only when a row is missing or inactive — so on any environment that
already has rows, the new layout renders with the OLD words and editing the
JSX changes nothing. The same trap drove the /about and /contact seeds.

The specific change requested: the directory describes its reach as
**"across India"**, not "across Mizoram". That phrase appears in the hero body
and, separately, in `stat_label` — the "N experts <stat_label>" line in the
at-a-glance panel, which is easy to miss because it lives on a different field
from the sentence it visually sits beside.

⚠ The district filter still offers Mizoram's eight districts only (there is no
`/skill/districts/` endpoint yet, so the list is hardcoded in
`components/skill/directoryOptions.js`). "Across India" is therefore a claim
the filter UI does not yet back up. That was a deliberate product decision, not
an oversight — but if the reach is meant to stay regional, revert this seed
rather than editing the JSX.

SAFETY
------
* Dry run by default; nothing is written without --yes.
* The rollback is a `raise` out of `transaction.atomic()`. It is NOT
  `transaction.savepoint()`, which returns None in autocommit mode and makes
  its matching `savepoint_rollback()` a silent no-op — a "dry run" that commits
  every row. That has actually happened in this repo; see the seeder docstring
  in courses/management/commands/seed_academy_launch.py.
* Per FIELD, not per row: an editor may have filled in `body` and left
  `subheading` empty, and a whole-row replace would blank their work. Only
  fields whose current value differs are touched, and each change is printed.
* Idempotent — a second run reports "Nothing to do".
* Blocks are created if absent, so this also works on an empty CMS.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from skills.marketing_models import SkillMarketingBlock


# key -> {field: new value}. Only these fields are considered; anything else on
# the row (image, is_active, cta_url) is left exactly as the editor set it.
COPY = {
    SkillMarketingBlock.KEY_BROWSE_HERO: {
        "subheading": "Skill Development",
        "heading": "Find a teacher for any skill",
        "body": (
            "Verified experts from across India — online, at their place, or "
            "travelling to you. Browsing is free and needs no account."
        ),
        "stat_label": "listed across India",
    },
    SkillMarketingBlock.KEY_TEACH_BANNER: {
        "heading": "Are you an expert at something?",
        "body": (
            "Share your craft with students across India. Create a teaching "
            "account — it takes less than 5 minutes."
        ),
        "cta_label": "I want to teach my craft",
    },
}


class _DryRun(Exception):
    """Raised to roll the transaction back after a dry run."""


class Command(BaseCommand):
    help = "Point the Skill Development CMS copy at the v2 design's wording."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this the command only reports.",
        )

    def handle(self, *args, **options):
        apply_changes = options["yes"]
        changes = []

        try:
            with transaction.atomic():
                for key, fields in COPY.items():
                    block, created = SkillMarketingBlock.objects.get_or_create(key=key)
                    if created:
                        changes.append(f"CREATE  {key}  (no row existed)")

                    for field, new in fields.items():
                        old = getattr(block, field) or ""
                        if old == new:
                            continue
                        changes.append(
                            f"UPDATE  {key}.{field}\n"
                            f"          from: {_short(old)}\n"
                            f"          to:   {_short(new)}"
                        )
                        setattr(block, field, new)
                    block.save()

                if not changes:
                    self.stdout.write(self.style.SUCCESS("Nothing to do — copy is already current."))
                    return

                for line in changes:
                    self.stdout.write(line)

                if not apply_changes:
                    # A rollback must be a raise out of atomic(); see the module
                    # docstring for why savepoint() is not used here.
                    self.stdout.write(self.style.WARNING(
                        f"\nDRY RUN — {len(changes)} change(s) NOT written. "
                        f"Re-run with --yes to apply."
                    ))
                    raise _DryRun()

                self.stdout.write(self.style.SUCCESS(
                    f"\nApplied {len(changes)} change(s)."
                ))
        except _DryRun:
            # Expected control flow for a dry run — not a failure. Swallowing
            # only this exception keeps a real error loud.
            pass


def _short(text, limit=72):
    text = (text or "").replace("\n", " ⏎ ")
    if not text:
        return "(empty)"
    return text if len(text) <= limit else text[: limit - 1] + "…"
