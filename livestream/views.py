# PLACEMENT: backend/backend/materials/views.py   (REPLACE THE WHOLE FILE)
# DEPLOY:    /app/shiksha-backend/materials/views.py
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# The old views were IsAuthenticated-only everywhere. That meant:
#   • ANY logged-in user (students included) could upload materials to any
#     chapter, create new chapters via `custom_chapter`, and DELETE any
#     material on the platform.
#   • StudentSubjectMaterials had no enrollment check — any account could list
#     and download every subject's files without paying/enrolling.
#   • Temp-file claiming (`file_ids`) fetched by bare id: a user could claim
#     someone else's pending upload, or re-parent a file already attached to
#     another material (silently detaching it).
#
# Fixes in this version:
#   1. WRITE gate `_can_manage_subject`: staff/superuser OR a SubjectTeacher
#      row for that subject. Applies to upload + delete (delete additionally
#      allows the original uploader).
#   2. READ gates: teacher/staff via the same check; students via the platform
#      access rule (active learner profile + has_active_subscription), matching
#      how livestream join gates access. Denies use the same 402 + lock_payload
#      shape the live-class join returns, so the frontends can reuse the lock UI.
#   3. Temp-file claiming filters on material__isnull=True AND
#      uploaded_by ∈ {me, NULL(legacy)}; all ids are validated up front and the
#      material create + claim happens in one atomic transaction (no orphan
#      materials on a bad id).
#   4. Custom chapters are get-or-created case-insensitively (no duplicate
#      "Chapter 5" rows from repeated uploads).
#   5. Enrolled-student notifications fire via transaction.on_commit, are
#      de-duplicated per account, and go through the (now fixed)
#      push_ws_notification → user_updates_<id> pipeline.
#   6. Deleting a material also deletes its files from storage (previously the
#      DB rows cascaded but the bytes stayed on disk forever).

import uuid as uuid_lib

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError as DjangoValidationError

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser

from courses.models import Subject, Chapter, SubjectTeacher
from enrollments.services import has_active_subscription, lock_payload
from accounts.auth_flow import get_active_profile
from livestream.services.notifications import push_ws_notification

from .models import StudyMaterial, MaterialFile
from .serializers import StudyMaterialSerializer
from .validators import validate_material_file


# True once the uploaded_by column exists on MaterialFile (post-migration).
# Lets this file deploy before/after the migration without crashing.
_HAS_UPLOADER = any(
    getattr(f, "name", "") == "uploaded_by" for f in MaterialFile._meta.get_fields()
)


# ===============================
# ACCESS HELPERS
# ===============================

def _can_manage_subject(user, subject):
    """May this user upload/delete materials for this subject?
    Staff/superusers always; otherwise only teachers assigned to the subject."""
    if user.is_staff or user.is_superuser:
        return True
    return SubjectTeacher.objects.filter(subject=subject, teacher=user).exists()


def _student_read_block(request, subject):
    """Return an error Response if this request may NOT read the subject's
    materials as a student, else None.

    Mirrors the livestream join gate: an active learner profile must hold a
    live subscription for the subject's course. Staff and the subject's
    teachers pass without a subscription.
    """
    user = request.user
    if _can_manage_subject(user, subject):
        return None

    learner = get_active_profile(request)
    if learner is None:
        return Response(
            {"detail": "Select a learner profile to view materials.",
             "lock_reason": "no_learner_profile"},
            status=status.HTTP_403_FORBIDDEN,
        )
    if not has_active_subscription(
        user=user, course=subject.course, learner_profile=learner
    ):
        return Response(
            lock_payload(user=user, course=subject.course, learner_profile=learner),
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )
    return None


def _parse_uuid_list(raw_ids):
    """Validate a list of uuid strings. Returns (uuids, bad_values)."""
    good, bad = [], []
    for raw in raw_ids:
        try:
            good.append(uuid_lib.UUID(str(raw)))
        except (ValueError, TypeError, AttributeError):
            bad.append(str(raw))
    return good, bad


# ===============================
# LIST MATERIALS OF A CHAPTER
# ===============================

