from courses.models import Subject
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from accounts.permissions import IsTeacherContext
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError as DjangoValidationError

from .models import StudyMaterial, MaterialFile
from .serializers import StudyMaterialSerializer
from .validators import validate_material_file
from django.db.models import Q
from django.core.exceptions import PermissionDenied

from courses.models import Chapter, Batch
from courses.services import resolve_or_create_chapter, teaches_subject
from courses.chapter_tags import (
    primary_chapter,
    resolve_tags,
    set_tags,
    validate_tag_payload,
)
from rest_framework.exceptions import ValidationError as DRFValidationError
from accounts.auth_flow import get_active_profile
from enrollments.services import active_batch_id, has_active_subscription

import json


def _parse_bool(raw):
    """Coerce a multipart form value to a bool.

    This endpoint is MultiPartParser-only (it takes file ids alongside
    metadata), so every value arrives as a STRING — including "false", which
    is truthy in Python. Without this, sending no_specific_chapter=false would
    set it True and then collide with any chapter tags in the same request.
    """
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_chapter_tags(request):
    """Read `chapter_tags` from a multipart body.

    Accepted as a JSON array in one field (what a JS client sends most
    naturally), or as repeated `chapter_tags` fields each holding one JSON
    object. A malformed value is treated as "no tags" rather than a 500 —
    tags are optional everywhere, so the safe failure is to ignore them.
    """
    raw = request.data.get("chapter_tags")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        candidates = raw
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        candidates = parsed if isinstance(parsed, list) else [parsed]
    return [c for c in candidates if isinstance(c, dict)]


# Sentinel returned (as the "batch_id") for a teacher: unlike a student's
# batch_id, which may genuinely be None (no batch assigned — restrict to
# course-wide material only), a teacher must see every batch's material.
# Overloading plain `None` for both meanings previously caused an unscoped
# student to be treated as "unrestricted" wherever a caller checked
# `if batch_id:` — see materials/views.py's ChapterMaterials/SubjectMaterials
# history.
TEACHER_UNRESTRICTED = object()


def _batch_scope_q(batch_id):
    """Batch isolation for a learner's material reads.

    A placed learner sees course-wide material (batch IS NULL) plus their own
    batch's. An UNPLACED learner (batch_id None — self-enrolled, not yet put
    in a cohort by an admin) sees everything in the course.

    That last clause is the whole point of this helper. The naive form,

        Q(batch__isnull=True) | Q(batch_id=batch_id)

    looks like it degrades safely, but with batch_id None the right-hand side
    compiles to `batch_id IS NULL` — identical to the left — so the OR
    collapses to "course-wide only" and every batch-scoped material becomes
    invisible. Meanwhile activity/signals.py's `_enrollments_for_batches`
    deliberately NOTIFIES unplaced learners about batch-scoped material. The
    two halves disagreed: the student got "New study material: X", clicked it,
    and landed on a list that structurally could not contain X.

    assignments/views.py:322-326 already had this right ("show every
    assignment in the course rather than only the course-wide ones, which
    would otherwise hide every batch-scoped assignment from most students").
    Materials just never copied the guard. This keeps the two readers honest
    with each other and with the notifier.
    """
    if batch_id is None:
        return Q()
    return Q(batch__isnull=True) | Q(batch_id=batch_id)


def _authorize_subject_materials(request, subject):
    """Gate material reads on the same rule the Student* views already
    enforce: a teacher assigned to the subject, or a learner profile with an
    active subscription to the subject's course.

    Returns (allowed, batch_id). batch_id is TEACHER_UNRESTRICTED for a
    teacher (sees every batch's material); for a student it is their
    enrollment's batch id, or None if they have no batch assigned yet.

    Callers must build the filter with `_batch_scope_q(batch_id)` — never by
    hand. This docstring used to claim the hand-written form "correctly
    degrades to course-wide material only" for a batch-less student; that was
    the bug, not the design. The notifier tells an unplaced learner about
    batch-scoped material, so a reader that hides it leaves them staring at
    an empty list they have a notification for. See `_batch_scope_q`.
    """
    if teaches_subject(request.user, subject):
        return True, TEACHER_UNRESTRICTED
    profile = get_active_profile(request)
    if has_active_subscription(
        user=request.user, course=subject.course, learner_profile=profile,
    ):
        batch_id = active_batch_id(
            learner_profile=profile, course_id=subject.course_id,
        )
        return True, batch_id
    return False, None


