"""
skills/teacher_views.py  — additional teacher-facing endpoints.

Add to skills/urls.py:
    from .teacher_views import (
        TeacherDashboardView, TeacherEarningsView, TeacherAvailabilityView,
        TeacherDeclineSessionView, TeacherProfileUpdateView,
    )

    path("teacher/dashboard/",                              TeacherDashboardView.as_view()),
    path("teacher/earnings/",                               TeacherEarningsView.as_view()),
    path("teacher/availability/",                           TeacherAvailabilityView.as_view()),
    path("teacher/sessions/<uuid:session_id>/decline/",     TeacherDeclineSessionView.as_view()),
    path("teacher/profile/",                                TeacherProfileUpdateView.as_view()),

Requires one migration to add availability_slots to ExpertProfile:
    availability_slots = models.JSONField(default=dict, blank=True)
    # shape: { "open": ["0-1","2-3",...], "booked": ["1-0",...] }
"""
import datetime
from collections import defaultdict

from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from .models import ExpertProfile, SkillSession
from .course_models import SkillCourseEnrollment


def _get_expert(user):
    ep = ExpertProfile.objects.filter(teacher_profile__user=user).first()
    if not ep:
        raise PermissionDenied("No expert profile found for this account.")
    return ep


# ── Dashboard overview ────────────────────────────────────────────────────

