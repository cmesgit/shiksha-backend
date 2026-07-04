# Creates BatchChapterProgress: per-batch teaching progress for each chapter,
# with an optional teacher note. This is the model that lets two batches of the
# same course progress independently.

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("courses", "0014_merge_20260630_1651"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BatchChapterProgress",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("is_covered", models.BooleanField(default=False, db_index=True, help_text="Marked by a teacher once this chapter has been taught to this batch.")),
                ("covered_at", models.DateTimeField(blank=True, null=True)),
                ("note", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="chapter_progress", to="courses.batch")),
                ("chapter", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="batch_progress", to="courses.chapter")),
                ("marked_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="marked_batch_chapters", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name="batchchapterprogress",
            constraint=models.UniqueConstraint(fields=("batch", "chapter"), name="unique_progress_per_batch_chapter"),
        ),
        migrations.AddIndex(
            model_name="batchchapterprogress",
            index=models.Index(fields=["batch", "is_covered"], name="batch_chapter_covered_idx"),
        ),
    ]