# ===============================
# LIST MATERIALS OF A CHAPTER
# ===============================

class ChapterMaterials(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, chapter_id):
        chapter = get_object_or_404(
            Chapter.objects.select_related("subject__course"), id=chapter_id
        )
        allowed, batch_id = _authorize_subject_materials(request, chapter.subject)
        if not allowed:
            raise PermissionDenied("No active subscription for this course.")
        materials = StudyMaterial.objects.filter(chapter=chapter)
        if batch_id is not TEACHER_UNRESTRICTED:
            materials = materials.filter(_batch_scope_q(batch_id))
        materials = (
            materials
            # subject: the serializer reports subject_id/subject_name.
            .select_related("subject__course__board", "chapter", "batch")
            .prefetch_related("files")
            .order_by("-created_at")
        )
        serializer = StudyMaterialSerializer(
            materials, many=True, context={"request": request}
        )
        return Response(serializer.data)


# ===============================
# UPLOAD STUDY MATERIAL
# ===============================

class UploadStudyMaterial(APIView):
    permission_classes = [IsAuthenticated, IsTeacherContext]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, chapter_id=None):
        chapter_id = request.data.get("chapter_id")
        custom_chapter = request.data.get("custom_chapter")

        # Resolve the SUBJECT first — it is the authorization anchor and the
        # model's NOT NULL column. A chapter, if one was named, implies it;
        # otherwise the caller states it with subject_id. Chapter itself is
        # optional now (a revision pack may span the whole term), so the gate
        # below runs on the subject either way.
        chapter = None
        if chapter_id:
            chapter = get_object_or_404(
                Chapter.objects.select_related("subject"), id=chapter_id
            )
            subject = chapter.subject
        else:
            subject_id = request.data.get("subject_id")
            if not subject_id:
                return Response(
                    {"detail": "Subject is required."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            subject = get_object_or_404(Subject, id=subject_id)

        # Checked BEFORE any chapter is minted, so an unauthorized request
        # cannot leave a stray Chapter row behind under a subject this teacher
        # has no claim to.
        if not teaches_subject(request.user, subject):
            raise PermissionDenied(
                "You are not assigned to teach this subject."
            )

        if chapter is None and custom_chapter:
            # Legacy single-value shim — the key the live Upload-material
            # screen sends today. A repeat (or case-varied) chapter name
            # reuses the existing row instead of hitting
            # unique_chapter_per_subject with a 500.
            chapter = resolve_or_create_chapter(
                subject, custom_title=custom_chapter, created_by=request.user,
            )

        # New multi-value payload. Validated before anything is written so a
        # contradictory request 400s rather than half-saving.
        raw_tags = _parse_chapter_tags(request)
        no_specific = _parse_bool(request.data.get("no_specific_chapter"))
        try:
            validate_tag_payload(raw_tags, no_specific)
            resolved_tags = resolve_tags(
                subject, raw_tags, teacher=request.user,
                save_to_course=_parse_bool(
                    request.data.get("save_chapters_to_course")
                ),
            )
        except DRFValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

        # The additive invariant: keep the single `chapter` FK pointing at the
        # first resolved chapter so authorization and every legacy read path
        # keep working. Only when tags were actually sent — otherwise the
        # legacy chapter above stands.
        if raw_tags:
            chapter = primary_chapter(resolved_tags) or chapter

        title = request.data.get("title")
        file_ids = request.data.getlist("file_ids")

        if not title:
            return Response({"detail": "Title is required"}, status=400)
        if not file_ids:
            return Response({"detail": "At least one file required"}, status=400)

        # Optional — the model's own default is course-wide (batch=NULL, a
        # "write once, reuse across batches" curriculum asset). A teacher can
        # scope a genuinely batch-specific handout by picking one.
        batch = None
        batch_id = request.data.get("batch_id")
        if batch_id:
            # course_id in the lookup, not just the pk. Without it a batch
            # from a DIFFERENT course was accepted silently, and every read
            # path filters `batch__isnull=True | batch_id=<their batch>`,
            # which can never match a foreign course's batch — so the
            # material was invisible to every student while the teacher's
            # list showed it uploaded. _enrollments_for below would also have
            # notified nobody, for the same reason.
            # subject.course_id, not chapter.subject.course_id — chapter is
            # optional now and would be None for a chapter-less upload.
            batch = get_object_or_404(
                Batch, id=batch_id, course_id=subject.course_id,
            )

        material = StudyMaterial.objects.create(
            subject=subject,
            chapter=chapter,
            batch=batch,
            title=title,
            description=request.data.get("description", ""),
            chapter_note=request.data.get("chapter_note", ""),
            no_specific_chapter=no_specific,
            uploaded_by=request.user
        )
        if raw_tags:
            set_tags(material, resolved_tags)

        for fid in file_ids:
            # Only an unclaimed temp file this user uploaded (or a legacy
            # NULL-uploader row, grandfathered per the model's own comment)
            # can be attached — stops claiming/re-parenting another
            # teacher's file by guessing or reading its UUID.
            file = get_object_or_404(
                MaterialFile.objects.filter(
                    Q(uploaded_by=request.user) | Q(uploaded_by__isnull=True)
                ),
                id=fid,
                material__isnull=True,
            )
            file.material = material
            file.save()

        # Notify students who can actually SEE this material.
        #
        # Two bugs lived here, both now fixed by reusing the exact path
        # assignments and quizzes already take (activity/signals.py):
        #
        #  1. RECIPIENTS IGNORED BATCH. The old query filtered on course +
        #     status only, so a material scoped to one batch notified every
        #     active enrollee in the course — and the read side DOES enforce
        #     batch isolation (see StudentSubjectMaterials / the
        #     PermissionDenied at :300 and :378 below), so the wrong-batch
        #     half got a notification that could only ever 403 them.
        #     _enrollments_for applies the same visibility rule the reader
        #     applies, which is the only correct basis for a notification.
        #
        #  2. NO ACTIVITY ROW. This was the one lifecycle in the codebase
        #     with no durable record, so an upload lived only in a
        #     fire-and-forget WS frame: it vanished on refresh (both bells
        #     read /activity/feed/, which serves Activity rows only) and its
        #     frame carried the MATERIAL's uuid, so mark-read PATCHed an id
        #     no Activity had and 404'd forever.
        #
        # _bulk_notify_students writes the Activity rows, the durable
        # Notification rows (verb → track/policy), and one WS frame per row
        # carrying the SERIALIZED ACTIVITY — same id and shape as the REST
        # feed, so dedupe and mark-read work. Profile isolation comes from
        # the enrollment's learner_profile, exactly as before.
        from activity.models import Activity
        from activity.signals import _bulk_notify_students, _enrollments_for

        # subject, not chapter.subject — chapter is optional now.
        course = subject.course
        _bulk_notify_students(
            _enrollments_for(course, material.batch_id),
            material,
            Activity.TYPE_MATERIAL,
            f"New study material: {title}",
            None,                       # materials have no due date
            subject.id,
            subject.name,
            extra={"chapter": chapter.title if chapter else None},
            verb="materials.uploaded",
            # ?course= is not decoration. The learner app's Study Material
            # list fetches strictly the ACTIVE course from CourseContext, but
            # nothing in the notification-click path ever sets the active
            # course. A learner enrolled in two courses whose active course is
            # "Class 12 Science" would click a Class 10 material notification,
            # get the Class 12 list filtered to a Class 10 subject id, and see
            # "No material for this subject" — the material is real, the
            # screen is just looking at the wrong course. Carrying the course
            # id lets the screen switch first, then filter.
            link_url=(
                f"/study-material/list/{subject.id}?course={course.id}"
            ),
        )

        serializer = StudyMaterialSerializer(
            material, context={"request": request})
        return Response(serializer.data, status=201)


# ===============================
# DELETE MATERIAL
# ===============================

class DeleteStudyMaterial(APIView):
    permission_classes = [IsAuthenticated, IsTeacherContext]

    def delete(self, request, material_id):
        material = get_object_or_404(
            StudyMaterial.objects.select_related("subject", "chapter"),
            id=material_id,
        )
        # Subject teaching staff (or an admin) may delete, not just the
        # uploader.
        #
        # This used to be `uploaded_by` only, which contradicted the list it
        # is reached from: /materials/teacher/materials/all/ returns every
        # material on the teacher's subjects INCLUDING colleagues', each row
        # with a Delete action that could only ever 403. The frontend
        # swallowed that 403 into console.error, so the dialog just sat there
        # and the teacher clicked again, forever. Recordings already used the
        # teaches_subject rule (DeleteRecordingView) — one of the two had to
        # move, and widening this one is what makes the offered action match
        # the offered list.
        #
        # It is not a widening of *reach*: teaches_subject is the same gate
        # that decides whether the material is visible to this teacher at all.
        if not (
            request.user.is_staff
            or material.uploaded_by_id == request.user.id
            or teaches_subject(request.user, material.subject)
        ):
            return Response(
                {"detail": "You are not assigned to teach this subject."},
                status=status.HTTP_403_FORBIDDEN,
            )
        material.delete()
        return Response(
            {"detail": "Material deleted successfully"},
            status=status.HTTP_204_NO_CONTENT
        )


# ===============================
# LIST MATERIALS OF A SUBJECT
# ===============================

class SubjectMaterials(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        subject = get_object_or_404(
            Subject.objects.select_related("course"), id=subject_id
        )
        allowed, batch_id = _authorize_subject_materials(request, subject)
        if not allowed:
            raise PermissionDenied("No active subscription for this course.")
        materials = StudyMaterial.objects.filter(subject=subject)
        if batch_id is not TEACHER_UNRESTRICTED:
            materials = materials.filter(_batch_scope_q(batch_id))
        materials = (
            materials
            # subject: the serializer reports subject_id/subject_name.
            .select_related("subject__course__board", "chapter", "batch")
            .prefetch_related("files")
            .order_by("-created_at")
        )
        serializer = StudyMaterialSerializer(
            materials, many=True, context={"request": request}
        )
        return Response(serializer.data)


# ===============================
# STUDENT SUBJECT MATERIALS
# ===============================

class StudentSubjectMaterials(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        subject = get_object_or_404(
            Subject.objects.select_related("course"), id=subject_id)
        # ENTITLEMENT FIRST. This view previously went straight to
        # active_batch_id with no subscription/enrolment check at all — and
        # active_batch_id returns None for someone with no enrolment, which
        # then degraded to "show all course-wide material" instead of
        # denying. Any authenticated account could read any course's handouts
        # by passing its subject id. _authorize_subject_materials is the same
        # gate every other read path in this file already uses.
        allowed, batch_id = _authorize_subject_materials(request, subject)
        if not allowed:
            raise PermissionDenied("You do not have access to this subject.")
        if batch_id is TEACHER_UNRESTRICTED:
            batch_q = Q()
        else:
            # Batch isolation: course-wide materials (batch IS NULL) + this
            # student's own batch's. Scoped to the ACTIVE PROFILE.
            batch_q = _batch_scope_q(batch_id)
        materials = (
            StudyMaterial.objects
            .filter(subject=subject)
            .filter(batch_q)
            .select_related("subject__course__board", "chapter", "batch")
            .prefetch_related("files")
            .order_by("-created_at")
        )
        serializer = StudyMaterialSerializer(
            materials, many=True, context={"request": request}
        )
        return Response(serializer.data)


# ===============================
# TEACHER — ALL MATERIALS ACROSS THEIR SUBJECTS
# ===============================

class TeacherAllMaterials(APIView):
    """Every material across every subject this teacher is assigned to.

    The faculty Study Materials screen is one flat, subject-filtered list
    (design handoff screen 13). Before this existed the frontend called
    SubjectMaterials once per subject and flattened client-side.

    No batch filter: a teacher owns every batch's material for their subjects,
    which is exactly how SubjectRecordingsView already treats teachers.
    """

    permission_classes = [IsAuthenticated, IsTeacherContext]

    def get(self, request):
        materials = (
            StudyMaterial.objects
            .filter(
                subject__teaching_assignments__teacher=request.user,
                subject__teaching_assignments__is_active=True,
            )
            # subject: the serializer reports subject_id/subject_name.
            .select_related("subject__course__board", "chapter", "batch")
            .prefetch_related("files")
            # distinct(): a teacher listed twice on one subject would otherwise
            # duplicate every material on it.
            .distinct()
            .order_by("-created_at")
        )
        serializer = StudyMaterialSerializer(
            materials, many=True, context={"request": request}
        )
        return Response(serializer.data)


# ===============================
# STUDENT COURSE MATERIALS
# ===============================

class StudentCourseMaterials(APIView):
    """Every material across a course's subjects, in one request.

    The learner's Study Material screen is a single flat, subject-filtered list
    (design handoff screen 13). Before this existed the frontend had to call
    StudentSubjectMaterials once per subject and flatten client-side — an N+1
    that grew with the syllabus.

    Batch isolation matches StudentSubjectMaterials: course-wide materials
    (batch IS NULL) plus this learner's own batch's materials.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, course_id):
        # ENTITLEMENT FIRST — same hole StudentSubjectMaterials had: this went
        # straight to batch scoping with no subscription check, and
        # active_batch_id returns None for a non-enrolled caller, degrading to
        # "all course-wide material" rather than denying. Any authenticated
        # account could read any course's handouts by passing its course id.
        from courses.models import Course
        course = get_object_or_404(Course, id=course_id)
        profile = get_active_profile(request)
        is_teacher = Subject.objects.filter(
            course_id=course_id,
            teaching_assignments__teacher=request.user,
            teaching_assignments__is_active=True,
        ).exists()
        if not is_teacher and not has_active_subscription(
            user=request.user, course=course, learner_profile=profile,
        ):
            raise PermissionDenied("No active subscription for this course.")

        # Scope the batch to the ACTIVE PROFILE, not just the account — two
        # children on one account can sit in different batches of one course.
        # A teacher of the course sees every batch's material, matching
        # _authorize_subject_materials' TEACHER_UNRESTRICTED branch.
        batch_id = active_batch_id(learner_profile=profile, course_id=course_id)
        batch_q = Q() if is_teacher else _batch_scope_q(batch_id)

        materials = (
            StudyMaterial.objects
            .filter(subject__course_id=course_id)
            .filter(batch_q)
            # subject_id/subject_name are read off chapter.subject per row.
            .select_related("subject__course__board", "chapter", "batch")
            .prefetch_related("files")
            .order_by("-created_at")
        )
        serializer = StudyMaterialSerializer(
            materials, many=True, context={"request": request}
        )
        return Response(serializer.data)


# ===============================
# MATERIAL DETAIL
# ===============================

class StudyMaterialDetail(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, material_id):
        material = get_object_or_404(
            StudyMaterial.objects
            .select_related("subject__course__board", "chapter", "batch")
            .prefetch_related("files"),
            id=material_id
        )
        allowed, batch_id = _authorize_subject_materials(
            request, material.subject
        )
        if not allowed:
            raise PermissionDenied("No active subscription for this course.")
        if batch_id is not TEACHER_UNRESTRICTED and material.batch_id not in (None, batch_id):
            raise PermissionDenied("This material is not available to your batch.")
        serializer = StudyMaterialSerializer(
            material,
            context={"request": request}
        )
        return Response(serializer.data)


# ===============================
# TEMP FILE UPLOAD (validated)
# ===============================

class UploadTempFile(APIView):
    permission_classes = [IsAuthenticated, IsTeacherContext]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        file = request.FILES.get("file")
        if not file:
            return Response({"detail": "File required"}, status=400)

        try:
            validate_material_file(file)
        except DjangoValidationError as e:
            return Response(
                {"detail": " ".join(e.messages)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        temp = MaterialFile.objects.create(
            file=file, material=None, uploaded_by=request.user
        )
        return Response({
            "id": str(temp.id),
            "file_name": temp.filename(),
            "file_url": temp.file.url,
        }, status=201)