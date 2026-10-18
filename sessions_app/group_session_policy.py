"""Product rules for the two kinds of GroupSession.

Both kinds use the same LiveKit room, token, lobby, attendance and cleanup
machinery. Keep access and duration decisions here so an instant-meeting
change cannot silently alter scheduled invitation-based sessions.
"""

from datetime import timedelta


class RoomTiming:
    """Shared clock arithmetic; each product still owns its access rules."""

    def base_deadline(self, session):
        if not session.room_started_at:
            return None
        return session.room_started_at + timedelta(minutes=session.duration_minutes)

    def deadline(self, session):
        baseline = self.base_deadline(session)
        if baseline is None:
            return None
        return max(baseline, session.cap_ends_at) if session.cap_ends_at else baseline


class ScheduledGroupPolicy(RoomTiming):
    kind = "scheduled"
    instant_capacity = None
    requires_paid_account = True
    personal_trial = True
    allows_extension = True

    def can_view(self, session, user):
        if session.host_id == user.id:
            return True
        if session.invited_teacher_id and session.invited_teacher_id == user.id:
            return True
        return session.invites.filter(user=user).exists()

    def join_error(self, session, user, invite):
        if session.host_id == user.id:
            return None
        if invite is None:
            return "You are not a participant in this group session."
        if invite.status == "declined":
            return "You declined this invite, so you can't join the room."
        if invite.status != "accepted":
            return "You must accept the invite before you can join the room."
        return None

    def start_error(self, session):
        if session.invites.filter(status="accepted").count() < 1:
            return "At least 1 invitee must accept before the room opens."
        return None

    def role(self, session, user, invite):
        if session.host_id == user.id:
            return "host"
        if (session.invited_teacher_id == user.id and invite
                and invite.status == "accepted"):
            return "teacher"
        return "student"

class InstantCollaborationPolicy(RoomTiming):
    kind = "instant"
    instant_capacity = 50
    requires_paid_account = False
    personal_trial = False
    allows_extension = False
    allowed_durations = frozenset({30, 45, 60, 90, 120, 150, 180})
    default_duration = 60

    def can_view(self, session, user):
        # Authentication is enforced by the view; the code/link supplies access.
        return True

    def join_error(self, session, user, invite):
        # Optional lobby admission is enforced after this check for both kinds.
        return None

    def start_error(self, session):
        return None

    def role(self, session, user, invite):
        return "host" if session.host_id == user.id else "student"

    def duration_for_create(self, raw):
        try:
            value = int(raw or self.default_duration)
        except (TypeError, ValueError):
            return self.default_duration
        return value if value in self.allowed_durations else self.default_duration


SCHEDULED_GROUP = ScheduledGroupPolicy()
INSTANT_COLLABORATION = InstantCollaborationPolicy()


def policy_for(session):
    return INSTANT_COLLABORATION if session.session_type == "instant" else SCHEDULED_GROUP
