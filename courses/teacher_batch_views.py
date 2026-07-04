"""Teacher-facing endpoint: the batches a teacher can record progress for.

A teacher teaches *subjects*; subjects belong to *courses*; batches belong to
courses. So a teacher's batches are the batches of every course in which they're
assigned at least one subject. Grouped by course, each batch carries a coverage
percentage so the list screen is informative at a glance.

Route (added in courses/urls.py):
    GET  courses/teacher/my-batches/
"""

from django.db.models import Count, Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Batch, Chapter, Course, SubjectTeacher
from .models_batch_progress import BatchChapterProgress


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
