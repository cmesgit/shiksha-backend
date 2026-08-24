"""Backfill Question.bank_state from Quiz.review_status.

Do NOT generalise Phase 1's reasoning here. Phase 1 (0020) deliberately
backfilled Quiz.is_assigned from is_published rather than review_status,
because is_published — not review_status — was the field every student
queryset actually filtered on; review_status was never the real gate for
student visibility, so using it as the backfill source would have silently
changed who could see a quiz.

The question bank is a different axis with a different history. Before this
migration, `quiz__review_status=Quiz.REVIEW_APPROVED` (see
TeacherQuestionBankView.get_queryset and TeacherBankFiltersView, both in
quizzes/views.py) was, and is, the literal filter that decided whether a
question could appear in the bank at all. There is no second, more-real gate
being masked here the way is_published masked review_status for quizzes —
review_status IS the thing that gated bank membership. So for bank_state,
review_status is the faithful, visibility-preserving backfill source:

    quiz.review_status == "approved"  -> bank_state = "accepted"
    everything else (draft/pending/rejected) -> bank_state = "suggested"

`suggest_to_bank` is left at its schema default (True) for every existing
row by this migration — nothing becomes "private" here. A pre-existing
question was never in a state where a teacher had opted it out (the concept
didn't exist yet), and defaulting it to False would incorrectly manufacture
an opt-out no teacher ever made.

Uses queryset.update() rather than looping + .save() in Python: there may be
many thousands of rows, and .update() bypasses Question.save()'s invariant
logic (suggest_to_bank=False -> bank_state="private") by design — that
invariant is irrelevant here since suggest_to_bank is untouched (stays True)
for every row this migration writes, so there is no divergence to enforce.

Reverse is a no-op: bank_state is dropped wholesale by the reverse of 0022,
and un-setting it here would be indistinguishable from real curation activity
(an admin's accept/request-changes decision) that happened after this
migration ran.
"""

from django.db import migrations


def backfill_bank_state(apps, schema_editor):
    Question = apps.get_model("quizzes", "Question")
    Question.objects.filter(quiz__review_status="approved").update(
        bank_state="accepted",
    )
    Question.objects.exclude(quiz__review_status="approved").update(
        bank_state="suggested",
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("quizzes", "0022_question_bank_state"),
    ]

    operations = [
        migrations.RunPython(backfill_bank_state, noop),
    ]
