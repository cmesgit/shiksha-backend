# skills/notifications.py
#
# SkillSession lifecycle notifications — mirrors sessions_app.views's
# _push_session_bell, reusing the SAME generic bell infrastructure (which
# turns out to have zero livestream-specific coupling despite living in that
# module) rather than building a parallel one. Every SkillSession transition
# was previously silent (no Activity row, no WS push) — this is the fix.
from django.contrib.contenttypes.models import ContentType

from .models import SkillSession

# One entry per event: (recipient_fn, title_fn). recipient_fn(session) -> User
_SKILL_NOTIFICATIONS = {
    "requested": (
        lambda s: s.expert.user,
        lambda s: f"🔔 {s.learner_profile.display_name} requested a session with you",
    ),
    "confirmed": (
        lambda s: s.learner_profile.account,
        lambda s: f"✅ {s.expert.display_name()} confirmed your session",
    ),
    "declined": (
        lambda s: s.learner_profile.account,
        lambda s: "❌ Your session request was declined",
    ),
    "cancelled": (
        lambda s: s.expert.user,
        lambda s: f"❌ {s.learner_profile.display_name} cancelled the session",
    ),
    "completed": (
        lambda s: s.learner_profile.account,
        lambda s: f"✔ Your session with {s.expert.display_name()} has ended",
    ),
    "reschedule_proposed": (
        lambda s: s.learner_profile.account,
        lambda s: f"📅 {s.expert.display_name()} proposed a new time for your session",
    ),
    "reschedule_confirmed": (
        lambda s: s.expert.user,
        lambda s: f"✅ {s.learner_profile.display_name} confirmed the new time",
    ),
    "reschedule_declined": (
        lambda s: s.expert.user,
        lambda s: f"❌ {s.learner_profile.display_name} declined the new time",
    ),
    "paid": (
        lambda s: s.learner_profile.account,
        lambda s: f"💰 {s.expert.display_name()} marked your session as paid",
    ),
}


# ── Durable notifications (bell row + email + push) ───────────────────────
#
# The Activity+WS layer above is live-UI only: the frame is dropped on the
# floor if the recipient doesn't happen to have a socket open, so a learner
# whose session got confirmed overnight was never told at all. notify()
# writes the one persistent Notification row every away-from-app channel
# reads from — the same path counseling/ and sessions_app/ already use.
#
# Kept as a SECOND table rather than folded into _SKILL_NOTIFICATIONS above
# because the two answer different questions: that one is "what does the
# live bell render", this one is "what verb is this, and what does it read
# like in an inbox". Titles here are deliberately plain — they become email
# subject lines, where the feed's emoji prefixes look like spam.

# Deep links are absolute app paths, NOT reassembled client-side from a
# flag: the bell previously routed skill sessions off an `is_skill_session`
# boolean, and every consumer that missed the flag deep-linked to the wrong
# page. The learner and expert links point at two DIFFERENT apps.
_LEARNER_LINK = "/skill-dev/sessions/{id}"   # shiksha-student-dashboard
# The teacher app has no per-session detail route (TeacherRoutes.jsx exposes
# `expert/bookings` as a queue, no `:id` child), so the expert lands on the
# queue — same shape as counseling's "/counselor/appointments".
_EXPERT_LINK = "/teacher/expert/bookings"


def _session_label(session):
    """What the session is *about*, for a body line an expert or learner can
    recognise without opening the app."""
    if session.listing_id and session.listing:
        return session.listing.title
    return session.expert.headline


def _when(session):
    from django.utils import timezone
    if not session.scheduled_for:
        return ""
    return timezone.localtime(session.scheduled_for).strftime("%I:%M %p, %d %b")


def _proposed_when(session):
    from django.utils import timezone
    if not session.proposed_scheduled_for:
        return ""
    return timezone.localtime(session.proposed_scheduled_for).strftime("%I:%M %p, %d %b")


def _on(when):
    return f" on {when}" if when else ""


# event -> (verb, title_fn, body_fn). The recipient is NOT repeated here —
# it is whatever _SKILL_NOTIFICATIONS above already resolved, so the two
# layers can never disagree about who an event is for.
_NOTIFY_SPEC = {
    "requested": (
        "skill.requested",
        lambda s: f"New session request: {s.learner_profile.display_name}",
        lambda s: (f"{s.learner_profile.display_name} requested a "
                   f"{_session_label(s)} session{_on(_when(s))}. "
                   f"Requests expire after 24 hours."),
    ),
    "confirmed": (
        "skill.confirmed",
        lambda s: "Session confirmed",
        lambda s: (f"{s.expert.display_name()} confirmed your "
                   f"{_session_label(s)} session{_on(_when(s))}."),
    ),
    "declined": (
        "skill.declined",
        lambda s: "Session request declined",
        lambda s: (f"{s.expert.display_name()} isn't able to take your "
                   f"{_session_label(s)} session request. "
                   f"You can book another time or another expert."),
    ),
    "cancelled": (
        "skill.cancelled",
        lambda s: f"Session cancelled: {s.learner_profile.display_name}",
        lambda s: (f"{s.learner_profile.display_name} cancelled the "
                   f"{_session_label(s)} session{_on(_when(s))}. "
                   f"The slot is open on your grid again."),
    ),
    "completed": (
        "skill.completed",
        lambda s: "Session completed",
        lambda s: (f"Your {_session_label(s)} session with "
                   f"{s.expert.display_name()} has ended. "
                   f"Leave a review to help other learners."),
    ),
    "reschedule_proposed": (
        "skill.reschedule_proposed",
        lambda s: "New time proposed for your session",
        lambda s: (f"{s.expert.display_name()} proposed "
                   f"{_proposed_when(s) or 'a new time'} for your "
                   f"{_session_label(s)} session. Confirm it, or keep the "
                   f"original time.")
                  + (f' Reason: "{s.reschedule_reason}"'
                     if s.reschedule_reason else ""),
    ),
    "reschedule_confirmed": (
        "skill.reschedule_confirmed",
        lambda s: f"New time accepted: {s.learner_profile.display_name}",
        lambda s: (f"{s.learner_profile.display_name} accepted the new time"
                   f"{_on(_when(s))}."),
    ),
    "reschedule_declined": (
        "skill.reschedule_declined",
        lambda s: f"New time declined: {s.learner_profile.display_name}",
        lambda s: (f"{s.learner_profile.display_name} kept the original time"
                   f"{_on(_when(s))} instead of the one you proposed."),
    ),
    "paid": (
        "skill.paid",
        lambda s: "Payment recorded",
        lambda s: (f"{s.expert.display_name()} confirmed they received "
                   f"payment for your {_session_label(s)} session."),
    ),
}


