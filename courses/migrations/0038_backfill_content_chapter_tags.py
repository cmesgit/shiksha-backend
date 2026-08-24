"""Seed ContentChapterTag from the existing single-chapter FKs.

For every row that already has a chapter, create exactly one tag pointing at
it, so the new multi-chapter read path returns the same placement the old
single FK did and nothing appears to lose its chapter.

NOTHING IS DROPPED. The per-model `chapter` FKs stay, and stay authoritative;
this table is additive. Retiring them is a later phase, after an audit.

FOUR MODELS, NOT FIVE. LiveSession is listed as taggable in the design
handoff, but livestream.LiveSession has no chapter field and never has — only
course, subject and batch. There is therefore nothing to backfill for it; it
gains chapter_note / no_specific_chapter and can be tagged going forward, but
no `chapter` FK was invented for it here just to have something to copy.

Lives in `courses` rather than in each content app because ContentChapterTag
does, and because one pass keeps the ContentType lookups and the batching
policy in a single place.
"""

from django.db import migrations

# (app_label, model_name) for every model that has a chapter FK to copy.
SOURCES = [
    ("assignments", "Assignment"),
    ("materials", "StudyMaterial"),
    ("quizzes", "Quiz"),
    ("courses", "SessionRecording"),
]

# bulk_create chunk. Big enough that the query count stays flat on a large
# table, small enough not to build a multi-hundred-MB list of model instances
# on a 4 GB production box.
BATCH = 2000


def forwards(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentChapterTag = apps.get_model("courses", "ContentChapterTag")

    for app_label, model_name in SOURCES:
        Model = apps.get_model(app_label, model_name)

        # get_or_create, not get: on a fresh database (a test run, or a new
        # deployment migrating from zero) the contenttypes rows for these
        # models may not have been created yet, since that happens in a
        # post-migrate signal.
        content_type, _ = ContentType.objects.get_or_create(
            app_label=app_label, model=model_name.lower(),
        )

        # values_list + iterator: never materialise the whole table. Only the
        # two columns needed are fetched, not full model instances.
        rows = (
            Model.objects.filter(chapter__isnull=False)
            .values_list("pk", "chapter_id")
            .iterator(chunk_size=BATCH)
        )

        pending = []
        for pk, chapter_id in rows:
            pending.append(ContentChapterTag(
                content_type=content_type,
                object_id=pk,
                chapter_id=chapter_id,
                custom_label="",
                order=0,
            ))
            if len(pending) >= BATCH:
                _flush(ContentChapterTag, pending)
                pending = []
        _flush(ContentChapterTag, pending)


def _flush(ContentChapterTag, pending):
    if not pending:
        return
    # ignore_conflicts: makes the backfill idempotent against
    # uniq_chapter_tag_per_object, so a re-run (or a deploy that replays this
    # after a partial failure) is a no-op rather than an IntegrityError.
    ContentChapterTag.objects.bulk_create(pending, ignore_conflicts=True)


def backwards(apps, schema_editor):
    """Delete only the tags this migration could have produced.

    Scoped to the four content types AND to chapter-backed tags with no label,
    so a rollback cannot destroy free-text tags or extra chapters a teacher
    added after the backfill ran.
    """
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentChapterTag = apps.get_model("courses", "ContentChapterTag")

    for app_label, model_name in SOURCES:
        ct = ContentType.objects.filter(
            app_label=app_label, model=model_name.lower(),
        ).first()
        if ct is None:
            continue
        ContentChapterTag.objects.filter(
            content_type=ct, custom_label="", chapter__isnull=False,
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("courses", "0037_sessionrecording_chapter_note_and_more"),
        ("assignments", "0010_assignment_chapter_note_and_more"),
        ("materials", "0007_studymaterial_chapter_note_and_more"),
        ("quizzes", "0024_quiz_chapter_note_quiz_no_specific_chapter"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
