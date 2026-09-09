"""
Tests for adding a teacher identity from inside the product (Phase 2).

Two things these lock down:

  * The whole matrix of track combinations, including faculty → skill, which
    the old policy refused. That refusal was never covered by a test, which is
    part of why it survived so long.
  * That no password is collected. The caller is already authenticated; the
    six signup branches that re-prove the account password exist only because
    signup can be entered by a stranger.
"""
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import (
    LearnerProfile, Role, TeacherProfile, User, UserRole,
)

URL = "/api/accounts/identities/teacher/"
PASSWORD = "pw-1234-abcd"


def make_user(email="t@test.com", verified=True):
    user = User.objects.create_user(
        username=email.split("@")[0], email=email, password=PASSWORD,
    )
    if verified:
        User.objects.filter(pk=user.pk).update(is_verified=True)
        user.refresh_from_db()
    LearnerProfile.objects.create(
        account=user, display_name="Me",
        relationship=LearnerProfile.RELATIONSHIP_SELF, is_default=True,
    )
    role, _ = Role.objects.get_or_create(name=Role.STUDENT)
    UserRole.objects.get_or_create(user=user, role=role,
                                   defaults={"is_active": True})
    return user


def client_for(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


class TeacherIdentityGetTest(TestCase):
    def test_learner_with_no_teacher_identity_can_add_either_track(self):
        res = client_for(make_user()).get(URL)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        body = res.json()
        self.assertFalse(body["has_teacher_identity"])
        self.assertTrue(body["can_add"]["academy"])
        self.assertTrue(body["can_add"]["skill"])

    def test_requires_authentication(self):
        self.assertEqual(APIClient().get(URL).status_code,
                         status.HTTP_401_UNAUTHORIZED)

    def test_requires_a_verified_email(self):
        user = make_user(verified=False)
        self.assertEqual(client_for(user).get(URL).status_code,
                         status.HTTP_403_FORBIDDEN)


class AddFirstTrackTest(TestCase):
    def test_skill_goes_live_immediately(self):
        user = make_user()
        res = client_for(user).post(URL, {"track": "skill"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        body = res.json()
        self.assertEqual(body["status"], TeacherProfile.TRACK_APPROVED)
        self.assertFalse(body["needs_review"])

        tp = User.objects.get(pk=user.pk).teacher_profile
        self.assertEqual(tp.skill_status, TeacherProfile.TRACK_APPROVED)
        self.assertIn("skill", tp.approved_tracks())
        self.assertTrue(user.has_role(Role.TEACHER))

    def test_academy_waits_for_admin_review(self):
        user = make_user()
        res = client_for(user).post(URL, {"track": "academy"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        body = res.json()
        self.assertEqual(body["status"], TeacherProfile.TRACK_PENDING)
        self.assertTrue(body["needs_review"])

        tp = User.objects.get(pk=user.pk).teacher_profile
        self.assertEqual(tp.academy_status, TeacherProfile.TRACK_PENDING)
        self.assertEqual(tp.approved_tracks(), [])
        self.assertFalse(tp.is_academy_faculty)

    def test_no_password_is_required(self):
        """The point of the phase: an authenticated caller proves nothing."""
        user = make_user()
        res = client_for(user).post(URL, {"track": "skill"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_self_learner_profile_is_preserved(self):
        user = make_user()
        client_for(user).post(URL, {"track": "skill"}, format="json")
        self.assertEqual(
            user.learner_profiles.filter(
                relationship=LearnerProfile.RELATIONSHIP_SELF, is_active=True,
            ).count(),
            1,
        )

    def test_bad_track_is_refused(self):
        user = make_user()
        for bad in ("", "teacher", "ACADEMYY", None):
            res = client_for(user).post(URL, {"track": bad}, format="json")
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            TeacherProfile.objects.filter(user=user).exists()
        )


class AddSecondTrackTest(TestCase):
    """The matrix. Either direction now works."""

    def _add(self, user, track):
        return client_for(user).post(URL, {"track": track}, format="json")

    def test_skill_then_academy(self):
        user = make_user()
        self._add(user, "skill")
        res = self._add(user, "academy")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        tp = User.objects.get(pk=user.pk).teacher_profile
        self.assertEqual(tp.skill_status, TeacherProfile.TRACK_APPROVED)
        self.assertEqual(tp.academy_status, TeacherProfile.TRACK_PENDING)
        self.assertEqual(tp.teacher_type, TeacherProfile.TYPE_BOTH)
        # The track they already had keeps working while academy is reviewed.
        self.assertIn("skill", tp.approved_tracks())

    def test_academy_then_skill_is_now_allowed(self):
        """Previously refused outright. The asymmetry was policy, not data."""
        user = make_user()
        self._add(user, "academy")
        res = self._add(user, "skill")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        tp = User.objects.get(pk=user.pk).teacher_profile
        self.assertEqual(tp.academy_status, TeacherProfile.TRACK_PENDING)
        self.assertEqual(tp.skill_status, TeacherProfile.TRACK_APPROVED)
        self.assertEqual(tp.teacher_type, TeacherProfile.TYPE_BOTH)

    def test_approved_faculty_can_add_skill(self):
        user = make_user()
        self._add(user, "academy")
        tp = user.teacher_profile
        tp.set_track_status(TeacherProfile.TRACK_ACADEMY,
                            TeacherProfile.TRACK_APPROVED)
        tp.sync_type_from_tracks()
        tp.save()

        res = self._add(user, "skill")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        tp.refresh_from_db()
        self.assertEqual(sorted(tp.approved_tracks()), ["academy", "skill"])

    def test_adding_a_held_track_is_a_conflict(self):
        user = make_user()
        self._add(user, "skill")
        res = self._add(user, "skill")
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.json()["code"], "track_held")

    def test_adding_a_pending_track_is_a_conflict(self):
        user = make_user()
        self._add(user, "academy")
        res = self._add(user, "academy")
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)

    def test_reapplying_after_rejection_clears_the_verdict(self):
        user = make_user()
        self._add(user, "academy")
        tp = user.teacher_profile
        tp.set_track_status(TeacherProfile.TRACK_ACADEMY,
                            TeacherProfile.TRACK_REJECTED)
        tp.academy_rejection_reason = "Insufficient documents"
        tp.save()

        res = self._add(user, "academy")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        tp.refresh_from_db()
        self.assertEqual(tp.academy_status, TeacherProfile.TRACK_PENDING)
        self.assertEqual(tp.academy_rejection_reason, "")
        self.assertIsNone(tp.academy_rejected_at)

    def test_report_reflects_state_after_adding(self):
        user = make_user()
        self._add(user, "skill")
        body = client_for(user).get(URL).json()
        self.assertTrue(body["has_teacher_identity"])
        self.assertEqual(body["tracks"]["skill"], TeacherProfile.TRACK_APPROVED)
        self.assertFalse(body["can_add"]["skill"])
        self.assertTrue(body["can_add"]["academy"])
        self.assertTrue(body["blocked_reason"]["skill"])
