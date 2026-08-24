"""Seed the new Quiz.batches M2M from the legacy single-batch FK.

For every quiz with a non-NULL `batch_id`, add that batch to `batches`.

Quizzes with `batch IS NULL` deliberately get an EMPTY `batches` set. NULL
currently means "visible to every batch of the course" (see Quiz.batch's model
comment and materials/views.py's StudentSubjectMaterials, which uses the same
rule), so empty-means-everyone is what preserves it. Writing every batch of
the course into the M2M instead would look equivalent today and diverge the
moment a new batch is created — the new batch would silently NOT see a quiz
that was course-wide.

Reverse is a no-op: clearing the M2M cannot distinguish rows this migration
created from batches a teacher assigned afterwards, and 0019's reverse drops
the join table wholesale anyway.
"""

from django.db import migrations


def backfill_batches(apps, schema_editor):
    Quiz = apps.get_model("quizzes", "Quiz")
    Through = Quiz.batches.through

    rows = [
        Through(quiz_id=quiz_id, batch_id=batch_id)
        for quiz_id, batch_id in Quiz.objects.filter(batch__isnull=False)
        .values_list("id", "batch_id")
        .iterator()
    ]
    # bulk_create on the through model rather than .batches.add() per quiz:
    # one statement instead of one per row, and the historical model's
    # related manager is the only thing available here anyway.
    Through.objects.bulk_create(rows, ignore_conflicts=True, batch_size=1000)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("quizzes", "0020_backfill_quiz_is_assigned"),
    ]

    operations = [
        migrations.RunPython(backfill_batches, noop),
    ]
