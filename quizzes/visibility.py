"""Canonical student-visibility rule for quizzes. One place, on purpose.

Phase 1 of the quiz refactor decoupled "this quiz is live for my batches"
(`Quiz.is_assigned`, teacher-controlled) from "an admin approved the
questions" (`Quiz.review_status`, now purely informational). Before this, a
separate `is_published` flag was the gate and an admin had to approve a
teacher's own quiz before that teacher's own class could take it. Phase 10
dropped that column entirely (migration 0029) — `is_assigned` below is the
only visibility gate there is, and nothing here has ever keyed on
`review_status`.

The rule, in full:

    visible  ==  is_assigned=True  AND  batch scope matches

and "batch scope matches" is three cases, in priority order:

  1. `batches` (M2M) is non-empty  → visible only to those batches.
  2. `batches` is empty, `batch` (legacy FK) is set → visible only to that
     batch. This fallback is load-bearing, not tidiness: QuizCreateSerializer
     and TeacherQuizDuplicateView still write only the legacy FK, so without
     it a freshly created batch-scoped quiz would have an empty `batches` set,
     fall into case 3, and leak to every batch of the course.
  3. both empty/NULL → course-wide, visible to every batch.

Case 3 is exactly what `batch IS NULL` meant before `batches` existed, which
is why an empty M2M has to mean "everyone" rather than "nobody".

WHY Exists() AND NOT A JOIN
---------------------------
The obvious spelling of case 1 is `Q(batches__isnull=True) | Q(batches=<id>)`.
It works, but it adds a multi-valued JOIN to the outer query, so a quiz in two
batches can yield two result rows. That is survivable in a list (`.distinct()`
mops it up) and *silently wrong* everywhere else: several of these call sites
compute a `Count()` or an `.aggregate()` on the same queryset, and a duplicated
join row inflates the count with no error and no test failure.

Exists() is a correlated subquery: it contributes zero joins to the outer
query, so it cannot multiply rows at all. The duplicate-row problem is
dissolved rather than patched, `.distinct()` is not required for correctness,
and existing aggregates keep their meaning. Do not "simplify" this back into
a join.
"""

from django.db.models import Exists, OuterRef, Q


def _through():
    # The auto-created Quiz.batches join table. Queried directly so the
    # subquery stays a bare 2-column lookup with no extra table access.
    from .models import Quiz
    return Quiz.batches.through.objects


def has_any_batch():
    """Exists(): this quiz names at least one batch (i.e. is NOT course-wide)."""
    return Exists(_through().filter(quiz_id=OuterRef("pk")))


def has_batch(batch_id):
    """Exists(): this quiz names `batch_id` specifically."""
    return Exists(_through().filter(quiz_id=OuterRef("pk"), batch_id=batch_id))


def batch_scope_q(batch_id):
    """Q for the batch half of the rule, for a learner in `batch_id`.

    `batch_id=None` means the learner has not been placed in a batch for this
    course. That degrades to course-wide-only, which is what the pre-Phase-1
    list filter did (`Q(batch__isnull=True) | Q(batch_id=None)` collapses to
    `batch IS NULL`). NOTE this is deliberately *stricter* than the per-object
    helper `learner_may_see_quiz` below, which lets an unplaced learner open
    anything — that asymmetry is pre-existing behaviour, faithfully preserved
    here rather than quietly "fixed", since changing it would move quizzes
    into or out of view for real unplaced learners.
    """
    # `batches` empty IS course-wide. There used to be a second clause here
    # falling back to the legacy single-batch `batch` FK, because writers that
    # set only that shim left the M2M empty — which this rule would otherwise
    # have read as "everyone", widening a batch-scoped quiz to the whole
    # course. Phase 10 closed those writers (create and duplicate both
    # populate the M2M now, and migration 0031 backfilled the stragglers) and
    # dropped the column, so empty now unambiguously means course-wide.
    course_wide = ~Q(has_any_batch())
    if batch_id is None:
        return course_wide
    return Q(has_batch(batch_id)) | course_wide


def visible_quiz_q(batch_id):
    """The complete student-visibility rule: assigned AND in scope."""
    return Q(is_assigned=True) & batch_scope_q(batch_id)


def quiz_batch_ids(quiz):
    """Effective batch scope of `quiz` as a set of ids; empty = course-wide.

    Python-side mirror of `batch_scope_q`, for per-object checks and for
    deciding who to notify. Two cases now, not three — the legacy `batch` FK
    fallback went with the column in Phase 10.
    """
    return set(quiz.batches.values_list("id", flat=True))


def learner_may_see_quiz(learner, quiz):
    """True unless `quiz`'s batch scope excludes this learner.

    Per-object counterpart to `batch_scope_q`. Does NOT check `is_assigned` —
    callers resolve the quiz with that filter already applied.
    """
    from enrollments.services import active_batch_id

    scope = quiz_batch_ids(quiz)
    if not scope:
        return True
    batch_id = active_batch_id(
        learner_profile=learner, course_id=quiz.subject.course_id
    )
    # None = not placed in a batch yet. Pre-existing behaviour: allow, so a
    # quiz an unplaced learner can see on a list never 404s when they open it.
    return batch_id is None or batch_id in scope
