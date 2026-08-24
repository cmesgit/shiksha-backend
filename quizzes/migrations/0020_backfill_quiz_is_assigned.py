"""Backfill Quiz.is_assigned from is_published — NOT from review_status.

The design handoff says to backfill `review_status="approved"` → is_assigned.
That is wrong and would lose student access. `review_status` never gated
student visibility: every student-facing queryset filtered on `is_published`
(quizzes/views.py StudentDashboardView/StartQuizView/SubmitQuizView/
CheckAnswerView/QuizDetailView/StudentQuizAttemptsView, dashboard/views.py
_learner_quizzes, courses/views.py SubjectDashboardView).

The two usually agree, because AdminQuizReviewView is the only place that
writes both and it writes them together. "Usually" is the problem: any row
where they diverge — a legacy row, a fixture, a manual DB edit, a future code
path — would have been visible to students yesterday via is_published and
would silently become invisible today. Preserving "nobody loses access to a
quiz they could see yesterday" is the entire point of Phase 1, so the backfill
must copy the field that actually did the gating.

Reverse is a no-op: is_assigned is dropped wholesale by the reverse of 0019,
and un-setting it here would be indistinguishable from a teacher deliberately
un-assigning a quiz after the migration ran.
"""

from django.db import migrations


def backfill_is_assigned(apps, schema_editor):
    Quiz = apps.get_model("quizzes", "Quiz")
    Quiz.objects.filter(is_published=True).update(is_assigned=True)
    # Explicit rather than relying on the field default: makes the invariant
    # (is_assigned == is_published at migration time) true for every row,
    # including any written between 0019 and this migration.
    Quiz.objects.filter(is_published=False).update(is_assigned=False)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("quizzes", "0019_quiz_is_assigned_and_batches"),
    ]

    operations = [
        migrations.RunPython(backfill_is_assigned, noop),
    ]
