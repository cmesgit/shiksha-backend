"""Phase 10: last backfill of `batches` from the `batch` shim, before 0032.

Migration 0021 already did this once. It has to run again because
`QuizCreateSerializer` kept writing ONLY the shim afterwards, so every quiz
created between 0021 and today reintroduced the divergence — the same reopened
gap the chapter FK had. The serializer now populates both, so this is the last
time.

WHY IT MATTERS THAT THIS RUNS FIRST
───────────────────────────────────
`quizzes/visibility.py` treats an empty `batches` set as "every batch of the
course". A quiz with the shim set and the M2M empty therefore reads as
course-wide the instant the shim's fallback clause is removed — a batch-scoped
quiz silently widening to the whole course, i.e. one batch seeing another
batch's test. This migration is what makes "empty means course-wide"
unambiguously true before 0032 drops the column.

Measured before writing: 0 affected rows on production and 0 on dev, so this
is expected to be a no-op in both. It exists because "expected" is not
"guaranteed" — a row created between this being written and being deployed
would be exactly the one that leaks.

Reverse is a no-op: once written, an M2M row from the shim is indistinguishable
from one a teacher set, and 0021's reverse already declined to guess for the
same reason.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    Quiz = apps.get_model("quizzes", "Quiz")
    Through = Quiz.batches.through

    # Quizzes that name a batch via the shim but have no M2M rows at all.
    already = set(
        Through.objects.values_list("quiz_id", flat=True).distinct()
    )
    rows = [
        Through(quiz_id=quiz_id, batch_id=batch_id)
        for quiz_id, batch_id in (
            Quiz.objects
            .filter(batch__isnull=False)
            .values_list("id", "batch_id")
            .iterator()
        )
        if quiz_id not in already
    ]
    Through.objects.bulk_create(rows, batch_size=500)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("quizzes", "0030_drop_chapter_fk"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
