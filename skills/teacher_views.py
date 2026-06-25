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


# ── Availability bookkeeping (shared with the booking flow) ───────────────
# `availability_slots` shape: {"open": ["0-1", ...], "booked": ["1-0", ...]}
# `open`   = teacher-declared free slots.
# `booked` = slots a learner has taken; this is the source of truth for
#            "taken" and is what greys a slot out for *everyone*.

def _avail(expert):
    return getattr(expert, "availability_slots", None) or {}


def slot_is_open(expert, slot_key):
    """A slot is bookable if the teacher declared it open and it isn't taken."""
    a = _avail(expert)
    return slot_key in a.get("open", []) and slot_key not in a.get("booked", [])


def mark_slot_booked(expert, slot_key):
    """Add slot_key to booked (idempotent) and persist."""
    a = _avail(expert)
    booked = list(a.get("booked", []))
    if slot_key not in booked:
        booked.append(slot_key)
    a["booked"] = booked
    expert.availability_slots = a
    expert.save(update_fields=["availability_slots"])


def free_slot(expert, slot_key):
    """Remove slot_key from booked (called on cancel/decline/complete)."""
    if not slot_key:
        return
    a = _avail(expert)
    booked = [s for s in a.get("booked", []) if s != slot_key]
    a["booked"] = booked
    expert.availability_slots = a
    expert.save(update_fields=["availability_slots"])


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

        # Advertising status (replaces the old earnings widget — there is no
        # earnings bar for guest experts; payments are settled directly with
        # learners, off-platform).
        sub = getattr(ep, "ad_subscription", None)
        advertising = {
            "is_advertised":  ep.is_advertised(),
            "is_featured":    ep.is_featured,
            "reach_count":    ep.reach_count,
            "billing_free":   ep.billing_is_free(),
            "sub_status":     sub.status if sub else "none",
            "sub_active":     bool(sub and sub.is_currently_active()),
            "period_end":     sub.current_period_end if sub else None,
        }

        # Profile-completeness nudges the dashboard can surface.
        profile_todo = {
            "needs_payment":  not bool(ep.payment_upi),
            "needs_location": ep.has_offline_class() and not bool(ep.class_location),
        }

        return Response({
            "stats": {
                "taught":          taught,
                "active":          confirmed,
                "pending":         pending,
                "course_students": course_students,
            },
            "next_up":      next_up,
            "advertising":  advertising,
            "profile_todo": profile_todo,
            "activity":     activity,
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
        ep.availability_slots = avail
        ep.save(update_fields=["availability_slots"])
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
        # Release the reserved availability slot back to the expert's grid.
        if sess.slot_key:
            free_slot(ep, sess.slot_key)
        return Response({"ok": True, "status": sess.status})


# ── Expert profile update ─────────────────────────────────────────────────

class TeacherProfileUpdateView(APIView):
    """
    GET   /skill/teacher/profile/  → current editable expert profile
    PATCH /skill/teacher/profile/  → update it

    Editable fields:
      hourly_rate (int ₹), bio, availability, subject_description,
      languages (list[str]),
      class_mode (home|travel|online) + class_location,
      pincode / state / district / city / latitude / longitude,
      payment_upi / payment_name / payment_note  (the expert's OWN payee
      details — learners pay them directly).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ep = _get_expert(request.user)
        return Response(self._serialize(ep))

    def _serialize(self, ep):
        return {
            "hourly_rate":         ep.hourly_rate // 100,
            "bio":                 ep.bio,
            "availability":        ep.availability,
            "subject_description": ep.subject_description,
            "languages":           ep.languages or [],
            "class_mode":          ep.class_mode,
            "class_location":      ep.class_location,
            "pincode":             ep.pincode,
            "state":               ep.state,
            "district":            ep.district,
            "city":                ep.city,
            "latitude":            ep.latitude,
            "longitude":           ep.longitude,
            "payment_upi":         ep.payment_upi,
            "payment_name":        ep.payment_name,
            "payment_note":        ep.payment_note,
            "is_advertised":       ep.is_advertised(),
            "reach_count":         ep.reach_count,
        }

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

        if "subject_description" in data:
            ep.subject_description = str(data["subject_description"])
            fields.append("subject_description")

        if "languages" in data:
            langs = data["languages"]
            if isinstance(langs, str):
                langs = [s.strip() for s in langs.split(",") if s.strip()]
            if not isinstance(langs, list):
                raise ValidationError({"languages": "Must be a list of languages."})
            ep.languages = [str(x)[:40] for x in langs][:10]
            fields.append("languages")

        # ── Location / class mode (offline-class search) ──────────────────
        new_mode = data.get("class_mode", ep.class_mode)
        if "class_mode" in data:
            if new_mode not in (ep.MODE_HOME, ep.MODE_TRAVEL, ep.MODE_ONLINE):
                raise ValidationError({"class_mode": "Must be home, travel or online."})
            ep.class_mode = new_mode
            fields.append("class_mode")

        if "class_location" in data:
            ep.class_location = str(data["class_location"])[:255]
            fields.append("class_location")

        for f in ("pincode", "state", "district", "city"):
            if f in data:
                setattr(ep, f, str(data[f])[:150])
                fields.append(f)

        for f in ("latitude", "longitude"):
            if f in data and data[f] not in (None, ""):
                try:
                    setattr(ep, f, float(data[f]))
                    fields.append(f)
                except (TypeError, ValueError):
                    raise ValidationError({f: "Must be a number."})

        # Exact location is required when teaching offline.
        effective_mode = ep.class_mode
        if effective_mode in (ep.MODE_HOME, ep.MODE_TRAVEL):
            location_text = (
                data.get("class_location", ep.class_location) or ""
            ).strip()
            if not location_text:
                raise ValidationError({
                    "class_location":
                    "Tell learners where the class is held (required for "
                    "'at my place' / 'I can travel')."
                })

        # ── Direct (P2P) payee details ────────────────────────────────────
        if "payment_upi" in data:
            ep.payment_upi = str(data["payment_upi"])[:120]
            fields.append("payment_upi")
        if "payment_name" in data:
            ep.payment_name = str(data["payment_name"])[:120]
            fields.append("payment_name")
        if "payment_note" in data:
            ep.payment_note = str(data["payment_note"])[:200]
            fields.append("payment_note")

        if fields:
            ep.save(update_fields=list(dict.fromkeys(fields)) + ["updated_at"])

        return Response({"ok": True, **self._serialize(ep)})


# ── Public availability read (any authenticated user, by expert id) ───────

class ExpertAvailabilityView(APIView):
    """
    GET /skill/teachers/<expert_id>/availability/  → { open: [...], booked: [...] }

    Public read of a *specific* expert's weekly availability, for the student
    booking screen. Unlike TeacherAvailabilityView (which serves the caller's
    OWN profile via _get_expert), this resolves the expert by id so a learner
    can see the tutor's real open/booked slots instead of a local mock.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, expert_id):
        ep = ExpertProfile.objects.filter(id=expert_id, is_listed=True).first()
        if not ep:
            raise NotFound("Expert not found.")
        a = getattr(ep, "availability_slots", None) or {}
        return Response({
            "open":   a.get("open",   []),
            "booked": a.get("booked", []),
        })