class ChapterMaterials(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, chapter_id):
        chapter = get_object_or_404(
            Chapter.objects.select_related("subject__course"), id=chapter_id
        )
        block = _student_read_block(request, chapter.subject)
        if block is not None:
            return block

        materials = (
            StudyMaterial.objects
            .filter(chapter=chapter)
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
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        chapter_id = request.data.get("chapter_id")
        custom_chapter = (request.data.get("custom_chapter") or "").strip()

        # ── Resolve the target chapter + authorize against its subject ──
        if chapter_id:
            chapter = get_object_or_404(
                Chapter.objects.select_related("subject__course"), id=chapter_id
            )
            subject = chapter.subject
            if not _can_manage_subject(request.user, subject):
                return Response(
                    {"detail": "Only teachers of this subject can upload materials."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        elif custom_chapter:
            subject_id = request.data.get("subject_id")
            if not subject_id:
                return Response(
                    {"detail": "Subject is required for custom chapter"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            subject = get_object_or_404(
                Subject.objects.select_related("course"), id=subject_id
            )
            if not _can_manage_subject(request.user, subject):
                return Response(
                    {"detail": "Only teachers of this subject can upload materials."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            # Case-insensitive get-or-create: repeated uploads with the same
            # custom title reuse one chapter instead of stacking duplicates.
            chapter = Chapter.objects.filter(
                subject=subject, title__iexact=custom_chapter
            ).first()
            if chapter is None:
                chapter = Chapter.objects.create(subject=subject, title=custom_chapter)
        else:
            return Response(
                {"detail": "Chapter or custom chapter required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        title = (request.data.get("title") or "").strip()
        raw_file_ids = request.data.getlist("file_ids")

        if not title:
            return Response({"detail": "Title is required"}, status=400)
        if not raw_file_ids:
            return Response({"detail": "At least one file required"}, status=400)

        file_ids, bad = _parse_uuid_list(raw_file_ids)
        if bad:
            return Response(
                {"detail": f"Invalid file id(s): {', '.join(bad)}"}, status=400
            )
        file_ids = list(dict.fromkeys(file_ids))  # de-dupe, keep order

        # ── Claimable = unattached + uploaded by me (NULL = legacy rows) ──
        claim_qs = MaterialFile.objects.filter(id__in=file_ids, material__isnull=True)
        if _HAS_UPLOADER:
            claim_qs = claim_qs.filter(
                Q(uploaded_by=request.user) | Q(uploaded_by__isnull=True)
            )
        claimable_ids = set(claim_qs.values_list("id", flat=True))
        missing = [str(fid) for fid in file_ids if fid not in claimable_ids]
        if missing:
            return Response(
                {"detail": (
                    "Some files can't be attached (not found, already attached, "
                    "or uploaded by another user): " + ", ".join(missing)
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Create material + claim files atomically ──
        with transaction.atomic():
            material = StudyMaterial.objects.create(
                chapter=chapter,
                title=title,
                description=request.data.get("description", ""),
                uploaded_by=request.user,
            )
            MaterialFile.objects.filter(id__in=file_ids).update(material=material)

            # ── Notify enrolled students AFTER the commit succeeds ──
            course = subject.course
            chapter_title = chapter.title
            subject_name = subject.name
            material_id = str(material.id)

            def _notify():
                from enrollments.models import Enrollment
                user_ids = set(
                    Enrollment.objects.filter(
                        course=course, status=Enrollment.STATUS_ACTIVE
                    ).values_list("user_id", flat=True)
                )
                for uid in user_ids:
                    push_ws_notification(uid, {
                        "type": "material",
                        "title": f"New study material: {title}",
                        "chapter": chapter_title,
                        "subject": subject_name,
                        "id": material_id,
                    })

            transaction.on_commit(_notify)

        serializer = StudyMaterialSerializer(material, context={"request": request})
        return Response(serializer.data, status=201)


# ===============================
# DELETE MATERIAL
# ===============================

class DeleteStudyMaterial(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, material_id):
        material = get_object_or_404(
            StudyMaterial.objects.select_related("chapter__subject"),
            id=material_id,
        )

        is_owner = material.uploaded_by_id == request.user.id
        if not (is_owner or _can_manage_subject(request.user, material.chapter.subject)):
            return Response(
                {"detail": "You can only delete materials you uploaded, or "
                           "materials in subjects you teach."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Remove the bytes from storage, then the rows (CASCADE handles the
        # MaterialFile rows; storage files are NOT cascaded by Django).
        for mf in material.files.all():
            try:
                mf.file.delete(save=False)
            except Exception:
                pass  # a missing blob must not block the delete
        material.delete()

        return Response(
            {"detail": "Material deleted successfully"},
            status=status.HTTP_204_NO_CONTENT,
        )


# ===============================
# LIST MATERIALS OF A SUBJECT (teacher/admin view)
# ===============================

class SubjectMaterials(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        subject = get_object_or_404(
            Subject.objects.select_related("course"), id=subject_id
        )
        block = _student_read_block(request, subject)
        if block is not None:
            return block

        materials = (
            StudyMaterial.objects
            .filter(chapter__subject=subject)
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
            Subject.objects.select_related("course"), id=subject_id
        )
        block = _student_read_block(request, subject)
        if block is not None:
            return block

        materials = (
            StudyMaterial.objects
            .filter(chapter__subject=subject)
            .select_related("chapter")
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
            .select_related("chapter__subject__course")
            .prefetch_related("files"),
            id=material_id,
        )
        block = _student_read_block(request, material.chapter.subject)
        if block is not None:
            return block

        serializer = StudyMaterialSerializer(material, context={"request": request})
        return Response(serializer.data)


# ===============================
# TEMP FILE UPLOAD (validated)
# ===============================

class UploadTempFile(APIView):
    permission_classes = [IsAuthenticated]
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

        create_kwargs = {"file": file, "material": None}
        if _HAS_UPLOADER:
            create_kwargs["uploaded_by"] = request.user

        temp = MaterialFile.objects.create(**create_kwargs)
        return Response({
            "id": str(temp.id),
            "file_name": temp.filename(),
            "file_url": temp.file.url,
        }, status=201)
