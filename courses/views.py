from .serializers import ChapterSerializer
from .models import Chapter
from django.db.models import Case, Count, IntegerField, Prefetch, Q, When
from .models import TeachingAssignment
from .services import (
    find_chapter_by_title,
    resolve_or_create_chapter,
    teaches_subject,
)
from accounts.models import LearnerProfile
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.throttling import ScopedRateThrottle
from rest_framework import status
from enrollments.models import Enrollment, EnrollmentRequest, Subscription
from enrollments.services import legacy_profile_q
from accounts.permissions import IsTeacherContext, IsAdmin, require_teacher_context
from accounts.auth_flow import get_active_profile
from quizzes.models import Quiz, QuizAttempt
from assignments.models import Assignment
from courses.progress_stats import average_quiz_score_pct
from .board_display import board_name_for
from .models import Course, Subject, Board, CourseDetail, Batch, CourseCategory, Stream, BoardNotifyRequest, CourseNotifyRequest
from content.models import ShowcaseCourse
from .serializers import (
    CourseSerializer, SubjectSerializer, BoardSerializer, CourseDetailSerializer,
    CourseCategorySerializer,
)
from .cache import LIST_TTL, list_cache_key
from django.core.cache import cache
from django.utils import timezone
from django.shortcuts import get_object_or_404
from datetime import timedelta
import json


# =========================
# CREATE COURSE
# =========================

class EnrollCourseSummaryView(APIView):
    """Lightweight course detail for the enrollment page — any authenticated user
    can read. (Renamed from PublicCourseDetailView: despite the old name this was
    never anonymous-accessible, and a genuinely public/AllowAny detail view now
    lives at PublicCourseDetailView below, `/courses/public/<id>/`.)"""
    permission_classes = [IsAuthenticated]

    def get(self, request, course_id):
        course = get_object_or_404(
            Course.objects.select_related("board", "stream").filter(
                status__in=[Course.STATUS_PUBLISHED, Course.STATUS_ARCHIVED],
            ),
            id=course_id,
        )
        data = {
            "id": str(course.id),
            "title": course.title,
            "description": course.description,
            "price": course.price,
            "board": course.board.name if course.board else None,
            "stream": course.stream.name if course.stream else None,
            # Active batches (Morning/Afternoon/Evening/Night etc) — EnrollModal
            # and Enroll.jsx show a picker when this is non-empty, and pass the
            # chosen id through to free-enroll / the manual-UPI request.
            "batches": [
                {
                    "id": str(b.id), "name": b.name, "code": b.code,
                    "is_full": b.is_full, "capacity": b.capacity,
                    "seats_taken": b.seats_taken,
                }
                for b in course.batches.filter(is_active=True)
            ],
        }
        return Response(data)


class CreateCourseView(APIView):
    # Admin-only. Course has no owner FK and new rows land straight in the
    # AllowAny public catalog, so "any teacher-context account" — which
    # includes never-reviewed, auto-approved skill experts — could publish
    # unattributable courses to the marketing site. No frontend uses this
    # endpoint: the Admin-dashboard creates courses via /courses/admin/courses/.
    permission_classes = [IsAuthenticated, IsAdmin]

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
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def get(self, request):
        courses = Course.objects.filter(
            subjects__teaching_assignments__teacher=request.user,
            subjects__teaching_assignments__is_active=True,
        ).select_related("board").distinct()

        serializer = CourseSerializer(courses, many=True)
        return Response(serializer.data)


# =========================
# UPDATE COURSE
# =========================

