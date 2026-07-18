"""Teacher-facing endpoint: the batches a teacher can record progress for.

A teacher teaches *subjects*; subjects belong to *courses*; batches belong to
courses. So a teacher's batches are the batches of every course in which they're
assigned at least one subject. Grouped by course, each batch carries a coverage
percentage so the list screen is informative at a glance.

Route (added in courses/urls.py):
    GET  courses/teacher/my-batches/
"""

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Batch, Chapter, Course, Subject, SubjectTeacher
from .models_batch_progress import BatchChapterProgress
from .services import teaches_subject


class TeacherMyBatchesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        course_ids = list(
            SubjectTeacher.objects.filter(teacher=user)
            .values_list("subject__course_id", flat=True)
            .distinct()
        )
        if not course_ids:
            return Response([])

        courses = {
            c.id: c
            for c in Course.objects.filter(id__in=course_ids).select_related("board")
        }

        # Total chapters per course (one query).
        chapter_totals = {}
        for row in (
            Chapter.objects.filter(subject__course_id__in=course_ids)
            .values("subject__course_id")
            .annotate(n=Count("id"))
        ):
            chapter_totals[row["subject__course_id"]] = row["n"]

        # Covered chapters per batch (one query).
        covered_by_batch = {}
        for row in (
            BatchChapterProgress.objects.filter(
                batch__course_id__in=course_ids, is_covered=True
            )
            .values("batch_id")
            .annotate(n=Count("id"))
        ):
            covered_by_batch[row["batch_id"]] = row["n"]

        batches = (
            Batch.objects.filter(course_id__in=course_ids)
            .annotate(_seats=Count("enrollments", filter=Q(enrollments__status="ACTIVE")))
            .order_by("-year", "code")
        )

        grouped = {}
        for b in batches:
            total = chapter_totals.get(b.course_id, 0)
            done = covered_by_batch.get(b.id, 0)
            grouped.setdefault(b.course_id, []).append({
                "id": str(b.id),
                "name": b.name,
                "code": b.code,
                "year": b.year,
                "seats_taken": b._seats,
                "capacity": b.capacity,
                "is_active": b.is_active,
                "chapters_total": total,
                "chapters_done": done,
                "percent": round(done / total * 100) if total else 0,
            })

        out = []
        for cid, course in courses.items():
            out.append({
                "course_id": str(cid),
                "course_title": course.title,
                "board": course.board.name if course.board else None,
                "batches": grouped.get(cid, []),
            })
        out.sort(key=lambda x: x["course_title"] or "")
        return Response(out)


class TeacherSubjectBatchesView(APIView):
    """Active batches a teacher can schedule content for, for one subject.

    Backs the batch picker on the teacher's create-live-session and
    create-assignment forms (which only know the subject from the URL). Returns
    the subject's course's active batches when the teacher teaches that subject.

    Route (added in courses/urls.py):
        GET  courses/subjects/<subject_id>/batches/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        subject = get_object_or_404(
            Subject.objects.select_related("course"), id=subject_id)

        if not (request.user.is_staff or teaches_subject(request.user, subject)):
            return Response(
                {"detail": "You are not assigned to this subject."},
                status=403,
            )

        batches = (
            Batch.objects.filter(course_id=subject.course_id, is_active=True)
            .order_by("-year", "code")
        )
        return Response([
            {
                "id": str(b.id),
                "name": b.name,
                "code": b.code,
                "year": b.year,
            }
            for b in batches
        ])
