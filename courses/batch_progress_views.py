"""Per-batch progress endpoints.

Routes (wired in courses/urls.py):

    GET  courses/batches/<batch_id>/progress/
         -> teacher/admin: full chapter checklist + percentages for the batch.

    POST courses/batches/<batch_id>/chapters/<chapter_id>/coverage/
         body: {"done": true|false, "note": "optional text"}
         -> teacher/admin: mark a chapter covered / not covered for this batch,
            with an optional note. Upserts BatchChapterProgress.

    GET  courses/my-batch-progress/?course=<course_id>
         -> student: resolves the student's batch for that course and returns
            that batch's progress. Falls back to course-wide coverage if the
            student's enrollment has no batch assigned (rollout safety).

    GET  courses/my-batch-roster/?course=<course_id>
         -> student: the teachers (per subject) and classmates (other active
            enrollments) in the student's batch for that course. 404s rather
            than falling back when there is no batch — a roster has nothing
            honest to say about a course-wide (not-yet-batched) enrollment.
"""

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Batch, Chapter, Course, Subject
from .models_batch_progress import BatchChapterProgress
from .batch_progress import (
    _batch_subject_teacher_name,
    build_batch_progress,
    can_view_batch_progress,
    can_edit_chapter_for_batch,
)
from .progress_stats import build_progress_stats


