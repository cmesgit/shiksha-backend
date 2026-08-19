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
