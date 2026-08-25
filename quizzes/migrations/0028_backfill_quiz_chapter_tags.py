"""Seed ContentChapterTag from the legacy Quiz.chapter FK.

Phase 10 wants to drop `Quiz.chapter`, but it cannot be dropped while it is
still the ONLY place some quizzes record their chapter. Two write paths
(`QuizCreateSerializer` via `resolve_or_create_chapter`, and
`TeacherQuizDuplicateView`) set the FK without ever creating a tag row, so a
typical quiz today has a chapter and zero tags. Every read that moved to tags
would silently see "no chapter" for those — which is exactly how
`serialize_tags()` came back empty on the S3 results screen.

This makes the two representations agree, so the reads can be migrated one at
a time afterwards with the FK still in place as a cross-check. Additive only:
no existing tag is touched, and the FK is left exactly as it is.

WHY `order=0` AND `custom_label=""`
──────────────────────────────────
`chapter_tags.primary_chapter()` defines the FK as "the FIRST resolved
chapter", so a tag built back out of the FK is by definition the first one.
The label is left blank because the FK carries no free text — a tag with a
chapter and an empty custom_label reads as "this chapter, no override", which
is what `serialize_tags()` already renders via `t.label`.

SKIPS QUIZZES THAT ALREADY HAVE TAGS
────────────────────────────────────
A quiz tagged with several chapters has an FK pointing at the first of them.
Adding another row from that FK would duplicate a tag it already has, and
duplicate rows inflate any GROUP BY over them. Only untagged quizzes are
touched.

Reverse is a no-op. Once written, a tag created here is indistinguishable
from one a teacher created, and deleting "tags that happen to match the FK"
would delete real teacher input.
"""

from django.db import migrations


def backfill_chapter_tags(apps, schema_editor):
    Quiz = apps.get_model("quizzes", "Quiz")
    ContentChapterTag = apps.get_model("courses", "ContentChapterTag")
    ContentType = apps.get_model("contenttypes", "ContentType")

    quiz_ct, _ = ContentType.objects.get_or_create(
        app_label="quizzes", model="quiz")

    already_tagged = set(
        ContentChapterTag.objects
        .filter(content_type=quiz_ct)
        .values_list("object_id", flat=True)
    )

    rows = [
        ContentChapterTag(
            content_type=quiz_ct,
            object_id=quiz_id,
            chapter_id=chapter_id,
            custom_label="",
            order=0,
        )
        for quiz_id, chapter_id in (
            Quiz.objects
            .filter(chapter__isnull=False)
            .values_list("id", "chapter_id")
            .iterator()
        )
        if quiz_id not in already_tagged
    ]

    # batch_size so a large bank does not build one enormous INSERT.
    ContentChapterTag.objects.bulk_create(rows, batch_size=500)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("quizzes", "0027_practicesession_practiceanswer_and_more"),
        ("courses", "0038_backfill_content_chapter_tags"),
    ]

    operations = [
        migrations.RunPython(backfill_chapter_tags, noop),
    ]
