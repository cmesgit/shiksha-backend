"""Teacher-facing endpoint: the batches a teacher can record progress for.

A teacher teaches *subjects*; subjects belong to *courses*; batches belong to
courses. So a teacher's batches are the batches of every course in which they're
assigned at least one subject. Grouped by course, each batch carries a coverage
percentage so the list screen is informative at a glance.

Route (added in courses/urls.py):
    GET  courses/teacher/my-batches/
"""

from django.db.models import Avg, Count, ExpressionWrapper, F, FloatField, Q
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from quizzes.models import QuizAttempt

from .batch_progress import visible_batches_q
from .board_display import board_name_for
from .models import Batch, Chapter, Course, Subject, TeachingAssignment
from .models_batch_progress import BatchChapterProgress
from .services import teaches_subject


def _avg_quiz_score(user):
    """Attempt-weighted average score % across this teacher's published
    quizzes' submitted attempts (every attempt counts once — not a
    per-quiz average of averages). None if there are no such attempts."""
    avg = (
        QuizAttempt.objects.filter(
            quiz__created_by=user,
            quiz__is_published=True,
            status=QuizAttempt.STATUS_SUBMITTED,
            quiz__total_marks__gt=0,
        )
        .annotate(
            pct=ExpressionWrapper(
                F("score") * 100.0 / F("quiz__total_marks"),
                output_field=FloatField(),
            )
        )
        .aggregate(avg=Avg("pct"))["avg"]
    )
    return round(avg) if avg is not None else None


class TeacherMyBatchesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        course_ids = list(
            TeachingAssignment.objects.filter(teacher=user, is_active=True)
            .values_list("subject__course_id", flat=True)
            .distinct()
        )
        if not course_ids:
            return Response({
                "groups": [],
                "stats": {
                    "active_batches": 0,
                    "avg_syllabus_completion": 0,
                    "students": 0,
                    "avg_quiz_score": _avg_quiz_score(user),
                },
            })

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

        # visible_batches_q, not course_id__in=course_ids: the latter is the
        # SUPERSET of what can_view_batch_progress will actually let this
        # teacher open, so every batch of a course they teach one batch of was
        # listed, and clicking any of them 403'd. See that helper's docstring.
        # Staff aren't special-cased here because this is the teacher's own
        # "my batches" view — an admin uses the admin batch screens.
        batches = (
            Batch.objects.filter(visible_batches_q(user))
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
                "start_date": b.start_date.isoformat() if b.start_date else None,
                "end_date": b.end_date.isoformat() if b.end_date else None,
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

        active_batches = [
            b for batch_list in grouped.values() for b in batch_list if b["is_active"]
        ]
        stats = {
            "active_batches": len(active_batches),
            # None, not 0, when there is nothing to average. The frontend has a
            # "—" no-data branch for exactly this case which was unreachable,
            # so a teacher with no active batches was shown "0%" — read as
            # "you have covered none of your syllabus" rather than "there is
            # nothing here yet".
            "avg_syllabus_completion": (
                round(sum(b["percent"] for b in active_batches) / len(active_batches))
                if active_batches else None
            ),
            "students": sum(b["seats_taken"] for b in active_batches),
            "avg_quiz_score": _avg_quiz_score(user),
        }

        return Response({"groups": out, "stats": stats})


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
            Subject.objects.select_related("course__board"), id=subject_id)

        if not (request.user.is_staff or teaches_subject(request.user, subject)):
            return Response(
                {"detail": "You are not assigned to this subject."},
                status=403,
            )

        batches = (
            Batch.objects.filter(course_id=subject.course_id, is_active=True)
            .order_by("-year", "code")
        )
        # Every batch here belongs to subject.course, so these two are constant
        # across the response — repeated per row because this feeds <select>
        # options that are rendered one at a time, and "Batch A" under an
        # unnamed course tells a teacher nothing about which board they picked.
        course_title = subject.course.title if subject.course_id else None
        board = board_name_for(subject.course if subject.course_id else None)
        return Response([
            {
                "id": str(b.id),
                "name": b.name,
                "code": b.code,
                "year": b.year,
                "course_title": course_title,
                "board_name": board,
            }
            for b in batches
        ])
