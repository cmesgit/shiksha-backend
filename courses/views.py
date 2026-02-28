from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from enrollments.models import Enrollment
from accounts.permissions import IsTeacher
from quizzes.models import Quiz
from assignments.models import Assignment
from .models import Course
from .serializers import CourseSerializer
from .models import Course, Subject
from .serializers import CourseSerializer, SubjectSerializer
from django.db.models import Count, Q

# update
from django.shortcuts import get_object_or_404


# Create Course (Teacher Only)


class CreateCourseView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def post(self, request):
        serializer = CourseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        course = serializer.save(teacher=request.user)

        return Response(
            CourseSerializer(course).data,
            status=status.HTTP_201_CREATED,
        )


# LIST OWN COURSES
class MyCoursesView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def get(self, request):
        courses = Course.objects.filter(teacher=request.user)
        serializer = CourseSerializer(courses, many=True)
        return Response(serializer.data)

# new


class UpdateCourseView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def patch(self, request, course_id):
        course = get_object_or_404(
            Course,
            id=course_id,
            teacher=request.user,  # 🔐 ownership enforced
        )

        serializer = CourseSerializer(
            course,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(serializer.data)


class DeleteCourseView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def delete(self, request, course_id):
        course = get_object_or_404(
            Course,
            id=course_id,
            teacher=request.user,  # 🔐 ownership enforced
        )

        course.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MyEnrolledCoursesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        enrollments = (
            Enrollment.objects
            .filter(user=request.user, status="ACTIVE")
            .select_related("course")
        )

        courses = [enrollment.course for enrollment in enrollments]

        serializer = CourseSerializer(courses, many=True)
        return Response(serializer.data)


class CourseSubjectsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, course_id):
        # Ensure student is enrolled
        is_enrolled = Enrollment.objects.filter(
            user=request.user,
            course__id=course_id,
            status="ACTIVE"
        ).exists()

        if not is_enrolled:
            return Response({"detail": "Not enrolled in this course."}, status=403)

        subjects = Subject.objects.filter(
            course__id=course_id).order_by("order")

        serializer = SubjectSerializer(subjects, many=True)
        return Response(serializer.data)


class SubjectDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        subject = get_object_or_404(
            Subject.objects.prefetch_related(
                "subject_teachers__teacher__teacher_profile"
            ),
            id=subject_id
        )

        serializer = SubjectSerializer(subject)
        return Response(serializer.data)


class SubjectDashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        user = request.user

        subject = get_object_or_404(
            Subject.objects.prefetch_related(
                "subject_teachers__teacher__teacher_profile"
            ),
            id=subject_id
        )

        # 🔒 Enrollment check
        if not Enrollment.objects.filter(
            user=user,
            course=subject.course,
            status="ACTIVE"
        ).exists():
            return Response(
                {"detail": "Not enrolled."},
                status=403
            )

        # -----------------------------
        # ASSIGNMENTS
        # -----------------------------
        assignments = Assignment.objects.filter(
            chapter__subject=subject
        )

        pending_assignments = assignments.filter(
            due_date__gt=timezone.now()
        ).exclude(
            submissions__student=user
        ).count()

        completed_assignments = assignments.filter(
            submissions__student=user
        ).count()

        total_assignments = assignments.count()

        # -----------------------------
        # QUIZZES
        # -----------------------------
        quizzes = Quiz.objects.filter(
            subject=subject,
            is_published=True
        )

        pending_quiz = quizzes.exclude(
            attempts__student=user,
            attempts__status="SUBMITTED"
        ).count()

        completed_quiz = quizzes.filter(
            attempts__student=user,
            attempts__status="SUBMITTED"
        ).count()

        total_quiz = quizzes.count()

        # -----------------------------
        # RESPONSE
        # -----------------------------
        serializer = SubjectSerializer(subject)

        return Response({
            "id": subject.id,
            "name": subject.name,
            "teachers": serializer.data["teachers"],

            "assignments": {
                "pending": pending_assignments,
                "completed": completed_assignments,
                "total": total_assignments,
            },

            "quizzes": {
                "pending": pending_quiz,
                "completed": completed_quiz,
                "total": total_quiz,
            },

            # placeholder until you add models
            "recordingsCount": 0,
            "studyMaterialsCount": 0,
            "upcomingSessions": [],
        })
