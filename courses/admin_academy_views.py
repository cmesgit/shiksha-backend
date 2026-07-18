"""Admin academy endpoints: subject-teacher assignment and batch management.

These back the admin panel's Course Management screen so teacher assignment and
batches no longer require Django admin. They mirror the style of the existing
Admin*View classes in courses/views.py (APIView + [IsAuthenticated, IsAdmin]).

Routes (added in courses/urls.py):

    GET    courses/admin/teachers/?q=            approved teachers (assign picker)

    GET    courses/admin/subjects/<subject_id>/teachers/     list assignments
    POST   courses/admin/subjects/<subject_id>/teachers/     assign a teacher
           body: { "teacher_id": "<uuid>", "display_role": "PRIMARY"|"ASSISTANT" }
    PATCH  courses/admin/subject-teachers/<int:assignment_id>/   change role
    DELETE courses/admin/subject-teachers/<int:assignment_id>/   unassign

    GET    courses/admin/courses/<course_id>/batches/       list batches
    POST   courses/admin/courses/<course_id>/batches/       create batch
    PATCH  courses/admin/batches/<batch_id>/                update batch
    DELETE courses/admin/batches/<batch_id>/                delete batch
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin
from .models import Batch, Course, Subject, SubjectTeacher, TeachingAssignment

User = get_user_model()

VALID_ROLES = (SubjectTeacher.ROLE_PRIMARY, SubjectTeacher.ROLE_ASSISTANT)
VALID_TA_ROLES = (
    TeachingAssignment.ROLE_PRIMARY,
    TeachingAssignment.ROLE_ASSISTANT,
    TeachingAssignment.ROLE_SUBSTITUTE,
)
# SubjectTeacher only has PRIMARY/ASSISTANT; map the richer TeachingAssignment
# roles down when dual-writing the legacy row.
_TA_TO_ST_ROLE = {
    TeachingAssignment.ROLE_PRIMARY: SubjectTeacher.ROLE_PRIMARY,
    TeachingAssignment.ROLE_ASSISTANT: SubjectTeacher.ROLE_ASSISTANT,
    TeachingAssignment.ROLE_SUBSTITUTE: SubjectTeacher.ROLE_ASSISTANT,
}


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #
def _teacher_name(user):
    lp = None
    if hasattr(user, "default_learner_profile"):
        try:
            lp = user.default_learner_profile()
        except Exception:
            lp = None
    if lp and getattr(lp, "full_name", ""):
        return lp.full_name
    full = (user.get_full_name() or "").strip()
    return full or user.username or user.email


def teacher_brief(user, st=None, request=None):
    """A teacher's assignable/assigned summary, including profile bits the admin
    wants to see at a glance (role, qualification, photo, rating)."""
    profile = getattr(user, "teacher_profile", None)

    photo = None
    if profile and getattr(profile, "photo", None):
        try:
            photo = request.build_absolute_uri(profile.photo.url) if request else profile.photo.url
        except Exception:
            photo = None

    data = {
        "user_id": str(user.id),
        "name": _teacher_name(user),
        "email": user.email,
        "qualification": (getattr(profile, "qualification", "") or ""),
        "rating": float(profile.rating) if (profile and profile.rating is not None) else None,
        "photo": photo,
    }
    if st is not None:
        data["assignment_id"] = st.id  # SubjectTeacher pk (integer)
        data["display_role"] = st.display_role
        data["order"] = st.order
    return data


def subject_teachers_payload(subject, request=None):
    """Ordered list of a subject's assigned teachers. Callers should prefetch
    ``subject_teachers`` to avoid a query per subject."""
    sts = (
        subject.subject_teachers
        .select_related("teacher", "teacher__teacher_profile")
        .order_by("order")
    )
    return [teacher_brief(st.teacher, st, request) for st in sts]


def _batch_payload(b):
    # Prefer the annotated seat count (one query for the whole list); fall back
    # to the model property for single-object responses.
    seats = getattr(b, "_seats", None)
    if seats is None:
        seats = b.seats_taken
    return {
        "id": str(b.id),
        "name": b.name,
        "code": b.code,
        "year": b.year,
        "start_date": b.start_date.isoformat() if b.start_date else None,
        "end_date": b.end_date.isoformat() if b.end_date else None,
        "capacity": b.capacity,
        "is_active": b.is_active,
        "seats_taken": seats,
        "is_full": (b.capacity is not None and seats >= b.capacity),
    }


def _parse_int_or_none(value):
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date_or_none(value):
    if value in (None, "", "null"):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _apply_optional_batch_fields(batch, data):
    if "year" in data:
        batch.year = _parse_int_or_none(data.get("year"))
    if "capacity" in data:
        batch.capacity = _parse_int_or_none(data.get("capacity"))
    if "start_date" in data:
        batch.start_date = _parse_date_or_none(data.get("start_date"))
    if "end_date" in data:
        batch.end_date = _parse_date_or_none(data.get("end_date"))


# --------------------------------------------------------------------------- #
# Teacher picker
# --------------------------------------------------------------------------- #
class AdminTeacherListView(APIView):
    """Approved teachers available to assign to a subject. Optional ?q= search."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        q = request.query_params.get("q", "").strip()
        qs = (
            User.objects.filter(
                teacher_profile__is_approved=True,
                user_roles__role__name="TEACHER",
                user_roles__is_active=True,
            )
            .select_related("teacher_profile")
            .distinct()
        )
        if q:
            qs = qs.filter(
                Q(email__icontains=q)
                | Q(username__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
            )
        return Response([teacher_brief(u, request=request) for u in qs[:100]])


# --------------------------------------------------------------------------- #
# Subject <-> teacher assignment
# --------------------------------------------------------------------------- #
class AdminSubjectTeachersView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, subject_id):
        subject = get_object_or_404(Subject, id=subject_id)
        return Response(subject_teachers_payload(subject, request))

    def post(self, request, subject_id):
        subject = get_object_or_404(Subject, id=subject_id)

        teacher_id = request.data.get("teacher_id") or request.data.get("user_id")
        if not teacher_id:
            return Response(
                {"detail": "teacher_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        role = (request.data.get("display_role") or SubjectTeacher.ROLE_PRIMARY).upper()
        if role not in VALID_ROLES:
            role = SubjectTeacher.ROLE_PRIMARY

        try:
            teacher = User.objects.select_related("teacher_profile").get(pk=teacher_id)
        except (User.DoesNotExist, DjangoValidationError, ValueError):
            return Response(
                {"detail": "Teacher not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        tp = getattr(teacher, "teacher_profile", None)
        if not (tp and tp.is_approved):
            return Response(
                {"detail": "Only approved teachers can be assigned."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        next_order = (
            SubjectTeacher.objects.filter(subject=subject)
            .order_by("-order")
            .values_list("order", flat=True)
            .first()
        )
        order = (next_order or 0) + 1

        try:
            st = SubjectTeacher.objects.create(
                subject=subject, teacher=teacher, display_role=role, order=order
            )
        except IntegrityError:
            return Response(
                {"detail": "This teacher is already assigned to this subject."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(teacher_brief(teacher, st, request), status=status.HTTP_201_CREATED)


class AdminSubjectTeacherDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def patch(self, request, assignment_id):
        st = get_object_or_404(
            SubjectTeacher.objects.select_related("teacher", "teacher__teacher_profile"),
            pk=assignment_id,
        )
        role = (request.data.get("display_role") or "").upper()
        if role in VALID_ROLES:
            st.display_role = role
            st.save(update_fields=["display_role"])
        return Response(teacher_brief(st.teacher, st, request))

    def delete(self, request, assignment_id):
        st = get_object_or_404(SubjectTeacher, pk=assignment_id)
        st.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Batch management
# --------------------------------------------------------------------------- #
class AdminCourseBatchesView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, course_id):
        get_object_or_404(Course, id=course_id)
        batches = (
            Batch.objects.filter(course_id=course_id)
            .annotate(_seats=Count("enrollments", filter=Q(enrollments__status="ACTIVE")))
            .order_by("-year", "code")
        )
        return Response([_batch_payload(b) for b in batches])

    def post(self, request, course_id):
        course = get_object_or_404(Course, id=course_id)

        name = (request.data.get("name") or "").strip()
        code = (request.data.get("code") or "").strip()
        if not name or not code:
            return Response(
                {"detail": "Batch name and code are both required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch = Batch(
            course=course,
            name=name,
            code=code,
            is_active=bool(request.data.get("is_active", True)),
        )
        _apply_optional_batch_fields(batch, request.data)

        try:
            batch.save()
        except IntegrityError:
            return Response(
                {"detail": f"A batch with code '{code}' already exists in this course."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(_batch_payload(batch), status=status.HTTP_201_CREATED)


class AdminBatchDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def patch(self, request, batch_id):
        batch = get_object_or_404(Batch, id=batch_id)

        if "name" in request.data and request.data.get("name") is not None:
            batch.name = str(request.data["name"]).strip() or batch.name
        if "code" in request.data and request.data.get("code") is not None:
            batch.code = str(request.data["code"]).strip() or batch.code
        if "is_active" in request.data:
            batch.is_active = bool(request.data["is_active"])
        _apply_optional_batch_fields(batch, request.data)

        try:
            batch.save()
        except IntegrityError:
            return Response(
                {"detail": f"A batch with code '{batch.code}' already exists in this course."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(_batch_payload(batch))

    def delete(self, request, batch_id):
        batch = get_object_or_404(Batch, id=batch_id)
        # Enrollment.batch is SET_NULL, so deleting a batch detaches its students
        # rather than deleting them. Progress rows (CASCADE) for this batch go too.
        batch.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Staffing matrix — per-batch teaching assignments (TeachingAssignment)
# --------------------------------------------------------------------------- #
def _teaching_assignment_payload(ta, request=None):
    return {
        "assignment_id": str(ta.id),
        "batch_id": str(ta.batch_id),
        "subject_id": str(ta.subject_id),
        "role": ta.role,
        "order": ta.order,
        "teacher": teacher_brief(ta.teacher, request=request),
    }


class AdminBatchTeachingAssignmentsView(APIView):
    """Staffing matrix for one batch: who teaches which subject.

    GET  -> active TeachingAssignment rows for the batch (with teacher briefs).
    POST -> assign a teacher to a subject in this batch.
            body: { "subject_id": <uuid>, "teacher_id": <uuid>,
                    "role": "PRIMARY"|"ASSISTANT"|"SUBSTITUTE" }

    Dual-writes a legacy SubjectTeacher row so the course-wide reads/authz that
    have not yet migrated keep seeing the assignment (removed in Phase 5).
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, batch_id):
        get_object_or_404(Batch, id=batch_id)
        rows = (
            TeachingAssignment.objects
            .filter(batch_id=batch_id, is_active=True)
            .select_related("teacher", "teacher__teacher_profile", "subject")
            .order_by("subject__order", "order")
        )
        return Response([_teaching_assignment_payload(ta, request) for ta in rows])

    def post(self, request, batch_id):
        batch = get_object_or_404(Batch.objects.select_related("course"), id=batch_id)

        subject_id = request.data.get("subject_id")
        teacher_id = request.data.get("teacher_id") or request.data.get("user_id")
        if not subject_id or not teacher_id:
            return Response(
                {"detail": "subject_id and teacher_id are both required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        role = (request.data.get("role") or TeachingAssignment.ROLE_PRIMARY).upper()
        if role not in VALID_TA_ROLES:
            role = TeachingAssignment.ROLE_PRIMARY

        subject = get_object_or_404(Subject, id=subject_id)
        # Triangle guard: the subject must belong to this batch's course.
        if subject.course_id != batch.course_id:
            return Response(
                {"detail": "Subject and batch belong to different courses."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            teacher = User.objects.select_related("teacher_profile").get(pk=teacher_id)
        except (User.DoesNotExist, DjangoValidationError, ValueError):
            return Response(
                {"detail": "Teacher not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        tp = getattr(teacher, "teacher_profile", None)
        if not (tp and tp.is_approved):
            return Response(
                {"detail": "Only approved teachers can be assigned."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        next_order = (
            TeachingAssignment.objects
            .filter(batch=batch, subject=subject, is_active=True)
            .order_by("-order")
            .values_list("order", flat=True)
            .first()
        )
        order = (next_order or 0) + 1

        try:
            ta = TeachingAssignment.objects.create(
                batch=batch, subject=subject, teacher=teacher,
                role=role, order=order, is_active=True,
                assigned_by=request.user,
            )
        except IntegrityError:
            # Violates one of the partial-unique constraints: either this
            # teacher is already active here, or a PRIMARY already exists.
            if role == TeachingAssignment.ROLE_PRIMARY:
                msg = ("This subject already has an active primary teacher in "
                       "this batch. End it first or assign as assistant.")
            else:
                msg = "This teacher is already assigned to this subject in this batch."
            return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)

        # Dual-write the legacy course-wide row (safety net for un-migrated
        # reads; SubjectTeacher only has PRIMARY/ASSISTANT).
        SubjectTeacher.objects.get_or_create(
            subject=subject, teacher=teacher,
            defaults={"display_role": _TA_TO_ST_ROLE[role], "order": order},
        )

        return Response(
            _teaching_assignment_payload(ta, request),
            status=status.HTTP_201_CREATED,
        )


class AdminTeachingAssignmentDetailView(APIView):
    """PATCH the role, or DELETE (end) a teaching assignment.

    DELETE is a soft end (is_active=False, ended_at=now) so the roster history
    "who taught this batch's maths in July" is preserved. The legacy
    SubjectTeacher row is intentionally left in place during the migration
    window; it is dropped wholesale in Phase 5.
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def patch(self, request, assignment_id):
        ta = get_object_or_404(
            TeachingAssignment.objects.select_related(
                "teacher", "teacher__teacher_profile"),
            pk=assignment_id,
        )
        role = (request.data.get("role") or "").upper()
        if role in VALID_TA_ROLES and role != ta.role:
            ta.role = role
            try:
                ta.save(update_fields=["role"])
            except IntegrityError:
                return Response(
                    {"detail": "This subject already has an active primary "
                               "teacher in this batch."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return Response(_teaching_assignment_payload(ta, request))

    def delete(self, request, assignment_id):
        ta = get_object_or_404(TeachingAssignment, pk=assignment_id)
        ta.is_active = False
        ta.ended_at = timezone.now()
        ta.save(update_fields=["is_active", "ended_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
