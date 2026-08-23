# PLACEMENT: backend/content/management/commands/seed_homepage_defaults.py
#
# Materialises the homepage content that has always been HARDCODED in the
# React components into real CMS rows, so it becomes admin-editable.
#
#     python manage.py seed_homepage_defaults           # dry run (default)
#     python manage.py seed_homepage_defaults --yes     # actually write
#
# Why this exists
# ---------------
# Every homepage component in shiksha-frontend follows a
# "fetch from the CMS, fall back to a local DEFAULT_ITEMS constant" pattern.
# Nobody ever transcribed those constants into the database, so on a fresh
# deploy every content table is empty and the site silently renders the
# fallbacks. An empty CMS and a broken CMS look identical from the browser.
# This command closes that gap: after running it the page looks EXACTLY the
# same, but every word of it is now editable in Admin-dashboard.
#
# Safety model
# ------------
# * Dry run by default. Nothing is written without --yes.
# * CREATE-ONLY by default: a row that already exists is left completely
#   alone. This command can never clobber copy an admin has edited. Pass
#   --update to also refresh existing rows back to these defaults.
# * Idempotent: re-running creates nothing new, and can never produce a
#   duplicate even after an editor has renamed things. Singleton rows are
#   keyed on their stable unique column (section / section+slot); list rows
#   are keyed by SCOPE (a section+variant, an FAQ page, the showcase grid) —
#   once a scope has any rows it is skipped wholesale. Nothing is keyed on pk.
#
# Data lives in _homepage_seed_data.py, mirroring the existing convention in
# courses/management/commands/_catalog_seed_data.py.

from django.core.management.base import BaseCommand
from django.db import transaction

from content.models import (
    FAQItem, HomeContentBlock, HomeFloater, HomeListItem, ShowcaseCourse,
)

from ._homepage_seed_data import BLOCKS, FAQS, FLOATERS, LIST_ITEMS, SHOWCASE

# Two shapes of model here, and they need different identity rules.
#
# SINGLETON models have a genuinely stable natural key that an editor cannot
# change from the CMS (HomeContentBlock.section is unique; HomeFloater is
# unique on section+slot). Matching row-by-row on that key is always correct.
#
# LIST models have no stable identity at all — the only candidate key is the
# title/question text, which is exactly the thing an editor is expected to
# edit. Matching row-by-row there is a trap: rename "School Education" in the
# CMS and the next run no longer finds it, so it CREATES the original again
# and you silently get a duplicate card on the homepage. (Observed, not
# hypothetical.) So for list models the unit of idempotency is the SCOPE, not
# the row: if a scope already holds any rows, that scope has been bootstrapped
# and we leave the whole thing alone.
#
# (label, model, rows, kind, key_or_scope_fields)
GROUPS = [
    ("content blocks", HomeContentBlock, BLOCKS, "singleton", ("section",)),
    ("floaters", HomeFloater, FLOATERS, "singleton", ("section", "slot")),
    ("list items", HomeListItem, LIST_ITEMS, "list", ("section", "variant")),
    ("FAQs", FAQItem, FAQS, "list", ("page",)),
    ("showcase cards", ShowcaseCourse, SHOWCASE, "list", ()),
]

GROUP_ALIASES = {
    "blocks": "content blocks", "items": "list items", "floaters": "floaters",
    "faqs": "FAQs", "showcase": "showcase cards",
}


