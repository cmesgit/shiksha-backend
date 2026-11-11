"""RC-005 — database constraints for the collaboration schema.

These test the *database*, not a service layer. Every assertion below drives
an insert or update to the point where the engine refuses it, because the
whole value of these constraints is that they hold even when a future caller
forgets the service helper. A test that only exercised a Python guard would
prove nothing about the invariant.

⚠ Engine coverage: this suite runs under `config.settings_test`, which is
SQLite. SQLite enforces CHECK constraints and partial unique indexes, so the
shape assertions below are real. It does **not** reproduce PostgreSQL row
locking, so the concurrent two-room approval race in RC-005's acceptance
criteria is **not** covered here — see evidence/collaboration-schema.md.
Do not read a green run as proof of the locking behaviour.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.models import User

from .models import (
    CollaborationAuditEvent,
    FileTransfer,
    GroupSession,
    PairedDevice,
    PrivateSession,
    RemoteControlGrant,
)


def _session(host):
    """A live instant GroupSession.

    `short_code` is unique with a default of "" — two rows created without one
    collide on the empty string, so every fixture must supply its own.
    """
    now = timezone.localtime()
    return GroupSession.objects.create(
        host=host, topic="Collab", scheduled_date=now.date(),
        scheduled_time=now.time().replace(microsecond=0),
        duration_minutes=60, session_type="instant", status="live",
        short_code=uuid.uuid4().hex[:12],
    )


def _private(teacher, requester):
    now = timezone.localtime()
    return PrivateSession.objects.create(
        teacher=teacher, requested_by=requester,
        scheduled_date=now.date(),
        scheduled_time=now.time().replace(microsecond=0),
    )


class CollaborationSchemaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user(
            username="rc005_alice", email="a@rc005.test", password="pw")
        cls.bob = User.objects.create_user(
            username="rc005_bob", email="b@rc005.test", password="pw")
        cls.session = _session(cls.alice)
        cls.device = PairedDevice.objects.create(
            owner=cls.bob, name="Bob's laptop", platform="windows", arch="x64",
            public_jwk={"kty": "OKP"}, key_thumbprint="thumb-rc005-1",
            status=PairedDevice.STATUS_PAIRED,
        )

    def _v1(self, **over):
        """A minimally-valid v1 grant. Override one field to test one rule."""
        values = dict(
            schema_version=RemoteControlGrant.SCHEMA_V1,
            public_id=uuid.uuid4(),
            session=self.session,
            controller=self.alice,
            target=self.bob,
            device=self.device,
            controller_binding_id=uuid.uuid4(),
            host_binding_id=uuid.uuid4(),
            status=RemoteControlGrant.STATUS_REQUESTED,
            request_expires_at=timezone.now() + timedelta(seconds=60),
        )
        values.update(over)
        return RemoteControlGrant.objects.create(**values)

    # -- exactly one session FK ------------------------------------------

    def test_a_grant_with_no_session_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._v1(session=None)

    def test_a_grant_with_two_sessions_is_refused(self):
        private = _private(self.bob, self.alice)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._v1(private_session=private)

    def test_each_session_kind_is_individually_valid(self):
        """The constraint must permit all four shapes, not just group."""
        grant = self._v1()
        self.assertEqual(grant.session_ref()["kind"], "group")

        private = _private(self.bob, self.alice)
        other_device = PairedDevice.objects.create(
            owner=self.bob, name="second", platform="linux", arch="x64",
            public_jwk={}, key_thumbprint="thumb-rc005-2",
            status=PairedDevice.STATUS_PAIRED,
        )
        g2 = self._v1(session=None, private_session=private, device=other_device)
        self.assertEqual(g2.session_ref(), {"kind": "private", "id": str(private.id)})

    # -- v1 completeness --------------------------------------------------

    def test_v1_without_a_device_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._v1(device=None)

    def test_v1_without_bindings_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._v1(host_binding_id=None)

    def test_v1_without_a_public_id_is_refused(self):
        """The UUID is the wire identity; an integer PK must never stand in."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._v1(public_id=None)

    def test_controlling_your_own_account_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._v1(target=self.alice)

    # -- the legacy path must keep working --------------------------------

    def test_a_legacy_row_still_inserts_with_none_of_the_v1_fields(self):
        """This is what lets RC-005 ship without a backfill or a code change.

        remote_control_views.py is untouched and still creates rows exactly
        like this one. If this test fails, the migration is not additive and
        the existing teacher→student path is broken.
        """
        legacy = RemoteControlGrant.objects.create(
            session=self.session, controller=self.alice, target=self.bob,
        )
        self.assertEqual(legacy.schema_version, RemoteControlGrant.SCHEMA_LEGACY)
        self.assertIsNone(legacy.public_id)
        self.assertIsNone(legacy.device)
        self.assertFalse(legacy.is_v1)

    def test_legacy_rows_do_not_occupy_the_device_slot(self):
        """A legacy row has no device, so it cannot block a v1 grant."""
        RemoteControlGrant.objects.create(
            session=self.session, controller=self.alice, target=self.bob,
            status=RemoteControlGrant.STATUS_ACTIVE,
        )
        self._v1(status=RemoteControlGrant.STATUS_ACTIVE)  # must not raise

    # -- one live grant per device, across rooms --------------------------

    def test_a_device_cannot_hold_two_live_grants_in_different_rooms(self):
        """The two-room race's invariant, asserted at the database level.

        Serial here rather than concurrent — SQLite cannot reproduce the
        PostgreSQL lock. What this proves is that even if two transactions
        both got past an application check, the second insert is still
        refused.
        """
        self._v1(status=RemoteControlGrant.STATUS_ACTIVE)
        other_room = _session(self.alice)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._v1(session=other_room, status=RemoteControlGrant.STATUS_APPROVED)

    def test_the_device_slot_frees_up_on_a_terminal_status(self):
        first = self._v1(status=RemoteControlGrant.STATUS_ACTIVE)
        first.status = RemoteControlGrant.STATUS_COMPLETED
        first.save(update_fields=["status"])
        self._v1(status=RemoteControlGrant.STATUS_ACTIVE)  # must not raise

    def test_requested_does_not_reserve_the_device(self):
        """A request is not a reservation — api-contracts.md says so."""
        self._v1(status=RemoteControlGrant.STATUS_REQUESTED)
        self._v1(controller=self.bob, target=self.alice,
                 status=RemoteControlGrant.STATUS_REQUESTED)

    def test_one_controller_cannot_flood_a_device_with_requests(self):
        self._v1(status=RemoteControlGrant.STATUS_REQUESTED)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._v1(status=RemoteControlGrant.STATUS_REQUESTED)

    # -- public_id uniqueness ---------------------------------------------

    def test_public_ids_are_unique(self):
        shared = uuid.uuid4()
        self._v1(public_id=shared)
        other_device = PairedDevice.objects.create(
            owner=self.bob, name="third", platform="macos", arch="arm64",
            public_jwk={}, key_thumbprint="thumb-rc005-3",
            status=PairedDevice.STATUS_PAIRED,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._v1(public_id=shared, device=other_device)

    # -- capabilities default ---------------------------------------------

    def test_capabilities_default_to_empty_not_permissive(self):
        """"No defaults granting input" — the safe state is the empty list."""
        self.assertEqual(self._v1().capabilities, [])


class PairedDeviceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username="rc005_owner", email="o@rc005.test", password="pw")

    def test_key_thumbprints_are_unique_across_accounts(self):
        other = User.objects.create_user(
            username="rc005_other", email="o2@rc005.test", password="pw")
        PairedDevice.objects.create(
            owner=self.owner, name="one", platform="windows", arch="x64",
            public_jwk={}, key_thumbprint="shared-thumb")
        with self.assertRaises(IntegrityError), transaction.atomic():
            PairedDevice.objects.create(
                owner=other, name="two", platform="windows", arch="x64",
                public_jwk={}, key_thumbprint="shared-thumb")

    def test_a_new_device_is_pending_not_paired(self):
        device = PairedDevice.objects.create(
            owner=self.owner, name="fresh", platform="linux", arch="x64",
            public_jwk={}, key_thumbprint="fresh-thumb")
        self.assertEqual(device.status, PairedDevice.STATUS_PENDING)
        self.assertFalse(device.is_usable)

    def test_is_usable_ignores_last_seen(self):
        """Liveness is Redis's job; this column must never gate authority."""
        device = PairedDevice.objects.create(
            owner=self.owner, name="stale", platform="linux", arch="x64",
            public_jwk={}, key_thumbprint="stale-thumb",
            status=PairedDevice.STATUS_PAIRED,
            last_seen_at=timezone.now() - timedelta(days=30))
        self.assertTrue(device.is_usable)

    def test_a_revoked_device_is_not_usable(self):
        device = PairedDevice.objects.create(
            owner=self.owner, name="gone", platform="linux", arch="x64",
            public_jwk={}, key_thumbprint="gone-thumb",
            status=PairedDevice.STATUS_REVOKED)
        self.assertFalse(device.is_usable)


class FileTransferTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = User.objects.create_user(
            username="rc005_fa", email="fa@rc005.test", password="pw")
        cls.b = User.objects.create_user(
            username="rc005_fb", email="fb@rc005.test", password="pw")
        cls.session = _session(cls.a)

    def _transfer(self, **over):
        values = dict(
            session=self.session, sender=self.a, recipient=self.b,
            mode=FileTransfer.MODE_REALTIME, name="notes.pdf",
            size_bytes=1024, sha256="a" * 64,
            offer_expires_at=timezone.now() + timedelta(seconds=60),
        )
        values.update(over)
        return FileTransfer.objects.create(**values)

    def test_sending_to_yourself_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._transfer(recipient=self.a)

    def test_a_transfer_needs_exactly_one_session(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._transfer(session=None)

    def test_stream_ids_are_unique(self):
        shared = uuid.uuid4()
        self._transfer(stream_id=shared)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._transfer(stream_id=shared)

    def test_recipient_is_required(self):
        """recipient=NULL must never be reachable as "everyone"."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._transfer(recipient=None)


class CollaborationAuditEventTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = User.objects.create_user(
            username="rc005_audit", email="au@rc005.test", password="pw")

    def _event(self, **over):
        values = dict(
            actor_user=self.actor, event_type="grant.activated",
            resource_type="grant", resource_id="r-1", resource_version=1,
        )
        values.update(over)
        return CollaborationAuditEvent.objects.create(**values)

    def test_the_same_transition_cannot_be_logged_twice(self):
        """Makes the audit write idempotent under a retried transaction."""
        self._event()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._event()

    def test_a_later_version_of_the_same_resource_is_a_new_row(self):
        self._event(resource_version=1)
        self._event(resource_version=2)
        self.assertEqual(CollaborationAuditEvent.objects.count(), 2)

    def test_deleting_the_actor_keeps_the_record(self):
        """SET_NULL, not CASCADE — deleting an account must not erase history."""
        event_id = self._event().id
        self.actor.delete()
        row = CollaborationAuditEvent.objects.get(id=event_id)
        self.assertIsNone(row.actor_user_id)
        self.assertEqual(row.event_type, "grant.activated")
