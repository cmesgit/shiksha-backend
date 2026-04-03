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
from django.shortcuts import get_object_or_404
from datetime import timedelta


# =========================
# CREATE COURSE
# =========================

class CreateCourseView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def post(self, request):
        serializer = CourseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        course = serializer.save()  # ✅ FIX

        return Response(
            CourseSerializer(course).data,  # ✅ FIX
            status=status.HTTP_201_CREATED,
        )


# =========================
# LIST OWN COURSES
# =========================

class MyCoursesView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def get(self, request):
        courses = Course.objects.filter(
            subjects__subject_teachers__teacher=request.user
        ).select_related("board").distinct()  # ✅ FIX

        serializer = CourseSerializer(courses, many=True)
        return Response(serializer.data)


# =========================
# UPDATE COURSE
# =========================

class UpdateCourseView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def patch(self, request, course_id):
        course = get_object_or_404(
            Course.objects.filter(
                subjects__subject_teachers__teacher=request.user
            ).distinct(),  # ✅ FIX
            id=course_id,
        )

        serializer = CourseSerializer(
            course,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(serializer.data)


# =========================
# DELETE COURSE
# =========================

class DeleteCourseView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def delete(self, request, course_id):
        course = get_object_or_404(
            Course.objects.filter(
                subjects__subject_teachers__teacher=request.user
            ).distinct(),  # ✅ FIX
            id=course_id,
        )

        course.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# =========================
# ENROLLED COURSES
# =========================

class MyEnrolledCoursesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        enrollments = (
            Enrollment.objects
            .filter(user=request.user, status="ACTIVE")
            .select_related("course__board")  # ✅ OPTIMIZED
        )

        courses = [enrollment.course for enrollment in enrollments]

        serializer = CourseSerializer(courses, many=True)
        return Response(serializer.data)


# =========================
# COURSE SUBJECTS
# =========================

class CourseSubjectsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, course_id):
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


# =========================
# SUBJECT DETAIL
# =========================

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


# =========================
# SUBJECT DASHBOARD
# =========================

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

        if user.has_role("TEACHER"):
            is_assigned = subject.subject_teachers.filter(
                teacher=user
            ).exists()

            if not is_assigned:
                return Response(
                    {"detail": "Not assigned to this subject."},
                    status=status.HTTP_403_FORBIDDEN
                )
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

        assignments = Assignment.objects.filter(
            chapter__subject=subject
        ).distinct()

        total_assignments = assignments.count()

        if user.has_role("STUDENT"):
            completed_assignments = assignments.filter(
                submissions__student=user
            ).distinct().count()
            pending_assignments = total_assignments - completed_assignments
        else:
            completed_assignments = 0
            pending_assignments = total_assignments

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

        # ── Recordings count ──
        from courses.models_recordings import SessionRecording
        recordings_count = SessionRecording.objects.filter(
            subject=subject
        ).count()

        # ── Study Materials count ──
        from materials.models import StudyMaterial
        study_materials_count = StudyMaterial.objects.filter(
            chapter__subject=subject
        ).count()

        # ── Upcoming Live Sessions ──
        from livestream.models import LiveSession
        now = timezone.now()
        upcoming_sessions = list(
            LiveSession.objects.filter(
                subject=subject,
                start_time__gte=now,
                status__in=[
                    LiveSession.STATUS_SCHEDULED,
                    LiveSession.STATUS_LIVE,
                ],
            )
            .order_by("start_time")[:5]
            .values("id", "title", "start_time", "status")
        )

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

            "recordingsCount": recordings_count,
            "recordings_count": recordings_count,
            "studyMaterialsCount": study_materials_count,
            "study_materials_count": study_materials_count,
            "upcomingSessions": upcoming_sessions,

            "studentsCount": Enrollment.objects.filter(
                course=subject.course,
                status=Enrollment.STATUS_ACTIVE,
            ).count(),
        })


# =========================
# TEACHER CLASSES
# =========================

class TeacherMyClassesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        if not user.has_role(Role.TEACHER):
            return Response(
                {"detail": "Only teachers allowed."},
                status=status.HTTP_403_FORBIDDEN
            )

        subjects = (
            Subject.objects
            .filter(subject_teachers__teacher=user)
            .select_related("course__stream")
            .distinct()
        )

        response_data = []

        for subject in subjects:
            students_count = Enrollment.objects.filter(
                course=subject.course,
                status=Enrollment.STATUS_ACTIVE
            ).count()

            response_data.append({
                "subject_id": str(subject.id),
                "subject_name": subject.name,
                "course_id": str(subject.course.id),
                "course_title": subject.course.title,
                "stream_name": subject.course.stream.name if subject.course.stream else None,
                "students_count": students_count,
            })

        return Response(response_data)


# =========================
# SUBJECT CHAPTERS
# =========================

class SubjectChaptersView(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):

        chapters = Chapter.objects.filter(
            subject_id=subject_id
        ).order_by("order")

        serializer = ChapterSerializer(chapters, many=True)

        return Response(serializer.data)


# =========================
# SUBJECT STUDENTS
# =========================

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


# =========================
# TEACHER ALL STUDENTS
# =========================

class TeacherAllStudentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        if not user.has_role(Role.TEACHER):
            return Response(
                {"detail": "Only teachers allowed."},
                status=status.HTTP_403_FORBIDDEN,
            )

        subjects = (
            Subject.objects
            .filter(subject_teachers__teacher=user)
            .select_related("course__stream")
            .distinct()
        )

        course_ids = [s.course_id for s in subjects]

        enrollments = (
            Enrollment.objects.filter(
                course_id__in=course_ids,
                status=Enrollment.STATUS_ACTIVE,
            )
            .select_related("user", "user__profile", "course")
            .order_by("user__profile__full_name")
        )

        seen = set()
        students = []

        for enrollment in enrollments:
            u = enrollment.user

            if u.id in seen:
                continue
            seen.add(u.id)

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
                "course_title": enrollment.course.title,
                "enrolled_at": enrollment.enrolled_at,
                "batch_code": enrollment.batch_code or "",
            })

        return Response({
            "total_students": len(students),
            "students": students,
        })
