"""
dashboard/serializers.py  — patched for mobile compatibility

Changes vs original:
  DashboardSessionSerializer   → adds live, status, start_time, end_time, teacher_left_at, color
  DashboardAssignmentSerializer → adds status, priority fields
  DashboardPrivateSessionSerializer → already fine, no changes
  DashboardActivitySerializer  → adds unread, subject (plain string), message; lowercases type
"""

from activity.models import Activity
from quizzes.models import Quiz
from assignments.models import Assignment
from rest_framework import serializers
from livestream.models import LiveSession
from sessions_app.models import PrivateSession
from django.utils import timezone


class DashboardSessionSerializer(serializers.ModelSerializer):
    subject       = serializers.SerializerMethodField()
    subject_id    = serializers.SerializerMethodField()
    topic         = serializers.CharField(source="title")
    teacher       = serializers.SerializerMethodField()
    dateTime      = serializers.DateTimeField(source="start_time")

    # ── Fields mobile index.tsx needs for live.tsx + calEvents ──────────────
    live          = serializers.SerializerMethodField()
    start_time    = serializers.DateTimeField()
    end_time      = serializers.DateTimeField()
    teacher_left_at = serializers.DateTimeField(allow_null=True)
    color         = serializers.SerializerMethodField()

    class Meta:
        model  = LiveSession
        fields = [
            "id", "subject", "subject_id", "topic", "teacher",
            "dateTime",           # web dashboard compat
            "start_time",         # mobile live.tsx compat
            "end_time",
            "status",
            "live",
            "teacher_left_at",
            "color",
        ]

    def get_subject(self, obj):
        try:
            return obj.subject.name if obj.subject_id else ""
        except Exception:
            return ""

    def get_subject_id(self, obj):
        try:
            return str(obj.subject_id) if obj.subject_id else None
        except Exception:
            return None

    def get_teacher(self, obj):
        try:
            return obj.created_by.email if obj.created_by_id else ""
        except Exception:
            return ""

    def get_live(self, obj):
        """True when the session is currently in progress."""
        try:
            now = timezone.now()
            if obj.status == LiveSession.STATUS_LIVE:
                return True
            if obj.start_time and obj.end_time:
                return obj.start_time <= now <= obj.end_time
        except Exception:
            pass
        return False

    def get_color(self, obj):
        # Reserved for per-subject colour — not used yet; mobile falls back
        # to colors.primary when None.
        return None


class DashboardAssignmentSerializer(serializers.ModelSerializer):
    teacher      = serializers.SerializerMethodField()
    due          = serializers.DateTimeField(source="due_date")
    subject_id   = serializers.SerializerMethodField()
    subject_name = serializers.SerializerMethodField()

    # ── Fields mobile index.tsx needs ───────────────────────────────────────
    # Assignment.status exists on your model (pending / submitted / graded).
    # priority is optional — falls back to "low" if field absent.
    status   = serializers.CharField(default="pending")
    priority = serializers.SerializerMethodField()

    class Meta:
        model  = Assignment
        fields = [
            "id", "title", "teacher", "due",
            "subject_id", "subject_name",
            "status", "priority",          # ← added
        ]

    def get_subject_id(self, obj):
        try:
            if obj.chapter_id and obj.chapter and obj.chapter.subject_id:
                return str(obj.chapter.subject.id)
            return None
        except Exception:
            return None

    def get_subject_name(self, obj):
        try:
            if obj.chapter_id and obj.chapter and obj.chapter.subject_id:
                return obj.chapter.subject.name
            return ""
        except Exception:
            return ""

    def get_teacher(self, obj):
        try:
            subject = obj.chapter.subject if obj.chapter_id else None
            if not subject:
                return "Unknown"
            teachers = getattr(subject, "prefetched_teachers", None)
            if teachers:
                t = teachers[0]
                if t and t.teacher_id:
                    return t.teacher.email
            teacher = subject.subject_teachers.first()
            if teacher and teacher.teacher_id:
                return teacher.teacher.email
        except Exception:
            pass
        return "Unknown"

    def get_priority(self, obj):
        # Return Assignment.priority if the field exists, otherwise "low"
        return getattr(obj, "priority", "low") or "low"