class Command(BaseCommand):
    help = (
        "Transcribe the hardcoded frontend homepage defaults into CMS rows. "
        "Dry-run by default; pass --yes. Create-only unless --update."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this flag, only reports what would happen.",
        )
        parser.add_argument(
            "--update", action="store_true",
            help="Also reset EXISTING rows back to these defaults. Off by "
                 "default so admin edits are never clobbered.",
        )
        parser.add_argument(
            "--only", default="",
            help="Comma-separated subset: blocks,items,floaters,faqs,showcase.",
        )

    def handle(self, *args, **options):
        dry_run = not options["yes"]
        do_update = options["update"]

        wanted = None
        if options["only"]:
            keys = [k.strip().lower() for k in options["only"].split(",") if k.strip()]
            unknown = [k for k in keys if k not in GROUP_ALIASES]
            if unknown:
                self.stderr.write(self.style.ERROR(
                    f"Unknown --only value(s): {', '.join(unknown)}. "
                    f"Valid: {', '.join(GROUP_ALIASES)}"
                ))
                return
            wanted = {GROUP_ALIASES[k] for k in keys}

        mode = "DRY RUN — nothing will be written" if dry_run else "WRITE MODE"
        policy = "create + UPDATE existing" if do_update else "create only (existing rows untouched)"
        self.stdout.write(self.style.WARNING(
            f"=== seed_homepage_defaults: {mode} | {policy} ==="
        ))

        totals = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}

        for label, model, rows, kind, fields in GROUPS:
            if wanted is not None and label not in wanted:
                continue
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(f"{label} ({len(rows)} defined)"))
            if kind == "singleton":
                counts = self._seed_singletons(model, rows, fields, dry_run, do_update)
            else:
                counts = self._seed_lists(model, rows, fields, dry_run, do_update)
            for k, v in counts.items():
                totals[k] += v

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            "TOTAL  created={created}  updated={updated}  "
            "unchanged={unchanged}  skipped={skipped}".format(**totals)
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --yes to write."))

    # ── helpers ──────────────────────────────────────────────────────

    def _seed_lists(self, model, rows, scope_fields, dry_run, do_update):
        """List models: the unit of idempotency is the scope, not the row.
        A scope that already holds rows is considered already-bootstrapped and
        is left entirely alone — see the GROUPS comment for why row-level
        matching creates duplicates here."""
        counts = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}

        scopes = {}
        for row in rows:
            scopes.setdefault(tuple(row[f] for f in scope_fields), []).append(row)

        for scope_values, scope_rows in scopes.items():
            lookup = dict(zip(scope_fields, scope_values))
            name = " / ".join(str(v) for v in scope_values) or "(all)"
            existing = model.objects.filter(**lookup).count()

            if existing:
                counts["skipped"] += len(scope_rows)
                self.stdout.write(self.style.HTTP_NOT_MODIFIED(
                    f"  SKIP SCOPE  {name}  — already has {existing} row(s); "
                    f"leaving all {len(scope_rows)} default(s) unseeded"
                ))
                continue

            for row in scope_rows:
                row = self._normalise(model, row)
                desc = row.get("title") or row.get("question") or row.get("stat_text") or name
                counts["created"] += 1
                self.stdout.write(f"  CREATE  {desc}")
                if not dry_run:
                    with transaction.atomic():
                        obj = model(**row)
                        obj.full_clean(exclude=self._clean_exclude(model))
                        obj.save()

        return counts

    def _seed_singletons(self, model, rows, key_fields, dry_run, do_update):
        counts = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}

        for row in rows:
            # A natural key uses only the key fields actually present and
            # non-empty on this row — HomeListItem rows are identified by
            # title, except stat chips which have no title, only stat_text.
            lookup = {
                f: row[f] for f in key_fields
                if f in row and row[f] not in ("", None)
            }
            if not lookup:
                counts["skipped"] += 1
                self.stdout.write(self.style.ERROR(
                    f"  SKIP    row has no usable natural key: {row!r:.80}"
                ))
                continue

            row = self._normalise(model, row)
            desc = " / ".join(f"{v}" for v in lookup.values())
            existing = model.objects.filter(**lookup).first()

            if existing is None:
                counts["created"] += 1
                self.stdout.write(f"  CREATE  {desc}")
                if not dry_run:
                    with transaction.atomic():
                        obj = model(**row)
                        obj.full_clean(exclude=self._clean_exclude(model))
                        obj.save()
                continue

            if not do_update:
                counts["unchanged"] += 1
                self.stdout.write(self.style.HTTP_NOT_MODIFIED(
                    f"  EXISTS  {desc}  (left as-is; pass --update to reset)"
                ))
                continue

            changed = [k for k, v in row.items() if getattr(existing, k) != v]
            if not changed:
                counts["unchanged"] += 1
                self.stdout.write(f"  SAME    {desc}")
                continue

            counts["updated"] += 1
            self.stdout.write(self.style.WARNING(
                f"  UPDATE  {desc}  (changed: {', '.join(changed)})"
            ))
            if not dry_run:
                with transaction.atomic():
                    for k, v in row.items():
                        setattr(existing, k, v)
                    existing.full_clean(exclude=self._clean_exclude(model))
                    existing.save()

        return counts

    @staticmethod
    def _normalise(model, row):
        """The React fallbacks use `null` for "no value" on fields that are
        NOT NULL text columns here (e.g. ShowcaseCourse.ribbon). Coerce None
        to "" for any non-nullable field so a transcription slip surfaces as
        a blank, not an IntegrityError mid-run."""
        fields = {f.name: f for f in model._meta.get_fields() if hasattr(f, "null")}
        return {
            k: ("" if v is None and k in fields and not fields[k].null else v)
            for k, v in row.items()
        }

    @staticmethod
    def _clean_exclude(model):
        """ShowcaseCourse.categories lacks blank=True, so full_clean() rejects
        the empty list that explore/board cards legitimately carry. Same
        workaround the admin serializer already applies
        (content/admin_serializers.py:212-221)."""
        return ["categories"] if model is ShowcaseCourse else None