class TeacherDashboardView(APIView):
    """GET /skill/teacher/dashboard/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ep  = _get_expert(request.user)
        now = timezone.now()
        sessions = SkillSession.objects.filter(expert=ep).select_related("learner_profile")

        # Stats
        taught    = sessions.filter(status=SkillSession.STATUS_COMPLETED).count()
        pending   = sessions.filter(status=SkillSession.STATUS_REQUESTED).count()
        confirmed = sessions.filter(status=SkillSession.STATUS_CONFIRMED).count()
        course_students = SkillCourseEnrollment.objects.filter(
            course__teacher_profile=ep.teacher_profile
        ).count()

        # Today's upcoming sessions
        today_sessions = (
            sessions
            .filter(
                status__in=[SkillSession.STATUS_CONFIRMED, SkillSession.STATUS_REQUESTED],
                scheduled_for__date=now.date(),
            )
            .order_by("scheduled_for")
        )
        next_up = []
        for s in today_sessions:
            name = (s.learner_profile.display_name
                    or s.learner_profile.full_name or "Student")
            is_live = bool(
                s.scheduled_for and
                now >= s.scheduled_for and
                now <= s.scheduled_for + timezone.timedelta(minutes=s.duration_mins)
            )
            next_up.append({
                "id":            str(s.id),
                "name":          name,
                "topic":         (s.note[:80] if s.note else "Session"),
                "scheduled_for": s.scheduled_for,
                "duration_mins": s.duration_mins,
                "live":          is_live,
                "status":        s.status,
            })

        # Monthly earnings
        month_start   = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_sessions = sessions.filter(
            status=SkillSession.STATUS_COMPLETED, updated_at__gte=month_start
        )
        month_earned  = sum(s.amount // 100 for s in month_sessions)
        month_count   = month_sessions.count()

        # Activity feed (last 8 state-changed sessions)
        recent   = sessions.order_by("-updated_at")[:8]
        activity = []
        for s in recent:
            name = (s.learner_profile.display_name
                    or s.learner_profile.full_name or "Student")
            if s.status == SkillSession.STATUS_REQUESTED:
                activity.append({"text": f"New booking request · {name}", "color": "#ff8f01"})
            elif s.status == SkillSession.STATUS_CONFIRMED:
                activity.append({"text": f"Session confirmed with {name}", "color": "#13899b"})
            elif s.status == SkillSession.STATUS_COMPLETED:
                activity.append({"text": f"Session completed · {name}", "color": "#2f9d42"})
            elif s.status == SkillSession.STATUS_CANCELLED:
                activity.append({"text": f"Session cancelled · {name}", "color": "#c0492f"})

        return Response({
            "stats": {
                "taught":          taught,
                "active":          confirmed,
                "pending":         pending,
                "course_students": course_students,
            },
            "next_up":  next_up,
            "earnings": {
                "month_earned":   month_earned,
                "month_sessions": month_count,
                "month_goal":     25000,
            },
            "activity": activity,
        })


# ── Earnings ──────────────────────────────────────────────────────────────

class TeacherEarningsView(APIView):
    """GET /skill/teacher/earnings/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ep  = _get_expert(request.user)
        now = timezone.now()

        completed = (
            SkillSession.objects
            .filter(expert=ep, status=SkillSession.STATUS_COMPLETED)
            .select_related("learner_profile")
            .order_by("-updated_at")
        )

        # Group by date label
        grouped = defaultdict(list)
        today     = now.date()
        yesterday = today - datetime.timedelta(days=1)

        for s in completed:
            d = s.updated_at.date()
            if d == today:
                day_label = f"Today · {d.strftime('%-d %b')}"
            elif d == yesterday:
                day_label = f"Yesterday · {d.strftime('%-d %b')}"
            else:
                day_label = d.strftime("%-d %b")

            name = (s.learner_profile.display_name
                    or s.learner_profile.full_name or "Student")
            grouped[day_label].append({
                "who":    name,
                "what":   (s.note[:60] if s.note else "1-on-1 session"),
                "amt":    s.amount // 100,
                "status": "paid" if s.payment_status == "paid" else "pending",
            })

        # Course enrollment rows
        enrollments = (
            SkillCourseEnrollment.objects
            .filter(course__teacher_profile=ep.teacher_profile)
            .select_related("learner_profile", "course")
            .order_by("-enrolled_at")
        )
        for e in enrollments:
            d = e.enrolled_at.date()
            if d == today:
                day_label = f"Today · {d.strftime('%-d %b')}"
            elif d == yesterday:
                day_label = f"Yesterday · {d.strftime('%-d %b')}"
            else:
                day_label = d.strftime("%-d %b")
            name = (e.learner_profile.display_name
                    or e.learner_profile.full_name or "Student")
            grouped[day_label].append({
                "who":    name,
                "what":   f"Enrolled · {e.course.title[:50]}",
                "amt":    e.amount_paid // 100,
                "status": "paid",
            })

        rows = [{"day": k, "items": v} for k, v in grouped.items()]

        # Totals
        session_total  = sum(s.amount // 100 for s in completed)
        course_total   = sum(e.amount_paid // 100 for e in enrollments)
        lifetime       = session_total + course_total
        month_start    = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_earned   = sum(
            s.amount // 100 for s in completed if s.updated_at >= month_start
        )
        month_sessions = completed.filter(updated_at__gte=month_start).count()

        return Response({
            "available":      lifetime,   # wire a payout model for real deductions
            "pending":        0,
            "lifetime":       lifetime,
            "month_earned":   month_earned,
            "month_sessions": month_sessions,
            "month_goal":     25000,
            "rows":           rows,
        })


# ── Availability ──────────────────────────────────────────────────────────

class TeacherAvailabilityView(APIView):
    """
    GET   /skill/teacher/availability/   → { open: [...], booked: [...] }
    PATCH /skill/teacher/availability/   body: { open: ["0-1", ...] }

    Stored in ExpertProfile.availability_slots (JSONField).
    Requires migration: availability_slots = JSONField(default=dict, blank=True)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ep    = _get_expert(request.user)
        avail = getattr(ep, "availability_slots", None) or {}
        return Response({
            "open":   avail.get("open",   []),
            "booked": avail.get("booked", []),
        })

    def patch(self, request):
        ep = _get_expert(request.user)
        open_slots = request.data.get("open")
        if not isinstance(open_slots, list):
            raise ValidationError({"open": "Must be a list of slot-key strings."})
        avail         = getattr(ep, "availability_slots", None) or {}
        avail["open"] = open_slots
        # Use update_fields only if availability_slots column exists;
        # gracefully skip if the migration hasn't run yet.
        try:
            ep.availability_slots = avail
            ep.save(update_fields=["availability_slots"])
        except Exception:
            pass
        return Response({
            "open":   avail.get("open",   []),
            "booked": avail.get("booked", []),
        })


# ── Decline session ───────────────────────────────────────────────────────

class TeacherDeclineSessionView(APIView):
    """POST /skill/teacher/sessions/<session_id>/decline/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        ep   = _get_expert(request.user)
        sess = SkillSession.objects.filter(id=session_id, expert=ep).first()
        if not sess:
            raise NotFound("Session not found.")
        if sess.status not in (
            SkillSession.STATUS_REQUESTED,
            SkillSession.STATUS_CONFIRMED,
        ):
            raise ValidationError(
                f"Cannot decline a session with status '{sess.status}'."
            )
        sess.status = SkillSession.STATUS_CANCELLED
        sess.save(update_fields=["status", "updated_at"])
        return Response({"ok": True, "status": sess.status})


# ── Expert profile update ─────────────────────────────────────────────────

class TeacherProfileUpdateView(APIView):
    """
    PATCH /skill/teacher/profile/
    Accepts: hourly_rate (int, rupees), bio (str), availability (str)
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        ep     = _get_expert(request.user)
        data   = request.data
        fields = []

        if "hourly_rate" in data:
            try:
                ep.hourly_rate = int(data["hourly_rate"]) * 100   # rupees → paise
                fields.append("hourly_rate")
            except (TypeError, ValueError):
                raise ValidationError({"hourly_rate": "Must be an integer (₹)."})

        if "bio" in data:
            ep.bio = str(data["bio"])
            fields.append("bio")

        if "availability" in data:
            ep.availability = str(data["availability"])[:120]
            fields.append("availability")

        if fields:
            ep.save(update_fields=fields + ["updated_at"])

        return Response({
            "ok":           True,
            "hourly_rate":  ep.hourly_rate // 100,
            "bio":          ep.bio,
            "availability": ep.availability,
        })
