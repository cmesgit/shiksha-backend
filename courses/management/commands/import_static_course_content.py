# PLACEMENT: backend/courses/management/commands/import_static_course_content.py
#
# One-shot migration of the frontend's hardcoded src/data/courseData.js /
# mbseCourseData (18 board x class combinations) into real Course / Subject /
# Chapter rows, so the public Courses.jsx cutover (Phase E) has real data to
# read instead of the static file it's retiring.
#
# The JS file itself isn't parsed here (it's not JSON) — it was converted
# once via Node and is checked in at courses/fixtures/static_course_data.json:
#     cd shiksha-frontend && node --input-type=module -e "
#       import { courseData, mbseCourseData } from './src/data/courseData.js';
#       import { writeFileSync } from 'fs';
#       writeFileSync('../shiksha-backend/courses/fixtures/static_course_data.json',
#         JSON.stringify({ cbse: courseData, mbse: mbseCourseData }, null, 2));
#     "
# Re-run that conversion (and re-check-in the JSON) only if courseData.js
# itself changes before this command is retired post-cutover.
#
# Of the 18 (class x board) combinations, 12 already have a real Course UUID
# (hardcoded in shiksha-frontend's Courses.jsx `CLASSES` array today, and
# already actively enrolled-in in production) — those are matched by PK
# directly, never by title/board, and only ever get Subject/Chapter rows
# backfilled. The other 6 (Class 11/12 Science/Commerce/Arts under CBSE) have
# no existing course at all; those are created fresh as DRAFT (never
# auto-published) with the same fixed UUID baked in below, so re-running this
# command against any environment is idempotent and deterministic.
#
# Usage:
#     python manage.py import_static_course_content            # dry run (default)
#     python manage.py import_static_course_content --yes       # actually write
#
# Idempotent: Subject matched on (course, name), Chapter on (subject, title) —
# safe to re-run. Never touches Batch/Enrollment/Subscription.

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from courses.models import Board, Chapter, Course, Stream, Subject

DEFAULT_FIXTURE = Path(__file__).resolve().parent.parent.parent / "fixtures" / "static_course_data.json"

# (class_key, board_key) -> known-good Course UUID, exactly as hardcoded in
# shiksha-frontend/src/components/Courses.jsx's `CLASSES` array today.
KNOWN_COURSE_IDS = {
    ("class8", "cbse"): "3b54e0cf-9e17-4652-b5de-110735c1ed8e",
    ("class8", "mbse"): "2b24c4a0-787e-4a0d-acf3-29e9f4e921cf",
    ("class9", "cbse"): "26b5b4ce-5b0a-4381-a492-c134676881f2",
    ("class9", "mbse"): "9fac2eae-5a90-411e-994d-d2613923cddf",
    ("class10", "cbse"): "41ec43ac-bac7-4a68-b5d6-eda2acd85585",
    ("class10", "mbse"): "cfe07ab8-1508-4c14-8181-8ba21d4cb331",
    ("class11science", "mbse"): "eb7700aa-a95b-4eeb-a4e4-cdffe9c27a73",
    ("class11commerce", "mbse"): "51724c07-b13a-4413-85d8-d7cf2561fabb",
    ("class11arts", "mbse"): "24056c0b-1d46-411a-912d-5fecd2b8d90f",
    ("class12science", "mbse"): "6493ae70-6f47-48e9-b3a7-cb345432cf0d",
    ("class12commerce", "mbse"): "933a79ca-b5ed-4df4-926d-2d241c3efde9",
    ("class12arts", "mbse"): "e0ccb831-57d4-49a1-818f-cc6d234db5af",
}

# Fixed UUIDs for the 6 combinations with no existing course, so re-running
# this command (in any environment) always creates/matches the same rows
# rather than duplicating them on a second run.
NEW_COURSE_IDS = {
    ("class11science", "cbse"): "b6b1e3b0-6b34-4b1a-8b1a-000000000001",
    ("class11commerce", "cbse"): "b6b1e3b0-6b34-4b1a-8b1a-000000000002",
    ("class11arts", "cbse"): "b6b1e3b0-6b34-4b1a-8b1a-000000000003",
    ("class12science", "cbse"): "b6b1e3b0-6b34-4b1a-8b1a-000000000004",
    ("class12commerce", "cbse"): "b6b1e3b0-6b34-4b1a-8b1a-000000000005",
    ("class12arts", "cbse"): "b6b1e3b0-6b34-4b1a-8b1a-000000000006",
}

