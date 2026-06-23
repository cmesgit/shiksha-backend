"""
skills/student_skill_views.py — learner-facing aggregation endpoints.

Add to skills/urls.py:
    from .student_skill_views import StudentSkillDashboardView, StudentSkillExpertsView
    path("student/dashboard/",  StudentSkillDashboardView.as_view()),
    path("student/experts/",    StudentSkillExpertsView.as_view()),
"""
import datetime
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import PermissionDenied

from accounts.auth_flow import get_active_profile
from .models import ExpertProfile, SkillSession, SkillCategory
from .course_models import SkillCourseEnrollment, SkillLectureProgress, SkillCourseLecture
from .review_models import ExpertReview


class StudentSkillDashboardView(APIView):
    """
    GET /skill/student/dashboard/

    Returns everything the student Skill Dev section needs in one call:
      - stats:            enrolled_count, lessons_done, hours_learned, upcoming_count
      - skill_courses:    in-progress enrollments with per-course progress
      - completed_courses: completed enrollments
      - upcoming_sessions: confirmed/requested sessions, upcoming first
      - past_sessions:    completed sessions with review status
      - reviewable:       sessions awaiting a review (mirrors my-reviewable-sessions)
      - experts:          experts the learner has sessions with
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        learner = get_active_profile(request)
        if not learner:
            raise PermissionDenied("Select a learner profile.")

        now = timezone.now()

        # ── Sessions ──────────────────────────────────────────────────
        all_sessions = (
            SkillSession.objects
            .filter(learner_profile=learner)
            .select_related("expert__teacher_profile__user", "expert__category")
            .order_by("scheduled_for")
        )

        upcoming = [
            s for s in all_sessions
            if s.status in (SkillSession.STATUS_CONFIRMED, SkillSession.STATUS_REQUESTED)
        ]
        past = [
            s for s in all_sessions
            if s.status == SkillSession.STATUS_COMPLETED
        ]

        reviewed_ids = set(
            ExpertReview.objects
            .filter(learner_profile=learner)
            .values_list("session_id", flat=True)
        )

        def fmt_session(s, *, is_past=False):
            expert_name = s.expert.display_name()
            scheduled   = s.scheduled_for
            is_live     = bool(
                scheduled and
                now >= scheduled and
                now <= scheduled + datetime.timedelta(minutes=s.duration_mins)
            )
            when_str = ""
            if scheduled:
                today = now.date()
                d     = scheduled.date()
                if d == today:
                    when_str = "Today · " + scheduled.strftime("%-I:%M %p")
                elif d == today + datetime.timedelta(days=1):
                    when_str = "Tomorrow · " + scheduled.strftime("%-I:%M %p")
                else:
                    when_str = scheduled.strftime("%-d %b · %-I:%M %p")

            return {
                "id":            str(s.id),
                "session_id":    str(s.id),
                "expert_id":     str(s.expert.id),
                "expert_name":   expert_name,
                "expert_img":    None,  # wired if expert.photo exists
                "topic":         (s.note[:60] if s.note else "1-on-1 session"),
                "when":          when_str,
                "scheduled_for": scheduled,
                "dur":           f"{s.duration_mins} min",
                "duration_mins": s.duration_mins,
                "live":          is_live,
                "status":        s.status,
                "reviewed":      str(s.id) in reviewed_ids if is_past else None,
            }

        upcoming_data = [fmt_session(s) for s in upcoming]
        past_data     = [fmt_session(s, is_past=True) for s in reversed(past)]

        reviewable = [
            {
                "session_id":   str(s.id),
                "expert_id":    str(s.expert.id),
                "expert_name":  s.expert.display_name(),
                "completed_at": s.updated_at,
            }
            for s in past
            if str(s.id) not in reviewed_ids
        ]

        # ── Enrollments / courses ────────────────────────────────────
        enrollments = (
            SkillCourseEnrollment.objects
            .filter(learner_profile=learner)
            .select_related("course__teacher_profile__user", "course__category")
            .order_by("-enrolled_at")
        )

        in_progress  = [e for e in enrollments if e.status == SkillCourseEnrollment.STATUS_ACTIVE]
        completed_en = [e for e in enrollments if e.status == SkillCourseEnrollment.STATUS_COMPLETED]

        def build_course(e):
            c        = e.course
            total_lec = SkillCourseLecture.objects.filter(section__course=c).count()
            done_lec  = SkillLectureProgress.objects.filter(enrollment=e).count()
            pct       = round(done_lec * 100 / total_lec) if total_lec else 0

            # Build modules list from sections
            sections = c.sections.prefetch_related("lectures").order_by("order")
            modules  = []
            lec_cursor = 0
            for sec in sections:
                sec_lecs   = list(sec.lectures.all())
                sec_total  = len(sec_lecs)
                completed_in_sec = SkillLectureProgress.objects.filter(
                    enrollment=e, lecture__section=sec
                ).count()
                done_all  = completed_in_sec == sec_total and sec_total > 0
                is_cur    = (not done_all and lec_cursor <= done_lec < lec_cursor + sec_total)
                modules.append({
                    "t":    sec.title,
                    "n":    sec_total,
                    "d":    f"{sec_total * 5}m",   # rough; real duration needs sum(duration_sec)
                    "done": done_all,
                    "cur":  is_cur,
                })
                lec_cursor += sec_total

            # Find resume point
            resume_mod    = next((m["t"] for m in modules if m.get("cur")), (modules[0]["t"] if modules else ""))
            resume_lesson = f"Lesson {done_lec + 1}"

            tp   = c.teacher_profile
            lp   = tp.user.default_learner_profile() if tp else None
            expert_name = (
                (lp.display_name or lp.full_name or "") if lp
                else (tp.user.username if tp else "Expert")
            )
            ep  = getattr(tp, "expert_profile", None) if tp else None

            return {
                "id":          str(c.id),
                "enrollment_id": str(e.id),
                "title":       c.title,
                "expert":      expert_name,
                "expert_id":   str(ep.id) if ep else None,
                "img":         None,
                "cat":         c.category.label if c.category_id else "",
                "color":       c.category.color if c.category_id else "#0a808a",
                "pct":         pct,
                "done":        done_lec,
                "total":       total_lec,
                "hrs":         f"{max(1, round(total_lec * 5 / 60))}h",
                "rating":      float(ep.rating) if ep and ep.rating else None,
                "reviews":     0,
                "resume": {
                    "mod":    resume_mod,
                    "lesson": resume_lesson,
                    "at":     f"{done_lec * 5}m in",
                },
                "modules":     modules,
            }

        skill_courses    = [build_course(e) for e in in_progress]
        completed_courses = [build_course(e) for e in completed_en]

        # ── Stats ────────────────────────────────────────────────────
        total_lessons_done = sum(c["done"] for c in skill_courses + completed_courses)
        hours_learned      = max(0, round(total_lessons_done * 5 / 60))

        # ── Experts the learner has sessions/enrollments with ────────
        expert_ids_seen = set()
        experts_data    = []
        for s in all_sessions:
            eid = str(s.expert.id)
            if eid not in expert_ids_seen:
                expert_ids_seen.add(eid)
                experts_data.append({
                    "id":     eid,
                    "name":   s.expert.display_name(),
                    "skill":  s.expert.headline or "",
                    "rating": float(s.expert.rating) if s.expert.rating else None,
                    "rate":   s.expert.rate_rupees,
                })

        return Response({
            "stats": {
                "enrolled_count":  len(in_progress),
                "lessons_done":    total_lessons_done,
                "hours_learned":   hours_learned,
                "upcoming_count":  len(upcoming_data),
            },
            "skill_courses":      skill_courses,
            "completed_courses":  completed_courses,
            "upcoming_sessions":  upcoming_data,
            "past_sessions":      past_data,
            "reviewable":         reviewable,
            "experts":            experts_data,
        })


class StudentSkillExpertsView(APIView):
    """
    GET /skill/student/experts/
    Returns listed experts for the Explore tab (public, paginated via ?cat=&search=).
    Mirrors ExpertListView but returns the shape the student UI expects.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        qs = ExpertProfile.objects.filter(is_listed=True).select_related(
            "category", "teacher_profile__user"
        )
        cat = request.query_params.get("cat")
        if cat:
            qs = qs.filter(category__slug=cat)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(headline__icontains=search)

        result = []
        for ep in qs[:40]:
            lp = ep.user.default_learner_profile()
            name = ""
            if lp:
                name = f"{lp.first_name} {lp.last_name}".strip() or lp.display_name or ""
            if not name:
                name = ep.user.username or ep.user.email or "Expert"
            result.append({
                "id":       str(ep.id),
                "name":     name,
                "role":     ep.headline,
                "img":      ep.photo.url if ep.photo else None,
                "rating":   float(ep.rating) if ep.rating else None,
                "rate":     ep.rate_rupees,
                "cat":      ep.category.slug if ep.category_id else "",
                "reply":    "~1h",
                "skills":   ep.skill_tags or [],
            })
        return Response(result)
