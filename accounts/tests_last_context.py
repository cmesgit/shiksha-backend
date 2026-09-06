"""
Tests for "land where you left off" (Phase 5).

The picker used to appear for ANY account with a teacher identity — even one
learner profile and one approved track — because "which person am I" and "am I
teaching right now" were treated as the same question. They are not, and this
is the split.

The restore is a CONVENIENCE, never an authority. Most of these tests are
about what must STILL stop it: a revoked track, a deactivated profile, a PIN.
"""
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import (
    LearnerProfile, Role, TeacherProfile, User, UserRole,
)

LOGIN_URL = "/api/accounts/login/"
PASSWORD  = "pw-1234-abcd"


def make_account(email="p@test.com", profiles=("Me",)):
    user = User.objects.create_user(
        username=email.split("@")[0], email=email, password=PASSWORD,
    )
    User.objects.filter(pk=user.pk).update(is_verified=True)
    user.refresh_from_db()
    made = []
    for i, name in enumerate(profiles):
        made.append(LearnerProfile.objects.create(
            account=user, display_name=name,
            relationship=(LearnerProfile.RELATIONSHIP_SELF if i == 0
                          else LearnerProfile.RELATIONSHIP_DEPENDENT),
            is_default=(i == 0),
        ))
    role, _ = Role.objects.get_or_create(name=Role.STUDENT)
    UserRole.objects.get_or_create(user=user, role=role, defaults={"is_active": True})
    return user, made


def give_teacher(user, track=TeacherProfile.TRACK_SKILL, approved=True):
    tp = TeacherProfile(user=user)
    tp.set_track_status(track, TeacherProfile.TRACK_APPROVED if approved
                        else TeacherProfile.TRACK_PENDING)
    tp.sync_type_from_tracks()
    tp.save()
    role, _ = Role.objects.get_or_create(name=Role.TEACHER)
    UserRole.objects.get_or_create(user=user, role=role, defaults={"is_active": approved})
    if approved:
        UserRole.objects.filter(user=user, role=role).update(is_active=True)
    return tp


def login(email="p@test.com"):
    return APIClient().post(LOGIN_URL, {"email": email, "password": PASSWORD},
                            format="json")


class TeacherIdentityNoLongerForcesThePickerTest(TestCase):
    """The headline change."""

    def test_one_profile_plus_teacher_identity_auto_selects(self):
        user, _ = make_account()
        give_teacher(user)
        res = login()
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        # Previously this was "account" — the picker — on every single login.
        self.assertEqual(res.json()["context"], "learner")
        self.assertTrue(res.json()["auto_selected"])

    def test_several_profiles_still_show_the_picker(self):
        make_account(profiles=("Me", "Kid"))
        self.assertEqual(login().json()["context"], "account")

    def test_a_pin_still_shows_the_picker(self):
        user, profiles = make_account()
        profiles[0].set_pin("4321")
        profiles[0].save()
        self.assertEqual(login().json()["context"], "account")


class RestoreLastContextTest(TestCase):
    def test_teacher_context_is_restored(self):
        user, _ = make_account()
        give_teacher(user, track=TeacherProfile.TRACK_SKILL)
        user.last_context = "teacher"
        user.last_track = "skill"
        user.save(update_fields=["last_context", "last_track"])

        res = login()
        self.assertEqual(res.json()["context"], "teacher")
        self.assertEqual(res.json()["teacher"]["active_track"], "skill")

    def test_last_learner_profile_is_restored_over_the_default(self):
        user, profiles = make_account(profiles=("Me", "Kid"))
        user.last_context = "learner"
        user.last_profile = profiles[1]
        user.save(update_fields=["last_context", "last_profile"])

        res = login()
        self.assertEqual(res.json()["context"], "learner")
        self.assertEqual(res.json()["profile"]["display_name"], "Kid")

    def test_selecting_a_profile_records_it(self):
        user, profiles = make_account(profiles=("Me", "Kid"))
        c = APIClient()
        c.post(LOGIN_URL, {"email": user.email, "password": PASSWORD}, format="json")
        c.post("/api/accounts/profiles/select/",
               {"profile_id": str(profiles[1].id)}, format="json")
        user.refresh_from_db()
        self.assertEqual(user.last_context, "learner")
        self.assertEqual(user.last_profile_id, profiles[1].id)

    def test_entering_teacher_mode_records_it(self):
        user, _ = make_account()
        give_teacher(user, track=TeacherProfile.TRACK_SKILL)
        c = APIClient()
        c.force_authenticate(user=user)
        c.post("/api/accounts/context/teacher/", {}, format="json")
        user.refresh_from_db()
        self.assertEqual(user.last_context, "teacher")
        self.assertEqual(user.last_track, "skill")