CLASS_LEVELS = {
    "class8": 8, "class9": 9, "class10": 10,
    "class11science": 11, "class11commerce": 11, "class11arts": 11,
    "class12science": 12, "class12commerce": 12, "class12arts": 12,
}

STREAMS = {
    "class11science": "SCIENCE", "class11commerce": "COMMERCE", "class11arts": "ARTS",
    "class12science": "SCIENCE", "class12commerce": "COMMERCE", "class12arts": "ARTS",
}

BOARD_NAMES = {"cbse": "CBSE", "mbse": "MBSE"}

TOPIC_PREFIX_RX = re.compile(r"^Topic\s+\S+:\s*")


def parse_price_to_paise(price_str):
    digits = re.sub(r"[^\d]", "", price_str or "")
    return int(digits) * 100 if digits else 0


class Command(BaseCommand):
    help = (
        "Import the static courseData.js/mbseCourseData content (subjects + "
        "chapters, and the 6 CBSE-stream courses that don't exist yet) into "
        "real DB rows. Dry-run by default; pass --yes to write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture", default=str(DEFAULT_FIXTURE),
            help="Path to the JSON export of courseData.js (see module docstring).",
        )
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this flag, only reports what would happen.",
        )

    def handle(self, *args, **options):
        fixture_path = Path(options["fixture"])
        if not fixture_path.is_file():
            raise CommandError(f"Fixture not found: {fixture_path}")
        data = json.loads(fixture_path.read_text())

        dry_run = not options["yes"]
        mode = "DRY RUN — nothing will be written" if dry_run else "WRITE MODE"
        self.stdout.write(self.style.WARNING(f"=== {mode} ==="))

        boards = {}
        for board_key, board_name in BOARD_NAMES.items():
            try:
                boards[board_key] = Board.objects.get(name__iexact=board_name)
            except Board.DoesNotExist:
                raise CommandError(
                    f"Board '{board_name}' not found — this command backfills content "
                    f"for existing boards only, it doesn't create boards."
                )

        streams = {}
        for stream_key in set(STREAMS.values()):
            streams[stream_key], _ = (
                (Stream.objects.get(name=stream_key), False)
                if Stream.objects.filter(name=stream_key).exists()
                else Stream.objects.get_or_create(name=stream_key)
            )

        totals = {"courses_matched": 0, "courses_created": 0, "courses_published": 0,
                   "subjects_created": 0, "chapters_created": 0, "errors": 0}

        for board_key in ("cbse", "mbse"):
            for class_key, class_data in data[board_key].items():
                key = (class_key, board_key)
                try:
                    with transaction.atomic():
                        self._import_one(
                            key, class_data, board_key, boards, streams,
                            dry_run, totals,
                        )
                except IntegrityError as exc:
                    totals["errors"] += 1
                    self.stdout.write(self.style.ERROR(f"[{class_key}/{board_key}] IntegrityError: {exc}"))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Courses matched (existing UUID): {totals['courses_matched']}\n"
            f"Courses created (new, DRAFT):    {totals['courses_created']}\n"
            f"Courses published (was DRAFT):   {totals['courses_published']}\n"
            f"Subjects created:                {totals['subjects_created']}\n"
            f"Chapters created:                {totals['chapters_created']}\n"
            f"Errors:                          {totals['errors']}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --yes to write."))

    def _import_one(self, key, class_data, board_key, boards, streams, dry_run, totals):
        class_key, _ = key
        board = boards[board_key]
        known_id = KNOWN_COURSE_IDS.get(key)
        fixed_new_id = NEW_COURSE_IDS.get(key)

        # `known_id` combos are the 12 already-live-in-production courses;
        # `fixed_new_id` combos are the 6 with no existing course. Checking
        # both (not just known_id) here is what makes re-running this command
        # idempotent for the "new" 6 too, once they exist from a prior run.
        course = None
        lookup_id = known_id or fixed_new_id
        if lookup_id:
            course = Course.objects.filter(id=lookup_id).first()

        created = False
        if course is not None:
            totals["courses_matched"] += 1
            if known_id and course.status == Course.STATUS_DRAFT:
                # Only the 12 already-live combinations get auto-published.
                # These are already actively enrolled-in in production
                # (they've had a real UUID hardcoded in the frontend since
                # before Course.status was ever exposed to any UI) — leaving
                # them DRAFT after Phase C's public-API status filtering
                # ships would 404 the live enroll page for every existing
                # student. Never downgrades an explicit ARCHIVED. The 6 brand
                # new combinations are deliberately left for an admin to
                # publish after review, even on a second run.
                self.stdout.write(f"[{class_key}/{board_key}] publishing existing course {course.id} (was DRAFT)")
                if not dry_run:
                    course.status = Course.STATUS_PUBLISHED
                    course.save(update_fields=["status"])
                totals["courses_published"] += 1
        else:
            # Either a known_id that no longer resolves (unexpected — report
            # loudly rather than silently creating a duplicate under a new
            # id), or one of the 6 combinations with no course at all yet.
            course_id = known_id or fixed_new_id
            if known_id and course is None:
                self.stdout.write(self.style.WARNING(
                    f"[{class_key}/{board_key}] known UUID {known_id} not found in this DB — "
                    f"creating it fresh with that same id."
                ))
            self.stdout.write(f"[{class_key}/{board_key}] creating course {course_id} (status=DRAFT)")
            totals["courses_created"] += 1
            created = True
            if not dry_run:
                course = Course.objects.create(
                    id=course_id,
                    title=class_data["title"],
                    description=class_data.get("desc", ""),
                    price=parse_price_to_paise(class_data.get("price", "")),
                    class_level=CLASS_LEVELS.get(class_key),
                    board=board,
                    stream=streams.get(STREAMS[class_key]) if class_key in STREAMS else None,
                    status=Course.STATUS_DRAFT,
                )
            else:
                # Dry run: nothing to backfill subjects/chapters onto since
                # the course doesn't exist — just report the topic count.
                self.stdout.write(
                    f"    would create {len(class_data.get('topics', []))} subjects"
                )
                return

        if course is None:
            return  # dry-run "matched" branch with no real row to inspect further

        if not created and course.subjects.exists():
            # A matched (already-existing) course that already has real
            # subjects was populated independently through the admin UI —
            # its subject names won't match this fixture's naming at all, so
            # a name-matched get_or_create would just create duplicates
            # alongside the real, already-curated content. Skip entirely;
            # only genuinely empty courses (typically the brand-new ones)
            # get the static fixture's subjects/chapters backfilled.
            self.stdout.write(
                f"[{class_key}/{board_key}] course already has "
                f"{course.subjects.count()} real subject(s) — skipping static "
                f"content backfill (assumed independently curated)."
            )
            return

        for order, topic in enumerate(class_data.get("topics", [])):
            name = TOPIC_PREFIX_RX.sub("", topic["title"]).strip()
            textbook = topic.get("textbook", "")

            if dry_run:
                exists = Subject.objects.filter(course=course, name=name).exists()
                if not exists:
                    totals["subjects_created"] += 1
                    self.stdout.write(f"    would create subject: {name}")
                for chapter_title in topic.get("chapters", []):
                    if not Chapter.objects.filter(
                        subject__course=course, subject__name=name, title=chapter_title,
                    ).exists():
                        totals["chapters_created"] += 1
                continue

            subject, subj_created = Subject.objects.get_or_create(
                course=course, name=name,
                defaults={"order": order, "textbook": textbook},
            )
            if subj_created:
                totals["subjects_created"] += 1
            elif not subject.textbook and textbook:
                # Backfill textbook onto an already-existing subject that
                # predates the field, without touching anything else about it.
                subject.textbook = textbook
                subject.save(update_fields=["textbook"])

            for chapter_order, chapter_title in enumerate(topic.get("chapters", [])):
                _, ch_created = Chapter.objects.get_or_create(
                    subject=subject, title=chapter_title,
                    defaults={"order": chapter_order},
                )
                if ch_created:
                    totals["chapters_created"] += 1
