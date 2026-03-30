from .serializers import ChapterSerializer
from .models import Chapter
from django.db.models import Count
from .models import SubjectTeacher
from accounts.models import Role
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
from django.utils import timezone
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
                "subject_teachers__teacher"
            ),
            id=subject_id
        )

        # =========================
        # TEACHER ACCESS
        # =========================
        if user.has_role("TEACHER"):
            is_assigned = subject.subject_teachers.filter(
                teacher=user
            ).exists()

            if not is_assigned:
                return Response(
                    {"detail": "Not assigned to this subject."},
                    status=status.HTTP_403_FORBIDDEN
                )

        # =========================
        # STUDENT ACCESS
        # =========================
        else:
            if not Enrollment.objects.filter(
                user=user,
                course=subject.course,
                status=Enrollment.STATUS_ACTIVE
            ).exists():
                return Response(
                    {"detail": "Not enrolled."},
                    status=status.HTTP_403_FORBIDDEN
                )

        # =========================
        # ASSIGNMENTS
        # =========================

        assignments = Assignment.objects.filter(
            chapter__subject=subject
        ).distinct()

        total_assignments = assignments.count()

        # For teachers we don't calculate completed/pending
        if user.has_role("STUDENT"):
            completed_assignments = assignments.filter(
                submissions__student=user
            ).distinct().count()
            pending_assignments = total_assignments - completed_assignments
        else:
            completed_assignments = 0
            pending_assignments = total_assignments

        # =========================
        # QUIZZES
        # =========================

        quizzes = Quiz.objects.filter(
            subject=subject,
            is_published=True
        ).distinct()

        total_quizzes = quizzes.count()

        if user.has_role("STUDENT"):
            completed_quizzes = quizzes.filter(
                attempts__student=user,
                attempts__status="SUBMITTED"
            ).distinct().count()
            pending_quizzes = total_quizzes - completed_quizzes
        else:
            completed_quizzes = 0
            pending_quizzes = total_quizzes

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
                "pending": pending_quizzes,
                "completed": completed_quizzes,
                "total": total_quizzes,
            },

            "recordingsCount": 0,
            "studyMaterialsCount": 0,
            "upcomingSessions": [],

            "studentsCount": Enrollment.objects.filter(
                course=subject.course,
                status=Enrollment.STATUS_ACTIVE,
            ).count(),
        })


class TeacherMyClassesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        # 🔐 Only teachers
        if not user.has_role(Role.TEACHER):
            return Response(
                {"detail": "Only teachers allowed."},
                status=status.HTTP_403_FORBIDDEN
            )

        subjects = (
            Subject.objects
            .filter(subject_teachers__teacher=user)
            .select_related("course")
            .distinct()
        )

        response_data = []

        for subject in subjects:
            # Count active students in that course
            students_count = Enrollment.objects.filter(
                course=subject.course,
                status=Enrollment.STATUS_ACTIVE
            ).count()

            response_data.append({
                "subject_id": str(subject.id),
                "subject_name": subject.name,
                "course_id": str(subject.course.id),
                "course_title": subject.course.title,
                "students_count": students_count,
            })

        return Response(response_data)


class SubjectChaptersView(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):

        chapters = Chapter.objects.filter(
            subject_id=subject_id
        ).order_by("order")

        serializer = ChapterSerializer(chapters, many=True)

        return Response(serializer.data)


# =====================================================
# TEACHER — STUDENTS LIST FOR A SUBJECT
# =====================================================

class SubjectStudentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        user = request.user

        if not user.has_role(Role.TEACHER):
            return Response(
                {"detail": "Only teachers allowed."},
                status=status.HTTP_403_FORBIDDEN,
            )

        subject = get_object_or_404(Subject, id=subject_id)

        # Verify teacher is assigned to this subject
        if not SubjectTeacher.objects.filter(
            subject=subject, teacher=user
        ).exists():
            return Response(
                {"detail": "You are not assigned to this subject."},
                status=status.HTTP_403_FORBIDDEN,
            )

        enrollments = (
            Enrollment.objects.filter(
                course=subject.course,
                status=Enrollment.STATUS_ACTIVE,
            )
            .select_related("user", "user__profile")
            .order_by("user__profile__full_name")
        )

        students = []
        for enrollment in enrollments:
            u = enrollment.user
            profile = getattr(u, "profile", None)

            students.append({
                "id": str(u.id),
                "email": u.email,
                "username": u.username,
                "full_name": profile.full_name if profile else "",
                "phone": profile.phone if profile else "",
                "student_id": profile.student_id if profile else "",
                "avatar_type": profile.avatar_type() if profile else None,
                "avatar": profile.avatar_value() if profile else None,
                "enrolled_at": enrollment.enrolled_at,
                "batch_code": enrollment.batch_code or "",
            })

        return Response({
            "subject_name": subject.name,
            "course_title": subject.course.title,
            "total_students": len(students),
            "students": students,
        })
