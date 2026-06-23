from .serializers import ChapterSerializer
from .models import Chapter
from django.db.models import Count, Q
from .models import SubjectTeacher
from accounts.models import Role
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from enrollments.models import Enrollment, EnrollmentRequest, Subscription
from accounts.permissions import IsTeacher, IsAdmin
from quizzes.models import Quiz
from assignments.models import Assignment
from .models import Course, Subject, Board
from .serializers import CourseSerializer, SubjectSerializer, BoardSerializer
from django.utils import timezone
from django.shortcuts import get_object_or_404
from datetime import timedelta


# =========================
# CREATE COURSE
# =========================

class PublicCourseDetailView(APIView):
    """Lightweight course detail for the enrollment page — any authenticated user can read."""
    permission_classes = [IsAuthenticated]

    def get(self, request, course_id):
        course = get_object_or_404(
            Course.objects.select_related("board", "stream"),
            id=course_id,
        )
        data = {
            "id": str(course.id),
            "title": course.title,
            "description": course.description,
            "price": course.price,
            "board": course.board.name if course.board else None,
            "stream": course.stream.name if course.stream else None,
        }
        return Response(data)


class CreateCourseView(APIView):
    permission_classes = [IsAuthenticated, IsTeacher]

    def post(self, request):
        serializer = CourseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        course = serializer.save()
        return Response(
            CourseSerializer(course).data,
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
        ).select_related("board").distinct()

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
            ).distinct(),
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
            ).distinct(),
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
            .select_related("course__board")
        )

        courses = [enrollment.course for enrollment in enrollments]
        course_ids = [c.id for c in courses]

        now = timezone.now()
        latest_sub_by_course = {}
        for sub in (
            Subscription.objects
            .filter(user=request.user, course_id__in=course_ids)
            .order_by("course_id", "-expires_at")
        ):
            latest_sub_by_course.setdefault(sub.course_id, sub)

        history_by_course = {}
        for req in (
            EnrollmentRequest.objects
            .filter(user=request.user, course_id__in=course_ids)
            .order_by("-submitted_at")[:50]
        ):
            history_by_course.setdefault(req.course_id, []).append({
                "id": str(req.id),
                "amount_paid": req.amount_paid,
                "payment_date": req.payment_date,
                "utr_number": req.utr_number,
                "payment_method": req.payment_method,
                "status": req.status,
                "submitted_at": req.submitted_at,
                "reviewed_at": req.reviewed_at,
            })

        serialized = CourseSerializer(courses, many=True).data
        for course_data, course in zip(serialized, courses):
            sub = latest_sub_by_course.get(course.id)
            payment_history = history_by_course.get(course.id, [])
            if sub is None:
                course_data["subscription"] = None
                course_data["payment_history"] = payment_history
            else:
                is_active = (
                    sub.status == Subscription.STATUS_ACTIVE
                    and sub.expires_at > now
                )
                days_remaining = max(0, (sub.expires_at - now).days)
                course_data["subscription"] = {
                    "starts_at": sub.starts_at,
                    "expires_at": sub.expires_at,
                    "status": sub.status,
                    "is_active": is_active,
                    "days_remaining": days_remaining,
                    # Per-course trials were removed; every subscription is a
                    # full (paid/free-grant) one now.
                    "is_trial": False,
                }
                course_data["payment_history"] = payment_history

        return Response(serialized)


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

        subjects = (
            Subject.objects
            .filter(course__id=course_id)
            .select_related("course__stream", "course__board")
            .order_by("order")
        )

        serializer = SubjectSerializer(
            subjects, many=True, context={"request": request})
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
            ).select_related("course__stream", "course__board"),
            id=subject_id
        )

        serializer = SubjectSerializer(subject, context={"request": request})
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
            ).select_related("course__stream", "course__board"),
            id=subject_id
        )

        if user.has_role("TEACHER"):
            if not subject.subject_teachers.filter(teacher=user).exists():
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

        is_student = user.has_role("STUDENT")

        # ── Assignments: 1 query ──
        assignment_qs = Assignment.objects.filter(chapter__subject=subject)
        assignment_counts = assignment_qs.aggregate(
            total=Count("id", distinct=True),
            completed=Count(
                "id",
                filter=Q(submissions__student=user),
                distinct=True
            ),
        )
        total_assignments = assignment_counts["total"] or 0
        completed_assignments = assignment_counts["completed"] or 0 if is_student else 0
        pending_assignments = total_assignments - completed_assignments

        # ── Quizzes: 1 query ──
        quiz_qs = Quiz.objects.filter(subject=subject, is_published=True)
        quiz_counts = quiz_qs.aggregate(
            total=Count("id", distinct=True),
            completed=Count(
                "id",
                filter=Q(
                    attempts__student=user,
                    attempts__status="SUBMITTED"
                ),
                distinct=True
            ),
        )
        total_quizzes = quiz_counts["total"] or 0
        completed_quizzes = quiz_counts["completed"] or 0 if is_student else 0
        pending_quizzes = total_quizzes - completed_quizzes

        # ── Misc counts: 1 query ──
        from courses.models_recordings import SessionRecording
        from materials.models import StudyMaterial

        recordings_count = SessionRecording.objects.filter(
            subject=subject).count()
        study_materials_count = StudyMaterial.objects.filter(
            chapter__subject=subject).count()
        students_count = Enrollment.objects.filter(
            course=subject.course,
            status=Enrollment.STATUS_ACTIVE
        ).count()

        # ── Upcoming Live Sessions ──
        from livestream.models import LiveSession
        upcoming_sessions = list(
            LiveSession.objects.filter(
                subject=subject,
                start_time__gte=timezone.now(),
                status__in=[
                    LiveSession.STATUS_SCHEDULED,
                    LiveSession.STATUS_LIVE,
                ],
            )
            .order_by("start_time")[:5]
            .values("id", "title", "start_time", "status")
        )

        serializer = SubjectSerializer(subject, context={"request": request})

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
            "studentsCount": students_count,
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
            .select_related("course__stream", "course__board")
            .annotate(
                students_count=Count(
                    "course__enrollments",
                    filter=Q(
                        course__enrollments__status=Enrollment.STATUS_ACTIVE),
                    distinct=True
                )
            )
            .distinct()
        )

        response_data = []

        for subject in subjects:
            response_data.append({
                "subject_id": str(subject.id),
                "subject_name": subject.name,
                "course_id": str(subject.course.id),
                "course_title": subject.course.title,
                "stream_name": subject.course.stream.name if subject.course.stream else None,
                "board_name": subject.course.board.name if subject.course.board else None,
                "students_count": subject.students_count,
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
            .select_related("user", "learner_profile")
            .order_by("learner_profile__full_name")
        )

        students = []
        for enrollment in enrollments:
            u = enrollment.user
            profile = enrollment.learner_profile

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
# SUBJECTS BY COURSE TITLE
# =========================

class SubjectsByCourseTitleView(APIView):
    """
    Return subjects filtered by course title (class+stream).
    GET /courses/subjects-by-course/?course_title=Class 12 Science
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        course_title = request.query_params.get("course_title", "").strip()
        if not course_title:
            courses = Course.objects.prefetch_related("subjects").all()
            data = {}
            for course in courses:
                subjects = list(course.subjects.values_list(
                    "name", flat=True).order_by("order"))
                data[course.title] = subjects
            return Response(data)

        courses = Course.objects.filter(
            title__icontains=course_title).prefetch_related("subjects")
        subjects = []
        for course in courses:
            for subj in course.subjects.all().order_by("order"):
                if subj.name not in subjects:
                    subjects.append(subj.name)
        return Response({"course_title": course_title, "subjects": subjects})


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
            .select_related("user", "learner_profile", "course")
            .order_by("learner_profile__full_name")
        )

        seen = set()
        students = []

        for enrollment in enrollments:
            u = enrollment.user

            if u.id in seen:
                continue
            seen.add(u.id)

            profile = enrollment.learner_profile

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


# =========================
# STUDENT'S OWN SUBJECTS
# =========================

class MySubjectsView(APIView):
    """
    Returns subjects for the student's active enrolled course(s).
    GET /courses/subjects/mine/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        course_ids = Enrollment.objects.filter(
            user=request.user,
            status=Enrollment.STATUS_ACTIVE,
        ).values_list("course_id", flat=True)

        if not course_ids:
            return Response([])

        subjects = (
            Subject.objects
            .filter(course_id__in=course_ids)
            .select_related("course")
            .order_by("course__title", "order")
        )

        return Response([
            {"id": str(s.id), "name": s.name}
            for s in subjects
        ])


# =========================
# ADMIN — LIST ALL COURSES
# =========================

class AdminCourseListView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        courses = (
            Course.objects
            .annotate(
                enrollment_count=Count(
                    "enrollments",
                    filter=Q(enrollments__status=Enrollment.STATUS_ACTIVE),
                )
            )
            .order_by("-created_at")
        )
        return Response([
            {
                "id": str(c.id),
                "title": c.title,
                "description": c.description,
                "price": c.price,
                "enrollment_count": c.enrollment_count,
                "created_at": c.created_at,
            }
            for c in courses
        ])


# =========================
# ADMIN BOARD CRUD
# =========================

class AdminBoardListCreateView(APIView):
    """GET → list every board (active + dormant). POST → create a board."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        boards = (
            Board.objects
            .annotate(course_count=Count("courses"))
            .order_by("board_type", "name")
        )
        return Response([
            {
                "id": str(b.id),
                "name": b.name,
                "board_type": b.board_type,
                "description": b.description,
                "is_active": b.is_active,
                "course_count": b.course_count,
            }
            for b in boards
        ])

    def post(self, request):
        serializer = BoardSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        board = serializer.save()
        return Response(
            BoardSerializer(board).data,
            status=status.HTTP_201_CREATED,
        )


class AdminBoardDetailView(APIView):
    """PATCH (toggle is_active / rename) and DELETE a board."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def patch(self, request, board_id):
        board = get_object_or_404(Board, id=board_id)
        serializer = BoardSerializer(board, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, board_id):
        board = get_object_or_404(Board, id=board_id)
        if board.courses.exists():
            return Response(
                {"detail": "Cannot delete a board that still has courses. Delete its courses first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        board.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# =========================
# ADMIN COURSE-BY-BOARD + COURSE CRUD
# =========================

class AdminBoardCoursesView(APIView):
    """List courses scoped to a single board."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, board_id):
        get_object_or_404(Board, id=board_id)
        courses = (
            Course.objects
            .filter(board_id=board_id)
            .annotate(
                enrollment_count=Count(
                    "enrollments",
                    filter=Q(enrollments__status=Enrollment.STATUS_ACTIVE),
                ),
                subject_count=Count("subjects", distinct=True),
            )
            .select_related("stream")
            .order_by("title")
        )
        return Response([
            {
                "id": str(c.id),
                "title": c.title,
                "description": c.description,
                "price": c.price,
                "subscription_duration_days": c.subscription_duration_days,
                "stream_name": c.stream.name if c.stream else None,
                "enrollment_count": c.enrollment_count,
                "subject_count": c.subject_count,
                "created_at": c.created_at,
            }
            for c in courses
        ])


class AdminCourseCreateView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request):
        serializer = CourseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        course = serializer.save()
        return Response(
            CourseSerializer(course).data,
            status=status.HTTP_201_CREATED,
        )


class AdminCourseDeleteView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def delete(self, request, course_id):
        course = get_object_or_404(Course, id=course_id)
        course.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# =========================
# ADMIN SUBJECT CRUD
# =========================

class AdminCourseSubjectsView(APIView):
    """GET subjects under a course; POST creates a new subject under that course."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, course_id):
        get_object_or_404(Course, id=course_id)
        subjects = Subject.objects.filter(course_id=course_id).order_by("order", "name")
        return Response([
            {
                "id": str(s.id),
                "name": s.name,
                "order": s.order,
                "image": request.build_absolute_uri(s.image.url) if s.image else None,
                "created_at": s.created_at,
            }
            for s in subjects
        ])

    def post(self, request, course_id):
        course = get_object_or_404(Course, id=course_id)
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response(
                {"detail": "Subject name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        order = request.data.get("order")
        if order in (None, ""):
            next_order = (
                Subject.objects.filter(course=course)
                .order_by("-order")
                .values_list("order", flat=True)
                .first()
            )
            order = (next_order or 0) + 1

        if Subject.objects.filter(course=course, name__iexact=name).exists():
            return Response(
                {"detail": f"A subject named '{name}' already exists in this course."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        subject = Subject.objects.create(
            course=course,
            name=name,
            order=order,
            image=request.FILES.get("image"),
        )
        return Response(
            {
                "id": str(subject.id),
                "name": subject.name,
                "order": subject.order,
                "image": request.build_absolute_uri(subject.image.url) if subject.image else None,
                "created_at": subject.created_at,
            },
            status=status.HTTP_201_CREATED,
        )


class AdminSubjectDeleteView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def delete(self, request, subject_id):
        subject = get_object_or_404(Subject, id=subject_id)
        subject.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
