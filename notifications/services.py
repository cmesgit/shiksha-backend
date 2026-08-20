# PLACEMENT: backend/backend/notifications/services.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/services.py
#
# The single entry point every app uses to notify a user:
#
#     from notifications.services import notify
#
#     notify(
#         recipient=post.author,
#         verb="forum.reply",
#         title=f'{request.user.username} replied to "{post.title}"',
#         actor=request.user,
#         link_url=f"/forum/thread/{post.id}",
#         payload={"thread_id": post.id},
#     )
#
# What it does, in order:
#   1. Persists a Notification row (survives the user being offline —
#      the WS-only pushes in materials/livestream/sessions_app do not).
#   2. Pushes real-time over the EXISTING per-user bus: it reuses
#      livestream.services.notifications.push_ws_notification, which
#      targets group user_updates_<id> (ws/updates/, UserUpdateConsumer)
#      via Celery with a synchronous fallback. No new transport code.
#   3. Optionally sends an email (email=True) through the same Gmail
#      helper the account flows use. Reserved for events that matter
#      when you're away: appointment confirmations, reports uploaded.
#
# Never raises: a notification failure must never 500 the request that
# triggered it. Worst case the row exists and the push/email is lost.

import logging

logger = logging.getLogger(__name__)


def _push_ws(user_id, data):
    """Real-time push over the canonical user_updates_<id> group.

    Reuses the existing livestream helper (Celery + sync fallback).
    Lazy import: keeps app-registry load order and test setups happy.
    """
    try:
        from livestream.services.notifications import push_ws_notification
        push_ws_notification(user_id, data)
    except Exception:
        logger.exception("notifications: WS push failed for user %s", user_id)


def _send_email(recipient, subject, body):
    try:
        from accounts.email_utils import send_gmail
        if recipient.email:
            send_gmail(recipient.email, subject, body)
    except Exception:
        logger.exception("notifications: email failed for user %s", recipient.pk)


# ── Multi-channel plumbing (policy-routed email / SMS / push) ───────────
#
# Channel decision = policy level (policy.py) × user preference:
#   REQUIRED  → always send (transactional; prefs ignored)
#   OPT_OUT   → send unless the user disabled the channel or muted the
#               verb's category in NotificationPreference
#   OFF       → never
# All dispatch is Celery-first with a synchronous fallback, mirroring the
# WS push above — a dead broker degrades to slower requests, not lost
# confirmations.

def _safe_dispatch(channel, verb, fn, /, *args, **kwargs):
    """Send on one channel; never let its failure reach the other two.

    A notification is not transactional across channels — "the email bounced"
    is no reason to also withhold the SMS. Failures are logged per channel so
    a dead provider is greppable by name instead of hiding behind a generic
    dispatch error.

    The first three parameters are positional-only: `_dispatch_sms` takes its
    own `verb=` keyword, which would otherwise collide with this wrapper's.
    """
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("notifications: %s dispatch failed (verb=%s)",
                         channel, verb)
        return None


def _channel_wanted(level, enabled, category, muted):
    from . import policy as _p
    if level == _p.REQUIRED:
        return True
    if level == _p.OPT_OUT:
        return bool(enabled) and category not in (muted or [])
    return False


def _prefs_for(user):
    """(email_enabled, sms_enabled, push_enabled, muted_categories) —
    defaults if the row doesn't exist or the table isn't migrated yet."""
    try:
        from .models import NotificationPreference
        p = NotificationPreference.objects.filter(user=user).first()
        if p is None:
            return True, True, True, []
        return p.email_enabled, p.sms_enabled, p.push_enabled, list(p.muted_categories or [])
    except Exception:
        return True, True, True, []


def _dispatch_email(recipient, subject, body):
    try:
        from .tasks import deliver_email_task
        deliver_email_task.delay(recipient.email, subject, body)
    except Exception:
        _send_email(recipient, subject, body)


def _dispatch_sms(recipient, template_key, sms_vars, verb, sms_to,
                  learner_profile):
    from .phone import phone_for_user
    to, source = (sms_to, "explicit") if sms_to else \
        phone_for_user(recipient, learner_profile=learner_profile)
    try:
        from .tasks import deliver_sms_task
        deliver_sms_task.delay(str(recipient.pk), to, template_key,
                               sms_vars or {}, verb, source or "")
    except Exception:
        from .sms import send_sms
        send_sms(to=to, template_key=template_key, variables=sms_vars or {},
                 verb=verb, user=recipient, phone_source=source or "")


