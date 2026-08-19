# PLACEMENT: backend/backend/notifications/tracks.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/tracks.py
#
# Which PRODUCT TRACK a notification belongs to: Academy, Skill Dev, or
# neither.
#
# Why this exists as a real field and not a query-time guess
# ──────────────────────────────────────────────────────────
# Before this module the only per-row signal of track was the `verb` string
# prefix, and the list API could only express it as `?verb_prefix=<one
# string>`. That makes "academy but not skill-dev" INEXPRESSIBLE — academy
# spans eight prefixes (session./group./livestream./assignment./quiz./
# materials./enrollment./…) while skill-dev is one. Every consumer that
# wanted the split had to re-implement the guess, and the unread badge
# couldn't be scoped at all.
#
# It also cannot be derived from `audience_identity`. Per chat/models.py's
# identity contract, a guest expert and a faculty teacher are the SAME
# TeacherProfile seen through two approved tracks, and a skill-dev learner
# and an academy student are the SAME LearnerProfile. A dual-track user has
# exactly one identity key, so identity separates PROFILES (which child),
# never tracks. The two axes are orthogonal and both are needed.
#
# The blank/NEUTRAL convention
# ────────────────────────────
# "" means "belongs to no single track — show it in both bells". This is
# the same meaning blank already carries for `audience_role` and
# `audience_identity`, and the filter helper below mirrors their
# `__in=["", value]` shape on purpose. Chat, forum, counseling, support and
# announcements are genuinely cross-track: a DM is a DM regardless of which
# dashboard you happen to be looking at, and hiding it behind a track would
# lose messages.
#
# Adding a verb: put it in _PREFIX_TRACKS if a whole app-prefix belongs to
# one track, or _VERB_TRACKS for a single exception. An unmapped verb
# resolves to NEUTRAL, which is the safe direction — a mis-tracked
# notification shows up twice, a wrongly-tracked one disappears.

ACADEMY = "academy"
SKILL = "skill"
NEUTRAL = ""

TRACK_CHOICES = [
    (ACADEMY, "Academy"),
    (SKILL, "Skill Dev"),
]

VALID_TRACKS = {ACADEMY, SKILL}

# Whole-app prefixes. Keys include the trailing dot so "skill." can never
# accidentally match a hypothetical "skillsomething.x" verb.
_PREFIX_TRACKS = {
    # ── Academy ────────────────────────────────────────────────────────
    "session.":    ACADEMY,   # PrivateSession lifecycle + reminders
    "group.":      ACADEMY,   # GroupSession invites/cancellations/reminders
    "livestream.": ACADEMY,   # live class started + reminders
    "assignment.": ACADEMY,
    "quiz.":       ACADEMY,
    "materials.":  ACADEMY,
    "enrollment.": ACADEMY,

    # ── Skill Dev ──────────────────────────────────────────────────────
    "skill.":      SKILL,     # SkillSession 9-verb lifecycle

    # ── Neutral: cross-track by nature, listed explicitly so that a
    #    reader can tell "deliberately both" from "nobody mapped it yet".
    "chat.":         NEUTRAL,
    "forum.":        NEUTRAL,
    "counseling.":   NEUTRAL,   # counselling is its own vertical, not a track
    "announcement.": NEUTRAL,
    "support.":      NEUTRAL,
    "payments.":     NEUTRAL,
}

# Single-verb exceptions, checked before the prefix map.
_VERB_TRACKS = {
    # A course receipt deep-links to /my-courses/<id>, which is Academy —
    # but a Skill Dev session payment is its own verb (skill.paid) and must
    # NOT be caught by a blanket "payments.* is academy" rule. Keeping
    # payments.* neutral and naming this one exception is clearer than
    # splitting the prefix.
    "payments.receipt": ACADEMY,
}


def track_for_verb(verb):
    """Resolve a dotted verb to ACADEMY / SKILL / NEUTRAL.

    Never raises and never guesses beyond the tables above: an unknown verb
    is NEUTRAL (visible in both bells) rather than silently academy.
    """
    if not verb:
        return NEUTRAL
    verb = str(verb)
    if verb in _VERB_TRACKS:
        return _VERB_TRACKS[verb]
    for prefix, track in _PREFIX_TRACKS.items():
        if verb.startswith(prefix):
            return track
    return NEUTRAL


def normalize(track):
    """Coerce arbitrary caller/query input to a valid stored value."""
    if not track:
        return NEUTRAL
    value = str(track).strip().lower()
    return value if value in VALID_TRACKS else NEUTRAL


def filter_queryset(qs, track):
    """Scope `qs` to one track PLUS the neutral rows.

    Mirrors the `audience_role` / `audience_identity` filters: asking for a
    track never hides the cross-track rows, it only hides the OTHER track.
    A blank/invalid `track` is a no-op, preserving the un-scoped default.
    """
    value = normalize(track)
    if not value:
        return qs
    return qs.filter(track__in=[NEUTRAL, value])
