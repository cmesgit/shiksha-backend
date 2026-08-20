"""
skills/course_views.py — Udemy-style skill course API.

Routes (wired in skills/urls.py):
  Public marketplace
    GET  /skill/courses/                 → approved courses list
    GET  /skill/courses/<id>/            → course detail + sections

  Teacher (must have GUEST/BOTH TeacherProfile)
    GET  /skill/teacher/courses/         → own courses
    POST /skill/teacher/courses/         → create draft
    PATCH/DELETE /skill/teacher/courses/<id>/
    POST  /skill/teacher/courses/<id>/submit/   → submit for admin review
    POST  /skill/teacher/courses/<id>/sections/ → add section
    POST  /skill/teacher/sections/<sid>/lectures/ → add lecture
    PATCH/DELETE /skill/teacher/lectures/<lid>/

  Student
    POST /skill/courses/<id>/enroll/     → free enroll (or check payment mode)
    GET  /skill/my-courses/              → enrolled courses
    POST /skill/my-courses/<id>/progress/ → mark lecture complete
    GET  /skill/my-courses/<id>/progress/ → progress for one enrollment

  Admin
    GET  /skill/admin/courses/           → submitted courses queue
    POST /skill/admin/courses/<id>/review/  → approve / reject
"""
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import ValidationError, NotFound, PermissionDenied
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework import status

from accounts.permissions import IsAdmin
from accounts.auth_flow import get_active_profile
from enrollments.payments import get_payment_provider

from .course_models import (
    SkillCourse, SkillCourseSection, SkillCourseLecture,
    SkillCourseEnrollment, SkillLectureProgress,
)
from .course_serializers import (
    SkillCourseListSerializer, SkillCourseDetailSerializer,
    SkillCourseWriteSerializer, SectionSerializer, LectureSerializer,
    EnrollmentSerializer, LectureProgressSerializer,
)


def _teacher_profile(user):
    tp = getattr(user, "teacher_profile", None)
    if tp is None:
        raise PermissionDenied("You do not have a teacher profile.")
    return tp


def _own_course(user, course_id):
    tp = _teacher_profile(user)
    c = SkillCourse.objects.filter(id=course_id, teacher_profile=tp).first()
    if not c:
        raise NotFound("Course not found.")
    return c


# ══════════════════════════════════════════
# Public marketplace
# ══════════════════════════════════════════

class PublicCourseListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        qs = SkillCourse.objects.filter(status=SkillCourse.STATUS_APPROVED).select_related(
            "teacher_profile__user", "category"
        )
        cat = request.query_params.get("category")
        if cat:
            qs = qs.filter(category__slug=cat)
        # Filter to a single expert's courses. `teacher` is the ExpertProfile id
        # (what the frontend knows as the expert id); it maps back to the
        # underlying TeacherProfile via the expert_profile one-to-one.
        teacher = request.query_params.get("teacher")
        if teacher:
            qs = qs.filter(teacher_profile__expert_profile__id=teacher)
        q = request.query_params.get("q", "").strip()
        if q:
            qs = qs.filter(title__icontains=q)
        data = SkillCourseListSerializer(qs, many=True, context={"request": request}).data
        return Response(data)


class PublicCourseDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, course_id):
        c = SkillCourse.objects.filter(
            id=course_id, status=SkillCourse.STATUS_APPROVED
        ).select_related("teacher_profile__user", "category").first()
        if not c:
            raise NotFound("Course not found.")
        return Response(SkillCourseDetailSerializer(c, context={"request": request}).data)


# ══════════════════════════════════════════
# Teacher — own courses
# ══════════════════════════════════════════

