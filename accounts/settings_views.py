"""
accounts/settings_views.py — the endpoints behind the Settings surface.

Everything here is new API for sections the redesigned Settings modal needs and
that had no server-side home:

  GET    /accounts/sessions/                  Sessions & devices — list
  POST   /accounts/sessions/<id>/revoke/      … revoke one
  POST   /accounts/sessions/revoke-others/    … log out of all other devices
  GET    /accounts/learning-goals/            Learning goals + real streak
  PATCH  /accounts/learning-goals/            … update the goal
  GET    /accounts/billing/                   Billing — real access & payments
  POST   /accounts/data-export/               Privacy — download my data
  POST   /accounts/delete-account/            Privacy — close my account
  GET    /accounts/choices/                   choice lists for the forms

DESIGN NOTE — nothing here invents data.
The handoff prototype showed a Free/Plus/Pro plan grid, session locations like
"Aizawl, Mizoram", and a 12-day streak. This platform has no plan catalogue (a
Course is bought individually), no geo-IP database, and no study-time log. So
each of those renders from what genuinely exists instead: real per-course access
windows and Razorpay payments; the session's IP; and a streak counted from dated
quiz attempts and assignment submissions. Where a value can't be known it is
absent from the payload rather than defaulted to something plausible.
"""
import json
from datetime import date, timedelta

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone

from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .auth_flow import get_active_profile, session_id_for, verify_account_password
from .models import AccountDeletionRequest, LearnerProfile, LearningGoal, UserSession
from .revocation import revoke_sessions


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _target_profile(request):
    """The LearnerProfile a per-profile setting applies to.

    Defaults to the profile in the JWT (`active_profile`), but accepts an
    explicit `profile_id` so a parent editing a child's settings from Settings →
    Profiles hits the right row. Always re-filtered on `account=request.user`,
    so a guessed id from another account 404s rather than leaking.
    """
    explicit = request.query_params.get("profile_id") or request.data.get("profile_id")
    if explicit:
        profile = LearnerProfile.objects.filter(
            id=explicit, account=request.user, is_active=True
        ).first()
        if not profile:
            raise PermissionDenied("Profile not found for this account.")
        return profile

    profile = get_active_profile(request)
    if profile is None:
        profile = request.user.default_learner_profile()
    if profile is None:
        raise ValidationError({"detail": "This account has no learner profile."})
    return profile


# ─────────────────────────────────────────────────────────────────────────────
# Sessions & devices
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_session(s, current_sid):
    return {
        "id": str(s.id),
        "device": s.label,
        "device_kind": s.device_kind,
        # The prototype showed a city here. Without a geo-IP database the only
        # truthful answer is the address itself, so that's what ships — a user
        # can still recognise "not my network" from an unfamiliar IP.
        "ip_address": s.ip_address or "",
        "created_at": s.created_at,
        "last_active_at": s.last_active_at,
        "is_current": str(s.id) == str(current_sid),
    }


class SessionListView(APIView):
    """Every live session on this account, most recently active first."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        current_sid = session_id_for(request)
        sessions = UserSession.objects.filter(
            user=request.user, revoked_at__isnull=True
        )
        return Response({
            "current_session_id": str(current_sid) if current_sid else None,
            "sessions": [_serialize_session(s, current_sid) for s in sessions],
        })


class SessionRevokeView(APIView):
    """Sign one other device out.

    Refuses to revoke the caller's own session: the UI has no Revoke button on
    the current device, and "log out" is the honest name for that action — it
    also needs to clear this browser's cookies, which this endpoint does not do.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        session = UserSession.objects.filter(
            id=session_id, user=request.user, revoked_at__isnull=True
        ).first()
        if not session:
            return Response({"detail": "Session not found."},
                            status=status.HTTP_404_NOT_FOUND)

        if str(session.id) == str(session_id_for(request)):
            raise ValidationError(
                {"detail": "That's this device — use Log out instead.",
                 "code": "cannot_revoke_current"}
            )

        revoke_sessions(request.user, [session])
        return Response({"detail": "Session revoked.", "revoked": 1})