class RestoreIsNeverAnAuthorityTest(TestCase):
    """Every gate must still run. These are the ones that matter."""

    def test_revoked_track_falls_back(self):
        user, _ = make_account()
        tp = give_teacher(user, track=TeacherProfile.TRACK_SKILL)
        user.last_context = "teacher"; user.last_track = "skill"
        user.save(update_fields=["last_context", "last_track"])

        tp.set_track_status(TeacherProfile.TRACK_SKILL, TeacherProfile.TRACK_REJECTED)
        tp.sync_type_from_tracks()
        tp.save()

        res = login()
        self.assertNotEqual(res.json()["context"], "teacher")

    def test_inactive_teacher_role_falls_back(self):
        user, _ = make_account()
        give_teacher(user)
        user.last_context = "teacher"; user.last_track = "skill"
        user.save(update_fields=["last_context", "last_track"])
        UserRole.objects.filter(user=user, role__name=Role.TEACHER).update(is_active=False)

        self.assertNotEqual(login().json()["context"], "teacher")

    def test_teacher_pin_blocks_the_restore(self):
        """Restoring past the PIN would defeat the entire point of it —
        it exists to stop someone already holding the session."""
        user, _ = make_account()
        tp = give_teacher(user)
        tp.set_pin("4321")
        tp.save(update_fields=["pin"])
        user.last_context = "teacher"; user.last_track = "skill"
        user.save(update_fields=["last_context", "last_track"])

        res = login()
        self.assertNotEqual(res.json()["context"], "teacher")

    def test_deactivated_last_profile_falls_back(self):
        # THREE profiles on purpose. With only two, deactivating the remembered
        # one leaves a single active profile, and auto-selecting that is the
        # correct answer — so the test would pass without proving the restore
        # was refused. A third keeps a real choice on the table, so reaching
        # the picker is genuine evidence the deactivated profile was rejected.
        user, profiles = make_account(profiles=("Me", "Kid", "Other"))
        user.last_context = "learner"; user.last_profile = profiles[1]
        user.save(update_fields=["last_context", "last_profile"])
        profiles[1].is_active = False
        profiles[1].save(update_fields=["is_active"])

        res = login()
        self.assertEqual(res.json()["context"], "account")
        self.assertNotIn(
            "Kid", [p["display_name"] for p in res.json()["profiles"]],
        )

    def test_deactivating_down_to_one_profile_auto_selects_the_survivor(self):
        """The other half of the rule above, asserted explicitly so the
        behaviour is documented rather than incidental."""
        user, profiles = make_account(profiles=("Me", "Kid"))
        user.last_context = "learner"; user.last_profile = profiles[1]
        user.save(update_fields=["last_context", "last_profile"])
        profiles[1].is_active = False
        profiles[1].save(update_fields=["is_active"])

        res = login()
        self.assertEqual(res.json()["context"], "learner")
        self.assertEqual(res.json()["profile"]["display_name"], "Me")

    def test_pinned_last_profile_falls_back_to_the_picker(self):
        user, profiles = make_account(profiles=("Me", "Kid"))
        user.last_context = "learner"; user.last_profile = profiles[1]
        user.save(update_fields=["last_context", "last_profile"])
        profiles[1].set_pin("4321")
        profiles[1].save()

        self.assertEqual(login().json()["context"], "account")

    def test_deleted_last_profile_does_not_break_login(self):
        """SET_NULL on the FK — a deleted profile must not 500 the login."""
        user, profiles = make_account(profiles=("Me", "Kid"))
        user.last_context = "learner"; user.last_profile = profiles[1]
        user.save(update_fields=["last_context", "last_profile"])
        profiles[1].delete()

        res = login()
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertIsNone(user.last_profile_id)
