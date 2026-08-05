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
from .review_models import ExpertReview
from . import profile_ops
from .notifications import push_skill_bell


def _get_expert(user):
    ep = ExpertProfile.objects.filter(teacher_profile__user=user).first()
    if ep:
        return ep
    # Defense in depth for accounts created before ExpertProfile was wired into
    # signup: auto-provision a (blank, UNLISTED) profile for anyone who holds
    # the skill / guest-expert track, so the dashboard + editor always load and
    # the completeness gate can take over. A faculty-only teacher gets none.
    tp = getattr(user, "teacher_profile", None)
    if tp and tp.holds_track(tp.TRACK_SKILL):
        return ExpertProfile.objects.create(teacher_profile=tp)
    raise PermissionDenied("No expert profile found for this account.")


# ── Availability bookkeeping (shared with the booking flow) ───────────────
# `availability_slots` shape: {"open": ["0-1", ...], "booked": ["1-0", ...]}
# `open`   = teacher-declared free slots.
# `booked` = slots a learner has taken; this is the source of truth for
#            "taken" and is what greys a slot out for *everyone*.

def _avail(expert):
    return getattr(expert, "availability_slots", None) or {}


def slot_is_open(expert, slot_key):
    """A slot is bookable if the teacher declared it open, it isn't taken,
    and its next real-calendar occurrence isn't inside a blackout range."""
    a = _avail(expert)
    if slot_key not in a.get("open", []) or slot_key in a.get("booked", []):
        return False
    from .views import _slot_to_datetime
    occurs_on = _slot_to_datetime(slot_key)
    if occurs_on and expert.blackouts.filter(
        date_from__lte=occurs_on.date(), date_to__gte=occurs_on.date()
    ).exists():
        return False
    return True


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
        # "Total students" = unique learners across every session, not a
        # completed-session count.
        total_students = (
            sessions.values("learner_profile").distinct().count()
        )
        course_students = SkillCourseEnrollment.objects.filter(
            course__teacher_profile=ep.teacher_profile
        ).count()

        # Reviews — cached average + latest three for the dashboard card.
        my_reviews    = ExpertReview.objects.filter(expert=ep, is_public=True)
        reviews_count = my_reviews.count()
        avg_rating    = float(ep.rating) if ep.rating is not None else None
        recent_reviews = [
            {
                "id":         str(r.id),
                "rating":     r.rating,
                "body":       (r.body[:160] if r.body else ""),
                "reviewer":   (r.learner_profile.display_name
                               or r.learner_profile.full_name or "Student"),
                "created_at": r.created_at,
            }
            for r in my_reviews.select_related("learner_profile")[:3]
        ]

        # Upcoming bookings — everything from now onward (not just today),
        # soonest first. Unscheduled requests sort last.
        upcoming_qs = (
            sessions
            .filter(status__in=[SkillSession.STATUS_CONFIRMED, SkillSession.STATUS_REQUESTED])
            .exclude(scheduled_for__lt=now)
            .order_by("scheduled_for")
        )
        next_up = []
        for s in upcoming_qs[:6]:
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

        # ── Dashboard stat tiles + 7-day chart (design_handoff_skilldev) ──
        monday = (now - timezone.timedelta(days=now.weekday())).date()
        week_start = timezone.make_aware(datetime.datetime.combine(monday, datetime.time.min))
        week_end = week_start + timezone.timedelta(days=7)
        prev_week_start = week_start - timezone.timedelta(days=7)

        week_sessions = sessions.filter(
            status__in=[SkillSession.STATUS_CONFIRMED, SkillSession.STATUS_COMPLETED],
            scheduled_for__gte=week_start, scheduled_for__lt=week_end,
        )
        sessions_this_week = week_sessions.count()
        sessions_last_week = sessions.filter(
            status__in=[SkillSession.STATUS_CONFIRMED, SkillSession.STATUS_COMPLETED],
            scheduled_for__gte=prev_week_start, scheduled_for__lt=week_start,
        ).count()

        week_chart = [0] * 7
        for s in week_sessions:
            week_chart[(s.scheduled_for.date() - monday).days] += 1

        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        hours_this_month = round(
            sum(sessions.filter(
                status=SkillSession.STATUS_COMPLETED, updated_at__gte=month_start,
            ).values_list("duration_mins", flat=True)) / 60, 1,
        )

        active_students = (
            sessions.exclude(status=SkillSession.STATUS_CANCELLED)
            .values("learner_profile").distinct().count()
        )

        today = now.date()
        today_schedule = [n for n in next_up if n["scheduled_for"] and n["scheduled_for"].date() == today]

        # Rank among listed experts by cached rating (ties broken by session count).
        rank = None
        if ep.rating is not None:
            better = ExpertProfile.objects.filter(is_listed=True, rating__gt=ep.rating).count()
            rank = better + 1
        total_experts = ExpertProfile.objects.filter(is_listed=True).count()

        return Response({
            "stats": {
                "taught":          taught,
                "total_students":  total_students,
                "active":          confirmed,
                "pending":         pending,
                "avg_rating":      avg_rating,
                "reviews_count":   reviews_count,
                "course_students": course_students,
                "sessions_this_week":      sessions_this_week,
                "sessions_this_week_delta": sessions_this_week - sessions_last_week,
                "active_students":         active_students,
                "hours_this_month":        hours_this_month,
                "rank":                    rank,
                "total_experts":           total_experts,
            },
            "week_chart":      {"labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "counts": week_chart},
            "today_schedule":  today_schedule,
            "next_up":         next_up,
            "recent_reviews":  recent_reviews,
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
            "open":      avail.get("open",   []),
            "booked":    avail.get("booked", []),
            "blackouts": _blackout_list(ep),
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


# ── Mastery ─────────────────────────────────────────────────────────────

class TeacherMasteryTargetView(APIView):
    """PUT /skill/teacher/mastery-target/  body: { target: int }"""
    permission_classes = [IsAuthenticated]

    def put(self, request):
        ep = _get_expert(request.user)
        try:
            target = int(request.data.get("target"))
        except (TypeError, ValueError):
            raise ValidationError({"target": "Must be an integer."})
        if not (1 <= target <= 12):
            raise ValidationError({"target": "Must be between 1 and 12."})
        ep.mastery_target = target
        ep.save(update_fields=["mastery_target"])
        return Response({"target": ep.mastery_target})


class TeacherStudentsView(APIView):
    """GET /skill/teacher/students/  — mastery tracker roster.

    One row per learner this expert has ever had a session with, with
    progress derived (never stored) against the expert's mastery_target.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import mastery_progress

        ep  = _get_expert(request.user)
        now = timezone.now()
        sessions = (
            SkillSession.objects
            .filter(expert=ep)
            .exclude(status__in=[SkillSession.STATUS_CANCELLED])
            .select_related("learner_profile")
            .order_by("scheduled_for")
        )

        by_learner = defaultdict(list)
        for s in sessions:
            by_learner[s.learner_profile_id].append(s)

        rows = []
        for learner_id, sess_list in by_learner.items():
            learner = sess_list[0].learner_profile
            m = mastery_progress(ep, learner)

            upcoming = [
                s for s in sess_list
                if s.status == SkillSession.STATUS_CONFIRMED
                and s.scheduled_for and s.scheduled_for >= now
            ]
            upcoming.sort(key=lambda s: s.scheduled_for)
            next_session = upcoming[0].scheduled_for if upcoming else None

            completed = [s for s in sess_list if s.status == SkillSession.STATUS_COMPLETED]
            last_session = max((s.updated_at for s in completed), default=None)

            # Most recent non-blank teacher note across this learner's sessions.
            noted = [s for s in sess_list if s.teacher_note]
            note = max(noted, key=lambda s: s.updated_at).teacher_note if noted else ""

            rows.append({
                "learner_id":     str(learner_id),
                "name":           learner.display_name or learner.full_name or "Student",
                "track":          ep.headline or (ep.category.label if ep.category_id else ""),
                "progress":       m["progress"],
                "target":         m["target"],
                "mastered":       m["mastered"],
                "next_session":   next_session,
                "last_session":   last_session,
                "note":           note,
            })

        rows.sort(key=lambda r: (r["mastered"], r["name"]))

        active_students   = sum(1 for r in rows if not r["mastered"])
        reached_mastery   = sum(1 for r in rows if r["mastered"])
        sessions_delivered = sum(r["progress"] for r in rows)

        return Response({
            "mastery_target": ep.mastery_target,
            "stats": {
                "active_students":    active_students,
                "reached_mastery":    reached_mastery,
                "sessions_delivered": sessions_delivered,
            },
            "students": rows,
        })


class TeacherMarkStudentSessionCompleteView(APIView):
    """POST /skill/teacher/students/<learner_id>/mark-complete/

    The Students (mastery tracker) card has one "Mark session complete"
    button per student, not per session — completes that student's oldest
    still-CONFIRMED session with this expert (mirrors TeacherCompleteSessionView's
    effects: bumps sessions_count, frees the slot).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, learner_id):
        ep = _get_expert(request.user)
        sess = (
            SkillSession.objects
            .filter(expert=ep, learner_profile_id=learner_id, status=SkillSession.STATUS_CONFIRMED)
            .order_by("scheduled_for")
            .first()
        )
        if not sess:
            raise ValidationError("No confirmed session to mark complete for this student.")
        sess.status = SkillSession.STATUS_COMPLETED
        sess.save(update_fields=["status", "updated_at"])
        ep.sessions_count = SkillSession.objects.filter(
            expert=ep, status=SkillSession.STATUS_COMPLETED
        ).count()
        ep.save(update_fields=["sessions_count"])
        if sess.slot_key:
            free_slot(ep, sess.slot_key)

        from .models import mastery_progress
        m = mastery_progress(ep, sess.learner_profile)
        return Response({"ok": True, "progress": m["progress"], "target": m["target"], "mastered": m["mastered"]})


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
        if sess.status == SkillSession.STATUS_CONFIRMED and sess.started_at:
            raise ValidationError(
                "This session is already live — use 'Mark done' once it's "
                "finished, not decline."
            )
        # A pending REQUEST being declined is DECLINED (design's Requests tab
        # outcome); backing out of an already-CONFIRMED session is a
        # cancellation, not a decline — keep that distinction visible.
        sess.status = (
            SkillSession.STATUS_DECLINED
            if sess.status == SkillSession.STATUS_REQUESTED
            else SkillSession.STATUS_CANCELLED
        )
        sess.save(update_fields=["status", "updated_at"])
        # Release the reserved availability slot back to the expert's grid.
        if sess.slot_key:
            free_slot(ep, sess.slot_key)
        push_skill_bell(sess, "declined")
        return Response({"ok": True, "status": sess.status})


# ── Expert profile update ─────────────────────────────────────────────────

class TeacherProfileUpdateView(APIView):
    """
    GET   /skill/teacher/profile/  → current editable expert profile + completeness
    PATCH /skill/teacher/profile/  → update it (JSON or multipart for the photo)

    This is the guest expert's "manage profile" screen. It covers the full
    signup field set so an expert can complete OR edit everything here:
      • subject: category + subject_description, headline, skill_tags
      • about you (bio), languages, availability note
      • hourly fee (₹ → stored in paise)
      • location: class_mode (home|travel|online) + class_location, and
        pincode/state/district/city for offline discovery
      • personal: full_name, date_of_birth, phone, profile photo (SELF learner)
      • payment: payment_upi / payment_name / payment_note (P2P — learners pay
        the expert directly; ShikshaCom takes no cut)

    The response always carries ``is_complete`` + ``missing`` so the dashboard
    can force completion before the expert is listed/discoverable.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ep = _get_expert(request.user)
        return Response(profile_ops.serialize_expert(ep))

    def patch(self, request):
        ep      = _get_expert(request.user)
        data    = request.data
        files   = getattr(request, "FILES", None) or {}

        # Expert-side fields (subject/teaching/location/payment + photo).
        ep_fields = profile_ops.apply_expert_fields(ep, data, files=files)
        profile_ops.validate_location(ep)
        if ep_fields:
            ep.save(update_fields=list(dict.fromkeys(ep_fields)) + ["updated_at"])

        # Personal fields live on the SELF learner profile.
        learner = request.user.default_learner_profile()
        if learner:
            p_fields = profile_ops.apply_personal_fields(learner, data, files=files)
            if p_fields:
                learner.save()

        # List the expert now if the profile just became complete.
        ep.refresh_listing()

        return Response({"ok": True, **profile_ops.serialize_expert(ep)})


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
            "open":      a.get("open",   []),
            "booked":    a.get("booked", []),
            "blackouts": _blackout_list(ep),
        })


def _blackout_list(ep):
    return [
        {"id": str(b.id), "date_from": b.date_from, "date_to": b.date_to, "label": b.label}
        for b in ep.blackouts.all()
    ]


class ExpertBlackoutsView(APIView):
    """
    GET  /skill/teacher/blackouts/   → this expert's blackout ranges
    POST /skill/teacher/blackouts/   body: { date_from, date_to, label? }
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ep = _get_expert(request.user)
        return Response(_blackout_list(ep))

    def post(self, request):
        from .blackout_models import ExpertBlackoutDate

        ep = _get_expert(request.user)
        date_from = request.data.get("date_from")
        date_to = request.data.get("date_to") or date_from
        if not date_from:
            raise ValidationError({"date_from": "Required."})
        if date_to < date_from:
            raise ValidationError({"date_to": "Must be on or after date_from."})
        b = ExpertBlackoutDate.objects.create(
            expert=ep, date_from=date_from, date_to=date_to,
            label=(request.data.get("label") or "")[:120],
        )
        return Response(
            {"id": str(b.id), "date_from": b.date_from, "date_to": b.date_to, "label": b.label},
            status=201,
        )


class ExpertBlackoutDetailView(APIView):
    """DELETE /skill/teacher/blackouts/<id>/"""
    permission_classes = [IsAuthenticated]

    def delete(self, request, blackout_id):
        ep = _get_expert(request.user)
        deleted, _ = ep.blackouts.filter(id=blackout_id).delete()
        if not deleted:
            raise NotFound("Blackout range not found.")
        return Response(status=204)


class ExpertPricingView(APIView):
    """
    GET /skill/teachers/<expert_id>/pricing/  — the pricing ladder for THIS
    learner booking THIS expert (WORKFLOW.md §5).

    The tier/copy ("First session is ₹99", the bundle-savings panel, etc.) is
    always computed and returned — the live prototype shows that panel even
    while free-launch is on, it's the *displayed price* that's overridden to
    "Free during launch" (`is_free: true`). No real payment collection is
    wired here either way — Razorpay stays unimplemented, booking always
    charges 0 while GlobalSettings.free_trial_enabled is on.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, expert_id):
        from accounts.auth_flow import get_active_profile
        from .models import mastery_progress
        from global_settings.models import GlobalSettings

        ep = ExpertProfile.objects.filter(id=expert_id, is_listed=True).first()
        if not ep:
            raise NotFound("Expert not found.")

        gs = GlobalSettings.load()
        is_free = gs.free_trial_enabled

        learner = get_active_profile(request)
        completed = 0
        if learner:
            completed = mastery_progress(ep, learner)["progress"]

        unit_price = ep.hourly_rate or 49900  # ₹499 fallback per the design
        remaining = max(0, ep.mastery_target - completed)

        if completed == 0:
            tier = "intro"
            # "Regular sessions are ₹499 after this one" needs the standard
            # rate alongside the intro price — the design shows both numbers
            # in the same sentence, not just the intro price.
            data = {
                "tier": tier,
                "unit_price": gs.skill_intro_session_paise,
                "regular_unit_price": unit_price,
            }
        elif remaining > 1:
            tier = "bundle"
            full_total = unit_price * remaining
            discount = round(full_total * gs.skill_bundle_discount_pct / 100)
            data = {
                "tier": tier,
                "unit_price": unit_price,
                "bundle_sessions": remaining,
                "bundle_total": full_total - discount,
                "bundle_savings": discount,
            }
        else:
            tier = "single"
            data = {"tier": tier, "unit_price": unit_price}

        return Response({"is_free": is_free, **data})
