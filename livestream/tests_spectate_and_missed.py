# Admin spectating, and the "teacher never turned up" case.

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import LearnerProfile, Role, UserRole
from courses.models import Batch, Board, Course, Subject, TeachingAssignment
from enrollments.models import Enrollment
from livestream.models import LiveSession, LiveSessionSpectate

User = get_user_model()


class Base(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="sp_t@x.com", email="sp_t@x.com", password="x")
        role, _ = Role.objects.get_or_create(name="TEACHER")
        UserRole.objects.create(user=self.teacher, role=role, is_active=True,
                                is_primary=True)
        self.admin = User.objects.create_user(
            username="sp_a@x.com", email="sp_a@x.com", password="x",
            is_staff=True)

        board = Board.objects.create(name="SPBoard", board_type=Board.TYPE_CENTRAL)
        self.course = Course.objects.create(board=board, title="SP10",
                                            class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Geo")
        self.batch = Batch.objects.create(course=self.course, name="SP-A")
        TeachingAssignment.objects.create(subject=self.subject,
                                          teacher=self.teacher, batch=None,
                                          is_active=True)
        now = timezone.now()
        self.session = LiveSession.objects.create(
            course=self.course, subject=self.subject, batch=self.batch,
            title="Geo", start_time=now - timedelta(minutes=10),
            end_time=now + timedelta(hours=1), room_name="room_sp",
            created_by=self.teacher, status=LiveSession.STATUS_LIVE,
            actual_started_at=now - timedelta(minutes=9),
        )

    def admin_client(self):
        c = APIClient()
        c.force_authenticate(user=self.admin, token={"context": "teacher"})
        return c


class AdminSpectateTests(Base):
    def test_admin_gets_a_subscribe_only_token(self):
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return "tok"

        with patch("livestream.services.token.generate_livekit_token",
                   side_effect=fake):
            r = self.admin_client().post(
                f"/api/livestream/admin/streams/{self.session.id}/spectate/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(captured.get("spectator"),
                        "admin was handed a publishing token — they could "
                        "have spoken into a live class")

    @override_settings(LIVEKIT_API_KEY="k", LIVEKIT_API_SECRET="s" * 32)
    def test_the_grant_really_cannot_publish(self):
        """Asserted on the grant itself, not just the flag: this is the only
        subscribe-only shape in the codebase and nothing else guards it."""
        from livekit.api import VideoGrants
        seen = {}
        real = VideoGrants

        def spy(**kw):
            seen.update(kw)
            return real(**kw)

        with patch("livestream.services.token.VideoGrants", side_effect=spy):
            from livestream.services.token import generate_livekit_token
            generate_livekit_token(user=self.admin, session=self.session,
                                   spectator=True)
        self.assertFalse(seen.get("can_publish"))
        self.assertFalse(seen.get("can_publish_data"))
        self.assertTrue(seen.get("can_subscribe"))
        self.assertTrue(seen.get("hidden"),
                        "spectator would appear in the participant list")

    def test_every_spectate_is_logged(self):
        """Watching is silent to the room by design — so it must not also be
        untraceable."""
        with patch("livestream.services.token.generate_livekit_token",
                   return_value="tok"):
            self.admin_client().post(
                f"/api/livestream/admin/streams/{self.session.id}/spectate/",
                {"reason": "quality check"}, format="json")
        row = LiveSessionSpectate.objects.get(session=self.session)
        self.assertEqual(row.admin_id, self.admin.id)
        self.assertEqual(row.admin_email, "sp_a@x.com")
        self.assertEqual(row.reason, "quality check")

    def test_a_teacher_cannot_spectate_another_class(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        r = c.post(f"/api/livestream/admin/streams/{self.session.id}/spectate/")
        self.assertIn(r.status_code, (401, 403))
        self.assertFalse(LiveSessionSpectate.objects.exists())

    def test_a_student_cannot_spectate(self):
        u = User.objects.create_user(username="sp_s@x.com",
                                     email="sp_s@x.com", password="x")
        p = LearnerProfile.objects.create(
            account=u, display_name="S", full_name="S", student_id="SP1",
            is_default=True)
        Enrollment.objects.create(user=u, learner_profile=p,
                                  course=self.course, batch=self.batch,
                                  status=Enrollment.STATUS_ACTIVE)
        c = APIClient()
        c.force_authenticate(user=u, token={"context": "learner",
                                            "active_profile": str(p.id)})
        r = c.post(f"/api/livestream/admin/streams/{self.session.id}/spectate/")
        self.assertIn(r.status_code, (401, 403))

    def test_cannot_spectate_a_finished_class(self):
        LiveSession.objects.filter(pk=self.session.pk).update(
            status=LiveSession.STATUS_COMPLETED)
        r = self.admin_client().post(
            f"/api/livestream/admin/streams/{self.session.id}/spectate/")
        self.assertEqual(r.status_code, 400)


class MissedClassTests(Base):
    """A class the teacher never turned up to used to read as 'Completed' —
    indistinguishable, on every roster and report, from one that ran fine."""

    def _finish(self, started):
        LiveSession.objects.filter(pk=self.session.pk).update(
            start_time=timezone.now() - timedelta(hours=3),
            end_time=timezone.now() - timedelta(hours=2),
            actual_started_at=started,
            status=LiveSession.STATUS_COMPLETED,
        )
        self.session.refresh_from_db()

    def test_a_class_nobody_joined_reads_as_missed(self):
        self._finish(started=None)
        self.assertTrue(self.session.was_missed)
        self.assertEqual(self.session.display_status(), "MISSED")

    def test_a_class_that_ran_still_reads_as_completed(self):
        self._finish(started=timezone.now() - timedelta(hours=3))
        self.assertFalse(self.session.was_missed)
        self.assertEqual(self.session.display_status(), "COMPLETED")

    def test_a_cancelled_class_is_not_missed(self):
        """Calling a class off is a decision; not turning up is not. They must
        not collapse into the same label."""
        LiveSession.objects.filter(pk=self.session.pk).update(
            status=LiveSession.STATUS_CANCELLED, actual_started_at=None)
        self.session.refresh_from_db()
        self.assertFalse(self.session.was_missed)

    def test_an_upcoming_class_is_not_missed(self):
        LiveSession.objects.filter(pk=self.session.pk).update(
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1, hours=1),
            actual_started_at=None,
            status=LiveSession.STATUS_SCHEDULED)
        self.session.refresh_from_db()
        self.assertFalse(self.session.was_missed,
                         "a class that hasn't happened yet was marked missed")

    def test_missed_is_display_only_and_does_not_gate_lifecycle(self):
        """The reason this is derived rather than a real status: sixteen places
        treat COMPLETED as terminal, and a new terminal value would have to be
        added to all of them."""
        self._finish(started=None)
        self.assertEqual(self.session.computed_status(),
                         LiveSession.STATUS_COMPLETED)