class UpdateCourseView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def patch(self, request, course_id):
        course = get_object_or_404(
            Course.objects.filter(
                subjects__teaching_assignments__teacher=request.user,
                subjects__teaching_assignments__is_active=True,
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
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def delete(self, request, course_id):
        course = get_object_or_404(
            Course.objects.filter(
                subjects__teaching_assignments__teacher=request.user,
                subjects__teaching_assignments__is_active=True,
            ).distinct(),
            id=course_id,
        )

        course.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# =========================
# ENROLLED COURSES
# =========================

class MyEnrolledCoursesView(APIView):
    """
    GET /courses/my/

    FIX: previously filtered by `user=`, which unions every learner
    profile on the account. CourseContext feeds the whole student app
    from this endpoint, so a sibling profile's courses (and this
    endpoint's payment-history block — including UTR numbers) leaked
    across profiles. Now scoped to the caller's ACTIVE learner profile.

    Teacher/account context (no active learner profile) → [].
    Legacy Enrollment rows with learner_profile=NULL are attributed to
    the account's default profile (same convention the enrollments app
    used for its own backfill), so pre-migration enrollees don't lose
    their dashboard.
    """
    permission_classes = [IsAuthenticated]

    def _profile_enrollment_q(self, learner):
        q = Q(learner_profile=learner)
        if learner.is_default:
            q |= Q(learner_profile__isnull=True, user=learner.account)
        return q

    def get(self, request):
        learner = get_active_profile(request)
        if learner is None:
            return Response([])

        enrollments = (
            Enrollment.objects
            .filter(self._profile_enrollment_q(learner), status="ACTIVE")
            .select_related("course__board", "batch")
        )

        courses = [enrollment.course for enrollment in enrollments]
        course_ids = [c.id for c in courses]
        enrollment_by_course = {e.course_id: e for e in enrollments}

        # Courses with an active batch but no batch on this enrollment yet —
        # the frontend prompts these students to self-select one (see
        # enrollments/payment_views.py's SelectEnrollmentBatchView).
        unbatched_course_ids = [
            e.course_id for e in enrollments if e.batch_id is None
        ]
        batches_by_course = {}
        if unbatched_course_ids:
            for b in Batch.objects.filter(
                course_id__in=unbatched_course_ids, is_active=True,
            ):
                batches_by_course.setdefault(b.course_id, []).append({
                    "id": str(b.id), "name": b.name, "code": b.code,
                    "is_full": b.is_full, "capacity": b.capacity,
                    "seats_taken": b.seats_taken,
                })

        now = timezone.now()
        latest_sub_by_course = {}
        # legacy_profile_q: a legacy Subscription carries learner_profile=NULL
        # alongside its legacy Enrollment. Matching on the profile alone found
        # no row, and the caller reads "no subscription" as FREE access — so a
        # genuinely EXPIRED legacy subscription rendered as "Free access" with
        # no expiry date and no renew button, while every assignment and
        # material endpoint went on 403ing.
        for sub in (
            Subscription.objects
            .filter(legacy_profile_q(learner), course_id__in=course_ids)
            .order_by("course_id", "-expires_at")
        ):
            latest_sub_by_course.setdefault(sub.course_id, sub)

        history_by_course = {}
        # The [:50] that used to sit here sliced the GLOBAL, newest-first list
        # before grouping, so a student with 50+ requests on a recent course saw
        # "No payments yet" against an older one they had genuinely paid for.
        # The cap belongs per-course, after grouping — same protection against
        # an unbounded response, without hiding a course's whole history.
        PER_COURSE_HISTORY_CAP = 50
        for req in (
            EnrollmentRequest.objects
            .filter(legacy_profile_q(learner), course_id__in=course_ids)
            .order_by("-submitted_at")
        ):
            bucket = history_by_course.setdefault(req.course_id, [])
            if len(bucket) >= PER_COURSE_HISTORY_CAP:
                continue
            bucket.append({
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

            enrollment = enrollment_by_course.get(course.id)
            if enrollment and enrollment.batch_id:
                course_data["batch"] = {
                    "id": str(enrollment.batch_id), "name": enrollment.batch.name,
                }
                course_data["available_batches"] = []
            else:
                course_data["batch"] = None
                course_data["available_batches"] = batches_by_course.get(course.id, [])

        return Response(serialized)


# =========================
# COURSE SUBJECTS
# =========================

class CourseSubjectsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, course_id):
        learner = get_active_profile(request)

        if learner is not None:
            profile_q = Q(learner_profile=learner)
            if learner.is_default:
                profile_q |= Q(learner_profile__isnull=True, user=learner.account)
            is_enrolled = Enrollment.objects.filter(
                Q(course__id=course_id, status="ACTIVE") & profile_q
            ).exists()
        else:
            # Teacher identity without an active learner profile: allow
            # only if assigned to teach this course's subjects.
            is_enrolled = TeachingAssignment.objects.filter(
                subject__course_id=course_id, teacher=request.user, is_active=True,
            ).exists()

        if not is_enrolled:
            return Response({"detail": "Not enrolled in this course."}, status=403)

        subjects = (
            Subject.objects
            .filter(course__id=course_id)
            .select_related("course__stream", "course__board")
            .prefetch_related(
                Prefetch(
                    "teaching_assignments",
                    queryset=TeachingAssignment.objects
                    .filter(batch__isnull=True, is_active=True)
                    .select_related("teacher", "teacher__teacher_profile")
                    .order_by("order"),
                )
            )
            .order_by("order")
        )

        serializer = SubjectSerializer(
            subjects, many=True, context={"request": request})
        return Response(serializer.data)


def _require_subject_access(request, subject):
    """Enrollment-or-teaching-assignment gate shared by every per-subject
    view. Enrollment wins over teaching assignment always, regardless of
    role — a TEACHER-role account can also be personally enrolled as a
    learner in a subject they don't teach. Returns a 403 Response if the
    caller has neither, else None.
    """
    user = request.user
    learner = get_active_profile(request)
    enrolled = False
    if learner is not None:
        enroll_q = Q(learner_profile=learner)
        if getattr(learner, "is_default", False):
            enroll_q |= Q(learner_profile__isnull=True, user=user)
        enrolled = Enrollment.objects.filter(
            enroll_q,
            course=subject.course,
            status=Enrollment.STATUS_ACTIVE,
        ).exists()

    if enrolled:
        return None

    is_assigned_teacher = (
        user.has_role("TEACHER")
        and teaches_subject(user, subject)
    )
    if is_assigned_teacher:
        return None

    detail = (
        "Not assigned to this subject."
        if user.has_role("TEACHER")
        else "Not enrolled."
    )
    return Response({"detail": detail}, status=status.HTTP_403_FORBIDDEN)


# =========================
# SUBJECT DETAIL
# =========================

class SubjectDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        subject = get_object_or_404(
            Subject.objects.prefetch_related(
                "teaching_assignments__teacher__teacher_profile"
            ).select_related("course__stream", "course__board"),
            id=subject_id
        )

        denied = _require_subject_access(request, subject)
        if denied is not None:
            return denied

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
                "teaching_assignments__teacher"
            ).select_related("course__stream", "course__board"),
            id=subject_id
        )

        denied = _require_subject_access(request, subject)
        if denied is not None:
            return denied

        is_student = user.has_role("STUDENT")

        # ── Assignments: 1 query ──
        assignment_qs = Assignment.objects.filter(subject=subject)
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
        # is_assigned, not is_published — Phase 1 moved student visibility onto
        # the teacher-controlled flag.
        #
        # NO batch scoping added here, deliberately. This view serves BOTH
        # students and teachers (see `is_student` above): the totals feed a
        # teacher's own subject dashboard as well as a learner's, and it has
        # never been batch-scoped for either. Adding batch scoping would be a
        # behaviour change beyond Phase 1's "preserve who sees what" mandate,
        # and would be outright wrong for the teacher branch. Flagged as a
        # separate question, not silently changed here.
        quiz_qs = Quiz.objects.filter(subject=subject, is_assigned=True)
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

        # Avg quiz score for this subject — same score/total_marks% math as
        # the Progress screen's stats block (courses.progress_stats), just
        # scoped to one subject's submitted attempts instead of a whole
        # course. None for teachers (they don't take their own quizzes) or
        # when the student has no submitted attempts yet.
        quiz_avg_pct = None
        if is_student:
            attempts = list(
                QuizAttempt.objects.filter(
                    quiz__subject=subject,
                    student=user,
                    status=QuizAttempt.STATUS_SUBMITTED,
                ).select_related("quiz")
            )
            quiz_avg_pct = average_quiz_score_pct(attempts)

        # ── Misc counts: 1 query ──
        from courses.models_recordings import SessionRecording
        from materials.models import StudyMaterial

        recordings_count = SessionRecording.objects.filter(
            subject=subject).count()
        study_materials_count = StudyMaterial.objects.filter(
            subject=subject).count()
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
            # Subject names repeat across courses ("Mathematics" exists in
            # every class), so callers rendering a heading need the course to
            # say WHICH Mathematics this is. select_related on `course` is
            # already in place above, so this costs no extra query.
            "course_id": str(subject.course_id),
            "course_title": subject.course.title,
            "board_name": board_name_for(subject.course),
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
            "quizAvgPct": quiz_avg_pct,
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

        # has_role alone would pass for a dual-role account's LEARNER-context
        # token too (e.g. a child profile on a shared account whose parent is
        # a teacher) — this is a teacher-only roster view, so it must also
        # require the active teacher-context claim, not just the role.
        require_teacher_context(request)

        subjects = (
            Subject.objects
            .filter(teaching_assignments__teacher=user, teaching_assignments__is_active=True)
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
    """The chapter list behind the shared chapter picker.

    GET  — the subject's syllabus chapters, plus custom chapters. A teacher
           sees their OWN custom chapters; everyone sees custom chapters an
           admin has promoted into the syllabus (`promoted_at` set). Another
           teacher's unpromoted scratch chapters stay private to them, so one
           teacher's shorthand doesn't clutter a colleague's picker.
    POST — create a custom chapter (`is_custom=True`) for this subject.
           Teacher-only, and only a teacher assigned to the subject.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        subject = get_object_or_404(
            Subject.objects.select_related("course"), id=subject_id
        )

        denied = _require_subject_access(request, subject)
        if denied is not None:
            return denied

        # Curated syllabus + promoted customs + this caller's own customs.
        chapters = Chapter.objects.filter(
            Q(is_custom=False)
            | Q(promoted_at__isnull=False)
            | Q(created_by=request.user),
            subject_id=subject_id,
        ).order_by("order", "title")

        serializer = ChapterSerializer(chapters, many=True)
        return Response(serializer.data)

    def post(self, request, subject_id):
        subject = get_object_or_404(
            Subject.objects.select_related("course"), id=subject_id
        )

        # Deliberately stricter than GET's _require_subject_access(), which
        # also admits enrolled students — a student must never be able to
        # write into a course syllabus.
        if not (request.user.has_role("TEACHER")
                and teaches_subject(request.user, subject)):
            return Response(
                {"detail": "Not assigned to this subject."},
                status=status.HTTP_403_FORBIDDEN,
            )

        title = (request.data.get("title") or "").strip()
        if not title:
            return Response(
                {"title": "Chapter name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Checked BEFORE the call so the response status can say truthfully
        # whether anything was created — 200 for "that chapter already
        # existed, here it is", 201 for a genuinely new row. The picker
        # dedupes on the returned id either way.
        existed = find_chapter_by_title(subject, title) is not None

        # resolve_or_create_chapter, not a bare create(): a repeat (or
        # case-varied) name must return the EXISTING chapter rather than trip
        # unique_chapter_per_subject with a 500. It also appends the new row
        # at max(order)+1 and stamps is_custom/created_by.
        chapter = resolve_or_create_chapter(
            subject, custom_title=title, created_by=request.user,
        )
        return Response(
            ChapterSerializer(chapter).data,
            status=status.HTTP_200_OK if existed else status.HTTP_201_CREATED,
        )


# =========================
# TEACHER ROSTER ROWS
# =========================
# A "student" in this app is a LearnerProfile, NOT a User: one account holds
# many learner profiles, so a parent with three enrolled children is 1 user and
# 3 students. Teacher rosters are therefore keyed on the learner profile —
# keying them on the account (as these views used to) collapses siblings into a
# single row and hands the frontend an id that identifies the family rather
# than the student. The admin-side equivalent of these helpers is
# ``accounts.admin_student_views._student_row`` / ``AdminStudentListView``.


def _resolve_enrollment_profiles(enrollments):
    """Map ``enrollment.id -> LearnerProfile or None`` for ``enrollments``.

    Enrollments predating per-profile enrollment have ``learner_profile=NULL``
    and belong to the account's DEFAULT profile — the same rule
    ``courses.progress_stats._dual_key_q`` applies when reading a student's
    activity. Applying it here keeps legacy students on the roster with a real
    learner-profile id instead of dropping them or falling back to their
    account id.

    The default profile is resolved with ONE extra query covering every legacy
    account, then picked in Python so the preference order matches
    ``User.default_learner_profile()`` exactly: an ``is_default`` profile, else
    a SELF profile, else the first in ``LearnerProfile.Meta.ordering``.
    """
    resolved = {e.id: e.learner_profile for e in enrollments if e.learner_profile_id}

    legacy_account_ids = {e.user_id for e in enrollments if not e.learner_profile_id}
    if not legacy_account_ids:
        return resolved

    by_account = {}
    for profile in LearnerProfile.objects.filter(
        account_id__in=legacy_account_ids, is_active=True
    ):
        by_account.setdefault(profile.account_id, []).append(profile)

    for enrollment in enrollments:
        if enrollment.learner_profile_id:
            continue
        candidates = by_account.get(enrollment.user_id, [])
        resolved[enrollment.id] = (
            next((p for p in candidates if p.is_default), None)
            or next(
                (
                    p for p in candidates
                    if p.relationship == LearnerProfile.RELATIONSHIP_SELF
                ),
                None,
            )
            or (candidates[0] if candidates else None)
        )
    return resolved


def _roster_row_key(enrollment, profile):
    """Dedupe key for a roster row: the STUDENT, falling back to the account
    for rows whose profile couldn't be resolved, so those neither collapse into
    each other nor into a real student's row."""
    if profile is not None:
        return ("profile", profile.id)
    return ("account", enrollment.user_id)


def _absolutise(url, request):
    """Make a root-relative media URL absolute against the API host.

    Same shape as accounts/views.py:1049. A no-op for values that are already
    absolute (Bunny/CDN URLs) or for the DiceBear-style identifiers
    avatar_value() returns when the learner has no uploaded image.
    """
    if request is not None and isinstance(url, str) and url.startswith("/"):
        return request.build_absolute_uri(url)
    return url


def _roster_row(enrollment, profile, request=None):
    """One roster row. ``profile`` is the resolved LearnerProfile (see
    ``_resolve_enrollment_profiles``), so ``id`` identifies the student; the
    account is reported separately as ``account_id``/``email``/``username`` so
    a teacher can still tell which family a student belongs to.

    ⚠️ Several rows legitimately share the same ``account_id`` and ``email``
    (siblings), so never treat those as a student identifier.

    ``request`` is needed to absolutise the avatar URL — see the note on the
    ``avatar`` key below. It is optional only so existing callers without one
    degrade to the old relative value rather than crashing.
    """
    account = enrollment.user
    row = {
        "account_id": str(account.id),
        "email": account.email,
        "username": account.username,
        "enrolled_at": enrollment.enrolled_at,
        "batch_code": enrollment.batch_code or "",
    }

    if profile is None:
        # The account has no active learner profile at all, so this enrollment
        # can't be attributed to a student. Kept rather than dropped so the
        # gap is visible instead of silently shortening the roster.
        row.update({
            "id": None,
            "display_name": "",
            "full_name": "",
            "phone": "",
            "student_id": "",
            "avatar_type": None,
            "avatar": None,
            "unresolved_profile": True,
        })
        return row

    row.update({
        "id": str(profile.id),
        # display_name is what the profile picker shows, and the only field
        # that reliably distinguishes siblings on one account.
        "display_name": profile.display_name,
        "full_name": profile.full_name or f"{profile.first_name} {profile.last_name}".strip(),
        "phone": profile.phone,
        "student_id": profile.student_id or "",
        "avatar_type": profile.avatar_type(),
        # Absolutise, matching accounts/serializers.py:52 and
        # accounts/views.py:1049. Returning the raw root-relative value made
        # EVERY student avatar a broken image: the dashboards run on
        # teacher./app.shikshacom.com while the API is on api.shikshacom.com,
        # so the browser resolved it against the SPA host and got the
        # index.html fallback. The initials fallback never rescued it either,
        # because that branch keys on avatar_type rather than on load failure.
        #
        # avatar_value() already yields the /api/media/secure/… path (private
        # prefixes are rewritten by SecureLocalStorage.url), so absolutising is
        # the only transform needed HERE — but see _check_learner_photo in
        # config/media_security.py, which had to be widened in the same change:
        # it authorised the owning account and staff only, so a teacher loading
        # their own roster was denied and the fixed URL would still have 404'd.
        "avatar": _absolutise(profile.avatar_value(), request),
        "unresolved_profile": False,
    })
    return row


def _roster_sort_key(row):
    return (row["full_name"] or row["display_name"]).lower()


# =========================
# SUBJECT STUDENTS
# =========================

class SubjectStudentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        user = request.user

        # See TeacherMyClassesView — role alone isn't enough, this must also
        # be gated on active teacher context.
        require_teacher_context(request)

        subject = get_object_or_404(
            Subject.objects.select_related("course__board"), id=subject_id)

        if not teaches_subject(user, subject):
            return Response(
                {"detail": "You are not assigned to this subject."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Not ordered in SQL: legacy rows have learner_profile=NULL, so
        # order_by("learner_profile__full_name") sorted them by NULL rather
        # than by the default profile they actually resolve to. Sorted in
        # Python below, after resolution.
        enrollments = list(
            Enrollment.objects.filter(
                course=subject.course,
                status=Enrollment.STATUS_ACTIVE,
            )
            .select_related("user", "learner_profile")
        )
        profiles = _resolve_enrollment_profiles(enrollments)

        # Deduped even though `unique_together = ("learner_profile", "course")`
        # already bounds it: that constraint doesn't bind NULL profiles, so an
        # account carrying both a legacy and a migrated row for this course
        # would otherwise emit the same student twice.
        seen = set()
        students = []
        for enrollment in enrollments:
            profile = profiles.get(enrollment.id)
            key = _roster_row_key(enrollment, profile)
            if key in seen:
                continue
            seen.add(key)
            students.append(_roster_row(enrollment, profile, request))

        students.sort(key=_roster_sort_key)

        return Response({
            "subject_name": subject.name,
            "course_title": subject.course.title,
            "board_name": board_name_for(subject.course),
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
    GET /courses/subjects-by-course/?course_title=Class 9&board=MBSE

    Titling is NOT an identity here. MBSE and CBSE each run a course titled
    "Class 9" with genuinely different syllabi (MBSE carries Hindi MIL papers
    CBSE does not), so both branches below used to conflate them:

    - the no-arg branch keyed its dict on `course.title`, so the second board's
      course silently OVERWROTE the first's subject list and one board's
      syllabus simply vanished from the response;
    - the filtered branch matched `title__icontains` across boards and then
      deduped by subject NAME, returning a union of two different syllabi
      presented as one course's.

    Keys are board-qualified now, and `board` is an optional exact filter.
    `subjects` is retained as the flat union for the documented shape, but
    `courses` is the one to read — it keeps each board's syllabus separate.

    NOTE: grepped for consumers across all four frontends, the Flutter app and
    the test suite on 2026-08-21 and found NONE — this endpoint appears to be
    dead surface. Fixed rather than deleted because an endpoint is outward
    facing and something unversioned (a script, an old mobile build) could
    still call it; worth confirming and removing.
    """
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _label(course):
        """Board-qualified label, so two boards' "Class 9" cannot collide.
        Falls back to the bare title for a course with no board (coaching
        courses legitimately have none) — those titles are already unique."""
        name = board_name_for(course)
        return f"{course.title} · {name}" if name else course.title

    def get(self, request):
        course_title = request.query_params.get("course_title", "").strip()
        board = request.query_params.get("board", "").strip()

        qs = Course.objects.select_related("board").prefetch_related("subjects")
        if course_title:
            qs = qs.filter(title__icontains=course_title)
        if board:
            qs = qs.filter(board__name__iexact=board)

        courses = []
        union = []
        for course in qs:
            names = list(course.subjects.order_by("order").values_list("name", flat=True))
            courses.append({
                "course_id": str(course.id),
                "course_title": course.title,
                "board_name": board_name_for(course),
                "label": self._label(course),
                "subjects": names,
            })
            for n in names:
                if n not in union:
                    union.append(n)

        if not course_title:
            # Preserved shape: a mapping of label -> subject names. The key is
            # board-qualified now, which is the whole point.
            return Response({c["label"]: c["subjects"] for c in courses})

        return Response({
            "course_title": course_title,
            "board": board or None,
            "subjects": union,
            "courses": courses,
        })


# =========================
# TEACHER ALL STUDENTS
# =========================

class TeacherAllStudentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        # See TeacherMyClassesView — role alone isn't enough, this must also
        # be gated on active teacher context.
        require_teacher_context(request)

        subjects = (
            Subject.objects
            .filter(teaching_assignments__teacher=user, teaching_assignments__is_active=True)
            .select_related("course__stream")
            .distinct()
        )

        course_ids = [s.course_id for s in subjects]

        # Sorted in Python after profile resolution — see SubjectStudentsView.
        enrollments = list(
            Enrollment.objects.filter(
                course_id__in=course_ids,
                status=Enrollment.STATUS_ACTIVE,
            )
            .select_related("user", "learner_profile", "course__board")
        )
        profiles = _resolve_enrollment_profiles(enrollments)

        # Deduped per STUDENT, not per account: this used to key `seen` on
        # User.id, so a parent with three enrolled children showed up as one
        # row instead of three.
        rows_by_key = {}
        students = []

        for enrollment in enrollments:
            profile = profiles.get(enrollment.id)
            key = _roster_row_key(enrollment, profile)
            course_title = enrollment.course.title
            board = board_name_for(enrollment.course)

            existing = rows_by_key.get(key)
            if existing is not None:
                # Same student, another of this teacher's courses. Keyed on the
                # (title, board) pair, not the title alone — a student enrolled
                # in "Class 9" under both boards is in two different courses.
                if (course_title, board) not in list(zip(
                        existing["course_titles"], existing["board_names"])):
                    existing["course_titles"].append(course_title)
                    existing["board_names"].append(board)
                continue

            row = _roster_row(enrollment, profile, request)
            # One student can sit in several of this teacher's courses, and
            # dedupe keeps a single row — so list every course rather than
            # letting whichever enrollment happened to come first decide.
            # `course_title` stays for the existing frontend column.
            # `board_names` runs parallel to `course_titles` by index.
            row["course_title"] = course_title
            row["board_name"] = board
            row["course_titles"] = [course_title]
            row["board_names"] = [board]
            rows_by_key[key] = row
            students.append(row)

        students.sort(key=_roster_sort_key)

        # One grouped query for every student's quiz average, rather than
        # the N+1 you'd get calling the per-student helper once per row.
        # Same real computation `build_progress_stats` uses for a single
        # learner (Avg(score/quiz.total_marks*100) over SUBMITTED attempts
        # on the teacher's own subjects) — just batched.
        profile_ids = [row["id"] for row in students if row["id"]]
        attempts_by_profile = {}
        if profile_ids:
            subject_ids = [s.id for s in subjects]
            attempts = (
                QuizAttempt.objects
                .filter(
                    quiz__subject_id__in=subject_ids,
                    status=QuizAttempt.STATUS_SUBMITTED,
                    learner_profile_id__in=profile_ids,
                )
                .select_related("quiz")
            )
            for attempt in attempts:
                attempts_by_profile.setdefault(
                    str(attempt.learner_profile_id), []
                ).append(attempt)

        for row in students:
            row["avg_quiz_score"] = (
                average_quiz_score_pct(attempts_by_profile.get(row["id"], []))
                if row["id"] else None
            )

        return Response({
            "total_students": len(students),
            "students": students,
        })


# =========================
# STUDENT'S OWN SUBJECTS
# =========================

class MySubjectsView(APIView):
    """
    Returns subjects for the ACTIVE PROFILE's enrolled course(s).
    GET /courses/subjects/mine/

    FIX: previously filtered Enrollment by `user=`, unioning every
    learner profile's courses. Now scoped to the caller's active
    learner profile (teacher/account context → []).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        learner = get_active_profile(request)
        if learner is None:
            return Response([])

        profile_q = Q(learner_profile=learner)
        if learner.is_default:
            profile_q |= Q(learner_profile__isnull=True, user=learner.account)

        course_ids = Enrollment.objects.filter(
            profile_q, status=Enrollment.STATUS_ACTIVE,
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
                "status": c.status,
                "thumbnail": request.build_absolute_uri(c.thumbnail.url) if c.thumbnail else None,
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
                "slug": b.slug,
                "logo": request.build_absolute_uri(b.logo.url) if b.logo else None,
                "display_order": b.display_order,
                "is_active": b.is_active,
                "course_count": b.course_count,
            }
            for b in boards
        ])

    def post(self, request):
        serializer = BoardSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        board = serializer.save()
        if request.FILES.get("logo"):
            board.logo = request.FILES["logo"]
            board.save(update_fields=["logo"])
        return Response(
            BoardSerializer(board, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class AdminBoardDetailView(APIView):
    """PATCH (toggle is_active / rename / logo / display_order) and DELETE a
    board. `logo` arrives as a multipart file, handled separately like
    Course.thumbnail — see AdminCourseDetailView.patch."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def patch(self, request, board_id):
        board = get_object_or_404(Board, id=board_id)
        serializer = BoardSerializer(
            board, data=request.data, partial=True, context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        board = serializer.save()
        if request.FILES.get("logo"):
            board.logo = request.FILES["logo"]
            board.save(update_fields=["logo"])
            board.refresh_from_db()
        return Response(BoardSerializer(board, context={"request": request}).data)

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
# ADMIN COURSE CATEGORY CRUD
# =========================
# CourseCategory powers the public catalog's category/group filters and the
# navbar mega-menu's "competitive" tab (see PublicCourseCatalogView /
# PublicNavMenuView above). Admin-authenticated the same way as the other
# course-admin endpoints (IsAuthenticated + IsAdmin), following the same
# plain APIView + path() convention as the Board CRUD immediately above
# (courses/urls.py has no router-based ViewSet setup to match instead).

class AdminCourseCategoryListCreateView(APIView):
    """GET → list every category (active + inactive). POST → create one."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        categories = CourseCategory.objects.all()
        return Response(CourseCategorySerializer(categories, many=True).data)

    def post(self, request):
        serializer = CourseCategorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        category = serializer.save()
        return Response(
            CourseCategorySerializer(category).data,
            status=status.HTTP_201_CREATED,
        )


class AdminCourseCategoryDetailView(APIView):
    """GET a single category; PATCH edits it; DELETE removes it."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, category_id):
        category = get_object_or_404(CourseCategory, id=category_id)
        return Response(CourseCategorySerializer(category).data)

    def patch(self, request, category_id):
        category = get_object_or_404(CourseCategory, id=category_id)
        serializer = CourseCategorySerializer(category, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, category_id):
        category = get_object_or_404(CourseCategory, id=category_id)
        category.delete()
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
            .select_related("stream", "details")
            .prefetch_related("categories")
            .order_by("title")
        )
        return Response([
            {
                "id": str(c.id),
                "title": c.title,
                "description": c.description,
                "price": c.price,
                "status": c.status,
                "thumbnail": request.build_absolute_uri(c.thumbnail.url) if c.thumbnail else None,
                "subscription_duration_days": c.subscription_duration_days,
                "stream_name": c.stream.name if c.stream else None,
                "enrollment_count": c.enrollment_count,
                "subject_count": c.subject_count,
                "created_at": c.created_at,
                # Fields needed for the admin course-list "content complete" /
                # "shows up on" columns — mirrors what openEditCourse already
                # fetches per-course via getCourse(), but at list scope so the
                # completeness score can render without a fetch per row.
                "details": (
                    {"syllabus": c.details.syllabus, "highlights": c.details.highlights}
                    if hasattr(c, "details") else None
                ),
                "is_featured": c.is_featured,
                "categories": [
                    {"id": cat.id, "slug": cat.slug, "name": cat.name, "group": cat.group}
                    for cat in c.categories.all()
                ],
                "seo_title": c.seo_title,
            }
            for c in courses
        ])


def _parse_maybe_json(value):
    """`details` (dict) and `categories` (list) both arrive as the real
    JSON type in a plain JSON body, but multipart requests can only carry
    strings, so the frontend JSON-encodes them there. Parse the string form;
    pass anything else (already a dict/list, or None) straight through."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _apply_course_details_and_categories(course, request):
    """Upsert CourseDetail and set the categories M2M from the `details` /
    `categories` keys in request.data. Shared by AdminCourseCreateView.post
    and AdminCourseDetailView.patch so create and edit can't drift — this
    used to live only in the PATCH view, silently dropping `details` (and
    now `categories`) on course creation."""
    details_data = _parse_maybe_json(request.data.get("details"))
    if isinstance(details_data, dict):
        detail, _ = CourseDetail.objects.get_or_create(course=course)
        detail_serializer = CourseDetailSerializer(detail, data=details_data, partial=True)
        detail_serializer.is_valid(raise_exception=True)
        detail_serializer.save()

    categories_data = _parse_maybe_json(request.data.get("categories"))
    if isinstance(categories_data, list):
        # Filter to real ids rather than letting a stale/typo'd id 500 the
        # request via the M2M through-table's FK constraint.
        course.categories.set(
            CourseCategory.objects.filter(id__in=categories_data)
        )


class AdminCourseCreateView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        """Flat list of ALL courses across ALL boards (unpaginated, matches
        this admin API's existing convention — see AdminBoardCoursesView).
        Powers the "All Courses" tab in Admin-dashboard's Courses page,
        which sits alongside the Boards drill-down rather than replacing
        it (an admin previously had to open a board first to find any
        course). Same row shape as AdminBoardCoursesView, plus board_id/
        board_name since a flat list needs to show which board each course
        belongs to. Optional filters: ?search= (icontains on title),
        ?board=<id>, ?status=<status>."""
        courses = (
            Course.objects
            .annotate(
                enrollment_count=Count(
                    "enrollments",
                    filter=Q(enrollments__status=Enrollment.STATUS_ACTIVE),
                ),
                subject_count=Count("subjects", distinct=True),
            )
            .select_related("stream", "details", "board")
            .prefetch_related("categories")
            .order_by("title")
        )

        search = request.query_params.get("search")
        if search:
            courses = courses.filter(title__icontains=search)
        board = request.query_params.get("board")
        if board:
            courses = courses.filter(board_id=board)
        course_status = request.query_params.get("status")
        if course_status:
            courses = courses.filter(status=course_status)

        return Response([
            {
                "id": str(c.id),
                "title": c.title,
                "description": c.description,
                "price": c.price,
                "status": c.status,
                "thumbnail": request.build_absolute_uri(c.thumbnail.url) if c.thumbnail else None,
                "subscription_duration_days": c.subscription_duration_days,
                "stream_name": c.stream.name if c.stream else None,
                "enrollment_count": c.enrollment_count,
                "subject_count": c.subject_count,
                "created_at": c.created_at,
                "details": (
                    {"syllabus": c.details.syllabus, "highlights": c.details.highlights}
                    if hasattr(c, "details") else None
                ),
                "is_featured": c.is_featured,
                "categories": [
                    {"id": cat.id, "slug": cat.slug, "name": cat.name, "group": cat.group}
                    for cat in c.categories.all()
                ],
                "seo_title": c.seo_title,
                "board_id": str(c.board_id) if c.board_id else None,
                "board_name": c.board.name if c.board else None,
            }
            for c in courses
        ])

    def post(self, request):
        serializer = CourseSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        course = serializer.save()
        if request.FILES.get("thumbnail"):
            course.thumbnail = request.FILES["thumbnail"]
            course.save(update_fields=["thumbnail"])
        _apply_course_details_and_categories(course, request)
        course.refresh_from_db()
        return Response(
            CourseSerializer(course, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class AdminCourseDetailView(APIView):
    """GET a single course (admin shape); PATCH edits it, including a nested
    ``details`` object (create-or-update the course's CourseDetail row), a
    ``categories`` id list (M2M `.set()`), and a multipart ``thumbnail``
    file; DELETE removes it."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, course_id):
        course = get_object_or_404(
            Course.objects.select_related("board", "stream", "details"),
            id=course_id,
        )
        return Response(CourseSerializer(course, context={"request": request}).data)

    def patch(self, request, course_id):
        course = get_object_or_404(Course.objects.select_related("details"), id=course_id)

        serializer = CourseSerializer(
            course, data=request.data, partial=True, context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        course = serializer.save()

        if request.FILES.get("thumbnail"):
            course.thumbnail = request.FILES["thumbnail"]
            course.save(update_fields=["thumbnail"])

        _apply_course_details_and_categories(course, request)

        course.refresh_from_db()
        return Response(CourseSerializer(course, context={"request": request}).data)

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
        subjects = (
            Subject.objects.filter(course_id=course_id)
            .prefetch_related("chapters")
            .order_by("order", "name")
        )
        return Response([
            {
                "id": str(s.id),
                "name": s.name,
                "order": s.order,
                "textbook": s.textbook,
                "image": request.build_absolute_uri(s.image.url) if s.image else None,
                "created_at": s.created_at,
                "chapters": [
                    {"id": str(ch.id), "title": ch.title, "order": ch.order,
                     "content_html": ch.content_html, "trusted_html": ch.trusted_html}
                    for ch in s.chapters.all()
                ],
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
            textbook=(request.data.get("textbook") or "").strip(),
            image=request.FILES.get("image"),
        )
        return Response(
            {
                "id": str(subject.id),
                "name": subject.name,
                "order": subject.order,
                "textbook": subject.textbook,
                "image": request.build_absolute_uri(subject.image.url) if subject.image else None,
                "created_at": subject.created_at,
                "chapters": [],
            },
            status=status.HTTP_201_CREATED,
        )


class AdminSubjectDeleteView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def patch(self, request, subject_id):
        subject = get_object_or_404(Subject, id=subject_id)
        if "name" in request.data and request.data.get("name"):
            subject.name = str(request.data["name"]).strip()
        if "order" in request.data and request.data.get("order") not in (None, ""):
            subject.order = request.data["order"]
        if "textbook" in request.data:
            subject.textbook = (request.data.get("textbook") or "").strip()
        if request.FILES.get("image"):
            subject.image = request.FILES["image"]
        subject.save()
        return Response({
            "id": str(subject.id),
            "name": subject.name,
            "order": subject.order,
            "textbook": subject.textbook,
            "image": request.build_absolute_uri(subject.image.url) if subject.image else None,
            "created_at": subject.created_at,
        })

    def delete(self, request, subject_id):
        subject = get_object_or_404(Subject, id=subject_id)
        subject.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminSubjectChaptersView(APIView):
    """POST creates a new chapter under a subject (admin authoring)."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, subject_id):
        subject = get_object_or_404(Subject, id=subject_id)
        title = (request.data.get("title") or "").strip()
        if not title:
            return Response(
                {"detail": "Chapter title is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        order = request.data.get("order")
        if order in (None, ""):
            next_order = (
                Chapter.objects.filter(subject=subject)
                .order_by("-order")
                .values_list("order", flat=True)
                .first()
            )
            order = (next_order or 0) + 1

        if Chapter.objects.filter(subject=subject, title__iexact=title).exists():
            return Response(
                {"detail": f"A chapter titled '{title}' already exists in this subject."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        chapter = Chapter(
            subject=subject,
            title=title,
            order=order,
            content_html=request.data.get("content_html") or "",
            trusted_html=bool(request.data.get("trusted_html", False)),
        )
        chapter.save()
        return Response(
            {
                "id": str(chapter.id),
                "title": chapter.title,
                "order": chapter.order,
                "content_html": chapter.content_html,
                "trusted_html": chapter.trusted_html,
            },
            status=status.HTTP_201_CREATED,
        )


class AdminChapterDetailView(APIView):
    """PATCH edits a chapter's title/order/content; DELETE removes it."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def patch(self, request, chapter_id):
        chapter = get_object_or_404(Chapter, id=chapter_id)
        if "title" in request.data and request.data.get("title"):
            chapter.title = str(request.data["title"]).strip()
        if "order" in request.data and request.data.get("order") not in (None, ""):
            chapter.order = request.data["order"]
        if "trusted_html" in request.data:
            chapter.trusted_html = bool(request.data["trusted_html"])
        if "content_html" in request.data:
            chapter.content_html = request.data.get("content_html") or ""
        chapter.save()
        return Response({
            "id": str(chapter.id),
            "title": chapter.title,
            "order": chapter.order,
            "content_html": chapter.content_html,
            "trusted_html": chapter.trusted_html,
        })

    def delete(self, request, chapter_id):
        chapter = get_object_or_404(Chapter, id=chapter_id)
        chapter.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# =========================
# COURSE CATALOG (student-facing "Browse Courses" shop)
# =========================

class CourseCatalogView(APIView):
    """Browsable catalog of every course, for the in-dashboard "Browse Courses"
    shop. Any authenticated learner can read it — enrollment gates a course's
    *content*, not its listing.

    Each course carries just enough for a shop card: title, description, price,
    board/stream, a subject count, a small teacher preview, and an
    ``is_enrolled`` flag so the UI shows courses the learner already owns as
    enrolled rather than purchasable. Matches the same "active enrollment for
    this user" rule that /courses/my/ uses, so the two stay in sync.

    Also includes COMING_SOON courses (no batch required — they're
    intentionally not purchasable yet) alongside PUBLISHED-with-an-active-batch
    courses, carrying the same mrp/discount_label/category fields as the public
    catalog so the shop can render strikethrough pricing and category chips.

    Optional query params:
        ?q=<text>        title / description search
        ?board=<uuid>    filter to one board
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Purchasable: published with at least one open (active) batch — keeps
        # DRAFT/half-built courses and courses with no running cohort out of
        # the buy flow. Coming-soon courses need no batch (not purchasable
        # yet); owned courses still appear via /courses/my/ regardless of
        # status.
        # A course is listed when it is COMING_SOON (shown, not purchasable),
        # or PUBLISHED and either has a running cohort OR has no batches at
        # all.
        #
        # That last clause matters: requiring `batches__is_active=True`
        # unconditionally meant a PUBLISHED course with no Batch rows was
        # invisible here — and since batches were introduced by the
        # catalog-vs-delivery refactor but never populated, that silently hid
        # EVERY real course on production (13 of them) while leaving only the
        # non-purchasable coming-soon placeholders visible. It also contradicted
        # the rest of the codebase, which treats "no batch" / batch IS NULL as
        # course-wide (see the batch-scoped reads in views_recordings.py,
        # materials, assignments) — the whole reason batch scoping was safe to
        # roll out was that existing content is batch=NULL.
        #
        # Courses that DO have batches but none active are still withheld: that
        # is a real "cohort finished, nothing running" signal, not missing data.
        # Counting with distinct=True so the two annotations don't inflate each
        # other across the subjects/batches joins.
        qs = (
            Course.objects
            .select_related("board", "stream")
            .prefetch_related("categories")
            .annotate(
                subject_count=Count("subjects", distinct=True),
                _active_batches=Count(
                    "batches", filter=Q(batches__is_active=True), distinct=True),
                _total_batches=Count("batches", distinct=True),
            )
            .filter(
                Q(status=Course.STATUS_COMING_SOON)
                | Q(status=Course.STATUS_PUBLISHED, _active_batches__gt=0)
                | Q(status=Course.STATUS_PUBLISHED, _total_batches=0)
            )
            .order_by("board__name", "title")
            .distinct()
        )

        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))

        board_id = request.query_params.get("board")
        if board_id:
            qs = qs.filter(board_id=board_id)

        stream_id = request.query_params.get("stream")
        if stream_id:
            qs = qs.filter(stream_id=stream_id)

        # Scope ownership to the ACTIVE LEARNER PROFILE, not the account. Keyed
        # on `user` this reported one sibling's enrolment as the other's: on a
        # family account child A's course showed as "Enrolled" for child B, who
        # then could not enrol in it at all. Enrollment.learner_profile exists
        # and every other student-facing read already scopes on it.
        #
        # With no profile in context (account-level session) fall back to
        # account-wide: you cannot enrol without a profile anyway, so the worst
        # case is over-reporting ownership rather than allowing a duplicate.
        #
        # legacy_profile_q additionally folds in pre-backfill rows
        # (learner_profile=NULL) for the DEFAULT profile. Omitting that here was
        # not merely cosmetic: the card rendered an active "Enrol — free" button
        # over a course the student already held, and since Postgres treats
        # NULLs as DISTINCT the unique_together did NOT block the second row —
        # so tapping it double-counted Batch.seats_taken and listed the course
        # twice in My Courses.
        from accounts.auth_flow import get_active_profile
        active_profile = get_active_profile(request)
        enrolled_qs = Enrollment.objects.filter(status=Enrollment.STATUS_ACTIVE)
        enrolled_qs = (enrolled_qs.filter(legacy_profile_q(active_profile))
                       if active_profile is not None
                       else enrolled_qs.filter(user=request.user))
        enrolled_ids = set(enrolled_qs.values_list("course_id", flat=True))

        # One preview teacher per course (the primary, else any), fetched in a
        # single pass to avoid an N+1 across the catalog.
        course_ids = [c.id for c in qs]
        teacher_by_course = {}
        if course_ids:
            # Any active assignment counts. This used to require
            # batch__isnull=True — i.e. course-wide assignments only — so once
            # staffing moved to per-batch rows the card showed no teacher at
            # all. PRIMARY first so the preview names the lead, not a
            # substitute; `order` then keeps it deterministic.
            links = (
                TeachingAssignment.objects
                .filter(subject__course_id__in=course_ids, is_active=True)
                .select_related("teacher", "subject")
                .order_by(
                    "subject__course_id",
                    Case(
                        When(role=TeachingAssignment.ROLE_PRIMARY, then=0),
                        default=1,
                        output_field=IntegerField(),
                    ),
                    "order",
                )
            )
            for link in links:
                cid = link.subject.course_id
                if cid in teacher_by_course:
                    continue
                profile = link.teacher.default_learner_profile()
                name = (
                    profile.full_name
                    if profile and getattr(profile, "full_name", "")
                    else link.teacher.username
                )
                teacher_by_course[cid] = name

        data = [
            {
                "id": str(c.id),
                "title": c.title,
                "description": c.description,
                "thumbnail": request.build_absolute_uri(c.thumbnail.url) if c.thumbnail else None,
                "price": c.price,  # paise (₹1 = 100 paise); 0 = free
                "mrp": c.mrp,
                "discount_label": c.discount_label,
                "is_coming_soon": c.status == Course.STATUS_COMING_SOON,
                "category_slugs": [cat.slug for cat in c.categories.all()],
                "subscription_duration_days": c.subscription_duration_days,
                "board": (
                    {
                        "id": str(c.board.id),
                        "name": c.board.name,
                        "board_type": c.board.board_type,
                    }
                    if c.board else None
                ),
                "stream_name": c.stream.name if c.stream else None,
                "stream": (
                    {"id": str(c.stream.id), "name": c.stream.name}
                    if c.stream else None
                ),
                "subject_count": c.subject_count,
                "lead_teacher": teacher_by_course.get(c.id),
                "is_enrolled": c.id in enrolled_ids,
            }
            for c in qs
        ]
        return Response(data)


# =========================
# PUBLIC (ANONYMOUS) CATALOG — the real /courses browse page + course detail.
# Unlike CourseCatalogView above (any *authenticated* learner), these three
# views are AllowAny: the marketing site's course browser works for guests.
# Only ever exposes status=PUBLISHED courses; never leaks DRAFT/ARCHIVED.
# =========================

class PublicBoardListView(APIView):
    """GET /courses/public/boards/ — active boards + whether each currently has
    any published course, so the frontend can compute "Coming Soon" without a
    hardcoded per-board flag."""
    permission_classes = [AllowAny]

    def get(self, request):
        key = list_cache_key(request)
        cached = cache.get(key)
        if cached is not None:
            return Response(cached)

        # NOTE (Phase B): dropped the previous `is_active=True` filter so that
        # coming-soon (inactive) boards are also returned, each flagged via the
        # explicit `coming_soon` boolean below. Without this the flag would be
        # dead weight (always False), and the public /courses page + navbar need
        # coming-soon boards to render as dormant chips.
        boards = (
            Board.objects
            .annotate(
                published_count=Count(
                    "courses", filter=Q(courses__status=Course.STATUS_PUBLISHED), distinct=True,
                )
            )
            .order_by("display_order", "name")
        )
        data = [
            {
                "id": str(b.id),
                "name": b.name,
                "board_type": b.board_type,
                "description": b.description,
                "slug": b.slug,
                "logo": request.build_absolute_uri(b.logo.url) if b.logo else None,
                "display_order": b.display_order,
                "coming_soon": not b.is_active,
                "has_published_courses": b.published_count > 0,
            }
            for b in boards
        ]
        cache.set(key, data, LIST_TTL)
        return Response(data)


class BoardNotifyMeView(APIView):
    """POST /courses/public/boards/<board_id>/notify/ — anonymous "notify me
    when {board} launches" lead capture from a locked board chip. The first
    anonymous-write endpoint in this codebase, so it's throttled (see
    board_notify in DEFAULT_THROTTLE_RATES) and de-duped via the model's
    (board, email) unique constraint rather than trusting client behavior."""
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "board_notify"

    def post(self, request, board_id):
        board = get_object_or_404(Board, id=board_id)
        email = (request.data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return Response({"detail": "A valid email is required."}, status=status.HTTP_400_BAD_REQUEST)
        BoardNotifyRequest.objects.get_or_create(board=board, email=email)
        return Response({"ok": True}, status=status.HTTP_201_CREATED)


class CourseNotifyMeView(APIView):
    """POST /courses/public/<course_id>/notify/ — anonymous "notify me when
    {course} launches" lead capture from a coming-soon course card/quick-view.
    Mirrors BoardNotifyMeView exactly (same throttle scope, same
    get_or_create dedup by (course, email))."""
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "board_notify"

    def post(self, request, course_id):
        course = get_object_or_404(Course, id=course_id)
        email = (request.data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return Response({"detail": "A valid email is required."}, status=status.HTTP_400_BAD_REQUEST)
        CourseNotifyRequest.objects.get_or_create(course=course, email=email)
        return Response({"ok": True}, status=status.HTTP_201_CREATED)


def _catalog_seats_left(course):
    """Sum capacity - seats_taken across all is_active batches (there is no
    single "current batch" flag, so a course can have several concurrently
    active cohorts). None if any active batch is uncapped or there are no
    active batches at all — the frontend hides the seats line in that case
    rather than showing a misleading number."""
    batches = getattr(course, "active_batches", [])
    if not batches:
        return None
    total = 0
    for b in batches:
        if b.capacity is None:
            return None
        total += max(0, b.capacity - b._seats)
    return total


class PublicCourseCatalogView(APIView):
    """GET /courses/public/catalog/ — same shop-card shape as CourseCatalogView,
    minus the per-user `is_enrolled` query (the public site has no logged-in
    user to key it to; the frontend checks enrollment separately once signed in).
    Optional ?q=, ?board=, ?stream= — same semantics as CourseCatalogView."""
    permission_classes = [AllowAny]

    def get(self, request):
        key = list_cache_key(request)
        cached = cache.get(key)
        if cached is not None:
            return Response(cached)

        # Both PUBLISHED and COMING_SOON are publicly visible; only PUBLISHED is
        # purchasable (the frontend gates the buy flow on `is_coming_soon`).
        qs = (
            Course.objects
            .filter(status__in=[Course.STATUS_PUBLISHED, Course.STATUS_COMING_SOON])
            .select_related("board", "stream", "details")
            .prefetch_related(
                "categories",
                Prefetch(
                    "batches",
                    queryset=Batch.objects.filter(is_active=True).annotate(
                        _seats=Count("enrollments", filter=Q(enrollments__status="ACTIVE"))
                    ),
                    to_attr="active_batches",
                ),
            )
            .annotate(subject_count=Count("subjects", distinct=True))
            .order_by("board__name", "title")
            .distinct()
        )

        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))

        board_id = request.query_params.get("board")
        if board_id:
            qs = qs.filter(board_id=board_id)

        stream_id = request.query_params.get("stream")
        if stream_id:
            qs = qs.filter(stream_id=stream_id)

        category = request.query_params.get("category")
        if category:
            qs = qs.filter(categories__slug=category)

        group = request.query_params.get("group")
        if group:
            qs = qs.filter(categories__group=group)

        kind = request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)

        from global_settings.models import GlobalSettings
        is_free = GlobalSettings.load().effective_mode == GlobalSettings.PAYMENT_FREE

        data = [
            {
                "id": str(c.id),
                "title": c.title,
                "slug": c.slug,
                "description": c.description,
                "class_level": c.class_level,
                "price": c.price,
                # price_override is a per-Batch concern (Batch.effective_price);
                # a catalog card has no batch context, so effective_price == the
                # course-level price here. See report notes.
                "effective_price": c.price,
                "mrp": c.mrp,
                "discount_label": c.discount_label,
                "badge": c.badge,
                "is_free": is_free,
                "is_coming_soon": c.status == Course.STATUS_COMING_SOON,
                "subscription_duration_days": c.subscription_duration_days,
                "thumbnail": request.build_absolute_uri(c.thumbnail.url) if c.thumbnail else None,
                "board": (
                    {"id": str(c.board.id), "name": c.board.name, "board_type": c.board.board_type}
                    if c.board else None
                ),
                "stream_name": c.stream.name if c.stream else None,
                "category_slugs": [cat.slug for cat in c.categories.all()],
                "subject_count": c.subject_count,
                "duration_weeks": getattr(getattr(c, "details", None), "duration_weeks", None) or None,
                "seats_left": _catalog_seats_left(c),
            }
            for c in qs
        ]
        cache.set(key, data, LIST_TTL)
        return Response(data)


# Courses publicly visible on the marketing site: purchasable (PUBLISHED) plus
# coming-soon teasers (COMING_SOON). DRAFT/ARCHIVED are never exposed.
PUBLIC_COURSE_STATUSES = [Course.STATUS_PUBLISHED, Course.STATUS_COMING_SOON]


def _public_course_detail_queryset():
    """Shared queryset (with prefetches) for the by-id and by-slug detail views."""
    return (
        Course.objects.filter(status__in=PUBLIC_COURSE_STATUSES)
        .select_related("board", "stream", "details")
        .prefetch_related(
            "categories",
            Prefetch(
                "subjects",
                queryset=Subject.objects.order_by("order").prefetch_related(
                    Prefetch("chapters", queryset=Chapter.objects.order_by("order")),
                ),
            ),
            Prefetch("batches", queryset=Batch.objects.filter(is_active=True)),
        )
    )


def _serialize_public_course_detail(course, request):
    """Build the full public course-detail dict. Shared by the by-id and by-slug
    detail endpoints so their shapes never drift."""
    from global_settings.models import GlobalSettings

    details = getattr(course, "details", None)
    return {
        "id": str(course.id),
        "title": course.title,
        "slug": course.slug,
        "description": course.description,
        "price": course.price,
        # price_override is a per-Batch concern (see Batch.effective_price on each
        # batch below); the course-level effective_price is just Course.price.
        "effective_price": course.price,
        "mrp": course.mrp,
        "discount_label": course.discount_label,
        "badge": course.badge,
        "is_free": GlobalSettings.load().effective_mode == GlobalSettings.PAYMENT_FREE,
        "is_coming_soon": course.status == Course.STATUS_COMING_SOON,
        "thumbnail": request.build_absolute_uri(course.thumbnail.url) if course.thumbnail else None,
        "board": (
            {"id": str(course.board.id), "name": course.board.name, "board_type": course.board.board_type}
            if course.board else None
        ),
        "stream_name": course.stream.name if course.stream else None,
        "category_slugs": [cat.slug for cat in course.categories.all()],
        "details": CourseDetailSerializer(details).data if details else None,
        "subjects": [
            {
                "id": str(s.id),
                "name": s.name,
                "order": s.order,
                "textbook": s.textbook,
                "image": request.build_absolute_uri(s.image.url) if s.image else None,
                "chapters": [
                    {
                        "id": str(ch.id),
                        "title": ch.title,
                        "order": ch.order,
                        "content_html": ch.content_html,
                    }
                    for ch in s.chapters.all()
                ],
            }
            for s in course.subjects.all()
        ],
        "batches": [
            {
                "id": str(b.id),
                "name": b.name,
                "code": b.code,
                "effective_price": b.effective_price,
                "is_full": b.is_full,
                "capacity": b.capacity,
                "seats_taken": b.seats_taken,
            }
            for b in course.batches.all()
        ],
    }


class PublicCourseDetailView(APIView):
    """GET /courses/public/<id>/ — full anonymous course detail: subjects →
    chapters (with content), active batches (with effective price), course
    details and thumbnail. Only PUBLISHED / COMING_SOON courses; anything else
    404s (never 403 — a status split would let a caller distinguish "exists but
    draft" from "doesn't exist" by UUID probing). Not cached (single-row lookup;
    matches content app's convention of caching only list endpoints)."""
    permission_classes = [AllowAny]

    def get(self, request, course_id):
        course = get_object_or_404(_public_course_detail_queryset(), id=course_id)
        return Response(_serialize_public_course_detail(course, request))


class PublicCourseBySlugView(APIView):
    """GET /courses/public/by-slug/<slug>/ — identical response shape to
    PublicCourseDetailView, looked up by slug instead of UUID. Additive; the
    by-id route keeps working unchanged."""
    permission_classes = [AllowAny]

    def get(self, request, slug):
        course = get_object_or_404(_public_course_detail_queryset(), slug=slug)
        return Response(_serialize_public_course_detail(course, request))


class PublicFeaturedView(APIView):
    """GET /courses/public/featured/ — homepage 'Featured courses' grid.
    Cards derive title/price/thumbnail from a linked Course/Board when set,
    computed fresh on every request (never written back to ShowcaseCourse)."""
    permission_classes = [AllowAny]

    def get(self, request):
        key = list_cache_key(request)
        cached = cache.get(key)
        if cached is not None:
            return Response(cached)

        cards_qs = (
            ShowcaseCourse.objects.filter(is_active=True)
            .select_related("course", "board")
            .order_by("order")
        )

        cards = []
        for card in cards_qs:
            course = card.course
            board = card.board

            # title
            if course:
                title = course.title
            elif board:
                title = board.name
            else:
                title = card.title

            # price_label — course price is in paise; format as "₹X,XXX/month".
            # A zero price is a real state, not missing data: the platform runs
            # free at launch (GlobalSettings.live_launch_free_mode), so say
            # "Free" rather than emitting "₹0/month" for the card to render.
            if course:
                price_label = (
                    "Free" if not course.price
                    else f"₹{course.price // 100:,}/month"
                )
            elif board:
                price_label = None
            else:
                price_label = card.price_label

            # is_coming_soon
            if course:
                is_coming_soon = course.status == Course.STATUS_COMING_SOON
            elif board:
                is_coming_soon = board.is_active is False
            else:
                is_coming_soon = False

            # thumbnail (absolute URL)
            if course and course.thumbnail:
                thumbnail = request.build_absolute_uri(course.thumbnail.url)
            elif board and board.logo:
                thumbnail = request.build_absolute_uri(board.logo.url)
            elif card.image:
                thumbnail = request.build_absolute_uri(card.image.url)
            else:
                thumbnail = card.image_url or None

            cards.append({
                "id": card.id,
                "title": title,
                "price_label": price_label,
                "mrp": course.mrp if course else None,
                "discount_label": course.discount_label if course else None,
                "is_coming_soon": is_coming_soon,
                "thumbnail": thumbnail,
                "tutor_name": card.tutor_name,
                "level_label": card.level_label,
                "ribbon": card.ribbon,
                "stars": card.stars,
                "review_count": card.review_count,
                "fact_line": card.fact_line,
                "is_explore_card": card.is_explore_card,
                "categories": card.categories,
                "gradient_css": card.gradient_css,
                "icon": card.icon,
                "link_path": card.link_path,
                "link_state": card.link_state,
                "course_id": str(card.course_id) if card.course_id else None,
                "board_id": str(card.board_id) if card.board_id else None,
                "order": card.order,
            })

        data = {"cards": cards}
        cache.set(key, data, LIST_TTL)
        return Response(data)


class PublicNavMenuView(APIView):
    """GET /courses/public/nav-menu/ — the navbar Courses mega-menu payload.
    Returns exactly two categories the backend can back with data: "school"
    (board tabs grouped by board_type, each listing its boards AND the classes
    each board offers) and "competitive" (courses tagged with a
    CourseCategory whose group == 'competitive'). The frontend merges its static
    "Skill & Career" category client-side; it is deliberately NOT returned here."""
    permission_classes = [AllowAny]

    @staticmethod
    def _class_links(board, group_key, prefix=False):
        """One nav link per class this board actually offers.

        Labels come from `class_level`/`stream` rather than the course title,
        so the menu reads "Class 11 · Science" regardless of how a given course
        happens to be titled in the catalog. Courses with no class_level (the
        coaching/competitive rows) are skipped — they belong to the separate
        "competitive" category below, not under a board."""
        courses = (
            Course.objects
            .filter(board=board, status__in=PUBLIC_COURSE_STATUSES,
                    class_level__isnull=False)
            .select_related("stream")
            .order_by("class_level", "stream__name", "title")
        )
        links = []
        for c in courses:
            label = f"Class {c.class_level}"
            if c.stream and c.stream.name:
                label += f" · {c.stream.name.title()}"
            if prefix:
                label = f"{board.name} · {label}"
            if c.status == Course.STATUS_COMING_SOON:
                links.append({"label": label, "soon": True})
            else:
                links.append({"label": label, "to": f"/courses/{c.slug}"})
        return links

    def get(self, request):
        key = list_cache_key(request)
        cached = cache.get(key)
        if cached is not None:
            return Response(cached)

        # --- "school" category: one tab per board_type present ---
        boards = list(Board.objects.order_by("display_order", "name"))
        type_display = dict(Board.TYPE_CHOICES)  # {"CENTRAL": "Central", ...}

        tabs = []
        seen_types = []
        for b in boards:
            if b.board_type not in seen_types:
                seen_types.append(b.board_type)
        for board_type in seen_types:
            group_key = board_type.lower()
            disp = type_display.get(board_type, board_type.title())
            label = f"{disp} Boards"
            group_boards = [b for b in boards if b.board_type == board_type]
            # Prefix class labels with the board name only when the tab holds
            # more than one active board — otherwise every label would read
            # "CBSE · Class 9" in a tab that already says "Central Boards".
            multi = sum(1 for b in group_boards if b.is_active) > 1

            links = []
            for b in group_boards:
                if not b.is_active:
                    links.append({"label": b.name, "soon": True})
                    continue
                links.append({
                    "label": b.name,
                    "to": "/courses",
                    "state": {"selectedBoardGroup": group_key, "selectedBoard": b.slug},
                })
                # ...then the individual classes offered by that board. Without
                # these the menu only ever offers a whole board, which is a
                # regression the frontend can't paper over: mergeLiveNavMenu()
                # replaces its static tabs wholesale as soon as this endpoint
                # answers, so anything omitted here simply vanishes from the nav.
                links.extend(self._class_links(b, group_key, prefix=multi))
            tabs.append({
                "id": group_key,
                "label": label,
                "heading": f"{disp} Board Courses",
                "links": links,
                "viewAll": {
                    "label": f"View All {label}",
                    "to": "/courses",
                    "state": {"selectedBoardGroup": group_key},
                },
            })

        # --- "competitive" category: courses tagged group == "competitive" ---
        competitive_courses = (
            Course.objects
            .filter(
                status__in=PUBLIC_COURSE_STATUSES,
                categories__group=CourseCategory.GROUP_COMPETITIVE,
            )
            .order_by("display_order", "title")
            .distinct()
        )
        competitive_links = []
        for c in competitive_courses:
            if c.status == Course.STATUS_COMING_SOON:
                competitive_links.append({"label": c.title, "soon": True})
            else:
                competitive_links.append({"label": c.title, "to": f"/courses/{c.slug}"})

        data = {
            "categories": [
                {"key": "school", "tabs": tabs},
                {
                    "key": "competitive",
                    "sections": [
                        {"heading": "Competitive Exams", "links": competitive_links},
                    ],
                },
            ]
        }
        cache.set(key, data, LIST_TTL)
        return Response(data)
