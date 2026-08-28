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

    # The bell (NotificationBell.jsx) needs this to pick the right icon and
    # deep-link route for a skill-session event. Previously it only arrived
    # on the ephemeral WS push payload (skills/notifications.py), so any
    # notification the bell loaded via this REST feed instead (e.g. on page
    # load, or "See all") fell through to the generic SESSION handling —
    # wrong icon (🎥 instead of 📅) and wrong route (/live-sessions instead
    # of /skill-dev/sessions/:id). Computed the same way regardless of
    # delivery path, so both agree.
    is_skill_session = serializers.SerializerMethodField()

    # Which model `object_id` points at ("assignment", "quiz", "studymaterial",
    # …). See get_object_type for why the bell needs it.
    object_type = serializers.SerializerMethodField()

    # Which product track this row belongs to, using the same vocabulary as
    # notifications.tracks ("academy" / "skill"). The bell scopes itself to
    # the track the user is currently in, so a Skill Dev booking never
    # renders inside Academy chrome.
    #
    # Unlike notifications.Notification there is no NEUTRAL case here: every
    # Activity type (ASSIGNMENT/QUIZ/SESSION/SUBMISSION/MATERIAL) is bound to
    # a course or a session, so every row belongs to exactly one track.
    # Cross-track things (chat, forum) never become Activity rows.
    track = serializers.SerializerMethodField()

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
            "object_type",
            # Server-authored click target. Both bells already prefer this
            # over their own type-based routing, so shipping it is what makes
            # them stop re-deriving routes by hand. Blank on pre-migration
            # rows, which is exactly the "fall through" case they handle.
            "link_url",
            "is_skill_session",
            "track",
        ]

    # NOTE the legacy mobile vocabulary overloads "material" to mean
    # "coursework-ish", which is why ASSIGNMENT and SUBMISSION both map to
    # it. TYPE_MATERIAL genuinely IS a study material and lands on the same
    # legacy string via the .lower() fallback — listed explicitly so nobody
    # reads the omission as an oversight. Web consumers use raw_type and
    # see the distinct "MATERIAL".
    _TYPE_MAP = {
        Activity.TYPE_SESSION:    "session",
        Activity.TYPE_QUIZ:       "quiz",
        Activity.TYPE_ASSIGNMENT: "material",
        Activity.TYPE_SUBMISSION: "material",
        Activity.TYPE_MATERIAL:   "material",
    }

    def get_unread(self, obj):
        return not obj.is_read

    def get_type(self, obj):
        return self._TYPE_MAP.get(obj.type, obj.type.lower())

    def get_subject(self, obj):
        return obj.subject_name or ""

    def get_learner_profile_id(self, obj):
        return str(obj.learner_profile_id) if obj.learner_profile_id else None

    def get_is_skill_session(self, obj):
        try:
            return obj.content_type.model == "skillsession"
        except Exception:
            return False

    def get_object_type(self, obj):
        """Lowercase model name of whatever `object_id` points at.

        The bell has to route a SUBMISSION row, and `type` alone cannot tell
        it whether the parent is an Assignment (→ the submissions page) or a
        Quiz (→ the quizzes page): both are Activity.TYPE_SUBMISSION. The
        backend does mark quiz submissions with a `subtype` discriminator,
        but only via `extra=` — which `_ws_payload` merges into the ephemeral
        WebSocket frame and nothing ever persists. So a quiz submission read
        back from THIS feed (page load, or "See all") arrived with no
        discriminator at all and fell through to the assignment branch.

        content_type is already stored on every row, so this needs no
        migration and cannot drift from the object it describes — unlike a
        duplicated `subtype` column would.
        """
        try:
            return obj.content_type.model
        except Exception:
            return ""

    def get_track(self, obj):
        # Derived from the same content_type probe as is_skill_session so
        # the two can never disagree. Defaults to "academy" (not blank) on
        # a lookup failure: an Activity row always belongs to a track, and
        # academy is the larger, default-landing surface.
        return "skill" if self.get_is_skill_session(obj) else "academy"
