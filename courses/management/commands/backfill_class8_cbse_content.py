# One-off backfill for a single known-bad course: CBSE Class 8
# (id 3b54e0cf-9e17-4652-b5de-110735c1ed8e, slug class-8-2), which is
# PUBLISHED and sold at ₹1,500/month with 7 subjects and 0 chapters.
#
# Why this exists instead of just re-running import_static_course_content:
# that command matches subjects by exact name, and this course's 7 existing
# subjects were created independently (admin UI) with slightly different
# names than the fixture — "3A: Social Science - History (Our Pasts III)"
# vs the fixture's "Topic 3A: Social Science — History (Our Pasts III)"
# (prefix wording + dash character both differ). A straight name match finds
# nothing, and import_static_course_content's own duplicate guard (any
# already-existing subject on the course skips the whole course) means it
# would do nothing at all here — which is safe, but leaves this empty.
#
# This command instead fuzzy-matches each fixture topic to an existing
# subject (strip any "Topic N:"/"NA:" prefix, normalise em/en dash to a
# plain hyphen, casefold) and backfills chapters into the MATCHED subject —
# never renames or deletes it. Subject "3C: ... Civics ..." has 2 live
# SessionRecording rows attached; renaming or recreating it would orphan
# them, so this command only ever adds Chapter rows under it.
# "Mathematics" has no existing subject at all and is created fresh.
#
# Idempotent (Chapter matched on (subject, title)); dry-run by default.
#
# Usage:
#   python manage.py backfill_class8_cbse_content            # dry run
#   python manage.py backfill_class8_cbse_content --yes      # write

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from courses.models import Chapter, Course, Subject

COURSE_ID = "3b54e0cf-9e17-4652-b5de-110735c1ed8e"
FIXTURE = Path(__file__).resolve().parent.parent.parent / "fixtures" / "static_course_data.json"

TOPIC_PREFIX_RX = re.compile(r"^(?:Topic\s+\S+|\d[A-Za-z]?)\s*:\s*")
DASH_RX = re.compile(r"[‒–—―]")  # figure/en/em/horizontal-bar dash


def normalize(name):
    name = TOPIC_PREFIX_RX.sub("", name)
    name = DASH_RX.sub("-", name)
    return re.sub(r"\s+", " ", name).strip().casefold()


class Command(BaseCommand):
    help = "Backfill chapters for CBSE Class 8 (class-8-2) from the static fixture."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Actually write.")

    def handle(self, *args, **options):
        dry_run = not options["yes"]
        with transaction.atomic():
            self._run(dry_run)
            if dry_run:
                transaction.set_rollback(True)

    def _run(self, dry_run):
        try:
            course = Course.objects.get(id=COURSE_ID)
        except Course.DoesNotExist:
            raise CommandError(f"Course {COURSE_ID} not found in this DB.")

        data = json.loads(FIXTURE.read_text())
        topics = data["cbse"]["class8"]["topics"]

        existing_subjects = list(course.subjects.all())
        by_norm = {normalize(s.name): s for s in existing_subjects}
        max_order = max((s.order for s in existing_subjects), default=-1)

        mode = "DRY RUN — nothing will be written" if dry_run else "WRITE MODE"
        self.stdout.write(self.style.WARNING(mode))
        self.stdout.write(f"Course: {course.title!r} ({course.id})")

        subjects_created = 0
        chapters_created = 0

        for topic in topics:
            fixture_title = TOPIC_PREFIX_RX.sub("", topic["title"]).strip()
            key = normalize(topic["title"])
            subject = by_norm.get(key)

            if subject:
                self.stdout.write(f"  MATCH  {topic['title']!r} -> subject {subject.id} {subject.name!r}")
            else:
                self.stdout.write(f"  NEW    {topic['title']!r} -> would create subject {fixture_title!r}")
                if not dry_run:
                    max_order += 1
                    subject = Subject.objects.create(
                        course=course, name=fixture_title,
                        order=max_order, textbook=topic.get("textbook", ""),
                    )
                    subjects_created += 1
                else:
                    subjects_created += 1

            for chapter_order, chapter_title in enumerate(topic.get("chapters", [])):
                if dry_run:
                    exists = subject and Chapter.objects.filter(
                        subject=subject, title=chapter_title,
                    ).exists()
                    if not exists:
                        chapters_created += 1
                        self.stdout.write(f"      + {chapter_title}")
                    continue

                _, created = Chapter.objects.get_or_create(
                    subject=subject, title=chapter_title,
                    defaults={"order": chapter_order},
                )
                if created:
                    chapters_created += 1

        self.stdout.write("")
        self.stdout.write(
            f"Subjects created: {subjects_created} | Chapters created: {chapters_created}"
        )
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --yes to write."))
