"""Course progress, computed from teacher-ticked Chapter.is_covered flags.

One shared state per course: every enrolled student sees the same coverage,
including students who join later. Course % is chapter-weighted:
    percent = chapters_covered / chapters_total * 100
across every chapter in every subject of the course.
"""

from .admin_academy_views import _teacher_name
from .models import TeachingAssignment


def _course_subject_teacher_name(subject):
    """No batch context here (this is the course-level progress view), so
    only the course-wide (batch=NULL) teaching assignment applies."""
    ta = (
        TeachingAssignment.objects
        .filter(subject=subject, batch__isnull=True, is_active=True)
        .select_related("teacher")
        .order_by("order")
        .first()
    )
    return _teacher_name(ta.teacher) if ta else ""


def build_course_progress(course):
    """Return a nested progress payload for one course."""
    # Subjects and chapters both define Meta.ordering, so .all() is ordered.
    subjects = list(course.subjects.all().prefetch_related("chapters"))

    total = 0
    done = 0
    subjects_payload = []

    for subject in subjects:
        chapters = list(subject.chapters.all())
        s_total = len(chapters)
        s_done = 0
        chapters_payload = []

        for ch in chapters:
            if ch.is_covered:
                s_done += 1
            chapters_payload.append({
                "id": str(ch.id),
                "title": ch.title,
                "order": ch.order,
                "is_covered": ch.is_covered,
                "covered_at": ch.covered_at.isoformat() if ch.covered_at else None,
            })

        total += s_total
        done += s_done
        subjects_payload.append({
            "id": str(subject.id),
            "name": subject.name,
            "order": subject.order,
            "teacher_name": _course_subject_teacher_name(subject),
            "chapters_total": s_total,
            "chapters_done": s_done,
            "percent": round(s_done / s_total * 100) if s_total else 0,
            "chapters": chapters_payload,
        })

    return {
        "course_id": str(course.id),
        "chapters_total": total,
        "chapters_done": done,
        "chapters_left": total - done,
        "percent": round(done / total * 100) if total else 0,
        "subjects": subjects_payload,
    }
