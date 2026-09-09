# Delivery-plane helpers (batch system). These are the two rules every
# batch-aware read path goes through, so the logic lives in exactly one place.
#
# Adoption plan (migration §Phase 3/4): permission checks in livestream,
# quizzes, assignments, materials and recordings switch from
# SubjectTeacher-based lookups to is_teacher_of(); student-facing content
# querysets wrap themselves in scope_to_enrollment().

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.shortcuts import get_object_or_404

from .models import Chapter, TeachingAssignment


def require_authoring_context(request):
    """The Step-2B gate, stated so it cannot be skipped by being staff.

    Anyone holding the TEACHER role must be in TEACHER context to author
    content. A pure admin (staff, no TEACHER role) has no teacher context to be
    in and is allowed through — but an account that is BOTH staff and a teacher
    is still gated, because the threat the gate exists for is a learner-context
    token on that teacher's own account (a child on a shared device), and being
    an admin does not make that token safe.

    Written as one rule rather than `if not request.user.is_staff:
    require_teacher_context(...)`, which is what let exactly that case through.
    """
    from accounts.permissions import require_teacher_context

    if request.user.has_role("TEACHER") or not request.user.is_staff:
        require_teacher_context(request)


def resolve_content_author(request):
    """Who a new piece of academy content is filed under.

    Returns a User. Every staffing check downstream must then run against the
    RETURNED user rather than `request.user` — that is the whole point: a
    teacher files under themselves, an admin files under a teacher they name,
    and in both cases the content has to belong to somebody who actually
    teaches the subject.

    Three paths:

      * No `teacher_id` → filing under yourself, and the teacher-context gate
        applies exactly as before. Nothing that ships today sends
        `teacher_id`, so every existing client keeps the code path it had,
        including a staff member who also holds the TEACHER role and uploads
        from the teacher dashboard. Branching on `is_staff` instead would have
        broken precisely those accounts. A PURE admin here gets a form error,
        not "switch to your teacher profile" — they have no teacher profile to
        switch to, so that advice would be unactionable.

      * `teacher_id` naming YOURSELF → still the self-authoring path, so the
        gate still applies. Without this an admin who is also a teacher could
        author from a learner-context token just by passing their own id,
        which is the exact bypass the gate exists to prevent.

      * `teacher_id` naming SOMEBODY ELSE → only an admin may do it, and only
        for a usable teaching account. Teacher context is NOT required, because
        the caller is acting as an admin and an admin's token carries no
        `context` claim at all.

    Raises PermissionDenied (403) when the caller may not do this, and DRF
    ValidationError (400) when the named teacher is unusable — a bad
    `teacher_id` is a form error the admin can fix, not an access failure.
    """
    # Imported here, not at module scope: accounts.permissions is a thin
    # module but courses.services is imported by half the codebase, and a
    # top-level accounts import from here has bitten this project before.
    from django.contrib.auth import get_user_model
    from rest_framework.exceptions import PermissionDenied
    from rest_framework.exceptions import ValidationError as DRFValidationError

    from accounts.permissions import require_teacher_context

    raw = request.data.get("teacher_id")
    if raw in (None, ""):
        if request.user.is_staff and not request.user.has_role("TEACHER"):
            raise DRFValidationError(
                {"teacher_id": ["Choose the teacher this belongs to."]}
            )
        require_teacher_context(request)
        return request.user

    try:
        teacher_id = uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        # A UUIDField comparison against junk raises Django's own
        # ValidationError, which DRF does not translate — it surfaces as a
        # 500. Same guard, same reason, as dashboard/teacher_resources.py's
        # _parse_ids.
        raise DRFValidationError({"teacher_id": ["Not a valid teacher id."]})

    if teacher_id == request.user.id:
        # Naming yourself is authoring your own content, whoever you are.
        require_teacher_context(request)
        return request.user

    if not request.user.is_staff:
        raise PermissionDenied(
            "Only an admin can file content under another teacher."
        )

    teacher = get_user_model().objects.filter(id=teacher_id).first()
    if teacher is None:
        raise DRFValidationError({"teacher_id": ["No such teacher."]})
    if not teacher.has_role("TEACHER"):
        # Checked separately from "no such teacher" so the admin can tell a
        # typo'd id from an account that simply is not a teacher.
        raise DRFValidationError(
            {"teacher_id": ["That account is not a teacher."]}
        )
    if not teacher.is_active:
        # A deactivated owner cannot log in, so filing under them produces the
        # one thing this whole design exists to prevent: content nobody can
        # look after. Their TeachingAssignment rows often outlive the
        # deactivation, so `teaches_subject` alone would have accepted them.
        raise DRFValidationError(
            {"teacher_id": ["That account is deactivated."]}
        )
    return teacher


