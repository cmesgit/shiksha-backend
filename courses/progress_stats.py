"""Cross-app activity stats for the student progress endpoint.

Aggregates data that lives outside this app (quizzes, assignments, live /
private / group sessions) into the small "stats" block merged onto
``MyBatchProgressView``'s response. Everything is scoped to one course's
subjects and one student, reusing the same account/learner_profile dual-key
pattern already used to resolve the student's enrollment in
``courses.batch_progress_views.MyBatchProgressView``.
"""

from django.db.models import Q, Sum, Count

from quizzes.models import QuizAttempt
from assignments.models import AssignmentSubmission


def _dual_key_q(account_field, user, learner):
    """Same dual-key rule as the enrollment lookup in MyBatchProgressView:
    prefer rows tied to the active learner profile, but also surface legacy
    (pre-profile) rows for the account's default profile."""
    if learner is None:
        return Q(**{account_field: user})
    q = Q(learner_profile=learner)
    if getattr(learner, "is_default", False):
        q |= Q(learner_profile__isnull=True, **{account_field: user})
    return q


def average_quiz_score_pct(attempts):
    """Average ``score / quiz.total_marks * 100`` across an iterable of
    submitted ``QuizAttempt``s (must be ``select_related("quiz")`` or have
    ``quiz`` otherwise prefetched to avoid N+1s). total_marks is a Quiz
    field (per-quiz, not per-attempt); attempts whose quiz never had marks
    configured are skipped to avoid a division by zero — callers that also
    report a completed-count should count those attempts separately, before
    calling this. Returns ``None`` if there's nothing left to average.

    Shared by ``build_progress_stats`` below (course-scoped) and
    ``dashboard.views`` (account/profile-scoped across possibly several
    courses' subjects) so the "average quiz score" definition stays in one
    place.
    """
    pct_list = [
        attempt.score / attempt.quiz.total_marks * 100
        for attempt in attempts
        if attempt.quiz.total_marks
    ]
    return round(sum(pct_list) / len(pct_list)) if pct_list else None


def build_progress_stats(course, student_user, subjects_qs, learner=None):
    """Return the ``stats`` block: quiz average/count, assignments done, and
    live hours attended, all scoped to this course + this student."""
    subject_ids = list(subjects_qs.values_list("id", flat=True))

    # ---- Quizzes: submitted attempts on quizzes belonging to this course's subjects.
    # A SUBMITTED attempt only counts as completed if it has answers — see
    # quizzes/views.py's StartQuizView/StudentDashboardView for the ghost-
    # attempt bug (a pre-existing zero-answer SUBMITTED row from the old
    # expiry behavior) this same guard remediates.
    attempt_q = Q(quiz__subject_id__in=subject_ids, status=QuizAttempt.STATUS_SUBMITTED)
    attempt_q &= _dual_key_q("student", student_user, learner)
    attempts = list(
        QuizAttempt.objects.filter(attempt_q)
        .annotate(_answer_count=Count("answers"))
        .filter(_answer_count__gt=0)
        .select_related("quiz")
    )

    quizzes_completed = len(attempts)
    quiz_avg_pct = average_quiz_score_pct(attempts)

    # ---- Assignments: submissions on assignments in this course's subjects (via chapter).
    submission_q = Q(assignment__chapter__subject_id__in=subject_ids)
    submission_q &= _dual_key_q("student", student_user, learner)
    assignments_done = AssignmentSubmission.objects.filter(submission_q).count()

    # ---- Live hours: live sessions + private sessions + group sessions.
    # Local imports: livestream/sessions_app aren't imported elsewhere in this
    # module and courses<->livestream/sessions_app import order isn't
    # exercised anywhere yet, so keep this cautious like the other
    # LiveSession local imports in courses/views.py.
    from livestream.models import LiveSessionAttendance
    from sessions_app.models import PrivateSession, GroupSessionAttendance

    live_seconds = LiveSessionAttendance.objects.filter(
        session__course=course, user=student_user,
    ).aggregate(total=Sum("total_seconds"))["total"] or 0

    # PrivateSession has no FK to course/subject — `subject` is a plain
    # CharField snapshot of Subject.name taken at creation time (see
    # sessions_app.views.request_session). There's no reliable FK path, so
    # match by subject name within this course instead. started_at/ended_at
    # stand in for a duration since private sessions don't track per-second
    # attendance the way live/group sessions do.
    subject_names = list(subjects_qs.values_list("name", flat=True))
    private_q = Q(
        subject__in=subject_names,
        started_at__isnull=False,
        ended_at__isnull=False,
    )
    private_q &= _dual_key_q("requested_by", student_user, learner)
    private_seconds = sum(
        max(0, int((ended_at - started_at).total_seconds()))
        for started_at, ended_at in PrivateSession.objects.filter(
            private_q
        ).values_list("started_at", "ended_at")
    )

    # GroupSession also has no course FK (course_title is a denormalised
    # snapshot, not a reliable link), but it does carry a real FK to
    # courses.Subject, so scope by subject membership in this course instead.
    # GroupSessionAttendance has no learner_profile column (account-level
    # only), so this is scoped by account regardless of `learner`.
    group_seconds = GroupSessionAttendance.objects.filter(
        session__subject_id__in=subject_ids, user=student_user,
    ).aggregate(total=Sum("total_seconds"))["total"] or 0

    total_seconds = live_seconds + private_seconds + group_seconds
    live_hours = round(total_seconds / 3600)

    return {
        "quiz_avg_pct": quiz_avg_pct,
        "quizzes_completed": quizzes_completed,
        "live_hours": live_hours,
        "assignments_done": assignments_done,
    }
