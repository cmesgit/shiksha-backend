# PLACEMENT: backend/backend/counseling/management/commands/import_career_guides.py  (NEW FILE)
# DEPLOY:    /app/shiksha-backend/counseling/management/commands/import_career_guides.py
#
# Converts the approved career-guidance .docx sources into CareerGuide /
# GuideChapter / GuideSection rows via counseling/guide_import.py.
#
# The source .docx live on the operator's machine, not in this repo — copy
# them to the server first (e.g. `scp` into a scratch directory) and point
# --source at that directory, or pass --manifest at a copy checked out
# anywhere. Always --dry-run first; nothing publishes without --publish.
#
#   python manage.py import_career_guides --dry-run
#   python manage.py import_career_guides --only secondary-school --dry-run
#   python manage.py import_career_guides --replace --publish
#   python manage.py import_career_guides --propose --source ~/docs

import hashlib
import json
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from content.models import PublishStatus
from counseling.guide_import import parse_docx, propose_structure
from counseling.guide_models import CareerGuide, GuideChapter, GuideSection
from counseling.models import Specialization

DEFAULT_MANIFEST = os.path.join(
    os.path.dirname(__file__), "..", "..", "fixtures", "career_guides", "manifest.json"
)


class Command(BaseCommand):
    help = "Import the career-guidance .docx library into CareerGuide/GuideChapter/GuideSection."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source", default=None,
            help="Directory containing the source .docx files (default: manifest's own directory).",
        )
        parser.add_argument("--manifest", default=None, help="Path to manifest.json.")
        parser.add_argument(
            "--only", action="append", default=None,
            help="Limit to this guide slug. Repeatable.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Parse and print a report; write nothing.",
        )
        parser.add_argument(
            "--replace", action="store_true",
            help="Delete and rebuild this guide's chapters/sections. The CareerGuide "
                 "row itself (status, publish_at, cover, view_count) is preserved.",
        )
        parser.add_argument(
            "--publish", action="store_true",
            help="Set status=published on guides created or replaced this run. "
                 "Without this flag, new guides land as DRAFT.",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Re-import even when source_sha256 is unchanged since the last import.",
        )
        parser.add_argument(
            "--propose", action="store_true",
            help="Print a first-draft chapter_titles list for --only's document(s) "
                 "(or all, if --only omitted) and exit. Writes nothing. An authoring "
                 "aid for updating manifest.json — never trusted by a real import.",
        )

    def handle(self, *args, **opts):
        manifest_path = opts["manifest"] or os.path.abspath(DEFAULT_MANIFEST)
        if not os.path.exists(manifest_path):
            raise CommandError(f"Manifest not found: {manifest_path}")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)

        source_dir = opts["source"] or os.path.dirname(manifest_path)
        only = set(opts["only"] or [])

        entries = [
            (fname, spec) for fname, spec in manifest.items()
            if not fname.startswith("_") and not spec.get("skip")
        ]

        if opts["propose"]:
            self._run_propose(entries, source_dir, only)
            return

        dry = opts["dry_run"]
        replace = opts["replace"]
        publish = opts["publish"]
        force = opts["force"]

        for fname, spec in entries:
            if only and spec["slug"] not in only:
                continue
            path = os.path.join(source_dir, fname)
            if not os.path.exists(path):
                self.stderr.write(self.style.WARNING(f"skip {spec['slug']}: {path} not found"))
                continue

            with open(path, "rb") as fh:
                raw = fh.read()
            sha = hashlib.sha256(raw).hexdigest()

            existing = CareerGuide.objects.filter(slug=spec["slug"]).first()
            if existing and existing.source_sha256 == sha and not force:
                self.stdout.write(f"skip {spec['slug']}: unchanged (source_sha256 matches, use --force)")
                continue

            import io
            result = parse_docx(io.BytesIO(raw), spec)
            self._report(spec["slug"], fname, result)

            if dry:
                continue
            if existing and not replace:
                self.stderr.write(self.style.WARNING(
                    f"skip write for {spec['slug']}: guide already exists — pass --replace to rebuild it"
                ))
                continue

            self._write(spec, result, sha, publish=publish)

    # ------------------------------------------------------------------

    def _run_propose(self, entries, source_dir, only):
        import io
        for fname, spec in entries:
            if only and spec["slug"] not in only:
                continue
            path = os.path.join(source_dir, fname)
            if not os.path.exists(path):
                self.stderr.write(self.style.WARNING(f"skip {fname}: not found at {path}"))
                continue
            with open(path, "rb") as fh:
                proposal = propose_structure(io.BytesIO(fh.read()))
            self.stdout.write(f"\n{'=' * 70}\n{fname}  ({spec['slug']})")
            self.stdout.write(
                f"  heading levels present: {proposal['heading_levels_present']}  "
                f"headings: {proposal['heading_count']}  "
                f"proposed levels: {proposal['chapter_levels']}"
            )
            self.stdout.write("  Proposed chapter_titles (VERIFY before pasting into manifest.json):")
            for title in proposal["chapter_titles"]:
                self.stdout.write(f'    "{title}",')

    def _report(self, slug, fname, result):
        s = result["stats"]
        mismatch = "" if s["chapters_declared"] == s["chapters_matched"] else "  *** CHAPTER MISMATCH ***"
        self.stdout.write(
            f"\n{slug:20} ({fname})\n"
            f"  chapters {s['chapters_matched']}/{s['chapters_declared']}{mismatch}   "
            f"sections={s['sections']}  blocks={s['blocks']}  chars={s['chars']}  "
            f"glance_rows={len(result['glance'])}  toc_dropped={s['toc_dropped']}\n"
            f"  block types: {s['block_types']}"
        )
        if mismatch:
            got = {c["title"] for c in result["chapters"]}
            self.stderr.write(self.style.ERROR(
                f"  Declared chapter(s) not found in the document — check manifest.json's "
                f"chapter_titles for {slug!r} against the actual heading text."
            ))

    @transaction.atomic
    def _write(self, spec, result, sha, *, publish):
        guide, created = CareerGuide.objects.get_or_create(
            slug=spec["slug"],
            defaults={
                "status": PublishStatus.DRAFT,
            },
        )
        guide.title = result["title"] or spec["title"]
        guide.blurb = spec.get("blurb", "")
        guide.audience = spec["audience"]
        guide.stage = spec["stage"]
        guide.stage_label = spec["stage_label"]
        guide.stage_order = spec.get("stage_order", 0)
        guide.accent = spec.get("accent", "teal")
        guide.legacy_slugs = spec.get("legacy_slugs", [])
        guide.glance = result["glance"]
        guide.class_levels = spec.get("class_levels", [])
        guide.order = spec.get("order", 0)
        guide.reading_minutes = max(1, round(result["stats"]["chars"] / 1000))
        guide.source_filename = spec.get("_source_filename", "")
        guide.source_sha256 = sha
        guide.import_version += 1
        guide.imported_at = timezone.now()
        if publish:
            guide.status = PublishStatus.PUBLISHED
        guide.save()

        if spec.get("specializations"):
            specs = Specialization.objects.filter(name__in=spec["specializations"])
            guide.specializations.set(specs)
            missing = set(spec["specializations"]) - set(specs.values_list("name", flat=True))
            if missing:
                self.stderr.write(self.style.WARNING(
                    f"  {guide.slug}: specialization(s) not found, skipped: {sorted(missing)}"
                ))

        # Rebuild chapters/sections. The guide row itself (status,
        # publish_at, cover, view_count) is untouched above.
        guide.sections.all().delete()
        guide.chapters.all().delete()

        chapter_rows = []
        for ch in result["chapters"]:
            chapter_rows.append(GuideChapter.objects.create(
                guide=guide,
                number=ch["number"],
                title=ch["title"],
                kind=ch.get("kind", "content"),
                order=ch["number"],
            ))

        for order, sec in enumerate(result["sections"]):
            chapter = chapter_rows[sec["chapter_index"]] if sec["chapter_index"] is not None else None
            GuideSection.objects.create(
                guide=guide,
                chapter=chapter,
                order=order,
                level=sec["level"],
                title=sec["title"],
                kind=sec["kind"],
                audience=sec["audience"],
                blocks=sec["blocks"],
            )

        verb = "created" if created else "replaced"
        self.stdout.write(self.style.SUCCESS(
            f"  {verb} {guide.slug}: {len(chapter_rows)} chapters, {len(result['sections'])} sections "
            f"({'published' if guide.status == PublishStatus.PUBLISHED else 'draft'})"
        ))