class TeacherCourseListCreateView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        tp = _teacher_profile(request.user)
        qs = SkillCourse.objects.filter(teacher_profile=tp).select_related("category")
        return Response(SkillCourseListSerializer(qs, many=True, context={"request": request}).data)

    def post(self, request):
        tp = _teacher_profile(request.user)
        ser = SkillCourseWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        course = ser.save(teacher_profile=tp, status=SkillCourse.STATUS_DRAFT)
        return Response(
            SkillCourseDetailSerializer(course, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class TeacherCourseDetailView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request, course_id):
        c = _own_course(request.user, course_id)
        return Response(SkillCourseDetailSerializer(c, context={"request": request}).data)

    def patch(self, request, course_id):
        c = _own_course(request.user, course_id)
        if c.status == SkillCourse.STATUS_APPROVED:
            raise ValidationError("Cannot edit an approved course. Submit a new version.")
        ser = SkillCourseWriteSerializer(c, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save(status=SkillCourse.STATUS_DRAFT)  # edits revert to draft
        return Response(SkillCourseDetailSerializer(c, context={"request": request}).data)

    def delete(self, request, course_id):
        c = _own_course(request.user, course_id)
        if c.status == SkillCourse.STATUS_APPROVED:
            raise ValidationError("Cannot delete an approved course.")
        c.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TeacherCourseSubmitView(APIView):
    """Teacher submits draft → admin review."""
    permission_classes = [IsAuthenticated]

    def post(self, request, course_id):
        c = _own_course(request.user, course_id)
        if c.status not in (SkillCourse.STATUS_DRAFT, SkillCourse.STATUS_REJECTED):
            raise ValidationError(f"Cannot submit a course in '{c.status}' state.")
        if not c.title.strip():
            raise ValidationError("Course must have a title.")
        c.status = SkillCourse.STATUS_SUBMITTED
        c.save(update_fields=["status", "updated_at"])
        return Response({"detail": "Submitted for admin review.", "status": c.status})


class TeacherSectionView(APIView):
    """List + add sections under a teacher's course."""
    permission_classes = [IsAuthenticated]

    def post(self, request, course_id):
        c = _own_course(request.user, course_id)
        ser = SectionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        order = c.sections.count()
        sec = ser.save(course=c, order=order)
        return Response(SectionSerializer(sec).data, status=status.HTTP_201_CREATED)


class TeacherLectureView(APIView):
    """Add a lecture to a section."""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, section_id):
        tp = _teacher_profile(request.user)
        sec = SkillCourseSection.objects.filter(
            id=section_id, course__teacher_profile=tp
        ).first()
        if not sec:
            raise NotFound("Section not found.")
        ser = LectureSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        order = sec.lectures.count()
        lec = ser.save(section=sec, order=order)
        return Response(LectureSerializer(lec).data, status=status.HTTP_201_CREATED)


class TeacherLectureDetailView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _get(self, user, lecture_id):
        tp = _teacher_profile(user)
        lec = SkillCourseLecture.objects.filter(
            id=lecture_id, section__course__teacher_profile=tp
        ).first()
        if not lec:
            raise NotFound("Lecture not found.")
        return lec

    def patch(self, request, lecture_id):
        lec = self._get(request.user, lecture_id)
        ser = LectureSerializer(lec, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(LectureSerializer(lec).data)

    def delete(self, request, lecture_id):
        lec = self._get(request.user, lecture_id)
        lec.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ══════════════════════════════════════════
# Student — enroll + progress
# ══════════════════════════════════════════

class CourseEnrollView(APIView):
    """Enroll (free) or initiate payment. Respects the payment provider."""
    permission_classes = [IsAuthenticated]

    def post(self, request, course_id):
        learner = get_active_profile(request)
        if not learner:
            raise PermissionDenied("Select a learner profile before enrolling.")

        c = SkillCourse.objects.filter(
            id=course_id, status=SkillCourse.STATUS_APPROVED
        ).first()
        if not c:
            raise NotFound("Course not found.")

        existing = SkillCourseEnrollment.objects.filter(
            learner_profile=learner, course=c
        ).first()
        if existing:
            return Response(EnrollmentSerializer(existing).data)

        # Course money is settled DIRECTLY between the learner and the expert —
        # the platform never collects it. We grant access on enrol and hand back
        # the expert's payee details + their chat id so the learner can pay them
        # directly and coordinate. (In the free launch phase price is 0 anyway.)
        enroll = SkillCourseEnrollment.objects.create(
            learner_profile=learner,
            course=c,
            status=SkillCourseEnrollment.STATUS_ACTIVE,
            amount_paid=0,           # platform collected nothing (P2P)
            payment_ref="direct",
        )

        tp = c.teacher_profile
        ep = getattr(tp, "expert_profile", None) if tp else None
        body = EnrollmentSerializer(enroll).data
        if c.price and c.price > 0:
            body.update({
                "settlement":        "direct",
                "amount":            c.price,
                "price_rupees":      c.price_rupees,
                "pay_to":            ep.pay_to() if ep else None,
                "expert_teacher_id": str(tp.id) if tp else None,  # open chat
                "detail": "Pay the expert directly and coordinate over chat.",
            })
        return Response(body, status=status.HTTP_201_CREATED)


class MySkillCoursesView(APIView):
    """All courses the active learner profile is enrolled in."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        learner = get_active_profile(request)
        if not learner:
            raise PermissionDenied("Select a learner profile.")
        qs = SkillCourseEnrollment.objects.filter(
            learner_profile=learner, status=SkillCourseEnrollment.STATUS_ACTIVE
        ).select_related("course")
        return Response(EnrollmentSerializer(qs, many=True).data)


class CourseLectureProgressView(APIView):
    """GET → progress list. POST { lecture_id } → mark lecture complete."""
    permission_classes = [IsAuthenticated]

    def _enrollment(self, request, course_id):
        """This PROFILE's enrolment — never a sibling's.

        This used to filter `learner_profile__account=user` and take
        `.first()`, i.e. any enrolment on the account. Since POST below marks
        a lecture complete and can flip the enrolment to STATUS_COMPLETED,
        child A pressing "complete" could write into child B's enrolment and
        finish a course B was halfway through.

        The old line `learner = get_active_profile(user)` passed a User where
        the helper expects a REQUEST, so it silently returned nothing and the
        result was discarded — the intended scoping was started and never
        wired up.
        """
        learner = get_active_profile(request)
        if learner is None:
            raise NotFound("Select a learner profile.")
        enroll = SkillCourseEnrollment.objects.filter(
            learner_profile=learner,
            course_id=course_id,
            status=SkillCourseEnrollment.STATUS_ACTIVE,
        ).first()
        if not enroll:
            raise NotFound("Enrollment not found.")
        return enroll

    def get(self, request, course_id):
        enroll = self._enrollment(request, course_id)
        prog   = SkillLectureProgress.objects.filter(enrollment=enroll)
        total  = SkillCourseLecture.objects.filter(section__course=enroll.course).count()
        done   = prog.count()
        pct    = round(done * 100 / total) if total else 0
        return Response({
            "total":   total,
            "done":    done,
            "percent": pct,
            "completed": LectureProgressSerializer(prog, many=True).data,
        })

    def post(self, request, course_id):
        enroll = self._enrollment(request, course_id)
        lid    = request.data.get("lecture_id")
        if not lid:
            raise ValidationError("lecture_id required.")
        lec = SkillCourseLecture.objects.filter(
            id=lid, section__course=enroll.course
        ).first()
        if not lec:
            raise NotFound("Lecture not found in this course.")
        prog, _ = SkillLectureProgress.objects.get_or_create(enrollment=enroll, lecture=lec)

        # If all lectures done, mark enrollment complete.
        total = SkillCourseLecture.objects.filter(section__course=enroll.course).count()
        done  = SkillLectureProgress.objects.filter(enrollment=enroll).count()
        if done >= total:
            enroll.status       = SkillCourseEnrollment.STATUS_COMPLETED
            enroll.completed_at = timezone.now()
            enroll.save(update_fields=["status","completed_at"])

        return Response({"ok": True, "percent": round(done * 100 / total) if total else 0})


# ══════════════════════════════════════════
# Admin — course review queue
# ══════════════════════════════════════════

class AdminSkillCourseQueueView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        st = request.query_params.get("status", SkillCourse.STATUS_SUBMITTED)
        qs = SkillCourse.objects.select_related("teacher_profile__user", "category")
        # status=all → every course regardless of review state, for the SkillDev
        # CMS media-moderation tab (which needs to reach any course's cover,
        # not just the submitted-review queue).
        if st != "all":
            qs = qs.filter(status=st)
        return Response(SkillCourseListSerializer(qs, many=True, context={"request": request}).data)


class AdminSkillCourseReviewView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, course_id):
        c = SkillCourse.objects.filter(
            id=course_id, status=SkillCourse.STATUS_SUBMITTED
        ).first()
        if not c:
            raise NotFound("Course not in submitted queue.")
        action = request.data.get("action", "")
        if action == "approve":
            c.status = SkillCourse.STATUS_APPROVED
            c.reject_reason = ""
        elif action == "reject":
            c.status = SkillCourse.STATUS_REJECTED
            c.reject_reason = request.data.get("reason", "")
        else:
            raise ValidationError("action must be 'approve' or 'reject'.")
        c.reviewed_by = request.user
        c.reviewed_at = timezone.now()
        c.save(update_fields=["status","reject_reason","reviewed_by","reviewed_at","updated_at"])
        return Response({"detail": f"Course {c.status}.", "id": str(c.id)})


class AdminSkillCourseMediaView(APIView):
    """PATCH /skill/admin/courses/<id>/media/  → media moderation: replace
    a course's cover image regardless of its review status."""
    permission_classes = [IsAuthenticated, IsAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def patch(self, request, course_id):
        c = SkillCourse.objects.filter(id=course_id).first()
        if not c:
            raise NotFound("Course not found.")
        cover_image = request.data.get("cover_image")
        if cover_image is None:
            raise ValidationError("cover_image is required.")
        c.cover_image = cover_image
        c.save(update_fields=["cover_image", "updated_at"])
        return Response(SkillCourseListSerializer(c, context={"request": request}).data)
