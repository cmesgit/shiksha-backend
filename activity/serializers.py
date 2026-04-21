# ============================================================
# BACKEND — activity/serializers.py  (FULL REPLACEMENT)
# ============================================================

from rest_framework import serializers
from .models import Activity


class ActivitySerializer(serializers.ModelSerializer):

    class Meta:
        model = Activity
        fields = [
            "id",
            "type",
            "title",
            "due_date",
            "is_read",
            "created_at",
            # These were missing — required by both NotificationBell and ActivityItem
            "subject_id",
            "subject_name",
            "object_id",
        ]
