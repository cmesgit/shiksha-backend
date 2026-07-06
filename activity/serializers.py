"""
activity/serializers.py  ·  FULL REPLACEMENT
────────────────────────────────────────────
The old serializer overwrote `type` with the mobile inbox map
(ASSIGNMENT→'material', SESSION→'session', …). The web dashboards,
NotificationCard, ActivityItem and both DropdownMenus all compare
against the UPPERCASE DB values — so every type filter on web has
been matching nothing, and label/color maps fell through to defaults.

Fix without breaking the mobile app:

  type       ← unchanged mobile-mapped lowercase (mobile keeps working
               with zero changes)
  raw_type   ← NEW: canonical DB value (ASSIGNMENT/QUIZ/SESSION/SUBMISSION)
  audience   ← NEW: LEARNER | TEACHER   (lets clients sanity-filter)
  learner_profile_id ← NEW: which profile the row targets (or null)

The rewritten web hook normalizes on `raw_type ?? map(type)`, so both
old rows (cached responses) and new rows render correctly.
"""

from rest_framework import serializers

from .models import Activity


class ActivitySerializer(serializers.ModelSerializer):
    """Returned by GET /activity/feed/ — see module docstring."""

    # ── Mobile-compat (unchanged) ─────────────────────────────────────
    unread  = serializers.SerializerMethodField()
    type    = serializers.SerializerMethodField()   # lowercase mobile map
    subject = serializers.SerializerMethodField()
    message = serializers.CharField(source="title", read_only=True)

    # ── Canonical additions ───────────────────────────────────────────
    raw_type = serializers.CharField(source="type", read_only=True)
    audience = serializers.CharField(read_only=True)
    learner_profile_id = serializers.SerializerMethodField()

    class Meta:
        model = Activity
        fields = [
            "id",
            "type",            # mobile map (legacy consumers)
            "raw_type",        # canonical UPPERCASE (web consumers)
            "audience",
            "learner_profile_id",
            "title",
            "message",
            "due_date",
            "is_read",
            "unread",
            "created_at",
            "subject_id",
            "subject_name",
            "subject",
            "object_id",
        ]

    _TYPE_MAP = {
        Activity.TYPE_SESSION:    "session",
        Activity.TYPE_QUIZ:       "quiz",
        Activity.TYPE_ASSIGNMENT: "material",
        Activity.TYPE_SUBMISSION: "material",
    }

    def get_unread(self, obj):
        return not obj.is_read

    def get_type(self, obj):
        return self._TYPE_MAP.get(obj.type, obj.type.lower())

    def get_subject(self, obj):
        return obj.subject_name or ""

    def get_learner_profile_id(self, obj):
        return str(obj.learner_profile_id) if obj.learner_profile_id else None
