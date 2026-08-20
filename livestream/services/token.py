import json
from datetime import timedelta

from django.conf import settings
from livekit.api import AccessToken, VideoGrants


def build_identity(user_id, session_id, profile_id=None):
    """The LiveKit participant identity for a live class.

    Carries the LEARNER PROFILE as well as the user, because one email is one
    account with many profiles (a parent and their children). Attendance is
    written from the LiveKit webhooks, which see nothing but this string — so
    without the profile here there is no way to tell which child was in the
    room, and two siblings on one account had their watch time merged into a
    single row that then showed on both their records. It cannot be looked up
    server-side either: when both children are enrolled in the same course,
    the enrolment alone is ambiguous. Only the join request knows, so it is
    baked in at token time.

    Composite, matching what group and private sessions already do. This was
    a bare str(user.id), and livestream was the only feature that did so —
    the other two carry an explicit comment about avoiding collisions when a
    user has more than one room open.

    LiveKit treats a repeated identity within a room as a replacement, so the
    bare form meant a student opening the class in a second tab silently
    killed the first with no explanation. Worse for attendance: if the kicked
    tab's participant_left landed after the new tab's participant_joined, the
    leave closed the freshly opened interval, leaving the student watching
    with no open interval at all — undercounted in the live viewer number and
    credited nothing for the rest of the lesson.
    """
    return f"{user_id}_{profile_id or NO_PROFILE}_{session_id}"


# Teachers have no learner profile. A placeholder keeps every identity the
# same shape, so segment positions never shift.
NO_PROFILE = "x"


def parse_identity(raw):
    """user id out of a participant identity, tolerating both legacy forms.

    Tokens are bearer credentials with a 2h TTL, so after each shape change
    there are live participants still holding the previous one. Handling all
    of them is what stops a deploy mid-class from corrupting the attendance of
    everyone already connected. Three shapes exist:

        "<user>"                     original bare form
        "<user>_<session>"           first composite (tab-collision fix)
        "<user>_<profile>_<session>" current

    The user id is the first segment in all three. Session and profile ids are
    UUIDs, which contain hyphens but never underscores, so splitting is safe.
    """
    raw = str(raw or "")
    return raw.split("_", 1)[0] if "_" in raw else raw


def parse_profile_id(raw):
    """Learner profile id, or None when the identity does not carry one.

    Returns None for both legacy shapes — deliberately, since a two-segment
    identity's second field is the SESSION id, and mistaking that for a
    profile id would write attendance against a profile that does not exist.
    """
    parts = str(raw or "").split("_")
    if len(parts) >= 3 and parts[1] and parts[1] != NO_PROFILE:
        return parts[1]
    return None


def generate_livekit_token(
    user,
    session,
    is_teacher=False,
    display_name=None,
    learner_profile=None,
    spectator=False,
):
    token = AccessToken(
        settings.LIVEKIT_API_KEY,
        settings.LIVEKIT_API_SECRET,
    )

    token.with_identity(
        build_identity(user.id, session.id,
                       getattr(learner_profile, "id", None))
    )

    # ✅ display name
    if display_name is None:
        profile = getattr(user, "profile", None)
        if profile and getattr(profile, "full_name", None):
            display_name = profile.full_name
        else:
            display_name = user.get_full_name() or user.username

    token.with_name(display_name)

    # ✅ metadata (role info)
    token.with_metadata(json.dumps({
        "role": ("spectator" if spectator
                 else "presenter" if is_teacher else "viewer"),
        "user_type": "teacher" if user.has_role("TEACHER") else "student",
        "user_id": str(user.id),
    }, default=str))

    # ✅ TTL
    token.with_ttl(timedelta(hours=2))

    # ✅ room
    room_name = getattr(session, "room_name", None)
    if not room_name:
        raise ValueError("Session has no room_name")

    if spectator:
        # 👁 SPECTATOR (admin quality monitoring) — subscribe only.
        #
        # The first grant in this codebase with can_publish=False. Every other
        # shape can publish at least a microphone, so an admin dropped into a
        # room with a viewer token could have spoken into a live class.
        #
        # `hidden=True` keeps them out of the participant list, per the
        # product decision that classes are not told an admin is watching.
        # That decision is the reason SpectateLog exists: monitoring that
        # leaves no trace at all is not something this codebase should make
        # possible, so the room stays unaware but the ACTION is recorded and
        # auditable by name, session and timestamp.
        grants = VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=False,
            can_publish_data=False,
            can_subscribe=True,
            hidden=True,
        )
    elif is_teacher:
        # 🎤 PRESENTER (creator only)
        grants = VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_publish_data=True,
            can_subscribe=True,
        )
    else:
        # 👀 VIEWER (students + other teachers)
        # can_publish=True so mic works, but frontend starts muted by default
        grants = VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,         # ✅ mic allowed (starts muted via frontend)
            can_publish_data=True,    # ✅ allows raise hand + chat
            can_subscribe=True,
            can_publish_sources=["microphone"],  # 🎤 mic only, no camera/screen
        )

    token.with_grants(grants)

    return token.to_jwt()