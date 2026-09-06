"""
Tests for teacher-mode entry and the teacher-mode PIN (Phase 3).

`TeacherContextView` had NO test coverage at all before this file — the
account-password gate it used to enforce was never asserted anywhere, which is
part of why it survived unexamined for so long. These tests cover both the new
PIN behaviour and the pre-existing track gates it has always had.

The security split under test:
  * ENTERING teacher mode  → PIN if set, nothing if not. Low friction.
  * CHANGING the PIN       → account password, always. Destructive + forgot-PIN.
"""
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import (
    LearnerProfile, Role, TeacherProfile, User, UserRole,
)

CONTEXT_URL = "/api/accounts/context/teacher/"
PIN_URL     = "/api/accounts/context/teacher/pin/"
PASSWORD    = "pw-1234-abcd"


def make_teacher(*, approved=True, track=TeacherProfile.TRACK_SKILL):
    user = User.objects.create_user(
        username="teach", email="teach@test.com", password=PASSWORD,
    )
    User.objects.filter(pk=user.pk).update(is_verified=True)
    user.refresh_from_db()
    LearnerProfile.objects.create(
        account=user, display_name="Me",
        relationship=LearnerProfile.RELATIONSHIP_SELF, is_default=True,
    )
    tp = TeacherProfile(user=user)
    tp.set_track_status(
        track,
        TeacherProfile.TRACK_APPROVED if approved else TeacherProfile.TRACK_PENDING,
    )
    tp.sync_type_from_tracks()
    tp.save()

    role, _ = Role.objects.get_or_create(name=Role.TEACHER)
    UserRole.objects.get_or_create(
        user=user, role=role, defaults={"is_active": approved},
    )
    return user, tp


