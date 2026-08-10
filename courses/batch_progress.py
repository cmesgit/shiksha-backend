"""Per-batch course progress.

Mirrors the shape of ``courses.progress.build_course_progress`` so the frontend
can reuse the same rendering, but coverage is read from
``BatchChapterProgress`` for one specific batch instead of the course-wide
``Chapter.is_covered`` flag.

Payload shape (identical to the course-level builder, plus batch context and a
per-chapter ``note``)::

    {
      "batch": {"id", "name", "code", "course_id"},
      "chapters_total", "chapters_done", "chapters_left", "percent",
      "subjects": [
        {"id", "name", "order", "teacher_name", "chapters_total", "chapters_done",
         "percent",
         "chapters": [
            {"id", "title", "order", "is_covered", "covered_at", "note"}
         ]}
      ]
    }
"""

from .admin_academy_views import _teacher_name
from .models import SubjectTeacher, TeachingAssignment
from .models_batch_progress import BatchChapterProgress
from .services import is_teacher_of


def _batch_subject_teacher_name(batch, subject):
    """Primary teacher for this subject in this batch, falling back to the
    legacy course-wide SubjectTeacher assignment during the migration window."""
    assignments = list(
        TeachingAssignment.objects
        .filter(batch=batch, subject=subject, is_active=True)
        .select_related("teacher")
        .order_by("order")
    )
    ta = next(
        (a for a in assignments if a.role == TeachingAssignment.ROLE_PRIMARY),
        assignments[0] if assignments else None,
    )
    if ta:
        return _teacher_name(ta.teacher)
    st = (
        SubjectTeacher.objects
        .filter(subject=subject)
        .select_related("teacher")
        .first()
    )
    return _teacher_name(st.teacher) if st else ""


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
def can_view_batch_progress(user, batch):
    """Admin/staff, or a teacher with an active assignment in this batch.

    Prefers the new per-batch TeachingAssignment roster; falls back to the
    legacy course-wide SubjectTeacher during the migration window (dropped in
    Phase 5 with SubjectTeacher)."""
    if not (user and user.is_authenticated):
        return False
    if user.is_staff:
        return True
    if TeachingAssignment.objects.filter(
        batch=batch, teacher=user, is_active=True
    ).exists():
        return True
    return SubjectTeacher.objects.filter(
        subject__course_id=batch.course_id, teacher=user
    ).exists()


def can_edit_chapter_for_batch(user, chapter, batch):
    """Admin/staff, or the teacher of this chapter's subject *in this batch*.

    Now that a teacher↔batch link exists (TeachingAssignment), this gates on
    the specific (batch, subject). Falls back to the legacy course-wide
    SubjectTeacher during the migration window (dropped in Phase 5)."""
    if not (user and user.is_authenticated):
        return False
    if user.is_staff:
        return True
    if is_teacher_of(user, batch, chapter.subject):
        return True
    return SubjectTeacher.objects.filter(
        subject_id=chapter.subject_id, teacher=user
    ).exists()


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def build_batch_progress(batch):
    """Return a nested progress payload for one batch. No N+1: coverage for the
    whole batch is fetched in a single query and joined in memory."""
    course = batch.course

    # Subjects and chapters both define Meta.ordering, so these come ordered.
    subjects = list(course.subjects.all().prefetch_related("chapters"))

    # chapter_id -> BatchChapterProgress, one query for the whole batch.
    progress_by_chapter = {
        bp.chapter_id: bp
        for bp in BatchChapterProgress.objects.filter(batch=batch)
    }

    total = 0
    done = 0
    subjects_payload = []

    for subject in subjects:
        chapters = list(subject.chapters.all())
        s_total = len(chapters)
        s_done = 0
        chapters_payload = []

        for ch in chapters:
            bp = progress_by_chapter.get(ch.id)
            covered = bool(bp and bp.is_covered)
            if covered:
                s_done += 1
            chapters_payload.append({
                "id": str(ch.id),
                "title": ch.title,
                "order": ch.order,
                "is_covered": covered,
                "covered_at": bp.covered_at.isoformat() if (bp and bp.covered_at) else None,
                "note": (bp.note if bp else ""),
            })

        total += s_total
        done += s_done
        subjects_payload.append({
            "id": str(subject.id),
            "name": subject.name,
            "order": subject.order,
            "teacher_name": _batch_subject_teacher_name(batch, subject),
            "chapters_total": s_total,
            "chapters_done": s_done,
            "percent": round(s_done / s_total * 100) if s_total else 0,
            "chapters": chapters_payload,
        })

    return {
        "batch": {
            "id": str(batch.id),
            "name": batch.name,
            "code": batch.code,
            "course_id": str(course.id),
        },
        "chapters_total": total,
        "chapters_done": done,
        "chapters_left": total - done,
        "percent": round(done / total * 100) if total else 0,
        "subjects": subjects_payload,
    }
