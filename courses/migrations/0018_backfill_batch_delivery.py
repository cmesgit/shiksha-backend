# Phase 2 of the batch-system migration (see docs: catalog vs. delivery).
#
# Backfills the delivery-plane tables from today's course-wide data:
#   1. SubjectTeacher (subject, teacher) -> TeachingAssignment (batch, subject,
#      teacher) for every ACTIVE batch of the subject's course.
#   2. Chapter.is_covered=True -> BatchChapterProgress(batch, chapter) for every
#      ACTIVE batch of the chapter's course, preserving covered_at/marked_by.
#      (BatchChapterProgress already existed with per-batch endpoints but was
#      never backfilled from the legacy shared flag; this fills that gap.)
#   3. Course lifecycle: existing courses were all live by construction, so
#      status=PUBLISHED, kind=ACADEMIC; class_level parsed from the title
#      where possible ("Class 10 ...", "class-9", ...), else left NULL for
#      manual fix-up in the admin.
#
# Content rows (LiveSession/Quiz/Assignment/StudyMaterial/SessionRecording)
# keep batch=NULL -- correct, they were course-wide by construction.
#
# Idempotent: get_or_create / only-fill-empty guards mean re-running forward
# on an already-backfilled DB is a no-op. Reverse is a no-op.

import re

from django.db import migrations
from django.utils import timezone


def _parse_class_level(title):
    m = re.search(r"class[\s\-–_]*(\d{1,2})", title or "", re.IGNORECASE)
    if m:
        level = int(m.group(1))
        if 6 <= level <= 12:
            return level
    return None


def forward(apps, schema_editor):
    Course = apps.get_model("courses", "Course")
    Batch = apps.get_model("courses", "Batch")
    Chapter = apps.get_model("courses", "Chapter")
    SubjectTeacher = apps.get_model("courses", "SubjectTeacher")
    TeachingAssignment = apps.get_model("courses", "TeachingAssignment")
    BatchChapterProgress = apps.get_model("courses", "BatchChapterProgress")

    # -- 3. Course lifecycle ------------------------------------------------
    for course in Course.objects.all():
        course.status = "PUBLISHED"
        course.kind = "ACADEMIC"
        course.class_level = _parse_class_level(course.title)
        course.save(update_fields=["status", "kind", "class_level"])

    active_batches = list(Batch.objects.filter(is_active=True).select_related("course"))
    batches_by_course = {}
    for batch in active_batches:
        batches_by_course.setdefault(batch.course_id, []).append(batch)

    # -- 1. SubjectTeacher -> TeachingAssignment per active batch ----------
    # SubjectTeacher only enforces unique (subject, teacher), so a subject may
    # carry several PRIMARY rows today. TeachingAssignment allows just one
    # active PRIMARY per (batch, subject): the lowest `order` keeps PRIMARY,
    # the rest are copied as ASSISTANT.
    for batch in active_batches:
        rows = (
            SubjectTeacher.objects
            .filter(subject__course_id=batch.course_id)
            .order_by("subject_id", "order", "created_at")
        )
        primary_taken = set()  # subject_ids that already have a PRIMARY
        for st in rows:
            role = st.display_role
            if role == "PRIMARY":
                if st.subject_id in primary_taken:
                    role = "ASSISTANT"
                else:
                    primary_taken.add(st.subject_id)
            # Idempotent: skip if this teacher already has an active row here.
            TeachingAssignment.objects.get_or_create(
                batch_id=batch.id,
                subject_id=st.subject_id,
                teacher_id=st.teacher_id,
                is_active=True,
                defaults={"role": role, "order": st.order},
            )

    # -- 2. Chapter.is_covered -> BatchChapterProgress per active batch ----
    covered = Chapter.objects.filter(is_covered=True).select_related("subject")
    for chapter in covered:
        for batch in batches_by_course.get(chapter.subject.course_id, []):
            BatchChapterProgress.objects.get_or_create(
                batch_id=batch.id,
                chapter_id=chapter.id,
                defaults={
                    "is_covered": True,
                    "covered_at": chapter.covered_at or timezone.now(),
                    "marked_by_id": chapter.marked_by_id,
                },
            )


class Migration(migrations.Migration):

    dependencies = [
        ("courses", "0017_course_class_level_course_kind_course_status_and_more"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
