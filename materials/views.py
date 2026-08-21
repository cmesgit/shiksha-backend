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
from courses.services import teaches_subject
from accounts.auth_flow import get_active_profile
from enrollments.services import active_batch_id, has_active_subscription


# Sentinel returned (as the "batch_id") for a teacher: unlike a student's
# batch_id, which may genuinely be None (no batch assigned — restrict to
# course-wide material only), a teacher must see every batch's material.
# Overloading plain `None` for both meanings previously caused an unscoped
# student to be treated as "unrestricted" wherever a caller checked
# `if batch_id:` — see materials/views.py's ChapterMaterials/SubjectMaterials
# history.
TEACHER_UNRESTRICTED = object()


def _authorize_subject_materials(request, subject):
    """Gate material reads on the same rule the Student* views already
    enforce: a teacher assigned to the subject, or a learner profile with an
    active subscription to the subject's course.

    Returns (allowed, batch_id). batch_id is TEACHER_UNRESTRICTED for a
    teacher (sees every batch's material); for a student it is their
    enrollment's batch id, or None if they have no batch assigned yet (in
    which case callers must filter to Q(batch__isnull=True) | Q(batch_id=
    batch_id) — which correctly degrades to "course-wide material only",
    not "every batch's material").
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
            materials = materials.filter(Q(batch__isnull=True) | Q(batch_id=batch_id))
        materials = (
            materials
            # chapter__subject: the serializer reports subject_id/subject_name.
            .select_related("chapter__subject__course__board", "batch")
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

        if chapter_id:
            chapter = get_object_or_404(
                Chapter.objects.select_related("subject"), id=chapter_id
            )
            if not teaches_subject(request.user, chapter.subject):
                raise PermissionDenied(
                    "You are not assigned to teach this subject."
                )
        elif custom_chapter:
            subject_id = request.data.get("subject_id")
            if not subject_id:
                return Response(
                    {"detail": "Subject is required for custom chapter"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            subject = get_object_or_404(Subject, id=subject_id)
            if not teaches_subject(request.user, subject):
                raise PermissionDenied(
                    "You are not assigned to teach this subject."
                )
            chapter = Chapter.objects.create(
                subject=subject,
                title=custom_chapter
            )
        else:
            return Response(
                {"detail": "Chapter or custom chapter required"},
                status=status.HTTP_400_BAD_REQUEST
            )

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
            batch = get_object_or_404(Batch, id=batch_id)

        material = StudyMaterial.objects.create(
            chapter=chapter,
            batch=batch,
            title=title,
            description=request.data.get("description", ""),
            uploaded_by=request.user
        )

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

        course = chapter.subject.course
        _bulk_notify_students(
            _enrollments_for(course, material.batch_id),
            material,
            Activity.TYPE_MATERIAL,
            f"New study material: {title}",
            None,                       # materials have no due date
            chapter.subject_id,
            chapter.subject.name,
            extra={"chapter": chapter.title},
            verb="materials.uploaded",
            link_url=f"/study-material/list/{chapter.subject_id}",
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
        material = get_object_or_404(StudyMaterial, id=material_id)
        # Only the uploading teacher (or staff) may delete — a teacher shouldn't
        # be able to delete a colleague's material.
        if material.uploaded_by_id != request.user.id and not request.user.is_staff:
            return Response(
                {"detail": "You can only delete your own material."},
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
        materials = StudyMaterial.objects.filter(chapter__subject=subject)
        if batch_id is not TEACHER_UNRESTRICTED:
            materials = materials.filter(Q(batch__isnull=True) | Q(batch_id=batch_id))
        materials = (
            materials
            # chapter__subject: the serializer reports subject_id/subject_name.
            .select_related("chapter__subject__course__board", "batch")
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
            batch_q = Q(batch__isnull=True) | Q(batch_id=batch_id)
        materials = (
            StudyMaterial.objects
            .filter(chapter__subject=subject)
            .filter(batch_q)
            .select_related("chapter__subject__course__board", "batch")
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
                chapter__subject__teaching_assignments__teacher=request.user,
                chapter__subject__teaching_assignments__is_active=True,
            )
            # chapter__subject: the serializer reports subject_id/subject_name.
            .select_related("chapter__subject__course__board", "batch")
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
        batch_q = Q() if is_teacher else (
            Q(batch__isnull=True) | Q(batch_id=batch_id)
        )

        materials = (
            StudyMaterial.objects
            .filter(chapter__subject__course_id=course_id)
            .filter(batch_q)
            # subject_id/subject_name are read off chapter.subject per row.
            .select_related("chapter__subject__course__board", "batch")
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
            .select_related("chapter__subject__course__board", "batch")
            .prefetch_related("files"),
            id=material_id
        )
        allowed, batch_id = _authorize_subject_materials(
            request, material.chapter.subject
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