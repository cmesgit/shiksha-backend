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
    email=False,
    ws_extra=None,
):
    """Create + push one notification. Returns the Notification, or None.

    audience_identity (M2 — Phase 3 §18): an identity key ("L:<uuid>" /
    "T:<id>") restricting this notification to ONE identity on the account
    — the precise per-profile scope that fixes the child-A/child-B leak.
    Blank = account-wide (unchanged behaviour). When given, audience_role
    is auto-derived from it if the caller didn't pass one explicitly, so
    every existing consumer/filter keeps working; callers migrate to the
    precise field verb-by-verb without a flag day.

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
    _push_ws(recipient.pk, data)

    if email:
        _send_email(recipient, subject=title, body=body or title)

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