def _dispatch_push(recipient, title, body, link_url, payload):
    try:
        from .push import push_to_users
        data = {"route": link_url} if link_url else {}
        if payload:
            data.update({k: str(v) for k, v in payload.items()})
        push_to_users(recipient.pk, title, body, data)
    except Exception:
        logger.exception("notifications: push failed for user %s", recipient.pk)


def _role_from_identity_key(identity_key):
    """M2: derive the coarse audience_role from a precise identity key, so a
    caller that passes only audience_identity still populates audience_role
    for any consumer (or dashboard filter) not yet reading the identity
    field. "L:..." -> STUDENT, "T:..." -> TEACHER, "C:..." -> COUNSELOR.
    Unknown/blank -> "" (account-wide). Kept as a plain prefix map rather
    than importing accounts.Identity, so notifications has no new
    cross-app import for a one-character lookup."""
    if not identity_key or ":" not in identity_key:
        return ""
    return {
        "L": "STUDENT",
        "T": "TEACHER",
        "C": "COUNSELOR",
    }.get(identity_key.split(":", 1)[0], "")


def _learner_profile_id_from_identity_key(identity_key):
    """Extract the learner profile id from an "L:<uuid>" key, else None.
    Fills the WS frame's learner_profile_id so UserUpdateConsumer._wanted()
    — which already drops learner events not matching the connection's
    active profile — gates per-child with no consumer change."""
    if identity_key and identity_key.startswith("L:"):
        return identity_key.split(":", 1)[1]
    return None


