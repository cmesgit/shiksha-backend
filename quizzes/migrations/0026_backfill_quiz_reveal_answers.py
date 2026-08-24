"""Backfill Quiz.reveal_answers for existing MOCK quizzes.

`reveal_answers` (Phase 4) says WHEN a learner may see correctness:
`after_each` / `after_submit` / `never`. The spec's defaults are per quiz
type — `after_each` for practice, `after_submit` for mock — which a plain
field default cannot express, so 0025 gave the column the practice value
and this migration corrects the mock rows.

Direction is deliberate. `after_each` is the pre-Phase-4 practice behaviour
(CheckAnswerView's instant per-question feedback), so leaving practice rows
on the schema default preserves exactly what those quizzes already did.
Mock rows never had instant feedback available at all — CheckAnswerView has
always refused `quiz_type != practice` — so moving them to `after_submit` is
behaviourally a no-op today; it only stops the stored value from claiming a
mode the quiz can never be in, which would read as a bug the first time
someone builds a settings screen off this field.

Nothing here touches `reveal_answers_after` (the separate attempt-budget
quota) — see Quiz's field comments for why the two coexist.

.update() rather than a Python loop: mock quizzes number in the thousands on
prod, and there is no model-level invariant on this field to preserve.
"""

from django.db import migrations


def set_mock_reveal_after_submit(apps, schema_editor):
    Quiz = apps.get_model("quizzes", "Quiz")
    Quiz.objects.filter(quiz_type="mock").update(reveal_answers="after_submit")


def back_to_after_each(apps, schema_editor):
    # Reverse only restores the schema default; the pre-migration value is
    # not recoverable (and was uniformly the default anyway).
    Quiz = apps.get_model("quizzes", "Quiz")
    Quiz.objects.filter(quiz_type="mock").update(reveal_answers="after_each")


class Migration(migrations.Migration):

    dependencies = [
        ("quizzes", "0025_mock_test_settings_and_sections"),
    ]

    operations = [
        migrations.RunPython(set_mock_reveal_after_submit, back_to_after_each),
    ]
