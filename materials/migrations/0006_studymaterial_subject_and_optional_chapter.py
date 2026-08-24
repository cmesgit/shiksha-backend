"""Give StudyMaterial an independent subject anchor, then let chapter go null.

Same reasoning as assignments/0009 — read that docstring first. In short: the
authorization gates for study material (materials/views.py's teaches_subject()
checks, and config/media_security.py's file-download gate) derived the subject
by walking `chapter.subject`. `chapter` has been a required FK since
0001_initial, so that was safe. It is about to become optional, so the subject
has to come from a column that cannot be NULL.

The backfill is exact rather than inferred: every existing row has a chapter,
so chapter.subject_id is always available and is by definition correct.

Also flips `chapter` CASCADE → SET_NULL: since teacher-typed chapters became
real courses.Chapter rows, deleting a chapter silently destroyed the study
material filed under it, along with its MaterialFile rows and the uploaded
files they point at.
"""

import django.db.models.deletion
from django.db import migrations, models


def backfill_subject_from_chapter(apps, schema_editor):
    StudyMaterial = apps.get_model("materials", "StudyMaterial")
    Chapter = apps.get_model("courses", "Chapter")

    StudyMaterial.objects.filter(subject__isnull=True).update(
        subject_id=models.Subquery(
            Chapter.objects.filter(pk=models.OuterRef("chapter_id"))
            .values("subject_id")[:1]
        )
    )


def unbackfill(apps, schema_editor):
    """No-op reverse — AddField's own reverse drops the column."""
    return None


class Migration(migrations.Migration):

    dependencies = [
        ("materials", "0005_studymaterial_batch"),
        ("courses", "0036_chapter_provenance_and_content_chapter_tag"),
    ]

    operations = [
        migrations.AddField(
            model_name="studymaterial",
            name="subject",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="materials",
                to="courses.subject",
            ),
        ),
        migrations.RunPython(backfill_subject_from_chapter, unbackfill),
        migrations.AlterField(
            model_name="studymaterial",
            name="subject",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="materials",
                to="courses.subject",
            ),
        ),
        migrations.AlterField(
            model_name="studymaterial",
            name="chapter",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="materials",
                to="courses.chapter",
            ),
        ),
    ]
