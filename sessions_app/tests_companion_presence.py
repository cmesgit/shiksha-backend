"""RC-009b — a companion is not a person in the room.

The acceptance criterion is "no attendance or capacity inflation", and the
thing that makes it non-trivial is this, in `group_attendance`:

    def parse_user_id(identity):
        parts = identity.split("_", 1)
        return parts[0] if parts else None

Group-session identities are ``{user.id}_{session.id}``. A companion joins
the *same room*, so without a guard its ``rcdev_<uuid>`` identity reaches
that function and yields the literal string ``"rcdev"`` as a user id. The
exclusion therefore has to happen **before** the parse, not after — which
is what these tests pin.

🔴 The companion-shaped identity used here is mostly **unregistered** on
purpose. Excluding only *registered* companions would leave exactly the gap
this guard exists to close, so the tests assert that a forged prefix is
excluded too.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from accounts.models import User

from livestream import views as lv

from .models import (
    GroupSession,
    GroupSessionAttendance,
    GroupSessionAttendanceInterval,
)


def _event(kind, room_name, identity):
    """A LiveKit webhook event with the fields the handlers read.

    Mirrors ``livestream/tests.py``'s ``_evt`` so both suites describe the
    same wire shape.
    """
    return SimpleNamespace(
        event=kind,
        room=SimpleNamespace(name=room_name),
        participant=SimpleNamespace(identity=str(identity)),
    )


class CompanionPresenceTests(TestCase):

    def setUp(self):
        super().setUp()
        self.student = User.objects.create_user(
            username="pres_student", email="s@pres.test", password="pw")
        now = timezone.localtime()
        self.room = f"room-{uuid.uuid4().hex[:8]}"
        self.session = GroupSession.objects.create(
            host=self.student, topic="Presence", scheduled_date=now.date(),
            scheduled_time=now.time().replace(microsecond=0),
            duration_minutes=60, session_type="instant", status="live",
            short_code=uuid.uuid4().hex[:12], room_name=self.room,
            room_started_at=timezone.now())

    def _intervals(self):
        return GroupSessionAttendanceInterval.objects.filter(
            session=self.session).count()

    # -- the human path still works ---------------------------------------

    def test_a_human_join_still_opens_an_interval(self):
        """🔴 First, because a guard that breaks ordinary attendance would
        be worse than the problem it fixes."""
        lv._handle_participant_join(
            _event("participant_joined", self.room,
                   f"{self.student.id}_{self.session.id}"))
        self.assertEqual(self._intervals(), 1)

    def test_a_human_leave_still_closes_it(self):
        identity = f"{self.student.id}_{self.session.id}"
        lv._handle_participant_join(
            _event("participant_joined", self.room, identity))
        lv._handle_participant_left(
            _event("participant_left", self.room, identity))
        open_now = GroupSessionAttendanceInterval.objects.filter(
            session=self.session, left_at__isnull=True).count()
        self.assertEqual(open_now, 0)

    # -- the companion is excluded ----------------------------------------

    def test_a_companion_join_opens_no_interval(self):
        lv._handle_participant_join(
            _event("participant_joined", self.room, f"rcdev_{uuid.uuid4()}"))
        self.assertEqual(self._intervals(), 0)
        self.assertEqual(
            GroupSessionAttendance.objects.filter(session=self.session).count(),
            0)

    def test_a_companion_leave_closes_nothing(self):
        """Symmetry. If join excluded and leave did not, a companion
        disconnect would run close_intervals for whichever user the legacy
        parse invented."""
        identity = f"{self.student.id}_{self.session.id}"
        lv._handle_participant_join(
            _event("participant_joined", self.room, identity))
        lv._handle_participant_left(
            _event("participant_left", self.room, f"rcdev_{uuid.uuid4()}"))
        still_open = GroupSessionAttendanceInterval.objects.filter(
            session=self.session, left_at__isnull=True).count()
        self.assertEqual(still_open, 1, "the human's interval must survive")

    def test_an_unregistered_companion_prefix_is_still_excluded(self):
        """🔴 The gap that gating on registration would leave open.

        A forged or stale `rcdev_` identity has no host binding behind it.
        It must still be kept out of attendance — the prefix test is
        fail-safe, and the worst a forgery achieves is excluding itself
        from a count it was never entitled to appear in.
        """
        for identity in (f"rcdev_{uuid.uuid4()}", "rcdev_not-a-uuid",
                         "rcdev_"):
            with self.subTest(identity=identity):
                lv._handle_participant_join(
                    _event("participant_joined", self.room, identity))
        self.assertEqual(self._intervals(), 0)

    def test_the_literal_rcdev_user_id_never_reaches_attendance(self):
        """The specific failure mode: `"rcdev"` parsed as a user id.

        Asserted as a property of the data rather than of the call path, so
        it keeps meaning if the guard ever moves.
        """
        lv._handle_participant_join(
            _event("participant_joined", self.room, f"rcdev_{uuid.uuid4()}"))
        self.assertFalse(
            GroupSessionAttendanceInterval.objects
            .filter(session=self.session).exists())
        self.assertFalse(User.objects.filter(username="rcdev").exists())

    def test_a_name_that_merely_starts_similarly_is_not_excluded(self):
        """`rcdevil_…` is not `rcdev_…`. A prefix test that matched it
        would exclude real humans from attendance, which is the failure
        this guard must not introduce."""
        from sessions_app.collaboration import companion_tokens
        self.assertFalse(companion_tokens.looks_like_companion("rcdevil_x"))
        self.assertFalse(
            companion_tokens.looks_like_companion(
                f"{self.student.id}_{self.session.id}"))

    def test_capacity_is_unaffected_because_the_companion_never_chats(self):
        """Documents why there is no capacity guard to write.

        `active_connections` — what `group_session_views` gates instant
        capacity on — is incremented by the **chat** WebSocket consumer,
        not by LiveKit webhooks. The companion opens the collaboration WSS
        instead, which RC-006b deliberately kept out of that counter. So a
        companion cannot inflate capacity, and this asserts the invariant
        rather than adding a guard that would imply otherwise.
        """
        before = GroupSession.objects.get(pk=self.session.pk).active_connections
        lv._handle_participant_join(
            _event("participant_joined", self.room, f"rcdev_{uuid.uuid4()}"))
        after = GroupSession.objects.get(pk=self.session.pk).active_connections
        self.assertEqual(before, after)
