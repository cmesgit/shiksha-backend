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
}


def push_skill_bell(session, event):
    """Create an Activity + push a WS bell for a SkillSession transition.
    Never lets a notification failure break the calling request."""
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

        content_type = ContentType.objects.get_for_model(SkillSession)
        activity, created = Activity.objects.get_or_create(
            user=recipient,
            type=Activity.TYPE_SESSION,
            content_type=content_type,
            object_id=session.id,
            title=title,
            defaults={
                "subject_name": session.expert.headline,
                "due_date": session.scheduled_for,
                "learner_profile": session.learner_profile,
            },
        )
        if created:
            push_ws_notification(recipient.id, {
                "type": "SESSION",
                "title": title,
                "subject_name": session.expert.headline,
                "id": str(session.id),
                "is_read": False,
                "created_at": activity.created_at.isoformat(),
                "is_skill_session": True,
            })
    except Exception:
        pass