class DashboardQuizSerializer(serializers.ModelSerializer):
    teacher      = serializers.SerializerMethodField()
    due          = serializers.DateTimeField(source="due_date")
    subject_id   = serializers.SerializerMethodField()
    subject_name = serializers.SerializerMethodField()

    class Meta:
        model  = Quiz
        fields = ["id", "title", "teacher", "due", "subject_id", "subject_name"]

    def get_subject_id(self, obj):
        try:
            return str(obj.subject_id) if obj.subject_id else None
        except Exception:
            return None

    def get_subject_name(self, obj):
        try:
            return obj.subject.name if obj.subject_id else ""
        except Exception:
            return ""

    def get_teacher(self, obj):
        try:
            return obj.created_by.email if obj.created_by_id else ""
        except Exception:
            return ""


class DashboardPrivateSessionSerializer(serializers.ModelSerializer):
    student      = serializers.SerializerMethodField()
    teacher_name = serializers.SerializerMethodField()
    date         = serializers.DateField(source="scheduled_date")
    time         = serializers.TimeField(source="scheduled_time")

    class Meta:
        model  = PrivateSession
        fields = [
            "id", "subject", "student", "teacher_name", "date", "time",
            "duration_minutes", "status", "session_type",
        ]

    def get_student(self, obj):
        try:
            return obj.requested_by.email if obj.requested_by_id else ""
        except Exception:
            return ""

    def get_teacher_name(self, obj):
        try:
            return obj.teacher.email if obj.teacher_id else ""
        except Exception:
            return ""


class DashboardActivitySerializer(serializers.ModelSerializer):
    """
    Used by DashboardView for the notifications/schedule slices.

    Mobile inbox.tsx reads:
      n.unread        ← bool   (we expose is_read inverted)
      n.type          ← lowercase string matching TONE keys in inbox.tsx:
                        'recording' | 'material' | 'quiz' | 'session'
      n.title ?? n.message
      n.subject       ← plain subject name string
      n.created_at

    Activity.type DB values:  ASSIGNMENT / QUIZ / SESSION / SUBMISSION
    inbox.tsx FMAP values:    session / quiz / material / recording

    Mapping applied in get_type():
      SESSION    → 'session'
      QUIZ       → 'quiz'
      ASSIGNMENT → 'material'    (closest match in inbox TONE map)
      SUBMISSION → 'material'
    """

    subject_id   = serializers.UUIDField(read_only=True)
    subject_name = serializers.CharField(read_only=True)
    object_id    = serializers.UUIDField(read_only=True)

    # ── Mobile-compat additions ───────────────────────────────────────────────
    unread   = serializers.SerializerMethodField()
    type     = serializers.SerializerMethodField()   # overrides model field
    subject  = serializers.SerializerMethodField()   # plain string alias
    message  = serializers.CharField(source="title", read_only=True)

    # ── Web-compat addition ─────────────────────────────────────────────────
    # Canonical UPPERCASE value (ASSIGNMENT/QUIZ/SESSION/SUBMISSION). The web
    # dashboards' type filters and label/color maps compare against this
    # vocabulary — `type` above stays mobile-mapped lowercase so the mobile
    # app needs zero changes.
    raw_type = serializers.CharField(source="type", read_only=True)

    class Meta:
        model  = Activity
        fields = [
            "id", "type", "raw_type", "title", "message",  # message = title alias
            "due_date", "created_at",
            "subject_id", "subject_name",
            "subject",                              # plain string for inbox
            "object_id", "is_read", "unread",       # both forms
        ]

    # Activity.TYPE_* → inbox.tsx FMAP key
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
        # Plain string for inbox subtitle line
        return obj.subject_name or ""