class SessionRevokeOthersView(APIView):
    """"Log out of all other devices" — keeps the caller signed in."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        current_sid = session_id_for(request)
        others = list(
            UserSession.objects
            .filter(user=request.user, revoked_at__isnull=True)
            .exclude(id=current_sid)
            if current_sid else
            UserSession.objects.filter(user=request.user, revoked_at__isnull=True)
        )
        count = revoke_sessions(request.user, others)
        return Response({"detail": f"Signed out of {count} other device(s).",
                         "revoked": count})


# ─────────────────────────────────────────────────────────────────────────────
# Learning goals
# ─────────────────────────────────────────────────────────────────────────────

def _study_dates(profile, since):
    """Dates (in the server's local timezone) this profile did something the
    platform can genuinely call studying, on or after `since`.

    Sources are the only dated, per-profile learner records that exist:
    quiz attempts and assignment submissions. Deliberately NOT VideoProgress —
    that model is keyed on the account rather than the profile, and its
    `last_watched_at` is auto_now, so it holds one mutable timestamp per
    recording rather than a history of viewing days.
    """
    from assignments.models import AssignmentSubmission
    from quizzes.models import QuizAttempt

    dates = set()

    attempts = (
        QuizAttempt.objects
        .filter(learner_profile=profile, started_at__date__gte=since)
        .values_list("started_at", flat=True)
    )
    submissions = (
        AssignmentSubmission.objects
        .filter(learner_profile=profile, submitted_at__date__gte=since)
        .values_list("submitted_at", flat=True)
    )
    for stamp in list(attempts) + list(submissions):
        dates.add(timezone.localtime(stamp).date())
    return dates


def _streak(profile):
    """Consecutive days up to today with study activity.

    Today not yet being active does not break the streak — it's still in
    progress — so counting starts from yesterday in that case. Bounded to a
    one-year lookback so the query stays cheap.
    """
    today = timezone.localdate()
    dates = _study_dates(profile, today - timedelta(days=366))
    if not dates:
        return 0

    cursor = today if today in dates else today - timedelta(days=1)
    count = 0
    while cursor in dates:
        count += 1
        cursor -= timedelta(days=1)
    return count


def _serialize_goal(goal, profile):
    return {
        "profile_id": str(profile.id),
        "profile_name": profile.display_name,
        "daily_minutes": goal.daily_minutes,
        "active_days": goal.active_days or [],
        "reminder_time": goal.reminder_time.strftime("%H:%M") if goal.reminder_time else "",
        "reminders_enabled": goal.reminders_enabled,
        "streak_days": _streak(profile),
        # Told to the client so the UI can label the streak accurately instead
        # of implying it tracks every minute spent in the app.
        "streak_basis": "quizzes and assignments",
    }


class LearningGoalView(APIView):
    """The selected profile's study-habit settings, plus a computed streak."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = _target_profile(request)
        return Response(_serialize_goal(LearningGoal.for_profile(profile), profile))

    def patch(self, request):
        profile = _target_profile(request)
        goal = LearningGoal.for_profile(profile)
        data = request.data or {}

        if "daily_minutes" in data:
            try:
                minutes = int(data["daily_minutes"])
            except (TypeError, ValueError):
                raise ValidationError({"daily_minutes": "Must be a whole number."})
            # Matches the slider's range in the design (10 min – 2 hrs).
            if not 10 <= minutes <= 120:
                raise ValidationError(
                    {"daily_minutes": "Must be between 10 and 120 minutes."})
            goal.daily_minutes = minutes

        if "active_days" in data:
            days = data["active_days"]
            if not isinstance(days, list) or any(
                not isinstance(d, int) or not 0 <= d <= 6 for d in days
            ):
                raise ValidationError(
                    {"active_days": "Must be a list of weekday numbers, Mon=0 … Sun=6."})
            goal.active_days = sorted(set(days))

        if "reminder_time" in data:
            raw = (data["reminder_time"] or "").strip()
            if not raw:
                goal.reminder_time = None
            else:
                try:
                    hh, mm = raw.split(":")[:2]
                    goal.reminder_time = timezone.datetime.strptime(
                        f"{int(hh):02d}:{int(mm):02d}", "%H:%M").time()
                except (ValueError, TypeError):
                    raise ValidationError({"reminder_time": "Use HH:MM (24-hour)."})

        if "reminders_enabled" in data:
            if not isinstance(data["reminders_enabled"], bool):
                raise ValidationError({"reminders_enabled": "Must be true or false."})
            goal.reminders_enabled = data["reminders_enabled"]

        goal.save()
        return Response(_serialize_goal(goal, profile))


# ─────────────────────────────────────────────────────────────────────────────
# Billing
# ─────────────────────────────────────────────────────────────────────────────

class BillingView(APIView):
    """What this account actually pays for.

    There is no subscription-plan catalogue on this platform — access is bought
    per Course and granted as a time-boxed enrollments.Subscription — so this
    returns those access windows plus the Razorpay payment history behind them,
    not a Free/Plus/Pro tier. `mode` tells the UI which regime is live so it can
    say "free while we launch" rather than showing prices nobody is charged.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from enrollments.models import Subscription
        from global_settings.models import GlobalSettings
        from payments.models import Order

        gs = GlobalSettings.load()
        now = timezone.now()

        subs = (
            Subscription.objects
            .filter(user=request.user)
            .select_related("course", "learner_profile")
            .order_by("-expires_at")[:50]
        )
        orders = (
            Order.objects
            .filter(user=request.user)
            .select_related("course")
            .order_by("-created_at")[:50]
        )

        return Response({
            # "free" while free_trial_enabled is on, else the configured mode —
            # GlobalSettings.effective_mode already encodes that precedence.
            "mode": gs.effective_mode,
            "is_free_phase": gs.effective_mode == GlobalSettings.PAYMENT_FREE,
            "upi_id": gs.upi_id if gs.effective_mode == GlobalSettings.PAYMENT_MANUAL_UPI else "",
            "access": [
                {
                    "id": str(s.id),
                    "course": getattr(s.course, "title", "") or str(s.course_id),
                    "profile": getattr(s.learner_profile, "display_name", "") or "",
                    "status": s.status,
                    "starts_at": s.starts_at,
                    "expires_at": s.expires_at,
                    "is_active": s.status == "ACTIVE" and s.expires_at > now,
                }
                for s in subs
            ],
            "payments": [
                {
                    "id": str(o.id),
                    "course": getattr(o.course, "title", "") or str(o.course_id),
                    # Stored in paise; converted once here so no client has to
                    # remember the unit.
                    "amount_rupees": o.amount / 100,
                    "status": o.status,
                    "reference": o.razorpay_order_id,
                    "created_at": o.created_at,
                }
                for o in orders
            ],
        })


# ─────────────────────────────────────────────────────────────────────────────
# Privacy & data
# ─────────────────────────────────────────────────────────────────────────────

class DataExportView(APIView):
    """"Download my data" — returns the account's own records as JSON.

    Served synchronously as a file attachment rather than the "we'll email you a
    link" flow the prototype implied: the payload is a handful of rows per
    profile, so there is no reason to build a job queue and an email pipeline
    for it, and an immediate download is a better answer anyway.

    Scope is what the account holder supplied or generated. Deliberately
    excluded: other people's content (forum replies by others, chat messages
    from other accounts) and anything derived about them.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from enrollments.models import Enrollment, Subscription
        from payments.models import Order

        user = request.user
        profiles = user.learner_profiles.all()

        def profile_block(p):
            return {
                "display_name": p.display_name,
                "relationship": p.relationship,
                "student_id": p.student_id,
                "is_default": p.is_default,
                "is_active": p.is_active,
                "personal": {
                    "first_name": p.first_name, "last_name": p.last_name,
                    "full_name": p.full_name, "phone": p.phone,
                    "gender": p.gender,
                    "date_of_birth": p.date_of_birth.isoformat() if p.date_of_birth else None,
                    "bio": p.bio,
                },
                "address": {
                    "state": p.state, "district": p.district,
                    "city_town": p.city_town, "pin_code": p.pin_code,
                },
                "guardian": {
                    "father_name": p.father_name, "father_phone": p.father_phone,
                    "mother_name": p.mother_name, "mother_phone": p.mother_phone,
                    "guardian_name": p.guardian_name, "guardian_phone": p.guardian_phone,
                    "parent_guardian_email": p.parent_guardian_email,
                },
                "academic": {
                    "currently_studying": p.currently_studying,
                    "current_class": p.current_class, "stream": p.stream,
                    "board": p.board, "board_other": p.board_other,
                    "school_name": p.school_name, "academic_year": p.academic_year,
                    "highest_education": p.highest_education,
                    "reason_not_studying": p.reason_not_studying,
                },
                "created_at": p.created_at.isoformat(),
            }

        payload = {
            "exported_at": timezone.now().isoformat(),
            "account": {
                "email": user.email,
                "username": user.username,
                "is_verified": user.is_verified,
                "date_joined": user.date_joined.isoformat() if user.date_joined else None,
                "roles": user.get_active_roles(),
            },
            "profiles": [profile_block(p) for p in profiles],
            "enrollments": [
                {
                    "course": getattr(e.course, "title", "") or str(e.course_id),
                    "profile": getattr(e.learner_profile, "display_name", "") or "",
                    "created_at": e.created_at.isoformat() if getattr(e, "created_at", None) else None,
                }
                for e in Enrollment.objects.filter(user=user).select_related(
                    "course", "learner_profile")
            ],
            "subscriptions": [
                {
                    "course": getattr(s.course, "title", "") or str(s.course_id),
                    "status": s.status,
                    "starts_at": s.starts_at.isoformat(),
                    "expires_at": s.expires_at.isoformat(),
                }
                for s in Subscription.objects.filter(user=user).select_related("course")
            ],
            "payments": [
                {
                    "course": getattr(o.course, "title", "") or str(o.course_id),
                    "amount_rupees": o.amount / 100,
                    "status": o.status,
                    "reference": o.razorpay_order_id,
                    "created_at": o.created_at.isoformat(),
                }
                for o in Order.objects.filter(user=user).select_related("course")
            ],
            "sessions": [
                {
                    "device": s.label,
                    "ip_address": s.ip_address,
                    "created_at": s.created_at.isoformat(),
                    "last_active_at": s.last_active_at.isoformat(),
                    "revoked_at": s.revoked_at.isoformat() if s.revoked_at else None,
                }
                for s in user.sessions.all()
            ],
        }

        body = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        stamp = timezone.localdate().isoformat()
        response = HttpResponse(body, content_type="application/json")
        response["Content-Disposition"] = (
            f'attachment; filename="shikshacom-data-{stamp}.json"'
        )
        return response


