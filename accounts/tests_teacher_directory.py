"""
Tests for the Academy teacher directory gate (accounts/views.py
TeacherListView + TeacherPublicProfileView).

The directory used to filter on `is_approved`, which is "approved on ANY
track". Because the Skill track auto-approves at signup with no human review,
that listed every self-registered guest expert as bookable school faculty —
false credentialing, and a dead end besides (the private-session request
re-validates against TeachingAssignment and 400s for them).

The DM gate in chat.services.teacher_is_public_faculty stays deliberately
WIDER than this; see chat/tests/test_m3_policy.py.

Run with:
    DJANGO_SETTINGS_MODULE=config.settings_test .venv/bin/python manage.py test \
        accounts.tests_teacher_directory -v2
"""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import (
    LearnerProfile, Role, TeacherProfile, User, UserRole,
)


class TeacherDirectoryTrackGateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher_role, _ = Role.objects.get_or_create(name="TEACHER")

        cls.viewer = User.objects.create_user(
            username="dir_viewer", email="dir_viewer@example.com", password="whatever-9",
        )
        cls.viewer_profile = LearnerProfile.objects.create(
            account=cls.viewer, display_name="Viewer", relationship="SELF", is_default=True,
        )

        cls.faculty = cls._teacher("dir_faculty", academy=TeacherProfile.TRACK_APPROVED)
        cls.guest = cls._teacher("dir_guest", skill=TeacherProfile.TRACK_APPROVED)
        cls.both = cls._teacher(
            "dir_both",
            academy=TeacherProfile.TRACK_APPROVED,
            skill=TeacherProfile.TRACK_APPROVED,
        )
        cls.pending = cls._teacher("dir_pending", academy=TeacherProfile.TRACK_PENDING)

    @classmethod
    def _teacher(cls, username, academy=TeacherProfile.TRACK_LOCKED,
                 skill=TeacherProfile.TRACK_LOCKED):
        user = User.objects.create_user(
            username=username, email=f"{username}@example.com", password="whatever-9",
        )
        UserRole.objects.create(
            user=user, role=cls.teacher_role, is_active=True, is_primary=True,
        )
        LearnerProfile.objects.create(
            account=user, display_name=username, relationship="SELF", is_default=True,
        )
        tp = TeacherProfile.objects.create(
            user=user, academy_status=academy, skill_status=skill,
        )
        # Mirrors what signup does — is_approved is derived, and is exactly the
        # field the old (buggy) filter trusted.
        tp.sync_type_from_tracks()
        tp.save()
        return tp

    def _client(self):
        client = APIClient()
        client.force_authenticate(
            user=self.viewer,
            token={"context": "learner", "active_profile": str(self.viewer_profile.id)},
        )
        return client

    # ── the bug this file exists for ─────────────────────────────────────
    def test_auto_approved_guest_expert_is_not_listed_as_faculty(self):
        ids = {row["id"] for row in self._client().get("/api/accounts/teachers/").data}
        self.assertNotIn(str(self.guest.user_id), ids)

    def test_guest_expert_detail_is_404_not_a_faculty_profile(self):
        r = self._client().get(f"/api/accounts/teachers/{self.guest.user_id}/")
        self.assertEqual(r.status_code, 404)

    def test_the_old_filter_would_have_listed_that_guest(self):
        """Guards the premise: if is_approved ever stops being true for an
        auto-approved skill expert, the tests above stop proving anything."""
        self.guest.refresh_from_db()
        self.assertTrue(self.guest.is_approved)
        self.assertFalse(self.guest.is_academy_faculty)

    # ── who SHOULD still be there ────────────────────────────────────────
    def test_academy_faculty_is_listed(self):
        ids = {row["id"] for row in self._client().get("/api/accounts/teachers/").data}
        self.assertIn(str(self.faculty.user_id), ids)

    def test_dual_track_teacher_is_listed(self):
        """Holding skill approval too must not exclude real faculty."""
        ids = {row["id"] for row in self._client().get("/api/accounts/teachers/").data}
        self.assertIn(str(self.both.user_id), ids)

    def test_pending_academy_application_is_not_listed(self):
        ids = {row["id"] for row in self._client().get("/api/accounts/teachers/").data}
        self.assertNotIn(str(self.pending.user_id), ids)

    def test_academy_faculty_detail_still_resolves(self):
        r = self._client().get(f"/api/accounts/teachers/{self.faculty.user_id}/")
        self.assertEqual(r.status_code, 200)
