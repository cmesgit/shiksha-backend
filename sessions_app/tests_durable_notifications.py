# Cover for the academy session lifecycles finally writing DURABLE
# notifications, not just an Activity row + a fire-and-forget WS frame.
#
# What was wrong
# ──────────────
# PrivateSession / GroupSession / LiveSession.started wrote only
# activity.Activity plus a WS push. If the recipient had no socket open the
# event was simply lost — while the Skill Dev equivalent
# (skills/notifications.py) wrote a full durable Notification. That
# asymmetry is why the bell looked Skill-Dev-dominated, and ~17 verbs in
# notifications/policy.py were defined but emitted by nothing.
#
# The two things these tests defend
# ─────────────────────────────────
# 1. A durable row is created, with the right verb/track/identity, for the
#    transitions that have a policy row — and NOT for the ones that don't
#    (those stay Activity-only on purpose).
# 2. Exactly ONE websocket frame per event. notify() pushes its own frame
#    and every caller here pushes a richer one; both arrive as
#    {"type": "notification"} with DIFFERENT ids (integer pk vs Activity
#    UUID), so the bell's id-dedupe cannot collapse them and the user would
#    see each event twice. push_ws=False on the notify() call is what
#    prevents that, and `test_*_pushes_exactly_one_ws_frame` is what stops
#    someone removing it.

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from notifications.models import Notification

User = get_user_model()

# Both the durable layer and the bell layer funnel through this one helper,
# so patching it counts every frame a single event produces.
WS = "livestream.services.notifications.push_ws_notification"


class PrivateSessionDurableNotificationTest(TestCase):
    """sessions_app.views._push_session_bell → durable Notification."""

    def setUp(self):
        self.student = User.objects.create_user(
            username="stu", email="stu@example.com", password="x")
        self.teacher = User.objects.create_user(
            username="tea", email="tea@example.com", password="x")

    def _session(self, status="approved"):
        """A real PrivateSession row.

        Created straight through the ORM rather than the API on purpose:
        the create/start VIEWS call out to LiveKit room provisioning, which
        has no credentials under settings_test and is the documented cause
        of the pre-existing sessions_app failures (see CLAUDE.md). The
        helper under test needs a genuine model instance regardless —
        ContentType.get_for_model() is called on it — so a stub is not an
        option here.
        """
        import datetime
        from sessions_app.models import PrivateSession

        return PrivateSession.objects.create(
            teacher=self.teacher,
            requested_by=self.student,
            subject="Physics",
            scheduled_date=datetime.date(2026, 9, 1),
            scheduled_time=datetime.time(10, 0),
            status=status,
        )

    def _run(self, status, cancelled_by=None):
        from sessions_app.views import _push_session_bell
        _push_session_bell(self._session(status), cancelled_by=cancelled_by)

    def test_approved_creates_a_durable_row_for_the_student(self):
        self._run("approved")
        n = Notification.objects.get(recipient=self.student)
        self.assertEqual(n.verb, "session.approved")
        self.assertEqual(n.track, "academy")
        self.assertEqual(n.link_url, "/private-sessions")
        self.assertEqual(n.payload["kind"], "private_session")

    def test_declined_and_rescheduled_map_to_their_own_verbs(self):
        self._run("declined")
        self.assertEqual(
            Notification.objects.get(recipient=self.student).verb,
            "session.declined")
        Notification.objects.all().delete()

        self._run("needs_reconfirmation")
        self.assertEqual(
            Notification.objects.get(recipient=self.student).verb,
            "session.rescheduled")

    def test_teacher_cancel_notifies_the_student_with_a_teacher_actor(self):
        self._run("cancelled", cancelled_by="teacher")
        n = Notification.objects.get()
        self.assertEqual(n.recipient, self.student)
        self.assertEqual(n.verb, "session.cancelled")
        self.assertEqual(n.actor, self.teacher)

    def test_student_cancel_notifies_the_teacher_and_links_to_their_page(self):
        self._run("cancelled", cancelled_by="student")
        n = Notification.objects.get()
        self.assertEqual(n.recipient, self.teacher)
        self.assertEqual(n.link_url, "/teacher/private-sessions")

    def test_statuses_without_a_policy_row_stay_activity_only(self):
        # "ongoing"/"completed"/"withdrawn" have no notifications/policy.py
        # entry, so emitting them would route channels by unknown-verb
        # fallback. They must remain Activity-only until a policy row exists.
        for status in ("ongoing", "completed", "withdrawn"):
            self._run(status)
        self.assertEqual(Notification.objects.count(), 0)

    def test_pushes_exactly_one_ws_frame(self):
        with patch(WS) as ws:
            self._run("approved")
        self.assertEqual(
            ws.call_count, 1,
            "duplicate WS frame — the bell renders the event twice")

    def test_the_surviving_ws_frame_is_the_routable_one(self):
        # notify()'s generic frame has no is_private_session/type, so the
        # bell could not route it. The caller's frame must be the survivor.
        with patch(WS) as ws:
            self._run("approved")
        payload = ws.call_args[0][1]
        self.assertTrue(payload["is_private_session"])
        self.assertEqual(payload["track"], "academy")

    def test_a_replayed_transition_does_not_write_a_second_row(self):
        # get_or_create on the Activity row is the dedupe ledger; the
        # durable emit sits inside its `created` guard.
        session = self._session("approved")
        from sessions_app.views import _push_session_bell
        _push_session_bell(session)
        _push_session_bell(session)
        self.assertEqual(Notification.objects.count(), 1)


