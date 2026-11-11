"""RC-001 — baseline contract for general user-to-user instant meetings.

These tests exist to be *broken* by a careless remote-assistance change.
They pin the behaviour the Shiksha Remote pack (``docs/architecture/
shiksha-remote/``) promises to leave alone, so that promise is executable
rather than a sentence in a README:

  * a general instant meeting authorizes on **participation**, not on an
    Enrollment, a Subject, a course, or a teacher/student pairing — two
    unrelated students can meet, and so can two unrelated teachers;
  * the LiveKit identity is ``<user_uuid>_<session_uuid>`` and the room is
    ``group_session_<session_uuid>``;
  * ``POST <uuid>/join/`` returns a fixed set of JSON keys that three web
    apps and a Flutter app already read;
  * and none of that weakens the denials — anonymous callers, lobby
    arrivals who have not been admitted, and terminal sessions still get
    no token.

Deliberately NOT covered here: host/end behaviour (``tests_group_session_end``
already owns it) and socket lifecycle (``tests_group_session_socket``).

LiveKit credentials are supplied by ``@override_settings`` with throwaway
values and the resulting JWT is decoded locally with the same throwaway
secret. That is RC-001 step 4's "patched signing settings or decoded test
JWT, no live secrets/network": the token builder runs for real, so an
identity or grant regression fails here, while the suite stays green on a
machine with no LIVEKIT_* environment (which is the documented cause of the
pre-existing ``sessions_app`` failures — see the repo CLAUDE.md). No
authorization branch is mocked.
"""

from unittest.mock import patch

import jwt
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Role, User, UserRole
from enrollments.models import Enrollment

from .models import GroupSession

# Throwaway LiveKit identity. Never a real credential; the secret below is
# also what the assertions decode with, so the signature is checked for real.
TEST_LIVEKIT = dict(
    LIVEKIT_URL="wss://livekit.test.invalid",
    LIVEKIT_API_KEY="rc001-test-key",
    LIVEKIT_API_SECRET="rc001-test-secret-not-a-real-credential",
)

# The exact join payload three web apps and the Flutter app read today.
# Adding a key is additive and fine; removing or renaming one is a breaking
# change for clients that this pack has committed not to make.
JOIN_RESPONSE_KEYS = {
    "livekit_url",
    "token",
    "room",
    "role",
    "duration_minutes",
    "room_started_at",
    "remaining_ms",
    "is_host",
    "features",
    "limits",
    "entitlement",
    "cap_ends_at",
}


