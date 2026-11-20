"""RC-008 part 2c — the legacy screen-control path is retired.

Three claims, and the third is the one that matters most:

1. ``request`` and ``respond`` refuse unconditionally. Not "refuse under the
   current configuration" — there must be no account state, role or flag
   that makes them create a row or open a gate.
2. ``revoke`` still works on legacy rows, because retiring the ways to start
   something while keeping the way to stop it is the correct asymmetry.
3. ``revoke`` cannot touch a **v1** grant. Before this change it could, and
   doing so bypassed the state machine: no version bump, no audit row, no
   notification, with both ends still showing live control.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from global_settings.models import GlobalSettings

from .models import (
    CollaborationAuditEvent,
    GroupSession,
    PairedDevice,
    RemoteControlGrant,
)


class LegacyRemoteControlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(
            username="legacy_teacher", email="t@legacy.test", password="pw")
        cls.student = User.objects.create_user(
            username="legacy_student", email="s@legacy.test", password="pw")

    def setUp(self):
        super().setUp()
        now = timezone.localtime()
        self.session = GroupSession.objects.create(
            host=self.teacher, topic="Legacy", scheduled_date=now.date(),
            scheduled_time=now.time().replace(microsecond=0),
            duration_minutes=60, session_type="scheduled", status="live",
            short_code=uuid.uuid4().hex[:12],
            room_name=f"room-{uuid.uuid4().hex[:8]}",
            room_started_at=timezone.now())

        # The legacy flag defaults True; leave it True so the tests prove the
        # refusal is unconditional rather than a side effect of a kill switch.
        conf = GlobalSettings.load()
        conf.live_remote_access_enabled = True
        conf.save(update_fields=["live_remote_access_enabled"])

        self.client = APIClient()

    def _url(self, name):
        return reverse(name, args=[self.session.id])

    def _legacy_grant(self, status=RemoteControlGrant.STATUS_ACTIVE):
        return RemoteControlGrant.objects.create(
            session=self.session, controller=self.teacher,
            target=self.student, status=status,
            schema_version=RemoteControlGrant.SCHEMA_LEGACY)

    def _v1_grant(self):
        device = PairedDevice.objects.create(
            owner=self.student, name="desk", platform="windows", arch="x64",
            public_jwk={"kty": "OKP"},
            key_thumbprint=f"t-{uuid.uuid4().hex[:8]}",
            status=PairedDevice.STATUS_PAIRED)
        return RemoteControlGrant.objects.create(
            session=self.session, controller=self.teacher,
            target=self.student, device=device,
            status=RemoteControlGrant.STATUS_ACTIVE,
            schema_version=RemoteControlGrant.SCHEMA_V1,
            public_id=uuid.uuid4(), version=4,
            controller_binding_id=uuid.uuid4(),
            host_binding_id=uuid.uuid4(),
            epoch=uuid.uuid4(),
            expires_at=timezone.now() + timedelta(minutes=5))

    # -- 1. the two retired endpoints -------------------------------------

    def test_request_is_retired_for_everyone(self):
        for user in (self.teacher, self.student):
            with self.subTest(user=user.username):
                self.client.force_authenticate(user)
                res = self.client.post(
                    self._url("group-session-remote-control-request"),
                    {"target_user_id": self.student.id}, format="json")
                self.assertEqual(res.status_code, 410)
                self.assertEqual(res.data["code"],
                                 "legacy_remote_control_retired")

    def test_request_creates_no_row(self):
        self.client.force_authenticate(self.teacher)
        self.client.post(self._url("group-session-remote-control-request"),
                         {"target_user_id": self.student.id}, format="json")
        self.assertEqual(RemoteControlGrant.objects.count(), 0)

    def test_respond_cannot_activate_anything(self):
        """🔴 The endpoint the retirement is actually about.

        A browser click must never be sufficient to open an injection gate.
        The grant must still be `requested` afterwards.
        """
        grant = self._legacy_grant(status=RemoteControlGrant.STATUS_REQUESTED)
        self.client.force_authenticate(self.student)
        res = self.client.post(
            self._url("group-session-remote-control-respond"),
            {"grant_id": grant.id, "allow": True}, format="json")
        self.assertEqual(res.status_code, 410)
        grant.refresh_from_db()
        self.assertEqual(grant.status, RemoteControlGrant.STATUS_REQUESTED)

    def test_the_legacy_flag_does_not_resurrect_the_endpoints(self):
        """Turning the old switch ON must not bring the old behaviour back."""
        conf = GlobalSettings.load()
        conf.live_remote_access_enabled = True
        conf.save(update_fields=["live_remote_access_enabled"])
        self.client.force_authenticate(self.teacher)
        res = self.client.post(
            self._url("group-session-remote-control-request"),
            {"target_user_id": self.student.id}, format="json")
        self.assertEqual(res.status_code, 410)

    # -- 2. revoke still works on legacy rows ------------------------------

    def test_revoke_still_stops_a_legacy_grant(self):
        """Retiring the ways to start something while keeping the way to
        stop it is the correct asymmetry."""
        grant = self._legacy_grant()
        self.client.force_authenticate(self.student)
        res = self.client.post(
            self._url("group-session-remote-control-revoke"),
            {"grant_id": grant.id}, format="json")
        self.assertEqual(res.status_code, 200)
        grant.refresh_from_db()
        self.assertEqual(grant.status, RemoteControlGrant.STATUS_REVOKED)
        self.assertEqual(grant.ended_by, "target")

    def test_a_stranger_still_cannot_revoke(self):
        grant = self._legacy_grant()
        stranger = User.objects.create_user(
            username="legacy_stranger", email="x@legacy.test", password="pw")
        self.client.force_authenticate(stranger)
        res = self.client.post(
            self._url("group-session-remote-control-revoke"),
            {"grant_id": grant.id}, format="json")
        self.assertEqual(res.status_code, 403)

    # -- 3. the hole this closes ------------------------------------------

    def test_a_v1_grant_cannot_be_revoked_through_the_legacy_endpoint(self):
        """🔴 The real finding.

        Before this change the lookup had no schema_version filter, so a v1
        grant could be terminated here — bypassing the state machine, so no
        version bump, no audit row and no notification. Both ends would keep
        showing live control over a revoked grant until the lease expired.
        """
        grant = self._v1_grant()
        self.client.force_authenticate(self.student)
        res = self.client.post(
            self._url("group-session-remote-control-revoke"),
            {"grant_id": grant.id}, format="json")

        self.assertEqual(res.status_code, 404)
        grant.refresh_from_db()
        self.assertEqual(grant.status, RemoteControlGrant.STATUS_ACTIVE)
        self.assertEqual(grant.version, 4, "version must not have moved")
        self.assertFalse(
            CollaborationAuditEvent.objects.filter(
                resource_id=str(grant.public_id)).exists())

    def test_the_host_cannot_reach_a_v1_grant_either(self):
        """The host has the broadest legacy permission, so it is the one
        most likely to slip past a narrower fix."""
        grant = self._v1_grant()
        self.client.force_authenticate(self.teacher)
        res = self.client.post(
            self._url("group-session-remote-control-revoke"),
            {"grant_id": grant.id}, format="json")
        self.assertEqual(res.status_code, 404)
        grant.refresh_from_db()
        self.assertEqual(grant.status, RemoteControlGrant.STATUS_ACTIVE)