def is_teacher_of(user, batch, subject):
    """True if `user` actively teaches `subject` in `batch` — either via a
    row scoped to that exact batch, or a course-wide (batch=NULL) row, which
    applies to every batch of the course.

    The student↔teacher relationship in the academy is always derived
    through the batch (student → Enrollment.batch → TeachingAssignment →
    teacher); never store or check a direct student–teacher link here.
    """
    if user is None or batch is None or subject is None:
        return False
    return TeachingAssignment.objects.filter(
        models.Q(batch=batch) | models.Q(batch__isnull=True),
        subject=subject, teacher=user, is_active=True,
    ).exists()


def teaches_subject(user, subject):
    """True if `user` actively teaches `subject` — in any specific batch, or
    course-wide (batch=NULL). Subject-level authorization gate."""
    if user is None or not getattr(user, "is_authenticated", False) or subject is None:
        return False
    return TeachingAssignment.objects.filter(
        subject=subject, teacher=user, is_active=True,
    ).exists()


def may_view_subject_directory(request, subject):
    """Is this caller a member of `subject`'s course, and so entitled to see
    who else is in it — the student roster or the teacher list?

    One definition, deliberately. Several directory endpoints grew up as
    independent forks of each other (courses.SubjectStudentsView,
    sessions_app.subject_students, sessions_app.subject_teachers), and two of
    those forks shipped with no gate beyond IsAuthenticated: any logged-in
    account could dump every enrolled student's name and student_id for any
    course, with subject ids discoverable from the public catalog. Divergence
    is precisely what allowed that, so the RULE lives here even though the
    views still differ in response shape (and courses' own roster view stays
    stricter, teacher-only).

    Returns True for:
      • a teacher, in teacher context, assigned to this subject
      • a student with an ACTIVE enrollment in the course that owns it
    """
    # Imported here: accounts.permissions and enrollments both import from
    # courses at module scope, so top-level imports would cycle.
    from accounts.permissions import IsTeacherContext
    from enrollments.models import Enrollment

    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False) or subject is None:
        return False

    if IsTeacherContext().has_permission(request, None) and teaches_subject(user, subject):
        return True

    return Enrollment.objects.filter(
        course=subject.course,
        user=user,
        status=Enrollment.STATUS_ACTIVE,
    ).exists()


def next_chapter_order(subject):
    """The `order` a newly appended chapter of `subject` should take.

    `Chapter.order` defaults to 0, so every chapter minted by
    resolve_or_create_chapter() used to pile up at 0 and sort arbitrarily
    against each other (and ahead of, or interleaved with, the curated
    syllabus). Appending puts a teacher's new chapter at the end of the list,
    which is where a teacher typing one expects it.
    """
    current_max = Chapter.objects.filter(subject=subject).aggregate(
        top=models.Max("order")
    )["top"]
    return 0 if current_max is None else current_max + 1


def find_chapter_by_title(subject, title):
    """The ONE definition of "this typed name is really that chapter".

    Case-insensitive, whitespace-trimmed. Both the legacy `custom_chapter`
    write path and the new chapter-tag path must dedupe identically, or the
    same typed name would create a chapter on one screen and reuse one on
    another. Returns None when nothing matches.
    """
    cleaned = (title or "").strip()
    if not cleaned:
        return None
    return Chapter.objects.filter(subject=subject, title__iexact=cleaned).first()


def resolve_or_create_chapter(subject, chapter_id=None, custom_title=None,
                              created_by=None):
    """Resolve a chapter for `subject`, either by id or by name.

    A `custom_title` that already exists for this subject (case-insensitive)
    reuses the existing row rather than hitting the `unique_chapter_per_subject`
    constraint — repeat teacher input shouldn't 500, and near-duplicate titles
    that differ only by case shouldn't fork into two chapters.

    A chapter CREATED here is by definition teacher-typed, so it is stamped
    `is_custom=True` (+ `created_by`, when the caller passes the teacher) and
    appended to the end of the subject's chapter order. Resolving an EXISTING
    chapter never rewrites those fields: matching the name of a curated
    syllabus chapter must not relabel it as custom.
    """
    if chapter_id:
        return get_object_or_404(Chapter, id=chapter_id, subject=subject)

    title = (custom_title or "").strip()
    if not title:
        raise ValidationError({"chapter": "Select a chapter or enter a new chapter name."})

    existing = find_chapter_by_title(subject, title)
    if existing:
        return existing
    return Chapter.objects.create(
        subject=subject,
        title=title,
        order=next_chapter_order(subject),
        is_custom=True,
        created_by=created_by,
    )


def scope_to_enrollment(qs, enrollment):
    """Filter a content queryset to what this enrollment may see:
    course-wide items (batch is NULL) plus the student's own batch's items.

    Works on any model with a nullable `batch` FK (LiveSession, Quiz,
    Assignment, StudyMaterial, SessionRecording). If the enrollment has no
    batch (legacy rows, or an admin detached it), the student sees only
    course-wide content — safe degradation, not a crash.
    """
    return qs.filter(
        models.Q(batch__isnull=True) | models.Q(batch_id=enrollment.batch_id)
    )
