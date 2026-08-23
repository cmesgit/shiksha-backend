"""
Server-side gates on the two self-service profile-edit endpoints.

Both halves of this file cover findings from the 2026-08-23 academy-dashboard
audit (§10) where a rule the FRONTEND believed in had no server behind it:

1. TeacherProfileView.patch — "verification documents lock once the Academy
   application is approved" was drawn by FacultyProfile.jsx's `docsLocked` and
   enforced nowhere else, so an approved teacher could PATCH new credentials
   over vetted ones. Same view also `setattr`'d every field raw, with no
   choice validation, no length check and no date parsing.

2. The student's Edit Profile screen PATCHed /accounts/me/, which is GET-only,
   so DRF 405'd and the frontend swallowed it into console.error — the screen
   reported success and saved nothing. The fix repoints it at
   ProfileDetailView; these tests pin the contract that fix now depends on so
   nobody quietly narrows it again.
"""
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import (
    LearnerProfile, Role, TeacherProfile, User, UserRole,
)


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


class TeacherDocumentLockTest(TestCase):
    """The compliance lock is server-enforced, and only once APPROVED."""

    URL = "/api/accounts/teacher/profile/"

    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")

    def make_teacher(self, email, academy_status):
        user = User.objects.create_user(username=email, email=email, password="x")
        UserRole.objects.create(
            user=user, role=self.teacher_role, is_active=True, is_primary=True,
        )
        LearnerProfile.objects.create(
            account=user, display_name=email.split("@")[0], is_default=True,
        )
        TeacherProfile.objects.create(user=user, academy_status=academy_status)
        return user

    def test_approved_teacher_cannot_replace_id_number(self):
        user = self.make_teacher("approved@t.com", TeacherProfile.TRACK_APPROVED)
        res = _client(user).patch(self.URL, {"id_number": "NEW-1234"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        user.teacher_profile.refresh_from_db()
        self.assertEqual(user.teacher_profile.id_number, "")

    def test_approved_teacher_cannot_replace_govt_id_type(self):
        user = self.make_teacher("approved2@t.com", TeacherProfile.TRACK_APPROVED)
        res = _client(user).patch(self.URL, {"govt_id_type": "pan"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_approved_teacher_cannot_upload_a_new_signed_agreement(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        user = self.make_teacher("approved3@t.com", TeacherProfile.TRACK_APPROVED)
        res = _client(user).patch(
            self.URL,
            {"signed_agreement": SimpleUploadedFile("a.pdf", b"%PDF-1.4 swapped")},
            format="multipart",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        user.teacher_profile.refresh_from_db()
        self.assertFalse(user.teacher_profile.signed_agreement)

    def test_approved_teacher_can_still_edit_unlocked_fields(self):
        """The lock is on the documents section only — not the whole profile."""
        user = self.make_teacher("approved4@t.com", TeacherProfile.TRACK_APPROVED)
        res = _client(user).patch(self.URL, {"bio": "Physics, 12 years."}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        user.teacher_profile.refresh_from_db()
        self.assertEqual(user.teacher_profile.bio, "Physics, 12 years.")

    def test_unapproved_teacher_may_still_submit_documents(self):
        """Gate is academy_status, not is_approved.

        A Skill expert is auto-approved at signup and holds the TEACHER role,
        so gating on `is_approved` would lock them out of ever completing a
        faculty application they have not started.
        """
        user = self.make_teacher("pending@t.com", TeacherProfile.TRACK_PENDING)
        res = _client(user).patch(self.URL, {"id_number": "ABC-9999"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        user.teacher_profile.refresh_from_db()
        self.assertEqual(user.teacher_profile.id_number, "ABC-9999")

    def test_skill_expert_with_locked_academy_track_may_submit_documents(self):
        user = self.make_teacher("expert@t.com", TeacherProfile.TRACK_LOCKED)
        user.teacher_profile.skill_status = TeacherProfile.TRACK_APPROVED
        user.teacher_profile.save()
        res = _client(user).patch(self.URL, {"govt_id_type": "aadhaar"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)


class TeacherProfileValidationTest(TestCase):
    """setattr-with-no-validation used to write garbage and 500 on bad dates."""

    URL = "/api/accounts/teacher/profile/"

    @classmethod
    def setUpTestData(cls):
        role = Role.objects.create(name="TEACHER")
        cls.user = User.objects.create_user(username="t", email="t@t.com", password="x")
        UserRole.objects.create(user=cls.user, role=role, is_active=True, is_primary=True)
        cls.profile = LearnerProfile.objects.create(
            account=cls.user, display_name="T", is_default=True,
        )
        TeacherProfile.objects.create(user=cls.user)

    def test_dd_mm_yyyy_date_is_a_400_not_a_500(self):
        res = _client(self.user).patch(
            self.URL, {"date_of_birth": "31/12/1990"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("date_of_birth", res.data)

    def test_invalid_gender_choice_is_rejected(self):
        res = _client(self.user).patch(self.URL, {"gender": "xyz"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.gender, "")

    def test_invalid_highest_degree_choice_is_rejected(self):
        res = _client(self.user).patch(
            self.URL, {"highest_degree": "not-a-degree"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_overlong_string_is_rejected(self):
        res = _client(self.user).patch(
            self.URL, {"field_of_study": "x" * 500}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_nothing_is_written_when_one_field_fails(self):
        """Errors are collected and raised before either model is saved."""
        res = _client(self.user).patch(
            self.URL, {"first_name": "Asha", "gender": "xyz"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.first_name, "")

    def test_valid_payload_still_saves(self):
        res = _client(self.user).patch(
            self.URL,
            {"first_name": "Asha", "gender": "female", "date_of_birth": "1990-12-31",
             "highest_degree": "masters", "teaching_certifications": ["CTET"]},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.profile.refresh_from_db()
        self.user.teacher_profile.refresh_from_db()
        self.assertEqual(self.profile.first_name, "Asha")
        self.assertEqual(str(self.profile.date_of_birth), "1990-12-31")
        self.assertEqual(self.user.teacher_profile.teaching_certifications, ["CTET"])


class StudentEditProfileEndpointTest(TestCase):
    """The endpoint the student's Edit Profile screen was repointed at.

    /accounts/me/ is GET-only — the old target — and ProfileDetailView is the
    one that actually persists a learner profile's name, about text and avatar.
    """

    @classmethod
    def setUpTestData(cls):
        role = Role.objects.create(name="STUDENT")
        cls.account = User.objects.create_user(
            username="parent", email="p@t.com", password="x")
        UserRole.objects.create(user=cls.account, role=role, is_active=True, is_primary=True)
        cls.child = LearnerProfile.objects.create(
            account=cls.account, display_name="Aria", is_default=True)

    def url(self):
        return f"/api/accounts/profiles/{self.child.id}/"

    def test_me_is_get_only(self):
        """Documents WHY the old call saved nothing, so nobody re-points at it."""
        res = _client(self.account).patch(
            "/api/accounts/me/", {"username": "Aria S"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_name_about_and_emoji_avatar_persist(self):
        res = _client(self.account).patch(
            self.url(),
            {"display_name": "Aria S", "first_name": "Aria", "last_name": "S",
             "bio": "Loves geometry.", "avatar_emoji": "🦊"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.child.refresh_from_db()
        self.assertEqual(self.child.display_name, "Aria S")
        self.assertEqual(self.child.first_name, "Aria")
        self.assertEqual(self.child.bio, "Loves geometry.")
        self.assertEqual(self.child.avatar_emoji, "🦊")

    def test_overlong_bio_is_rejected(self):
        res = _client(self.account).patch(
            self.url(), {"bio": "x" * 400}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_another_accounts_profile_is_not_editable(self):
        stranger = User.objects.create_user(
            username="s", email="s@t.com", password="x")
        res = _client(stranger).patch(
            self.url(), {"display_name": "Hacked"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.child.refresh_from_db()
        self.assertEqual(self.child.display_name, "Aria")