class DeleteAccountView(APIView):
    """Close the account. Password-confirmed, and NOT an immediate hard delete.

    What happens synchronously: the account and every profile under it are
    deactivated, all sessions are revoked, and an AccountDeletionRequest is
    written. The user is signed out and can no longer log in — from their side
    it is gone.

    What happens later: a purge job hard-deletes after
    AccountDeletionRequest.GRACE_DAYS. That deferral is deliberate — see the
    model docstring — and means support can reverse a hostile or mistaken
    deletion inside the window.

    Refuses while paid access is still live, matching ProfileDetailView.delete's
    existing rule: closing an account someone has paid for should be a support
    conversation, not a silent forfeit.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from enrollments.models import Subscription

        user = request.user
        verify_account_password(request)

        live_paid = Subscription.objects.filter(
            user=user, status="ACTIVE", expires_at__gt=timezone.now()
        ).exists()
        if live_paid:
            raise ValidationError({
                "detail": (
                    "You still have active paid course access. Contact support "
                    "to close this account so your payment can be settled."
                ),
                "code": "active_subscription",
            })

        request_row = AccountDeletionRequest.objects.create(
            user=user,
            email=user.email,
            reason=(request.data.get("reason") or "")[:300],
            purge_after=timezone.now() + timedelta(
                days=AccountDeletionRequest.GRACE_DAYS),
        )

        user.learner_profiles.update(is_active=False)
        user.user_roles.update(is_active=False)
        user.is_active = False
        user.save(update_fields=["is_active"])

        # Kill every session, including this one — the caller is signing out by
        # definition, so there is nothing to keep alive.
        revoke_sessions(
            user, list(user.sessions.filter(revoked_at__isnull=True))
        )

        from django.conf import settings as dj_settings
        response = Response({
            "detail": "Your account is closed.",
            "purge_after": request_row.purge_after,
            "grace_days": AccountDeletionRequest.GRACE_DAYS,
        })
        response.delete_cookie("access", domain=dj_settings.COOKIE_DOMAIN)
        response.delete_cookie("refresh", domain=dj_settings.COOKIE_DOMAIN)
        return response


# ─────────────────────────────────────────────────────────────────────────────
# Choice lists
# ─────────────────────────────────────────────────────────────────────────────

class SettingsChoicesView(APIView):
    """The choice lists the Settings forms render.

    Served rather than duplicated in JS so the dropdowns can never drift from
    what the serializers will actually accept — the previous SettingsModal kept
    its own hardcoded CLASS_OPTS / BOARD_OPTS copies, which is exactly the drift
    this prevents.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        def pairs(choices):
            return [{"value": v, "label": l} for v, l in choices]

        return Response({
            "gender": pairs(LearnerProfile.GENDER_CHOICES),
            "currently_studying": pairs(LearnerProfile.CURRENTLY_STUDYING_CHOICES),
            "current_class": pairs(LearnerProfile.CLASS_CHOICES),
            "stream": pairs(LearnerProfile.STREAM_CHOICES),
            "board": pairs(LearnerProfile.BOARD_CHOICES),
            "highest_education": pairs(LearnerProfile.HIGHEST_EDUCATION_CHOICES),
            "relationship": pairs(LearnerProfile.RELATIONSHIP_CHOICES),
        })
