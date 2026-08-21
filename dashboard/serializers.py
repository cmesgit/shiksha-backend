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
from courses.board_display import board_name_for, board_name_via
from livestream.models import LiveSession
from sessions_app.models import PrivateSession
from django.utils import timezone


class DashboardSessionSerializer(serializers.ModelSerializer):
    subject       = serializers.SerializerMethodField()
    subject_id    = serializers.SerializerMethodField()
    topic         = serializers.CharField(source="title")
    teacher       = serializers.SerializerMethodField()
    dateTime      = serializers.DateTimeField(source="start_time")

    # The teacher dashboard's own "Today's Sessions" row needs a second line
    # of context that isn't "your own email" (what `teacher` resolves to for
    # a teacher looking at their own session) — the session's batch (falling
    # back to the course when it's a course-wide/unscoped session) plus how
    # many students are in it. Mirrors livestream/serializers.py's
    # LiveSessionSerializer, which already exposes both for the dedicated
    # Live Sessions page.
    batch_name           = serializers.SerializerMethodField()
    batch_student_count  = serializers.SerializerMethodField()

    course_title         = serializers.SerializerMethodField()
    board_name           = serializers.SerializerMethodField()

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
            "batch_name", "batch_student_count",
            "course_title", "board_name",
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
        # Show the host's real name (e.g. "Kavita Iyer") on the dashboard's
        # "Next class" hero + "Upcoming Live Sessions" cards, not the raw
        # login email — same bug/fix as livestream.serializers
        # .LiveSessionListSerializer.get_teacher, reusing the same name
        # resolution the chat directory already gets right (prefer the
        # teacher's SELF learner-profile name, then username/email).
        try:
            if not obj.created_by_id:
                return ""
            tp = getattr(obj.created_by, "teacher_profile", None)
            if tp:
                from chat.services import teacher_display_name
                return teacher_display_name(tp)
            return obj.created_by.get_full_name() or obj.created_by.email
        except Exception:
            return ""

    def get_batch_name(self, obj):
        try:
            if obj.batch_id:
                return obj.batch.name
            if not obj.course_id:
                return ""
            # Course-wide session: the bare title is the ambiguous string
            # ("Class 9" exists under two boards), and this is rendered as the
            # card's only context line, so the board goes inline here.
            title = obj.course.title
            board = board_name_for(obj.course)
            return f"{title} · {board}" if board else title
        except Exception:
            return ""

    def get_course_title(self, obj):
        try:
            return obj.course.title if obj.course_id else ""
        except Exception:
            return ""

    def get_board_name(self, obj):
        return board_name_via(obj, "course")

    def get_batch_student_count(self, obj):
        if not obj.batch_id:
            return None
        try:
            from enrollments.models import Enrollment
            return Enrollment.objects.filter(
                batch_id=obj.batch_id, status=Enrollment.STATUS_ACTIVE
            ).count()
        except Exception:
            return None

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
    course_title = serializers.SerializerMethodField()
    board_name   = serializers.SerializerMethodField()

    # ── Fields mobile index.tsx needs ───────────────────────────────────────
    # `status` is a CONSTANT, and that is now correct rather than accidental.
    #
    # The old comment here claimed "Assignment.status exists on your model
    # (pending / submitted / graded)". It does not — Assignment has no status
    # field and no status property, so the attribute lookup always failed and
    # DRF silently fell back to this default. Every assignment reported
    # "pending" to every learner forever, including ones they had submitted.
    #
    # The queryset is now the source of truth instead: _learner_assignments
    # excludes anything this learner profile has already submitted, so
    # everything serialized here genuinely IS outstanding. Do not "fix" this
    # into a SerializerMethodField reading obj.status — derive it from
    # AssignmentSubmission (see assignments/serializers.py's
    # AssignmentListSerializer.get_status) if a real per-row status is ever
    # needed here.
    # priority is optional — falls back to "low" if field absent.
    status   = serializers.CharField(default="pending")
    priority = serializers.SerializerMethodField()

    class Meta:
        model  = Assignment
        fields = [
            "id", "title", "teacher", "due",
            "subject_id", "subject_name",
            "course_title", "board_name",
            "status", "priority",          # ← added
        ]

    def get_course_title(self, obj):
        try:
            course = obj.chapter.subject.course if obj.chapter_id else None
            return course.title if course else ""
        except Exception:
            return ""

    def get_board_name(self, obj):
        return board_name_via(obj, "chapter", "subject", "course")

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
            teacher = subject.teaching_assignments.filter(
                batch__isnull=True, is_active=True,
            ).first()
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
    subject_id   = serializers.SerializerMethodField()
    subject_name = serializers.SerializerMethodField()
    course_title = serializers.SerializerMethodField()
    board_name   = serializers.SerializerMethodField()

    class Meta:
        model  = Quiz
        fields = [
            "id", "title", "teacher", "subject_id", "subject_name",
            "course_title", "board_name",
        ]

    def get_course_title(self, obj):
        try:
            course = obj.subject.course if obj.subject_id else None
            return course.title if course else ""
        except Exception:
            return ""

    def get_board_name(self, obj):
        return board_name_via(obj, "subject", "course")

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

    # ── Board disambiguation ────────────────────────────────────────────────
    course_title = serializers.SerializerMethodField()
    board_name   = serializers.SerializerMethodField()

    class Meta:
        model  = Activity
        fields = [
            "id", "type", "raw_type", "title", "message",  # message = title alias
            "due_date", "created_at",
            "subject_id", "subject_name",
            "subject",                              # plain string for inbox
            "course_title", "board_name",
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

    def _course_map(self):
        """{subject_id: (course_title, board_name)} for the whole page, 1 query.

        `Activity.subject_id` is a bare UUIDField with no FK (see
        activity/models.py) so there is no relation for select_related to
        follow. Resolving per row would be one query per notification, so the
        page's distinct subject ids are looked up once and cached here.
        """
        cached = getattr(self, "_course_map_cache", None)
        if cached is not None:
            return cached

        root = self
        while root.parent is not None:
            root = root.parent
        rows = root.instance
        if rows is None:
            rows = []
        elif not hasattr(rows, "__iter__"):
            rows = [rows]

        ids = {r.subject_id for r in rows if getattr(r, "subject_id", None)}
        mapping = {}
        if ids:
            from courses.models import Subject
            for s in Subject.objects.filter(id__in=ids).select_related(
                "course__board"
            ):
                course = s.course if s.course_id else None
                mapping[s.id] = (
                    course.title if course else "",
                    board_name_for(course),
                )
        self._course_map_cache = mapping
        return mapping

    def get_course_title(self, obj):
        if not obj.subject_id:
            return ""
        return self._course_map().get(obj.subject_id, ("", None))[0]

    def get_board_name(self, obj):
        if not obj.subject_id:
            return None
        return self._course_map().get(obj.subject_id, ("", None))[1]


class DashboardGradingItemSerializer(serializers.Serializer):
    """
    A single item in the teacher's grading queue: an assignment submission
    awaiting review. Real rows (assignments track no graded flag, so the
    queue surfaces actual submissions on the teacher's assignments, newest
    first) — the "Grade" button deep-links to the submissions view.
    """
    id            = serializers.SerializerMethodField()
    student       = serializers.SerializerMethodField()
    title         = serializers.SerializerMethodField()
    subject       = serializers.SerializerMethodField()
    subject_id    = serializers.SerializerMethodField()
    course_title  = serializers.SerializerMethodField()
    board_name    = serializers.SerializerMethodField()
    assignment_id = serializers.SerializerMethodField()
    submitted_at  = serializers.DateTimeField()

    def get_course_title(self, obj):
        try:
            return obj.assignment.chapter.subject.course.title
        except Exception:
            return ""

    def get_board_name(self, obj):
        return board_name_via(obj, "assignment", "chapter", "subject", "course")

    def get_id(self, obj):
        return str(obj.id)

    def get_student(self, obj):
        u = getattr(obj, "student", None)
        if not u:
            return "Student"
        full = (u.get_full_name() or "").strip() if hasattr(u, "get_full_name") else ""
        if full:
            return full
        if getattr(u, "username", ""):
            return u.username
        email = getattr(u, "email", "") or ""
        return email.split("@")[0] if email else "Student"

    def get_title(self, obj):
        try:
            return obj.assignment.title
        except Exception:
            return ""

    def get_subject(self, obj):
        try:
            return obj.assignment.chapter.subject.name
        except Exception:
            return ""

    def get_subject_id(self, obj):
        try:
            return str(obj.assignment.chapter.subject_id)
        except Exception:
            return None

    def get_assignment_id(self, obj):
        try:
            return str(obj.assignment_id)
        except Exception:
            return None
