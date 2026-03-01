from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Prefetch
from django.db.models import Count
from courses.models import Subject
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

        # Attach user_submission
        for obj in queryset:
            obj.user_submission = (
                obj.user_submission_list[0]
                if obj.user_submission_list else None
            )

        return queryset

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        user = request.user
        subject = instance.chapter.subject
        course = subject.course

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

        return super().retrieve(request, *args, **kwargs)

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

        if assignment.due_date < timezone.now():
            return Response(
                {"detail": "Assignment expired."},
                status=status.HTTP_400_BAD_REQUEST,
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

        for obj in queryset:
            obj.user_submission = (
                obj.user_submission_list[0]
                if obj.user_submission_list else None
            )

        return queryset


class TeacherCreateAssignmentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user

        if not user.has_role(Role.TEACHER):
            return Response(
                {"detail": "Only teachers allowed."},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = TeacherAssignmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        chapter = get_object_or_404(
            Chapter.objects.select_related("subject"),
            id=serializer.validated_data["chapter_id"]
        )

        if not chapter.subject.subject_teachers.filter(
            teacher=user
        ).exists():
            raise PermissionDenied("Not assigned to this subject.")

        assignment = serializer.save()

        return Response(
            TeacherAssignmentListSerializer(assignment).data,
            status=status.HTTP_201_CREATED
        )


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
            TeacherAssignmentListSerializer(assignment).data
        )


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
            .annotate(total_submissions=Count("submissions"))
            .order_by("-created_at")
        )


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
            .select_related("student", "student__profile")
            .order_by("-submitted_at")
        )