def notify(
    recipient,
    verb,
    title,
    body="",
    actor=None,
    link_url="",
    payload=None,
    audience_role="",
    audience_identity="",
    email=None,
    sms=None,
    push=None,
    sms_vars=None,
    sms_to=None,
    learner_profile=None,
    track=None,
    push_ws=True,
    ws_extra=None,
):
    """Create + push one notification. Returns the Notification, or None.

    Channels — the verb's row in notifications.policy decides email/SMS/
    push by default; the explicit tri-state flags override per call:
        email/sms/push = None   → policy decides (the normal case)
                         True   → force-send (back-compat: every existing
                                  ``email=True`` call keeps its old
                                  unconditional behaviour)
                         False  → force-suppress for this call
    sms_vars       — variables for the verb's DLT SMS template (see
                     settings.SMS_TEMPLATES). SMS is silently skipped
                     when the policy wants SMS but the template's
                     variables can't be rendered.
    sms_to         — explicit E.164 destination; otherwise resolved via
                     notifications.phone.phone_for_user(recipient,
                     learner_profile).
    learner_profile — the LearnerProfile this event is about, so a
                     dependent child's SMS reaches *that child's*
                     guardian number.

    audience_identity (M2 — Phase 3 §18): an identity key ("L:<uuid>" /
    "T:<id>") restricting this notification to ONE identity on the account
    — the precise per-profile scope that fixes the child-A/child-B leak.
    Blank = account-wide (unchanged behaviour). When given, audience_role
    is auto-derived from it if the caller didn't pass one explicitly, so
    every existing consumer/filter keeps working; callers migrate to the
    precise field verb-by-verb without a flag day.

    track: "academy" / "skill", or None to derive it from the verb via
    notifications.tracks.track_for_verb() — which is what every caller
    should do unless the same verb can legitimately belong to either track.
    Blank/NEUTRAL means cross-track and shows in BOTH bells. This is a
    SEPARATE axis from audience_identity: identity picks which profile, and
    cannot pick a track at all, because one TeacherProfile/LearnerProfile
    spans both (see chat/models.py's identity contract).

    push_ws: set False when the CALLER already pushes its own bell frame for
    this same event. Both frames arrive as {"type": "notification"} on the
    same socket but carry different ids — the caller's Activity UUID vs this
    row's integer pk — so the bell's id-based dedupe can't collapse them and
    the user sees the event TWICE. The caller's frame is the one to keep: it
    carries the type/subject_id/is_*_session keys the bell's click handler
    routes on, which this generic frame does not.

    ws_extra: extra keys merged into the websocket frame's `data` dict.
    Used by the forum to keep emitting the legacy keys
    (type/notification_type/message/thread_id) the current NotificationBell
    components already understand — remove once the bells consume the new
    shape.
    """
    from .models import Notification  # lazy: services importable pre-migrate

    if recipient is None:
        return None
    # Never self-notify.
    if actor is not None and getattr(actor, "pk", None) == recipient.pk:
        return None

    # M2: if the caller gave a precise identity but no explicit role, derive
    # the coarse role so nothing reading only audience_role regresses.
    if audience_identity and not audience_role:
        audience_role = _role_from_identity_key(audience_identity)

    # Track: explicit caller value wins, otherwise derive from the verb.
    # `normalize` also guards against a caller passing junk or a stale
    # spelling — an unrecognised value degrades to NEUTRAL (both bells)
    # rather than to a track that would hide the row from everyone.
    from . import tracks as _tracks
    resolved_track = (_tracks.normalize(track) if track is not None
                      else _tracks.track_for_verb(verb))

    try:
        notification = Notification.objects.create(
            recipient=recipient,
            actor=actor,
            verb=verb,
            title=title[:255],
            body=body,
            link_url=link_url,
            payload=payload or {},
            audience_role=audience_role,
            audience_identity=audience_identity,
            track=resolved_track,
        )
    except Exception:
        logger.exception("notifications: row insert failed (verb=%s)", verb)
        return None

    data = {
        "id": notification.id,
        "verb": verb,
        "title": notification.title,
        "body": body,
        "link_url": link_url,
        "payload": notification.payload,
        "audience_role": audience_role,
        "audience_identity": audience_identity,
        # Live pushes carry the track too, so a bell that scoped its REST
        # seed can apply the SAME scope to sockets instead of letting the
        # other track's rows leak in through the realtime path.
        "track": resolved_track,
        "created_at": notification.created_at.isoformat(),
    }
    # M2: map the identity onto the {audience, learner_profile_id} envelope
    # UserUpdateConsumer._wanted() already filters on — so a per-child
    # notification is dropped on the wrong child's socket with NO consumer
    # change. "audience" mirrors audience_role's STUDENT/TEACHER split; the
    # consumer expects the "TEACHER"/"LEARNER" spelling.
    if audience_identity:
        role = _role_from_identity_key(audience_identity)
        if role == "TEACHER":
            data["audience"] = "TEACHER"
        elif role in ("STUDENT", "COUNSELOR"):
            data["audience"] = "LEARNER" if role == "STUDENT" else role
        lp_id = _learner_profile_id_from_identity_key(audience_identity)
        if lp_id:
            data["learner_profile_id"] = lp_id
    if ws_extra:
        data.update(ws_extra)
    if push_ws:
        _push_ws(recipient.pk, data)

    # ── Away-from-app channels: policy × preferences × explicit flags ──
    try:
        from . import policy as _policy
        rules = _policy.for_verb(verb)
        pref_email, pref_sms, pref_push, muted = _prefs_for(recipient)
        category = rules["category"]

        want_email = email if email is not None else _channel_wanted(
            rules["email"], pref_email, category, muted)
        want_sms = sms if sms is not None else _channel_wanted(
            rules["sms"], pref_sms, category, muted)
        want_push = push if push is not None else _channel_wanted(
            rules["push"], pref_push, category, muted)

        # Each channel is isolated. These used to share one try/except, with
        # email dispatched first — so a single Resend hiccup (down, throttled,
        # bad address) aborted the whole block and the SMS and push for that
        # notification were never even attempted. That stayed hidden only
        # because the time-critical reminders had email=OFF and never entered
        # this path; the moment email was switched on for them, a transient
        # email failure would have silently taken the SMS down with it — on
        # precisely the messages that most need to arrive.
        if want_email and recipient.email:
            _safe_dispatch("email", verb, _dispatch_email, recipient,
                           subject=title, body=body or title)
        if want_sms:
            _safe_dispatch("sms", verb, _dispatch_sms, recipient,
                           template_key=rules.get("sms_template") or verb,
                           sms_vars=sms_vars, verb=verb, sms_to=sms_to,
                           learner_profile=learner_profile)
        if want_push:
            _safe_dispatch("push", verb, _dispatch_push, recipient, title,
                           body, link_url, notification.payload)
    except Exception:
        # Only reached if resolving policy/preferences above failed, i.e. no
        # channel could be decided at all — not a per-channel send error.
        logger.exception("notifications: channel routing failed (verb=%s)", verb)

    return notification


def notify_many(recipients, **kwargs):
    """Fan out one event to several users. De-dupes, skips the actor.

    Use for small, targeted sets (thread author + parent-comment author,
    all admins, one class's students). Do NOT use for "every user on the
    platform" blasts — that anti-pattern was already removed from thread
    creation once; keep it dead.
    """
    seen, results = set(), []
    for recipient in recipients:
        if recipient is None or recipient.pk in seen:
            continue
        seen.add(recipient.pk)
        n = notify(recipient=recipient, **kwargs)
        if n is not None:
            results.append(n)
    return results
