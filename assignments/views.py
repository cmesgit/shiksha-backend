from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Prefetch, Count, Case, When, Value, CharField, Q
from django.db import IntegrityError
from django.http import HttpResponse

from courses.models import Subject, TeachingAssignment
from courses.services import teaches_subject
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import MultiPartParser, FormParser

from enrollments.models import Enrollment

from courses.models import Chapter
from accounts.permissions import require_teacher_context, IsTeacherContext, _in_teacher_context
from accounts.auth_flow import get_active_profile

from .models import Assignment, AssignmentFile, AssignmentSubmission
from .serializers import (
    AssignmentListSerializer,
    AssignmentDetailSerializer,
    TeacherAssignmentCreateSerializer,
    TeacherAssignmentUpdateSerializer,
    TeacherAssignmentListSerializer,
    TeacherSubmissionListSerializer,
    AssignmentFileSerializer,
)

import zipfile
from io import BytesIO


# ==========================================
# HELPER
# ==========================================

def _assert_teacher_owns_assignment(user, assignment):
    """Raises PermissionDenied if the teacher is not assigned to the subject."""
    if not teaches_subject(user, assignment.chapter.subject):
        raise PermissionDenied("Not assigned to this subject.")


# ==========================================
# ASSIGNMENT DETAIL VIEW
# ==========================================

