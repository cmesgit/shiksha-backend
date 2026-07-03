"""
PLACEMENT: backend/backend/skills/student_skill_views.py
ACTION:    Replace the entire file.

Changes from original:
  1. fmt_session() adds expert_teacher_id (TeacherProfile UUID) so the
     frontend can open a WS chat DM with the expert.
  2. experts_data list adds teacher_id for the same reason.
  3. New SkillSessionDetailView at the bottom — powers the session detail page.
     Registered in urls.py as: path("sessions/<uuid:session_id>/", SkillSessionDetailView.as_view())
"""
import datetime
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import PermissionDenied, NotFound

from accounts.auth_flow import get_active_profile
from .models import ExpertProfile, SkillSession, SkillCategory
from .course_models import SkillCourseEnrollment, SkillLectureProgress, SkillCourseLecture
from .review_models import ExpertReview


class StudentSkillDashboardView(APIView):
    """
    GET /skill/student/dashboard/

    Returns everything the student Skill Dev section needs in one call:
      - stats:             enrolled_count, lessons_done, hours_learned, upcoming_count
      - skill_courses:     in-progress enrollments with per-course progress
      - completed_courses: completed enrollments
      - upcoming_sessions: confirmed/requested sessions, upcoming first
      - past_sessions:     completed sessions with review status
      - reviewable:        sessions awaiting a review
      - experts:           experts the learner has sessions with
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
            is_confirmed = s.status == SkillSession.STATUS_CONFIRMED
            # Live if the expert has actually started the class (started_at set),
            # OR we're inside the originally scheduled window. Either way only
            # for an accepted (confirmed) session.
            in_window = bool(
                scheduled and
                now >= scheduled and
                now <= scheduled + datetime.timedelta(minutes=s.duration_mins)
            )
            is_live = bool(is_confirmed and (s.started_at or in_window))
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

            # Expert photo
            img = None
            if s.expert.photo:
                img = request.build_absolute_uri(s.expert.photo.url)
            else:
                lp = s.expert.user.default_learner_profile()
                if lp and lp.profile_photo:
                    img = request.build_absolute_uri(lp.profile_photo.url)

            return {
                "id":                 str(s.id),
                "session_id":         str(s.id),
                "expert_id":          str(s.expert.id),
                "expert_teacher_id":  str(s.expert.teacher_profile_id),  # TeacherProfile UUID for chat
                "expert_name":        expert_name,
                "expert_img":         img,
                "topic":              (s.note[:60] if s.note else "1-on-1 session"),
                "when":               when_str,
                "scheduled_for":      scheduled,
                "dur":                f"{s.duration_mins} min",
                "duration_mins":      s.duration_mins,
                "live":               is_live,
                "joinable":           is_confirmed,
                "started":            bool(s.started_at),
                "status":             s.status,
                "reviewed":           str(s.id) in reviewed_ids if is_past else None,
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
            c         = e.course
            total_lec = SkillCourseLecture.objects.filter(section__course=c).count()
            done_lec  = SkillLectureProgress.objects.filter(enrollment=e).count()
            pct       = round(done_lec * 100 / total_lec) if total_lec else 0

            sections = c.sections.prefetch_related("lectures").order_by("order")
            modules  = []
            lec_cursor = 0
            for sec in sections:
                sec_lecs         = list(sec.lectures.all())
                sec_total        = len(sec_lecs)
                completed_in_sec = SkillLectureProgress.objects.filter(
                    enrollment=e, lecture__section=sec
                ).count()
                done_all = completed_in_sec == sec_total and sec_total > 0
                is_cur   = (not done_all and lec_cursor <= done_lec < lec_cursor + sec_total)
                modules.append({
                    "t":    sec.title,
                    "n":    sec_total,
                    "d":    f"{sec_total * 5}m",
                    "done": done_all,
                    "cur":  is_cur,
                })
                lec_cursor += sec_total

            resume_mod    = next((m["t"] for m in modules if m.get("cur")), (modules[0]["t"] if modules else ""))
            resume_lesson = f"Lesson {done_lec + 1}"

            tp          = c.teacher_profile
            lp          = tp.user.default_learner_profile() if tp else None
            expert_name = (
                (lp.display_name or lp.full_name or "") if lp
                else (tp.user.username if tp else "Expert")
            )
            ep = getattr(tp, "expert_profile", None) if tp else None

            return {
                "id":            str(c.id),
                "enrollment_id": str(e.id),
                "title":         c.title,
                "expert":        expert_name,
                "expert_id":     str(ep.id) if ep else None,
                "img":           None,
                "cat":           c.category.label if c.category_id else "",
                "color":         c.category.color if c.category_id else "#0a808a",
                "pct":           pct,
                "done":          done_lec,
                "total":         total_lec,
                "hrs":           f"{max(1, round(total_lec * 5 / 60))}h",
                "rating":        float(ep.rating) if ep and ep.rating else None,
                "reviews":       0,
                "resume": {
                    "mod":    resume_mod,
                    "lesson": resume_lesson,
                    "at":     f"{done_lec * 5}m in",
                },
                "modules": modules,
            }

        skill_courses     = [build_course(e) for e in in_progress]
        completed_courses = [build_course(e) for e in completed_en]

        # ── Stats ────────────────────────────────────────────────────
        total_lessons_done = sum(c["done"] for c in skill_courses + completed_courses)
        hours_learned      = max(0, round(total_lessons_done * 5 / 60))

        # ── Experts the learner has sessions with ────────────────────
        expert_ids_seen = set()
        experts_data    = []
        for s in all_sessions:
            eid = str(s.expert.id)
            if eid not in expert_ids_seen:
                expert_ids_seen.add(eid)
                experts_data.append({
                    "id":         eid,
                    "teacher_id": str(s.expert.teacher_profile_id),  # TeacherProfile UUID for chat
                    "name":       s.expert.display_name(),
                    "skill":      s.expert.headline or "",
                    "rating":     float(s.expert.rating) if s.expert.rating else None,
                    "rate":       s.expert.rate_rupees,
                })

        # ── Session-based stats ──────────────────────────────────────
        # The learner Skill Dev dashboard is now 1-on-1 only (no self-paced
        # courses), so the headline stats are session/tutor based.
        completed_minutes = sum(s.duration_mins for s in past)
        session_hours     = round(completed_minutes / 60, 1)

        return Response({
            "stats": {
                # Primary — session/tutor focused (drives the learner dashboard)
                "tutors_booked":  len(experts_data),
                "sessions_done":  len(past),
                "session_hours":  session_hours,
                "upcoming_count": len(upcoming_data),
                # Legacy course fields — retained for backward compatibility
                # with any consumer still reading them.
                "enrolled_count": len(in_progress),
                "lessons_done":   total_lessons_done,
                "hours_learned":  hours_learned,
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
    Listed experts for the Explore tab (public). Supports:
      ?cat=  &search=
      ?offline=1                  → only experts who teach offline
      ?pincode= &district= &state= → "near me" location match
    Advertised experts are floated to the top.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        qs = ExpertProfile.objects.filter(is_listed=True).select_related(
            "category", "teacher_profile__user"
        ).prefetch_related("categories")
        cat = request.query_params.get("cat")
        if cat:
            from django.db.models import Q as _Q
            qs = qs.filter(
                _Q(category__slug=cat) | _Q(categories__slug=cat)
            ).distinct()
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(headline__icontains=search)

        p = request.query_params
        if (p.get("offline") or "").lower() in ("1", "true", "yes"):
            qs = qs.filter(class_mode__in=[ExpertProfile.MODE_HOME, ExpertProfile.MODE_TRAVEL])
        from django.db.models import Q
        loc = Q()
        if p.get("pincode"):
            loc |= Q(pincode=p["pincode"].strip())
        if p.get("district"):
            loc |= Q(district__iexact=p["district"].strip())
        if p.get("state"):
            loc |= Q(state__iexact=p["state"].strip())
        if loc:
            qs = qs.filter(loc)

        # Advertised first, then reach / rating.
        rows = list(qs.order_by("-reach_count", "-rating", "-sessions_count")[:80])
        rows = [e for e in rows if e.is_advertised()] + [e for e in rows if not e.is_advertised()]

        result = []
        for ep in rows[:40]:
            lp = ep.user.default_learner_profile()
            name = ""
            if lp:
                name = f"{lp.first_name} {lp.last_name}".strip() or lp.display_name or ""
            if not name:
                name = ep.user.username or ep.user.email or "Expert"
            result.append({
                "id":         str(ep.id),
                "teacher_id": str(ep.teacher_profile_id),  # TeacherProfile UUID for chat
                "name":       name,
                "role":       ep.headline,
                "img":        ep.photo.url if ep.photo else None,
                "rating":     float(ep.rating) if ep.rating else None,
                "rate":       ep.rate_rupees,
                "cat":        ep.category.slug if ep.category_id else "",
                # every subject this expert teaches (labels for chips)
                "subjects":   [c.label for c in ep.categories.all()]
                              or ([ep.category.label] if ep.category_id else []),
                "reply":      "~1h",
                "skills":     ep.skill_tags or [],
                # location / offline-teaching signals
                "class_mode": ep.class_mode,
                "offline":    ep.has_offline_class(),
                "location": (
                    {"city": ep.city, "district": ep.district,
                     "state": ep.state, "pincode": ep.pincode}
                    if (ep.city or ep.district or ep.state or ep.pincode) else None
                ),
                "languages":  ep.languages or [],
                "advertised": ep.is_advertised(),
            })
        return Response(result)


class SkillSessionDetailView(APIView):
    """
    GET /skill/sessions/<session_id>/
    Full detail for one session — powers the SkillSessionDetail.jsx page.
    Only the owning learner can access their own session.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, session_id):
        learner = get_active_profile(request)
        if not learner:
            raise PermissionDenied("Select a learner profile.")

        try:
            session = SkillSession.objects.select_related(
                "expert__teacher_profile__user",
                "expert__category",
            ).get(id=session_id, learner_profile=learner)
        except (SkillSession.DoesNotExist, ValueError):
            raise NotFound("Session not found.")

        expert = session.expert
        now    = timezone.now()

        # Live detection — 5 min before start until session end
        is_live = False
        if session.scheduled_for:
            start   = session.scheduled_for
            end     = start + datetime.timedelta(minutes=session.duration_mins)
            is_live = now >= start - datetime.timedelta(minutes=5) and now <= end

        reviewed = ExpertReview.objects.filter(
            session=session, learner_profile=learner
        ).exists()

        booking_ref = f"SHK-{session.id.hex[:8].upper()}"

        # Friendly time string
        when_str = ""
        if session.scheduled_for:
            d     = session.scheduled_for
            today = now.date()
            if d.date() == today:
                when_str = "Today · " + d.strftime("%-I:%M %p")
            elif d.date() == today + datetime.timedelta(days=1):
                when_str = "Tomorrow · " + d.strftime("%-I:%M %p")
            else:
                when_str = d.strftime("%-d %b %Y · %-I:%M %p")

        # Expert photo
        img = None
        if expert.photo:
            img = request.build_absolute_uri(expert.photo.url)
        else:
            lp = expert.user.default_learner_profile()
            if lp and lp.profile_photo:
                img = request.build_absolute_uri(lp.profile_photo.url)

        return Response({
            "id":             str(session.id),
            "booking_ref":    booking_ref,
            "topic":          session.note or "1-on-1 session",
            "status":         session.status,
            "payment_status": session.payment_status,
            # Money is settled DIRECTLY with the expert; surface their payee
            # details so the learner can pay them.
            "settlement":     "direct",
            "pay_to":         expert.pay_to(),
            "amount_rupees":  (session.amount or 0) // 100,
            "scheduled_for":  session.scheduled_for,
            "when":           when_str,
            "duration_mins":  session.duration_mins,
            "contact_mode":   session.contact_mode,
            "meeting_url":    session.meeting_url or None,
            "amount":         session.amount,
            "is_live":        is_live,
            "reviewed":       reviewed,
            "created_at":     session.created_at,
            "expert": {
                "id":                 str(expert.id),
                "teacher_profile_id": str(expert.teacher_profile_id),
                "name":               expert.display_name(),
                "img":                img,
                "title":              expert.headline or "",
                "cat":                expert.category.label if expert.category_id else "",
                "rate":               expert.rate_rupees,
                "rating":             float(expert.rating) if expert.rating else None,
                "skills":             expert.skill_tags or [],
                "availability":       expert.availability or "",
            },
        })