@override_settings(**TEST_LIVEKIT)
class InstantMeetingBaselineTests(TestCase):
    """General meetings must work between any two authorized accounts."""

    @classmethod
    def setUpTestData(cls):
        for name in (Role.STUDENT, Role.TEACHER):
            Role.objects.get_or_create(name=name)

    def setUp(self):
        self.client = APIClient()
        self.instant_url = reverse("group-session-instant-create")

        # settings_test runs Celery eagerly, so the hard-duration cutoff that
        # production schedules for an hour's time executes inline instead —
        # it correctly refuses to end a room that just opened, then raises
        # Retry and logs a traceback per call. Stub the scheduling call only;
        # the task's own behaviour belongs to its own tests, not to a
        # join-contract baseline.
        scheduler = patch(
            "sessions_app.group_session_tasks."
            "hard_expire_group_session.apply_async"
        )
        self.schedule_cutoff = scheduler.start()
        self.addCleanup(scheduler.stop)

    # -- helpers ---------------------------------------------------------

    def _user(self, username, role):
        user = User.objects.create_user(
            username=username, email=f"{username}@rc001.test", password="pw",
        )
        UserRole.objects.create(
            user=user, role=Role.objects.get(name=role), is_active=True,
        )
        return user

    def _create_instant(self, host, admit_mode="open"):
        """Host opens an instant room. Returns the created GroupSession.

        ``admit_mode`` is set explicitly rather than inherited from whatever
        ``instant_create`` currently defaults to. That default is NOT stable
        across branches — it was ``"open"`` until ``f572151`` ("Allow approved
        anonymous guests in instant collaborations") changed it to ``"lobby"``
        — and a baseline that silently inherits it tests a different thing on
        each branch. ``test_instant_meetings_default_to_the_lobby_gate`` pins
        the default on purpose; every other test states what it wants.
        """
        self.client.force_authenticate(user=host)
        response = self.client.post(self.instant_url, {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        session = GroupSession.objects.get(pk=response.data["id"])
        if session.admit_mode != admit_mode:
            session.admit_mode = admit_mode
            session.save(update_fields=["admit_mode"])
        return session

    def _join(self, user, session):
        self.client.force_authenticate(user=user)
        return self.client.post(
            reverse("group-session-join", kwargs={"session_id": session.id}),
            {},
            format="json",
        )

    def _claims(self, token):
        """Decode with the same throwaway secret, verifying the signature."""
        return jwt.decode(
            token,
            TEST_LIVEKIT["LIVEKIT_API_SECRET"],
            algorithms=["HS256"],
            options={"verify_aud": False},
        )

    def _assert_join_contract(self, response, user, session, expected_role):
        """Every client-visible guarantee of a successful join, in one place."""
        self.assertEqual(response.status_code, 200, response.data)
        body = response.data

        self.assertEqual(
            set(body), JOIN_RESPONSE_KEYS,
            "the join response key set changed — three web apps and the "
            "Flutter app read these",
        )
        self.assertEqual(body["livekit_url"], TEST_LIVEKIT["LIVEKIT_URL"])
        self.assertEqual(body["room"], f"group_session_{session.id}")
        self.assertEqual(body["role"], expected_role)
        self.assertEqual(body["is_host"], session.host_id == user.id)
        self.assertIsInstance(body["duration_minutes"], int)
        self.assertIn("features", body)
        self.assertIn("max_participants", body["limits"])

        claims = self._claims(body["token"])
        self.assertEqual(
            claims["sub"], f"{user.id}_{session.id}",
            "LiveKit identity shape changed; RC-002 and every existing "
            "attendance/moderation path key on it",
        )
        self.assertEqual(claims["iss"], TEST_LIVEKIT["LIVEKIT_API_KEY"])
        self.assertTrue(claims["video"]["roomJoin"])
        self.assertEqual(claims["video"]["room"], f"group_session_{session.id}")

    # -- the general-meeting guarantee -----------------------------------

    def test_two_unrelated_students_can_meet_without_enrollment(self):
        alice = self._user("rc001_student_a", Role.STUDENT)
        bob = self._user("rc001_student_b", Role.STUDENT)

        session = self._create_instant(alice)
        self.assertEqual(session.session_type, "instant")
        self.assertIsNone(
            session.subject,
            "an instant meeting must not require a Subject",
        )

        self._assert_join_contract(self._join(alice, session), alice, session, "HOST")
        self._assert_join_contract(self._join(bob, session), bob, session, "STUDENT")

        self.assertEqual(
            Enrollment.objects.count(), 0,
            "no Enrollment was created or needed for a general meeting",
        )

    def test_two_unrelated_teachers_can_meet(self):
        first = self._user("rc001_teacher_a", Role.TEACHER)
        second = self._user("rc001_teacher_b", Role.TEACHER)

        session = self._create_instant(first)
        self._assert_join_contract(self._join(first, session), first, session, "HOST")

        # A TEACHER-role account joining someone else's instant room carries
        # the STUDENT in-room badge: "teacher" is reserved for an explicitly
        # invited_teacher on a scheduled session. Pinned because it looks like
        # a bug until you read join_group_session, and because the remote pack
        # authorizes on participation rather than on this badge.
        self._assert_join_contract(
            self._join(second, session), second, session, "STUDENT",
        )
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_direction_is_symmetric(self):
        """B hosting A is the same contract as A hosting B."""
        alice = self._user("rc001_sym_a", Role.STUDENT)
        bob = self._user("rc001_sym_b", Role.STUDENT)

        a_hosts = self._create_instant(alice)
        self._assert_join_contract(self._join(bob, a_hosts), bob, a_hosts, "STUDENT")

        b_hosts = self._create_instant(bob)
        self._assert_join_contract(self._join(alice, b_hosts), alice, b_hosts, "STUDENT")

        self.assertNotEqual(a_hosts.id, b_hosts.id)
        self.assertNotEqual(a_hosts.room_name, b_hosts.room_name)

    def test_teacher_student_pair_uses_the_identical_path(self):
        teacher = self._user("rc001_mixed_teacher", Role.TEACHER)
        student = self._user("rc001_mixed_student", Role.STUDENT)

        session = self._create_instant(teacher)
        self._assert_join_contract(
            self._join(student, session), student, session, "STUDENT",
        )
        self.assertEqual(Enrollment.objects.count(), 0)

    # -- the denials that must survive -----------------------------------

    def test_anonymous_cannot_create_or_join(self):
        host = self._user("rc001_anon_host", Role.STUDENT)
        session = self._create_instant(host)

        self.client.force_authenticate(user=None)

        created = self.client.post(self.instant_url, {}, format="json")
        self.assertIn(created.status_code, (401, 403), created.data)

        joined = self.client.post(
            reverse("group-session-join", kwargs={"session_id": session.id}),
            {},
            format="json",
        )
        self.assertIn(joined.status_code, (401, 403), joined.data)
        self.assertNotIn("token", joined.data)

    def test_lobby_arrival_gets_no_token_until_admitted(self):
        host = self._user("rc001_lobby_host", Role.STUDENT)
        visitor = self._user("rc001_lobby_visitor", Role.STUDENT)

        session = self._create_instant(host, admit_mode="lobby")

        pending = self._join(visitor, session)
        self.assertEqual(pending.status_code, 202, pending.data)
        self.assertEqual(pending.data["status"], "pending")
        self.assertNotIn(
            "token", pending.data,
            "a lobby arrival must not receive a LiveKit token",
        )
        request_id = pending.data["join_request_id"]

        # The host admits through the real endpoint — not by flipping the row.
        self.client.force_authenticate(user=host)
        admitted = self.client.post(
            reverse(
                "group-session-join-request-admit",
                kwargs={"session_id": session.id, "request_id": request_id},
            ),
            {},
            format="json",
        )
        self.assertEqual(admitted.status_code, 200, admitted.data)

        session.refresh_from_db()
        self._assert_join_contract(
            self._join(visitor, session), visitor, session, "STUDENT",
        )

    def test_terminal_sessions_issue_no_token(self):
        host = self._user("rc001_terminal_host", Role.STUDENT)
        visitor = self._user("rc001_terminal_visitor", Role.STUDENT)

        for state in ("completed", "cancelled", "expired"):
            with self.subTest(state=state):
                session = self._create_instant(host)
                session.status = state
                session.ended_at = timezone.now()
                session.save(update_fields=["status", "ended_at"])

                response = self._join(visitor, session)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertNotIn("token", response.data)

    def test_instant_meetings_default_to_the_lobby_gate(self):
        """A general meeting admits strangers by host decision, not by link.

        This is the one assertion in the file that depends on the branch it
        runs on, and it is deliberate. Knowing the shipped default matters to
        the remote-assistance work specifically: it means an un-invited
        arrival already passes through a host admission step before holding a
        token, which is a consent gate the companion design can build on
        rather than invent. If this starts failing, the default moved and
        RC-002 needs to know before it freezes anything.
        """
        host = self._user("rc001_default_host", Role.STUDENT)

        self.client.force_authenticate(user=host)
        response = self.client.post(self.instant_url, {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        session = GroupSession.objects.get(pk=response.data["id"])

        self.assertEqual(
            session.admit_mode, "lobby",
            "instant meetings default to the knock-to-join gate as of "
            "f572151; it was 'open' before that",
        )

    def test_join_does_not_invent_a_room_name(self):
        """room_name is assigned by the server, never accepted from a client."""
        host = self._user("rc001_roomname_host", Role.STUDENT)
        session = self._create_instant(host)

        self.client.force_authenticate(user=host)
        response = self.client.post(
            reverse("group-session-join", kwargs={"session_id": session.id}),
            {"room": "group_session_somebody_elses_room"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["room"], f"group_session_{session.id}")
        self.assertEqual(
            self._claims(response.data["token"])["video"]["room"],
            f"group_session_{session.id}",
        )