def _sms_vars(session, for_learner):
    """DLT template variables for the booking_* templates. `title` is capped
    at 30 chars because the registered template bodies are fixed-length."""
    when = _proposed_when(session) or _when(session)
    if not when:
        return None      # unscheduled: the template can't render, skip SMS
    other = (session.expert.display_name() if for_learner
             else session.learner_profile.display_name)
    return {"title": f"with {other}"[:30], "when": when}


def _emit_notification(session, event, recipient, is_for_learner, actor):
    """Write the durable Notification for a skill event. Additive — the
    Activity row and WS frame above are unchanged and still drive live UI."""
    spec = _NOTIFY_SPEC.get(event)
    if not spec:
        return
    verb, title_fn, body_fn = spec

    from notifications.services import notify

    if is_for_learner:
        link_url = _LEARNER_LINK.format(id=session.id)
        identity = f"L:{session.learner_profile_id}"
        # Route SMS to THIS child's guardian number, not the account holder's.
        learner_profile = session.learner_profile
    else:
        link_url = _EXPERT_LINK
        identity = f"T:{session.expert.teacher_profile_id}"
        learner_profile = None

    notify(
        recipient=recipient,
        actor=actor,
        verb=verb,
        title=title_fn(session),
        body=body_fn(session),
        link_url=link_url,
        payload={"session_id": str(session.id),
                 "expert_id": str(session.expert_id)},
        audience_identity=identity,
        sms_vars=_sms_vars(session, is_for_learner),
        learner_profile=learner_profile,
    )


def push_skill_bell(session, event, actor=None):
    """Create an Activity + push a WS bell for a SkillSession transition.
    Never lets a notification failure break the calling request.

    `actor` is the User who performed the transition, when there is one —
    passed straight to notify(), whose self-notify guard is what stops a
    teacher who cancels from being told about their own cancellation. Left
    None by the Celery auto-decline sweep, which has no human actor.
    """
    try:
        from activity.models import Activity
        from livestream.services.notifications import push_ws_notification

        rule = _SKILL_NOTIFICATIONS.get(event)
        if not rule:
            return
        recipient_fn, title_fn = rule
        recipient = recipient_fn(session)
        title = title_fn(session)
        if not recipient or not title:
            return

        # The feed is strictly scoped per activity/views.py's _scoped_qs:
        # LEARNER-audience rows need learner_profile = the active profile (or
        # NULL); TEACHER-audience rows need learner_profile IS NULL. This rule
        # applies to EVERY event here, not just the learner-facing ones — an
        # expert-recipient row (requested/cancelled/reschedule_*) must be
        # audience=TEACHER with learner_profile=None, or it's invisible to the
        # expert's own feed regardless of which User it's attached to.
        is_for_learner = recipient == session.learner_profile.account
        audience = Activity.AUDIENCE_LEARNER if is_for_learner else Activity.AUDIENCE_TEACHER
        row_learner_profile = session.learner_profile if is_for_learner else None

        content_type = ContentType.objects.get_for_model(SkillSession)
        activity, created = Activity.objects.get_or_create(
            user=recipient,
            type=Activity.TYPE_SESSION,
            content_type=content_type,
            object_id=session.id,
            title=title,
            defaults={
                # subject_id doubles as the deep-link target the bell routes
                # on (see NotificationBell's is_skill_session branch) — the
                # SkillSession id, so "confirmed your session" is clickable
                # straight through to that session instead of a dead item.
                "subject_id": session.id,
                "subject_name": session.expert.headline,
                "due_date": session.scheduled_for,
                "audience": audience,
                "learner_profile": row_learner_profile,
            },
        )
        if created:
            # Inside the `created` guard on purpose: get_or_create is already
            # this layer's "is this a NEW real event" ledger, and reusing it
            # means a retried/double-submitted transition can't spend a second
            # email or SMS. Every transition that can legitimately repeat
            # produces a different Activity key (a new session id, or a
            # different title), so nothing real is swallowed.
            _emit_notification(session, event, recipient, is_for_learner, actor)
            push_ws_notification(recipient.id, {
                "type": "SESSION",
                "title": title,
                "subject_name": session.expert.headline,
                "id": str(activity.id),
                "subject_id": str(session.id),
                "is_read": False,
                "created_at": activity.created_at.isoformat(),
                "is_skill_session": True,
            })
    except Exception:
        pass