class AssignmentDetailView(generics.RetrieveAPIView):
    serializer_class = AssignmentDetailSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "id"
    lookup_url_kwarg = "assignment_id"

    def get_queryset(self):
        # The "already submitted" card must reflect the ACTIVE LEARNER
        # PROFILE, not any submission by the account. In teacher context
        # (no active profile) the prefetch is empty, which is correct —
        # teachers read submissions through their own roster views.
        learner = get_active_profile(self.request)
        submission_prefetch = Prefetch(
            "submissions",
            queryset=AssignmentSubmission.objects.filter(learner_profile=learner)
            if learner is not None
            else AssignmentSubmission.objects.none(),
            to_attr="user_submission_list",
        )
        return (
            Assignment.objects
            .select_related("chapter__subject__course")
            .prefetch_related(submission_prefetch, "files")
        )

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        user = request.user
        subject = instance.chapter.subject
        course = subject.course

        instance.user_submission = (
            instance.user_submission_list[0]
            if instance.user_submission_list else None
        )

        if _in_teacher_context(request):
            _assert_teacher_owns_assignment(user, instance)
        else:
            from enrollments.services import has_active_subscription, lock_payload
            from accounts.auth_flow import get_active_profile

            learner = get_active_profile(request)
            if learner is None:
                return Response(
                    {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                    status=403,
                )
            if not has_active_subscription(user=user, course=course, learner_profile=learner):
                return Response(
                    lock_payload(user=user, course=course, learner_profile=learner),
                    status=402,
                )

        serializer = self.get_serializer(instance)
        return Response(serializer.data)


# ==========================================
# SUBMIT ASSIGNMENT VIEW
# ==========================================

class SubmitAssignmentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, assignment_id):
        assignment = get_object_or_404(
            Assignment.objects.select_related("chapter__subject__course"),
            id=assignment_id,
        )

        from enrollments.services import has_active_subscription, lock_payload
        from accounts.auth_flow import get_active_profile

        course = assignment.chapter.subject.course
        learner = get_active_profile(request)
        if learner is None:
            return Response(
                {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                status=403,
            )
        if not has_active_subscription(user=request.user, course=course, learner_profile=learner):
            return Response(
                lock_payload(user=request.user, course=course, learner_profile=learner),
                status=402,
            )

        file = request.FILES.get("file")
        if not file:
            return Response({"detail": "File required."}, status=status.HTTP_400_BAD_REQUEST)

        # Keyed on (assignment, learner_profile): re-submitting replaces only
        # THIS learner's file. Previously the key was (assignment, account),
        # so one child's upload silently overwrote a sibling's.
        AssignmentSubmission.objects.update_or_create(
            assignment=assignment,
            learner_profile=learner,
            defaults={"submitted_file": file, "student": request.user},
        )

        return Response({"detail": "Submission successful."}, status=status.HTTP_200_OK)


# ==========================================
# COURSE ASSIGNMENTS LIST VIEW
# ==========================================

class CourseAssignmentsView(generics.ListAPIView):
    serializer_class = AssignmentListSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        course_id = self.kwargs["course_id"]
        user = self.request.user
        learner = get_active_profile(self.request)

        submission_prefetch = Prefetch(
            "submissions",
            queryset=AssignmentSubmission.objects.filter(learner_profile=learner)
            if learner is not None
            else AssignmentSubmission.objects.none(),
            to_attr="user_submission_list",
        )

        if _in_teacher_context(self.request):
            queryset = Assignment.objects.filter(
                chapter__subject__course__id=course_id,
                chapter__subject__teaching_assignments__teacher=user,
                chapter__subject__teaching_assignments__is_active=True,
            )
        else:
            from courses.models import Course
            from enrollments.services import has_active_subscription

            try:
                course_obj = Course.objects.get(pk=course_id)
            except Course.DoesNotExist:
                raise PermissionDenied("Course not found.")
            if not has_active_subscription(user=user, course=course_obj, learner_profile=learner):
                raise PermissionDenied("Your subscription for this course has expired.")
            queryset = Assignment.objects.filter(
                chapter__subject__course__id=course_id)
            # Batch isolation: show course-wide assignments (batch IS NULL) plus
            # this student's own batch's assignments. Due dates are cohort-
            # relative, so a later batch must not inherit an earlier one's.
            # The batch is the ACTIVE PROFILE's — two children in the same
            # course can sit in different batches.
            enrollment = Enrollment.objects.filter(
                learner_profile=learner, course_id=course_id,
                status=Enrollment.STATUS_ACTIVE,
            ).first()
            batch_id = enrollment.batch_id if enrollment else None
            # No batch on the enrollment (e.g. self-enrolled, not yet placed
            # by an admin) means we can't tell which cohort's assignments
            # apply — show every assignment in the course rather than only
            # the course-wide (batch IS NULL) ones, which would otherwise
            # hide every batch-scoped assignment from most students.
            if batch_id is not None:
                queryset = queryset.filter(
                    Q(batch__isnull=True) | Q(batch_id=batch_id))

        return (
            queryset
            .select_related("chapter__subject__course")
            .prefetch_related(submission_prefetch)
            .distinct()
        )

    def list(self, request, *args, **kwargs):
        queryset = list(self.get_queryset())
        for obj in queryset:
            obj.user_submission = (
                obj.user_submission_list[0] if obj.user_submission_list else None
            )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


# ==========================================
# TEACHER CREATE ASSIGNMENT VIEW
# — Idempotency guard prevents double-submit
# ==========================================

class TeacherCreateAssignmentView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        user = request.user

        require_teacher_context(request)

        # ── Idempotency check ────────────────────────────────────────
        # Frontend sends a per-session UUID so accidental double-clicks
        # return the existing assignment rather than creating a second one.
        idempotency_key = request.data.get(
            "idempotency_key") or request.headers.get("X-Idempotency-Key")

        if idempotency_key:
            existing = Assignment.objects.filter(
                idempotency_key=idempotency_key).first()
            if existing:
                return Response(
                    {
                        "message": "Assignment already created.",
                        "id": str(existing.id),
                        "duplicate": True,
                    },
                    status=status.HTTP_200_OK,
                )

        serializer = TeacherAssignmentCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        try:
            assignment = serializer.save()
        except IntegrityError:
            # Race condition: two requests with same key hit simultaneously
            existing = Assignment.objects.filter(
                idempotency_key=idempotency_key).first()
            if existing:
                return Response(
                    {"message": "Assignment already created.",
                        "id": str(existing.id), "duplicate": True},
                    status=status.HTTP_200_OK,
                )
            raise

        # Handle additional uploaded files (multi-file support)
        extra_files = request.FILES.getlist("files")
        for f in extra_files:
            AssignmentFile.objects.create(
                assignment=assignment,
                file=f,
                original_filename=f.name,
            )

        return Response(
            {"message": "Assignment created successfully",
                "id": str(assignment.id)},
            status=status.HTTP_201_CREATED,
        )


# ==========================================
# TEACHER UPDATE ASSIGNMENT VIEW
# ==========================================

class TeacherUpdateAssignmentView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def patch(self, request, assignment_id):
        user = request.user

        require_teacher_context(request)

        assignment = get_object_or_404(
            Assignment.objects.select_related(
                "chapter__subject").prefetch_related("files"),
            id=assignment_id,
        )

        _assert_teacher_owns_assignment(user, assignment)

        if assignment.due_date < timezone.now():
            return Response(
                {"detail": "Cannot edit an expired assignment."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Merge uploaded files list into validated data manually because
        # DRF's ListField doesn't auto-grab from request.FILES.getlist().
        data = request.data.copy()
        new_files = request.FILES.getlist("new_files")

        serializer = TeacherAssignmentUpdateSerializer(
            assignment, data=data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        # Inject the file list after validation so the serializer's
        # update() method receives them.
        serializer.validated_data["new_files"] = new_files

        updated = serializer.save()

        return Response(
            {
                "message": "Assignment updated successfully",
                "data": TeacherAssignmentListSerializer(
                    updated, context={"request": request}
                ).data,
            }
        )


# ==========================================
# TEACHER DELETE SINGLE FILE VIEW
# DELETE /assignments/teacher/<assignment_id>/files/<file_id>/
# ==========================================

class TeacherDeleteAssignmentFileView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, assignment_id, file_id):
        user = request.user

        require_teacher_context(request)

        assignment = get_object_or_404(
            Assignment.objects.select_related("chapter__subject"),
            id=assignment_id,
        )

        _assert_teacher_owns_assignment(user, assignment)

        file_obj = get_object_or_404(
            AssignmentFile, id=file_id, assignment=assignment)
        file_obj.file.delete(save=False)  # remove from storage
        file_obj.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


# ==========================================
# TEACHER DELETE ASSIGNMENT VIEW
# ==========================================

class TeacherDeleteAssignmentView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, assignment_id):
        user = request.user

        require_teacher_context(request)

        assignment = get_object_or_404(
            Assignment.objects.select_related("chapter__subject"),
            id=assignment_id,
        )

        _assert_teacher_owns_assignment(user, assignment)

        if assignment.submissions.exists():
            return Response(
                {"detail": "Cannot delete an assignment that already has submissions."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        assignment.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ==========================================
# TEACHER SUBJECT ASSIGNMENTS VIEW
# ==========================================

class TeacherSubjectAssignmentsView(generics.ListAPIView):
    serializer_class = TeacherAssignmentListSerializer
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def get_queryset(self):
        user = self.request.user
        subject_id = self.kwargs["subject_id"]

        subject = get_object_or_404(Subject, id=subject_id)

        if not teaches_subject(user, subject):
            raise PermissionDenied("Not assigned to this subject.")

        return (
            Assignment.objects
            .filter(chapter__subject=subject)
            .select_related("chapter__subject", "batch")
            .prefetch_related("files")
            .annotate(total_submissions=Count("submissions", distinct=True))
            .order_by("-created_at")
        )


# ==========================================
# TEACHER — ALL ASSIGNMENTS ACROSS THEIR SUBJECTS
# ==========================================

class TeacherAllAssignmentsView(generics.ListAPIView):
    """Every assignment across every subject this teacher is assigned to.

    The faculty Assignments screen is one flat, subject-filtered list (design
    handoff screen 11). Before this existed the frontend called
    TeacherSubjectAssignmentsView once per subject and flattened client-side —
    an N+1 that grew with the teacher's timetable.

    Scope is the same as the per-subject view's permission check, expressed as
    a filter instead: subjects with an active TeachingAssignment for this user.
    """

    serializer_class = TeacherAssignmentListSerializer
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def get_queryset(self):
        return (
            Assignment.objects
            .filter(
                chapter__subject__teaching_assignments__teacher=self.request.user,
                chapter__subject__teaching_assignments__is_active=True,
            )
            # chapter__subject + batch: the serializer reports subject_id/
            # subject_name and batch_id/batch_name off these.
            .select_related("chapter__subject", "batch")
            .prefetch_related("files")
            .annotate(total_submissions=Count("submissions", distinct=True))
            # distinct(): a teacher listed twice on one subject would otherwise
            # duplicate every assignment on it.
            .distinct()
            .order_by("-created_at")
        )


# ==========================================
# TEACHER ASSIGNMENT SUBMISSIONS VIEW
# ==========================================

class TeacherAssignmentSubmissionsView(generics.ListAPIView):
    serializer_class = TeacherSubmissionListSerializer
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def get_queryset(self):
        user = self.request.user
        assignment_id = self.kwargs["assignment_id"]

        assignment = get_object_or_404(
            Assignment.objects.select_related("chapter__subject"),
            id=assignment_id,
        )

        _assert_teacher_owns_assignment(user, assignment)

        return (
            AssignmentSubmission.objects
            .filter(assignment=assignment)
            .select_related("student", "learner_profile", "assignment")
            .annotate(
                submission_status=Case(
                    When(submitted_at__gt=assignment.due_date, then=Value("Late")),
                    default=Value("On time"),
                    output_field=CharField(),
                )
            )
            .order_by("-submitted_at")
        )


# ==========================================
# SUBJECT ASSIGNMENTS VIEW (STUDENT)
# ==========================================

class SubjectAssignmentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        user = request.user
        learner = get_active_profile(request)

        subject = get_object_or_404(
            Subject.objects.select_related("course"), id=subject_id)

        from enrollments.services import has_active_subscription

        if learner is None:
            return Response(
                {"detail": "Select a learner profile.", "lock_reason": "no_learner_profile"},
                status=403,
            )
        if not has_active_subscription(user=user, course=subject.course, learner_profile=learner):
            raise PermissionDenied("Your subscription for this course has expired.")

        submission_prefetch = Prefetch(
            "submissions",
            queryset=AssignmentSubmission.objects.filter(learner_profile=learner)
            if learner is not None
            else AssignmentSubmission.objects.none(),
            to_attr="user_submission_list",
        )

        teacher_prefetch = Prefetch(
            "chapter__subject__teaching_assignments",
            queryset=TeachingAssignment.objects.filter(
                batch__isnull=True, is_active=True,
            ).select_related("teacher").order_by("order"),
            to_attr="prefetched_teachers",
        )

        assignments = (
            Assignment.objects
            .filter(chapter__subject_id=subject_id)
            .select_related("chapter__subject")
            .prefetch_related(submission_prefetch, teacher_prefetch, "files")
        )

        # Same batch isolation CourseAssignmentsView enforces: course-wide
        # assignments (batch IS NULL) plus this student's own batch. No batch
        # on the enrollment (not yet placed by an admin) shows every
        # assignment in the subject, matching CourseAssignmentsView's
        # degrade-to-everything behavior for an unplaced student.
        enrollment = Enrollment.objects.filter(
            learner_profile=learner, course_id=subject.course_id,
            status=Enrollment.STATUS_ACTIVE,
        ).first()
        batch_id = enrollment.batch_id if enrollment else None
        if batch_id is not None:
            assignments = assignments.filter(
                Q(batch__isnull=True) | Q(batch_id=batch_id))

        data = []
        for assignment in assignments:
            submission = (
                assignment.user_submission_list[0]
                if assignment.user_submission_list else None
            )

            teachers = assignment.chapter.subject.prefetched_teachers
            teacher_name = (
                (lambda p: p.full_name if p else None)(teachers[0].teacher.default_learner_profile()) if teachers else None if teachers else None
            )

            data.append({
                "id": assignment.id,
                "title": assignment.title,
                "due_date": assignment.due_date,
                "status": "SUBMITTED" if submission else "PENDING",
                "subject": assignment.chapter.subject.name,
                "chapter": assignment.chapter.title,
                "teacher": teacher_name,
            })

        return Response(data)


# ==========================================
# DOWNLOAD ALL SUBMISSIONS VIEW
# ==========================================

class DownloadAllSubmissionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, assignment_id):
        user = request.user

        require_teacher_context(request)

        assignment = get_object_or_404(
            Assignment.objects.select_related("chapter__subject"),
            id=assignment_id,
        )

        _assert_teacher_owns_assignment(user, assignment)

        submissions = (
            AssignmentSubmission.objects
            .filter(assignment=assignment)
            .select_related("student", "learner_profile")
        )

        buffer = BytesIO()
        used_names = set()
        with zipfile.ZipFile(buffer, "w") as zf:
            for sub in submissions:
                if sub.submitted_file:
                    # Name by the learner who submitted it — on a shared
                    # family account the default profile would mislabel a
                    # sibling's work.
                    lp = sub.learner_profile or sub.student.default_learner_profile()
                    student_name = (
                        (lp.full_name or lp.display_name) if lp else sub.student.email
                    ) or sub.student.email
                    filename = f"{student_name}_{sub.submitted_file.name.split('/')[-1]}"
                    # Two learners can now legitimately upload files with the
                    # same name; ZipFile silently accepts duplicate entries,
                    # so dedupe explicitly.
                    if filename in used_names:
                        stem, dot, ext = filename.rpartition(".")
                        base = stem if dot else filename
                        suffix = f".{ext}" if dot else ""
                        n = 2
                        while f"{base}_{n}{suffix}" in used_names:
                            n += 1
                        filename = f"{base}_{n}{suffix}"
                    used_names.add(filename)
                    zf.writestr(filename, sub.submitted_file.read())

        response = HttpResponse(
            buffer.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = (
            f'attachment; filename="{assignment.title}_submissions.zip"'
        )
        return response
