# Delivery-plane helpers (batch system). These are the two rules every
# batch-aware read path goes through, so the logic lives in exactly one place.
#
# Adoption plan (migration §Phase 3/4): permission checks in livestream,
# quizzes, assignments, materials and recordings switch from
# SubjectTeacher-based lookups to is_teacher_of(); student-facing content
# querysets wrap themselves in scope_to_enrollment().

from django.db import models

from .models import TeachingAssignment


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