def client_for(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


class EnterTeacherModeWithoutPinTest(TestCase):
    """The default. Nobody set a PIN, so nothing is asked for."""

    def test_no_pin_means_instant_entry(self):
        user, _tp = make_teacher()
        res = client_for(user).post(CONTEXT_URL, {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.json()["context"], "teacher")
        self.assertIn("access", res.cookies)

    def test_no_password_is_requested(self):
        """Regression guard for the change itself: a caller sending nothing at
        all must succeed, where previously this was a 400."""
        user, _tp = make_teacher()
        self.assertEqual(
            client_for(user).post(CONTEXT_URL, {}, format="json").status_code,
            status.HTTP_200_OK,
        )

    def test_track_is_resolved_to_the_approved_one(self):
        user, _tp = make_teacher(track=TeacherProfile.TRACK_SKILL)
        res = client_for(user).post(CONTEXT_URL, {}, format="json")
        self.assertEqual(res.json()["teacher"]["active_track"], "skill")


class EnterTeacherModeWithPinTest(TestCase):
    def setUp(self):
        self.user, self.tp = make_teacher()
        self.tp.set_pin("4321")
        self.tp.save(update_fields=["pin"])

    def test_correct_pin_enters(self):
        res = client_for(self.user).post(CONTEXT_URL, {"pin": "4321"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.json()["context"], "teacher")

    def test_wrong_pin_refused(self):
        res = client_for(self.user).post(CONTEXT_URL, {"pin": "0000"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()["code"], "bad_pin")
        self.assertNotIn("access", res.cookies)

    def test_missing_pin_refused(self):
        res = client_for(self.user).post(CONTEXT_URL, {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()["code"], "bad_pin")

    def test_account_password_is_not_a_substitute(self):
        """The PIN protects against someone who is ALREADY on the account —
        a child on a shared device. That person may well know, or be able to
        reach, the account password. Accepting it here would defeat the point.
        """
        res = client_for(self.user).post(
            CONTEXT_URL, {"password": PASSWORD}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()["code"], "bad_pin")


class TeacherModeGatesTest(TestCase):
    """Pre-existing gates, previously untested. Unchanged by this phase."""

    def test_account_with_no_teacher_identity(self):
        user = User.objects.create_user(
            username="learner", email="l@test.com", password=PASSWORD,
        )
        User.objects.filter(pk=user.pk).update(is_verified=True)
        res = client_for(user).post(CONTEXT_URL, {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.json()["code"], "no_teacher")

    def test_pending_application_cannot_enter(self):
        user, _tp = make_teacher(approved=False,
                                 track=TeacherProfile.TRACK_ACADEMY)
        res = client_for(user).post(CONTEXT_URL, {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["code"], "not_approved")

    def test_asking_for_a_track_still_in_review(self):
        user, tp = make_teacher(track=TeacherProfile.TRACK_SKILL)
        tp.set_track_status(TeacherProfile.TRACK_ACADEMY,
                            TeacherProfile.TRACK_PENDING)
        tp.save()
        res = client_for(user).post(CONTEXT_URL, {"track": "academy"},
                                    format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["code"], "track_pending")

    def test_asking_for_a_track_never_held(self):
        user, _tp = make_teacher(track=TeacherProfile.TRACK_SKILL)
        res = client_for(user).post(CONTEXT_URL, {"track": "academy"},
                                    format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["code"], "track_locked")

    def test_unknown_track_is_a_400(self):
        user, _tp = make_teacher()
        res = client_for(user).post(CONTEXT_URL, {"track": "nonsense"},
                                    format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


class TeacherPinManagementTest(TestCase):
    def setUp(self):
        self.user, self.tp = make_teacher()

    def test_reports_whether_a_pin_is_set(self):
        self.assertFalse(client_for(self.user).get(PIN_URL).json()["requires_pin"])
        self.tp.set_pin("1234")
        self.tp.save(update_fields=["pin"])
        self.assertTrue(client_for(self.user).get(PIN_URL).json()["requires_pin"])

    def test_setting_a_pin_requires_the_account_password(self):
        res = client_for(self.user).post(PIN_URL, {"pin": "1234"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.tp.refresh_from_db()
        self.assertFalse(self.tp.has_pin())

    def test_wrong_account_password_refused(self):
        res = client_for(self.user).post(
            PIN_URL, {"pin": "1234", "password": "wrong-one"}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.tp.refresh_from_db()
        self.assertFalse(self.tp.has_pin())

    def test_setting_a_pin(self):
        res = client_for(self.user).post(
            PIN_URL, {"pin": "1234", "password": PASSWORD}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.json()["requires_pin"])
        self.tp.refresh_from_db()
        self.assertTrue(self.tp.has_pin())
        self.assertTrue(self.tp.check_pin("1234"))
        # Stored hashed, never in the clear.
        self.assertNotEqual(self.tp.pin, "1234")

    def test_forgot_pin_resets_without_the_old_one(self):
        self.tp.set_pin("1111")
        self.tp.save(update_fields=["pin"])
        res = client_for(self.user).post(
            PIN_URL, {"pin": "2222", "password": PASSWORD}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.tp.refresh_from_db()
        self.assertTrue(self.tp.check_pin("2222"))

    def test_clearing_the_pin(self):
        self.tp.set_pin("1111")
        self.tp.save(update_fields=["pin"])
        res = client_for(self.user).post(
            PIN_URL, {"pin": "", "password": PASSWORD}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.json()["requires_pin"])
        self.tp.refresh_from_db()
        self.assertFalse(self.tp.has_pin())
        # And entry is open again.
        self.assertEqual(
            client_for(self.user).post(CONTEXT_URL, {}, format="json").status_code,
            status.HTTP_200_OK,
        )

    def test_pin_must_be_four_to_six_digits(self):
        for bad in ("123", "1234567", "abcd", "12a4"):
            res = client_for(self.user).post(
                PIN_URL, {"pin": bad, "password": PASSWORD}, format="json",
            )
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, bad)
        self.tp.refresh_from_db()
        self.assertFalse(self.tp.has_pin())

    def test_learner_account_has_no_teacher_pin(self):
        user = User.objects.create_user(
            username="learner", email="l@test.com", password=PASSWORD,
        )
        User.objects.filter(pk=user.pk).update(is_verified=True)
        self.assertEqual(client_for(user).get(PIN_URL).status_code,
                         status.HTTP_409_CONFLICT)
        self.assertEqual(
            client_for(user).post(PIN_URL, {"pin": "1234", "password": PASSWORD},
                                  format="json").status_code,
            status.HTTP_409_CONFLICT,
        )

    def test_profile_pin_and_teacher_pin_are_independent(self):
        profile = self.user.learner_profiles.get()
        client_for(self.user).post(
            "/api/accounts/profiles/pin/",
            {"profile_id": str(profile.id), "pin": "9999", "password": PASSWORD},
            format="json",
        )
        client_for(self.user).post(
            PIN_URL, {"pin": "1234", "password": PASSWORD}, format="json",
        )
        profile.refresh_from_db()
        self.tp.refresh_from_db()
        self.assertTrue(profile.check_pin("9999"))
        self.assertFalse(profile.check_pin("1234"))
        self.assertTrue(self.tp.check_pin("1234"))
        self.assertFalse(self.tp.check_pin("9999"))
