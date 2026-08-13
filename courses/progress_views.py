from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Course, Chapter, TeachingAssignment
from .progress import build_course_progress
from .services import teaches_subject


def _can_edit_subject(user, subject):
    """A staff/admin user, or a teacher assigned to this subject, may tick it."""
    if not (user and user.is_authenticated):
        return False
    if user.is_staff:
        return True
    return teaches_subject(user, subject)


def _can_edit_any_subject_in_course(user, course):
    if user.is_staff:
        return True
    return TeachingAssignment.objects.filter(
        subject__course=course, teacher=user, is_active=True
    ).exists()


class CourseProgressView(APIView):
    """Teacher/admin view: full chapter checklist + percentages for the course."""
    permission_classes = [IsAuthenticated]

    def get(self, request, course_id):
        course = get_object_or_404(Course, pk=course_id)

        if not _can_edit_any_subject_in_course(request.user, course):
            return Response(
                {"detail": "You don't teach any subject in this course."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(build_course_progress(course))


class ChapterCoverageView(APIView):
    """Mark a chapter covered / not covered (one shared state per course).

    LEGACY course-wide flag. Per-batch coverage now lives in
    ``BatchChapterCoverageView`` (POST courses/batches/<batch_id>/chapters/
    <chapter_id>/coverage/), backed by BatchChapterProgress. This endpoint is
    kept only until the teacher UI fully moves to the per-batch route.

    POST body:
        { "done": true }   # mark covered
        { "done": false }  # un-mark
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, chapter_id):
        chapter = get_object_or_404(
            Chapter.objects.select_related("subject", "subject__course"),
            pk=chapter_id,
        )

        if not _can_edit_subject(request.user, chapter.subject):
            return Response(
                {"detail": "You are not assigned to this subject."},
                status=status.HTTP_403_FORBIDDEN,
            )

        done = request.data.get("done", True)
        if isinstance(done, str):
            done = done.lower() not in ("false", "0", "no", "")
        done = bool(done)

        chapter.is_covered = done
        chapter.covered_at = timezone.now() if done else None
        chapter.marked_by = request.user if done else None
        chapter.save(update_fields=["is_covered", "covered_at", "marked_by"])

        # Recompute just this subject so the UI can update without a full refetch.
        chapters = list(chapter.subject.chapters.all())
        s_total = len(chapters)
        s_done = sum(1 for ch in chapters if ch.is_covered)

        return Response({
            "chapter_id": str(chapter.id),
            "is_covered": done,
            "covered_at": chapter.covered_at.isoformat() if chapter.covered_at else None,
            "subject_id": str(chapter.subject_id),
            "subject_chapters_total": s_total,
            "subject_chapters_done": s_done,
            "subject_percent": round(s_done / s_total * 100) if s_total else 0,
        })


class MyCourseProgressView(APIView):
    """Student view: course coverage. Any enrolled student sees the same state,
    including students who joined after teaching had already progressed."""
    permission_classes = [IsAuthenticated]

    def get(self, request, course_id):
        course = get_object_or_404(Course, pk=course_id)

        # Local import avoids a courses<->enrollments circular import at load.
        from enrollments.models import Enrollment

        is_enrolled = Enrollment.objects.filter(
            user=request.user, course=course, status=Enrollment.STATUS_ACTIVE
        ).exists()
        if not is_enrolled:
            return Response(
                {"detail": "You are not enrolled in this course."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(build_course_progress(course))
