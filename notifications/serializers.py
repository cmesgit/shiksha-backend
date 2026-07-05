# PLACEMENT: backend/backend/notifications/serializers.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/serializers.py

from rest_framework import serializers

from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    """Canonical shape — what /api/notifications/ returns and what the
    redesigned forum bell (and every new dashboard bell) should consume."""

    actor_username = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = (
            "id",
            "verb",
            "title",
            "body",
            "link_url",
            "payload",
            "actor_username",
            "audience_role",
            "audience_identity",
            "is_read",
            "created_at",
        )

    def get_actor_username(self, obj):
        return obj.actor.username if obj.actor_id else None


class LegacyForumNotificationSerializer(serializers.ModelSerializer):
    """Byte-compatible with forum's old NotificationSerializer:
    (id, notification_type, message, thread_id, sender_username, is_read,
    created_at). Served on the old /api/forum/notifications/ routes so the
    three existing dashboards keep working with ZERO frontend edits.
    Delete this class once the bells are migrated to the canonical shape.
    """

    notification_type = serializers.SerializerMethodField()
    message = serializers.SerializerMethodField()
    thread_id = serializers.SerializerMethodField()
    sender_username = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = (
            "id",
            "notification_type",
            "message",
            "thread_id",
            "sender_username",
            "is_read",
            "created_at",
        )

    def get_notification_type(self, obj):
        # Rows copied from the old table keep their exact original type in
        # payload["legacy_type"]; rows created by the patched forum views
        # set it too. Fallback: derive from the verb.
        legacy = (obj.payload or {}).get("legacy_type")
        if legacy:
            return legacy
        return {
            "forum.reply": "new_reply",
            "forum.upvote": "upvote",
            "forum.thread": "new_thread",
        }.get(obj.verb, obj.verb)

    def get_message(self, obj):
        return obj.body or obj.title

    def get_thread_id(self, obj):
        return (obj.payload or {}).get("thread_id")

    def get_sender_username(self, obj):
        return obj.actor.username if obj.actor_id else ""
