from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Prefetch
from django.db.models import Count
from django.db.models import Case, When, Value, CharField
from courses.models import Subject, SubjectTeacher
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import ListAPIView
from enrollments.models import Enrollment
from enrollments.permissions import IsEnrolledInCourse

from courses.models import Chapter
from accounts.models import Role
from .models import Assignment
from .serializers import TeacherAssignmentCreateSerializer

from django.http import HttpResponse
import zipfile
from io import BytesIO

from .models import AssignmentSubmission
from .serializers import (
    AssignmentListSerializer,
    AssignmentDetailSerializer,
    TeacherAssignmentCreateSerializer,
    TeacherAssignmentUpdateSerializer,
    TeacherAssignmentListSerializer,
    TeacherSubmissionListSerializer
)


# ==========================================
# ASSIGNMENT DETAIL VIEW
# ==========================================

class AssignmentDetailView(generics.RetrieveAPIView):
    serializer_class = AssignmentDetailSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "id"
    lookup_url_kwarg = "assignment_id"

    def get_queryset(self):
        user = self.request.user

        submission_prefetch = Prefetch(
            "submissions",
            queryset=AssignmentSubmission.objects.filter(student=user),
            to_attr="user_submission_list",
        )

        queryset = (
            Assignment.objects
            .select_related("chapter__subject__course")
            .prefetch_related(submission_prefetch)
        )

        return queryset

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        user = request.user
        subject = instance.chapter.subject
        course = subject.course

        instance.user_submission = (
            instance.user_submission_list[0]
            if instance.user_submission_list else None
        )

        if user.has_role(Role.TEACHER):
            if not subject.subject_teachers.filter(teacher=user).exists():
                raise PermissionDenied("Not assigned to this subject.")
        else:
            if not Enrollment.objects.filter(
                user=user,
                course=course,
                status=Enrollment.STATUS_ACTIVE
            ).exists():
                raise PermissionDenied("Not authorized.")

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

        if not request.user.has_role(Role.STUDENT):
            return Response(
                {"detail": "Only students can submit assignments."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not Enrollment.objects.filter(
            user=request.user,
            course=assignment.chapter.subject.course,
            status=Enrollment.STATUS_ACTIVE
        ).exists():
            return Response(
                {"detail": "Not authorized."},
                status=status.HTTP_403_FORBIDDEN,
            )

        file = request.FILES.get("file")
        if not file:
            return Response(
                {"detail": "File required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        AssignmentSubmission.objects.update_or_create(
            assignment=assignment,
            student=request.user,
            defaults={"submitted_file": file},
        )

        return Response(
            {"detail": "Submission successful."},
            status=status.HTTP_200_OK,
        )


# ==========================================
# COURSE ASSIGNMENTS LIST VIEW
# ==========================================

class CourseAssignmentsView(generics.ListAPIView):
    serializer_class = AssignmentListSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        course_id = self.kwargs["course_id"]
        user = self.request.user

        submission_prefetch = Prefetch(
            "submissions",
            queryset=AssignmentSubmission.objects.filter(student=user),
            to_attr="user_submission_list",
        )

        if user.has_role(Role.TEACHER):
            queryset = Assignment.objects.filter(
                chapter__subject__course__id=course_id,
                chapter__subject__subject_teachers__teacher=user
            )
        else:
            if not Enrollment.objects.filter(
                user=user,
                course_id=course_id,
                status=Enrollment.STATUS_ACTIVE
            ).exists():
                raise PermissionDenied("Not enrolled.")

            queryset = Assignment.objects.filter(
                chapter__subject__course__id=course_id
            )

        queryset = queryset.select_related(
            "chapter__subject__course"
        ).prefetch_related(submission_prefetch).distinct()

        return queryset

    def list(self, request, *args, **kwargs):
        queryset = list(self.get_queryset())

        for obj in queryset:
            obj.user_submission = (
                obj.user_submission_list[0]
                if obj.user_submission_list else None
            )

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


# ==========================================
# TEACHER CREATE ASSIGNMENT VIEW
# ==========================================

class TeacherCreateAssignmentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user

        if not user.has_role(Role.TEACHER):
            return Response(
                {"detail": "Only teachers allowed."},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = TeacherAssignmentCreateSerializer(
            data=request.data,
            context={"request": request}
        )

        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {"message": "Assignment created successfully"},
            status=status.HTTP_201_CREATED
        )


# ==========================================
# TEACHER UPDATE ASSIGNMENT VIEW
# ==========================================

class TeacherUpdateAssignmentView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, assignment_id):
        user = request.user

        if not user.has_role(Role.TEACHER):
            raise PermissionDenied("Only teachers allowed.")

        assignment = get_object_or_404(
            Assignment.objects.select_related("chapter__subject"),
            id=assignment_id
        )

        subject = assignment.chapter.subject

        if not subject.subject_teachers.filter(teacher=user).exists():
            raise PermissionDenied("Not assigned to this subject.")

        if assignment.due_date < timezone.now():
            return Response(
                {"detail": "Cannot edit expired assignment."},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = TeacherAssignmentUpdateSerializer(
            assignment,
            data=request.data,
            partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {
                "message": "Assignment updated successfully",
                "data": TeacherAssignmentListSerializer(assignment).data
            }
        )


# ==========================================
# TEACHER DELETE ASSIGNMENT VIEW
# ==========================================

class TeacherDeleteAssignmentView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, assignment_id):
        user = request.user

        if not user.has_role(Role.TEACHER):
            raise PermissionDenied("Only teachers allowed.")

        assignment = get_object_or_404(
            Assignment.objects.select_related("chapter__subject"),
            id=assignment_id
        )

        subject = assignment.chapter.subject

        if not subject.subject_teachers.filter(teacher=user).exists():
            raise PermissionDenied("Not assigned to this subject.")

        if assignment.submissions.exists():
            return Response(
                {"detail": "Cannot delete assignment with submissions."},
                status=status.HTTP_400_BAD_REQUEST
            )

        assignment.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


# ==========================================
# TEACHER SUBJECT ASSIGNMENTS VIEW
# ==========================================

class TeacherSubjectAssignmentsView(generics.ListAPIView):
    serializer_class = TeacherAssignmentListSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        subject_id = self.kwargs["subject_id"]

        if not user.has_role(Role.TEACHER):
            raise PermissionDenied("Only teachers allowed.")

        subject = get_object_or_404(Subject, id=subject_id)

        if not subject.subject_teachers.filter(teacher=user).exists():
            raise PermissionDenied("Not assigned to this subject.")

        return (
            Assignment.objects
            .filter(chapter__subject=subject)
            .select_related("chapter")
            .annotate(
                total_submissions=Count("submissions", distinct=True)
            )
            .order_by("-created_at")
        )


# ==========================================
# TEACHER ASSIGNMENT SUBMISSIONS VIEW
# ==========================================

class TeacherAssignmentSubmissionsView(generics.ListAPIView):
    serializer_class = TeacherSubmissionListSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        assignment_id = self.kwargs["assignment_id"]

        if not user.has_role(Role.TEACHER):
            raise PermissionDenied("Only teachers allowed.")

        assignment = get_object_or_404(
            Assignment.objects.select_related("chapter__subject"),
            id=assignment_id
        )

        if not assignment.chapter.subject.subject_teachers.filter(
            teacher=user
        ).exists():
            raise PermissionDenied("Not assigned to this subject.")

        return (
            AssignmentSubmission.objects
            .filter(assignment=assignment)
            .select_related("student", "student__profile", "assignment")
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

        submission_prefetch = Prefetch(
            "submissions",
            queryset=AssignmentSubmission.objects.filter(student=user),
            to_attr="user_submission_list",
        )

        teacher_prefetch = Prefetch(
            "chapter__subject__subject_teachers",
            queryset=SubjectTeacher.objects.select_related(
                "teacher__profile"
            ).order_by("order"),
            to_attr="prefetched_teachers",
        )

        assignments = (
            Assignment.objects
            .filter(chapter__subject_id=subject_id)
            .select_related("chapter__subject")
            .prefetch_related(submission_prefetch, teacher_prefetch)
        )

        data = []

        for assignment in assignments:
            submission = (
                assignment.user_submission_list[0]
                if assignment.user_submission_list else None
            )

            sub_status = "SUBMITTED" if submission else "PENDING"

            teachers = assignment.chapter.subject.prefetched_teachers
            teacher_name = (
                teachers[0].teacher.profile.full_name
                if teachers else None
            )

            data.append({
                "id": assignment.id,
                "title": assignment.title,
                "due_date": assignment.due_date,
                "status": sub_status,
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

        if not user.has_role(Role.TEACHER):
            raise PermissionDenied("Only teachers allowed.")

        assignment = get_object_or_404(
            Assignment.objects.select_related("chapter__subject"),
            id=assignment_id
        )

        if not assignment.chapter.subject.subject_teachers.filter(
            teacher=user
        ).exists():
            raise PermissionDenied("Not assigned.")

        submissions = AssignmentSubmission.objects.filter(
            assignment=assignment
        ).select_related("student__profile")

        buffer = BytesIO()
        zip_file = zipfile.ZipFile(buffer, "w")

        for sub in submissions:
            if sub.submitted_file:
                filename = f"{sub.student.profile.full_name}_{sub.submitted_file.name.split('/')[-1]}"
                zip_file.writestr(filename, sub.submitted_file.read())

        zip_file.close()

        response = HttpResponse(
            buffer.getvalue(),
            content_type="application/zip"
        )
        response["Content-Disposition"] = "attachment; filename=submissions.zip"
        return response
