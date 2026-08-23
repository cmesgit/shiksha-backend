"""In-class moderation must address the identity the student is REALLY using.

Written alongside the fix for the audit finding "'Remove participant' and
'Mute participant' target an identity that doesn't exist": both views called
``build_identity(target_id, session.id)`` with the profile argument omitted,
which yields the *teacher*-shaped ``"<uid>_x_<sid>"`` (``NO_PROFILE``), while
a learner joins as ``"<uid>_<profile_id>_<sid>"``. LiveKit was asked to mute /
disconnect a participant that was never in the room, so a disruptive student
could not be removed at all.

No LiveKit credentials needed — ``room_admin``'s two calls are patched at the
point of use, which is also what lets us assert the exact identity string.

To see these fail against the old code, replace the ``_moderation_identities``
loops in livestream/views.py with the original single
``build_identity(target_id, session.id)`` call.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import LearnerProfile, Role, UserRole
from courses.models import Batch, Board, Course, Subject, TeachingAssignment
from enrollments.models import Enrollment
from livestream.models import LiveSession, LiveSessionAttendanceInterval
from livestream.services.token import NO_PROFILE

User = get_user_model()


class ModerationIdentityTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="mod_t@x.com", email="mod_t@x.com", password="x")
        role, _ = Role.objects.get_or_create(name="TEACHER")
        UserRole.objects.create(user=self.teacher, role=role, is_active=True,
                                is_primary=True)

        self.student = User.objects.create_user(
            username="mod_s@x.com", email="mod_s@x.com", password="x")
        # One account, two children — the exact shape that makes the profile
        # unrecoverable from the enrolment alone.
        self.profile = LearnerProfile.objects.create(
            account=self.student, display_name="Child A")
        self.sibling = LearnerProfile.objects.create(
            account=self.student, display_name="Child B")

        board = Board.objects.create(name="ModBoard", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="M10", class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Chem")
        self.batch = Batch.objects.create(course=self.course, name="MOD-A")
        TeachingAssignment.objects.create(subject=self.subject,
                                          teacher=self.teacher, batch=None,
                                          is_active=True)
        Enrollment.objects.create(user=self.student, course=self.course,
                                  learner_profile=self.profile,
                                  status=Enrollment.STATUS_ACTIVE)

        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, batch=self.batch,
            title="Chem", start_time=now - timedelta(minutes=10),
            end_time=now + timedelta(hours=1), room_name="room_mod",
            created_by=self.teacher, status=LiveSession.STATUS_LIVE,
            actual_started_at=now - timedelta(minutes=9),
        )
        # The participant_joined webhook has landed: Child A is in the room.
        LiveSessionAttendanceInterval.objects.create(
            session=self.session, user=self.student,
            learner_profile=self.profile, joined_at=now,
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.teacher,
                                       token={"context": "teacher"})

    @property
    def joined_identity(self):
        return f"{self.student.id}_{self.profile.id}_{self.session.id}"

    # ── remove ───────────────────────────────────────────────────────
    def test_remove_addresses_the_profile_identity_the_student_joined_with(self):
        with patch("livestream.services.room_admin.remove_participant") as remove:
            res = self.client.post(
                f"/api/livestream/sessions/{self.session.id}/remove/",
                {"user_id": str(self.student.id)}, format="json",
            )
        self.assertEqual(res.status_code, 200)

        addressed = [c.args[1] for c in remove.call_args_list]
        self.assertIn(self.joined_identity, addressed)

    def test_remove_still_covers_the_no_profile_shape(self):
        """Teachers and pre-profile tokens are genuinely ``<uid>_x_<sid>``."""
        with patch("livestream.services.room_admin.remove_participant") as remove:
            self.client.post(
                f"/api/livestream/sessions/{self.session.id}/remove/",
                {"user_id": str(self.student.id)}, format="json",
            )
        addressed = [c.args[1] for c in remove.call_args_list]
        self.assertIn(f"{self.student.id}_{NO_PROFILE}_{self.session.id}",
                      addressed)

    def test_remove_reports_failure_when_nothing_matched(self):
        """Every identity raising means the student is still in the room —
        the teacher must be told, not shown a success message."""
        with patch("livestream.services.room_admin.remove_participant",
                   side_effect=Exception("not found")):
            res = self.client.post(
                f"/api/livestream/sessions/{self.session.id}/remove/",
                {"user_id": str(self.student.id)}, format="json",
            )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["disconnected"])
        self.assertIn("may still be connected", res.data["detail"])

    # ── mute ─────────────────────────────────────────────────────────
    def test_mute_addresses_the_profile_identity_the_student_joined_with(self):
        with patch("livestream.services.room_admin.mute_participant") as mute:
            res = self.client.post(
                f"/api/livestream/sessions/{self.session.id}/mute/",
                {"user_id": str(self.student.id)}, format="json",
            )
        self.assertEqual(res.status_code, 200)

        addressed = [c.args[1] for c in mute.call_args_list]
        self.assertIn(self.joined_identity, addressed)

    def test_mute_succeeds_when_only_the_real_identity_is_in_the_room(self):
        """The realistic case: every other candidate shape raises, because
        LiveKit has no such participant. One hit is still a success."""
        def only_the_real_one(room, identity, **kw):
            if identity != self.joined_identity:
                raise Exception("participant not found")
            return 1

        with patch("livestream.services.room_admin.mute_participant",
                   side_effect=only_the_real_one):
            res = self.client.post(
                f"/api/livestream/sessions/{self.session.id}/mute/",
                {"user_id": str(self.student.id)}, format="json",
            )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["muted"])

    def test_mute_502s_when_no_identity_is_in_the_room(self):
        with patch("livestream.services.room_admin.mute_participant",
                   side_effect=Exception("participant not found")):
            res = self.client.post(
                f"/api/livestream/sessions/{self.session.id}/mute/",
                {"user_id": str(self.student.id)}, format="json",
            )
        self.assertEqual(res.status_code, 502)

    def test_a_sibling_profile_is_covered_before_the_webhook_lands(self):
        """Removed seconds after joining, so there is no open interval yet —
        every profile on the account is tried as a fallback."""
        LiveSessionAttendanceInterval.objects.all().delete()

        with patch("livestream.services.room_admin.remove_participant") as remove:
            self.client.post(
                f"/api/livestream/sessions/{self.session.id}/remove/",
                {"user_id": str(self.student.id)}, format="json",
            )
        addressed = [c.args[1] for c in remove.call_args_list]
        self.assertIn(f"{self.student.id}_{self.profile.id}_{self.session.id}",
                      addressed)
        self.assertIn(f"{self.student.id}_{self.sibling.id}_{self.session.id}",
                      addressed)
