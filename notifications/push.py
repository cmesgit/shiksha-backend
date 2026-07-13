# PLACEMENT: backend/backend/notifications/push.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/push.py
#
# Mobile patch 07's push layer, made real — with one difference: it is a
# SAFE NO-OP until `pip install fcm-django firebase-admin` + the Firebase
# service-account settings from the patch are applied. notify() calls
# this today; pushes silently start flowing the day FCM is configured,
# with zero further code changes.
#
# `data["route"]` is the tap deep-link (the Flutter router already has
# these): /student/live/<id>/room, /student/assignment/<id>,
# /student/chat/<conversation_id>, ...

import logging

logger = logging.getLogger(__name__)


def push_to_users(user_ids, title, body="", data=None):
    """Fire an FCM notification to one user id or a list. Never raises."""
    if not isinstance(user_ids, (list, tuple, set)):
        user_ids = [user_ids]
    try:
        from fcm_django.models import FCMDevice
        from firebase_admin.messaging import Message, Notification
    except ImportError:
        logger.debug("push: fcm_django not installed — skipping (%s)", title)
        return 0
    try:
        devices = FCMDevice.objects.filter(user_id__in=list(user_ids),
                                           active=True)
        if not devices.exists():
            return 0
        message = Message(
            notification=Notification(title=title[:100], body=(body or "")[:200]),
            data={k: str(v) for k, v in (data or {}).items()},
        )
        devices.send_message(message)
        return devices.count()
    except Exception:
        logger.exception("push: FCM send failed for users %s", user_ids)
        return 0