class BatchProgressView(APIView):
    """Teacher/admin view: chapter checklist + percentages for one batch."""
    permission_classes = [IsAuthenticated]

    def get(self, request, batch_id):
        batch = get_object_or_404(Batch.objects.select_related("course"), pk=batch_id)

        if not can_view_batch_progress(request.user, batch):
            return Response(
                {"detail": "You don't teach any subject in this batch's course."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(build_batch_progress(batch))


class BatchChapterCoverageView(APIView):
    """Mark a chapter covered / not covered for a specific batch (+ note)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, batch_id, chapter_id):
        batch = get_object_or_404(Batch.objects.select_related("course"), pk=batch_id)
        chapter = get_object_or_404(
            Chapter.objects.select_related("subject", "subject__course"),
            pk=chapter_id,
        )

        # The chapter must live in the batch's course.
        if chapter.subject.course_id != batch.course_id:
            return Response(
                {"detail": "This chapter is not part of the batch's course."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not can_edit_chapter_for_batch(request.user, chapter, batch):
            return Response(
                {"detail": "You are not assigned to this subject."},
                status=status.HTTP_403_FORBIDDEN,
            )

        done = request.data.get("done", True)
        if isinstance(done, str):
            done = done.lower() not in ("false", "0", "no", "")
        done = bool(done)

        note = request.data.get("note", None)

        bp, _ = BatchChapterProgress.objects.get_or_create(
            batch=batch, chapter=chapter
        )
        bp.is_covered = done
        bp.covered_at = timezone.now() if done else None
        bp.marked_by = request.user if done else None
        if note is not None:
            bp.note = str(note)[:5000]
        bp.save()

        # Recompute just this subject so the UI can update without a full refetch.
        chapter_ids = list(
            chapter.subject.chapters.values_list("id", flat=True)
        )
        s_total = len(chapter_ids)
        s_done = BatchChapterProgress.objects.filter(
            batch=batch, chapter_id__in=chapter_ids, is_covered=True
        ).count()

        return Response({
            "batch_id": str(batch.id),
            "chapter_id": str(chapter.id),
            "is_covered": done,
            "covered_at": bp.covered_at.isoformat() if bp.covered_at else None,
            "note": bp.note,
            "subject_id": str(chapter.subject_id),
            "subject_chapters_total": s_total,
            "subject_chapters_done": s_done,
            "subject_percent": round(s_done / s_total * 100) if s_total else 0,
        })


def _resolve_student_enrollment(request, course):
    """The student's own ACTIVE enrollment in `course`, or None.

    Same dual-key rule used throughout this module (and `progress_stats.
    _dual_key_q`): prefer the row tied to the active learner profile, but
    also surface a legacy (pre-profile) row for the account's default
    profile. Factored out of `MyBatchProgressView` so the new roster view
    resolves "am I enrolled, and in which batch" identically rather than
    re-deriving the same rule a second way.
    """
    # Local imports avoid a courses<->enrollments/accounts circular import.
    from django.db.models import Q
    from enrollments.models import Enrollment
    try:
        from accounts.auth_flow import get_active_profile
        learner = get_active_profile(request)
    except Exception:
        learner = None

    enrollment_qs = Enrollment.objects.select_related("batch", "batch__course")

    if learner is not None:
        profile_q = Q(learner_profile=learner)
        if getattr(learner, "is_default", False):
            profile_q |= Q(
                learner_profile__isnull=True, user=getattr(learner, "account", None)
            )
        enrollment = enrollment_qs.filter(
            Q(course=course, status=Enrollment.STATUS_ACTIVE) & profile_q
        ).first()
    else:
        enrollment = enrollment_qs.filter(
            user=request.user, course=course, status=Enrollment.STATUS_ACTIVE
        ).first()

    return enrollment, learner


class MyBatchProgressView(APIView):
    """Student view: coverage for the batch the student belongs to.

    Query params:
        - course: Course UUID (required)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        course_id = request.query_params.get("course")
        if not course_id:
            return Response(
                {"detail": "course query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        course = get_object_or_404(Course, pk=course_id)
        enrollment, learner = _resolve_student_enrollment(request, course)

        if enrollment is None:
            return Response(
                {"detail": "You are not enrolled in this course."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Quiz/assignment/live-hours stats: same "this course's subjects" +
        # "this student" scope regardless of which branch below runs.
        subjects_qs = course.subjects.all()
        stats = build_progress_stats(
            course, request.user, subjects_qs, learner=learner
        )

        # Batch assigned -> per-batch progress.
        if enrollment.batch_id:
            payload = build_batch_progress(enrollment.batch)
            payload["stats"] = stats
            _attach_per_subject_stats(payload, course, request.user, learner)
            return Response(payload)

        # No batch yet (e.g. enrolled before batch assignment rolled out):
        # fall back to the course-wide coverage so the student still sees
        # something. Flagged so the client can label it appropriately.
        from .progress import build_course_progress
        payload = build_course_progress(course)
        payload["batch"] = None
        payload["fallback"] = "course_wide"
        payload["stats"] = stats
        _attach_per_subject_stats(payload, course, request.user, learner)
        return Response(payload)


def _attach_per_subject_stats(payload, course, user, learner):
    """Merge a per-SUBJECT ``stats`` block (quiz average, assignments done,
    live minutes attended) onto each entry of ``payload["subjects"]``, for
    the subject progress detail screen.

    Reuses `build_progress_stats` exactly as the course-wide `stats` block
    above does, just scoped to one subject's queryset instead of all of the
    course's — the function already derives every filter from `subjects_qs`,
    so no new aggregation logic exists here, only a narrower scope.
    """
    for entry in payload["subjects"]:
        entry["stats"] = build_progress_stats(
            course, user, Subject.objects.filter(id=entry["id"]), learner=learner
        )


class MyBatchRosterView(APIView):
    """Student view: who teaches this batch's subjects, and who else is in
    it — the design's "batch: teachers and classmates" screen, which had no
    backend support at all before this (verified: no student-facing
    roster endpoint existed anywhere in this codebase).

    Query params:
        - course: Course UUID (required)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        course_id = request.query_params.get("course")
        if not course_id:
            return Response(
                {"detail": "course query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        course = get_object_or_404(Course, pk=course_id)
        enrollment, learner = _resolve_student_enrollment(request, course)

        if enrollment is None:
            return Response(
                {"detail": "You are not enrolled in this course."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if enrollment.batch_id is None:
            # A roster is about to a SPECIFIC cohort; a course-wide
            # enrollment (no batch yet) has no cohort to name. Honest 404
            # rather than an empty roster, which would read as "nobody is
            # in your class" instead of "you aren't in a batch yet".
            return Response(
                {"detail": "You are not yet assigned to a batch."},
                status=status.HTTP_404_NOT_FOUND,
            )

        batch = enrollment.batch

        # ---- Teachers, one row per staffed subject. ------------------------
        # `_batch_subject_teacher_name` already resolves batch-scoped OR
        # course-wide PRIMARY assignments and the "(deleted teacher)"/no-row
        # cases — the exact same helper `build_batch_progress` uses, so this
        # can never disagree with what the syllabus-progress screen shows.
        teachers = []
        for subject in course.subjects.all():
            name = _batch_subject_teacher_name(batch, subject)
            if name:
                teachers.append({"subject_name": subject.name, "teacher_name": name})

        # ---- Classmates: other ACTIVE enrollments in the same batch. ------
        from enrollments.models import Enrollment

        classmates_qs = (
            Enrollment.objects.filter(batch=batch, status=Enrollment.STATUS_ACTIVE)
            .exclude(pk=enrollment.pk)
            .select_related("learner_profile")
        )
        classmates = []
        for e in classmates_qs:
            lp = e.learner_profile
            name = (lp.display_name if lp and lp.display_name else None) or "Classmate"
            avatar_url = None
            if lp and lp.avatar_image:
                avatar_url = request.build_absolute_uri(lp.avatar_image.url)
            elif lp and lp.profile_photo:
                avatar_url = request.build_absolute_uri(lp.profile_photo.url)
            classmates.append({
                "display_name": name,
                "avatar_emoji": (lp.avatar_emoji if lp else "") or "",
                "avatar_url": avatar_url,
            })
        # Several Enrollment rows can share one learner (a dropped-then-
        # re-enrolled history is ACTIVE only once, but belt-and-suspenders
        # against any future dual-key edge case) — dedupe by display name
        # rather than trusting one row per person.
        seen = set()
        deduped = []
        for c in classmates:
            key = c["display_name"]
            if key in seen:
                continue
            seen.add(key)
            deduped.append(c)

        return Response({
            "batch": {"id": str(batch.id), "name": batch.name, "code": batch.code},
            "teachers": teachers,
            "classmates": deduped,
        })