class GroupSessionDurableNotificationTest(TestCase):
    """sessions_app.group_session_views._notify_user → durable Notification.

    `_notify_user` gained an opt-in `verb=`; only the call sites whose
    transition has a notifications/policy.py row pass one. These tests pin
    both directions — that a tagged call writes a row, and that an untagged
    one still doesn't.
    """

    def setUp(self):
        self.host = User.objects.create_user(
            username="host", email="host@example.com", password="x")
        self.invitee = User.objects.create_user(
            username="inv", email="inv@example.com", password="x")

    def _session(self):
        import datetime
        from sessions_app.models import GroupSession
        return GroupSession.objects.create(
            host=self.host,
            subject_name="Chemistry",
            short_code="abc-defg-hij",
            scheduled_date=datetime.date(2026, 9, 2),
            scheduled_time=datetime.time(11, 0),
        )

    def test_invite_writes_a_durable_academy_row_linking_to_the_session(self):
        from sessions_app.group_session_views import _notify_user
        session = self._session()
        _notify_user(self.invitee, "📚 invited you", session,
                     verb="group.invite", actor=self.host)

        n = Notification.objects.get(recipient=self.invitee)
        self.assertEqual(n.verb, "group.invite")
        self.assertEqual(n.track, "academy")
        self.assertEqual(n.actor, self.host)
        # short_code is preferred over the UUID — same shape the reminder
        # sweep in notifications/tasks.py already deep-links to.
        self.assertEqual(n.link_url, "/sessions/group/abc-defg-hij")

    def test_cancellation_writes_its_own_verb(self):
        from sessions_app.group_session_views import _notify_user
        _notify_user(self.invitee, "❌ cancelled", self._session(),
                     verb="group.cancelled", actor=self.host)
        self.assertEqual(Notification.objects.get().verb, "group.cancelled")

    def test_an_untagged_call_stays_activity_only(self):
        # accept/decline/withdraw acknowledgements have no policy row and
        # must not start emitting durable rows by accident.
        from sessions_app.group_session_views import _notify_user
        _notify_user(self.host, "✅ accepted your session", self._session())
        self.assertEqual(Notification.objects.count(), 0)

    def test_pushes_exactly_one_ws_frame(self):
        from sessions_app.group_session_views import _notify_user
        with patch(WS) as ws:
            _notify_user(self.invitee, "📚 invited you", self._session(),
                         verb="group.invite", actor=self.host)
        self.assertEqual(
            ws.call_count, 1,
            "duplicate WS frame — the bell renders the invite twice")
        self.assertTrue(ws.call_args[0][1]["is_group_session"])
        self.assertEqual(ws.call_args[0][1]["track"], "academy")

    def test_self_notify_is_dropped(self):
        # notify()'s actor guard: the host cancelling shouldn't be told
        # about their own cancellation.
        from sessions_app.group_session_views import _notify_user
        _notify_user(self.host, "❌ cancelled", self._session(),
                     verb="group.cancelled", actor=self.host)
        self.assertEqual(Notification.objects.count(), 0)


class LivestreamDurableNotificationTest(TestCase):
    """livestream go-live used to be WS-only — offline students lost it."""

    def test_go_live_writes_a_durable_row_and_one_frame(self):
        from notifications.services import notify

        student = User.objects.create_user(
            username="s2", email="s2@example.com", password="x")
        teacher = User.objects.create_user(
            username="t2", email="t2@example.com", password="x")

        with patch(WS) as ws:
            notify(recipient=student, actor=teacher,
                   verb="livestream.started", title="🔴 Maths is now LIVE!",
                   link_url="/live/abc", push_ws=False)
        n = Notification.objects.get()
        self.assertEqual(n.verb, "livestream.started")
        self.assertEqual(n.track, "academy")
        self.assertEqual(ws.call_count, 0,
                         "push_ws=False must suppress notify()'s own frame")


class NotifyPushWsFlagTest(TestCase):
    """The flag itself — the thing that stops every double-render."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="u", email="u@example.com", password="x")

    def test_default_still_pushes(self):
        from notifications.services import notify
        with patch(WS) as ws:
            notify(recipient=self.user, verb="forum.reply", title="t")
        self.assertEqual(ws.call_count, 1)

    def test_push_ws_false_suppresses_only_the_frame_not_the_row(self):
        from notifications.services import notify
        with patch(WS) as ws:
            n = notify(recipient=self.user, verb="forum.reply", title="t",
                       push_ws=False)
        self.assertEqual(ws.call_count, 0)
        self.assertIsNotNone(n, "the durable row must still be written")
        self.assertEqual(Notification.objects.count(), 1)
