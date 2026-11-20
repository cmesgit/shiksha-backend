"""RC-008 part 2b — activation tickets and bounded leases.

What these are actually for: the lease is the only thing standing between a
revoked grant and continued OS input, and its entire safety property is the
three-second bound. So the assertions here are about *bounds and refusals* —
how long a lease can possibly be valid, and every condition under which one
must not be minted at all — rather than about JWT plumbing.

Mapped to the security test plan: T05 (ticket is not a licence to inject),
T06 (lease deadline), T08 (stale epoch), T11 (revoked device), T13 (kill
flag invalidates new leases), T14 (binding staleness), T17 (nonce binding),
T20 (unknown key).
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from global_settings.models import GlobalSettings

from .collaboration import bindings as binding_store
from .collaboration import grants, leases
from .collaboration.policy import Principal
from .models import GroupSession, PairedDevice, RemoteControlGrant


def _principal(user, context="learner"):
    return Principal(user=user, auth_sid=f"sid-{user.id}", context=context,
                     profile_id=None)


class LeaseBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user(
            username="lease_alice", email="a@lease.test", password="pw")
        cls.bob = User.objects.create_user(
            username="lease_bob", email="b@lease.test", password="pw")

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()

        now = timezone.localtime()
        self.session = GroupSession.objects.create(
            host=self.alice, topic="Lease", scheduled_date=now.date(),
            scheduled_time=now.time().replace(microsecond=0),
            duration_minutes=60, session_type="instant", status="live",
            short_code=uuid.uuid4().hex[:12],
            room_name=f"room-{uuid.uuid4().hex[:8]}",
            room_started_at=timezone.now())
        self.device = PairedDevice.objects.create(
            owner=self.bob, name="desk", platform="windows", arch="x64",
            public_jwk={"kty": "OKP"}, key_thumbprint=f"t-{uuid.uuid4().hex[:8]}",
            status=PairedDevice.STATUS_PAIRED)

        conf = GlobalSettings.load()
        conf.collaboration_bindings_enabled = True
        conf.collaboration_control_enabled = True
        conf.save(update_fields=["collaboration_bindings_enabled",
                                 "collaboration_control_enabled"])

        self.controller_binding = str(uuid.uuid4())
        self._write_binding(self.controller_binding)
        self.grant = self._active_grant()

    def _write_binding(self, binding_id, *, age=0.0):
        """A binding record as the store would hold it, optionally stale."""
        from django.core.cache import cache
        import json
        record = {
            "binding_id": binding_id,
            "user_id": str(self.alice.id),
            "auth_sid": f"sid-{self.alice.id}",
            "context": "learner",
            "profile_id": None,
            "session_kind": "group",
            "session_id": str(self.session.id),
            "room_name": self.session.room_name,
            "identity": f"{self.alice.id}_{self.session.id}",
            "participant_sid": "PA_test",
            "capabilities": ["pointer.move"],
            "created_at": time.time(),
            "heartbeat_at": time.time() - age,
        }
        cache.set(f"collab:binding:{binding_id}", json.dumps(record),
                  timeout=60)
        return record

    def _active_grant(self):
        grant = grants.request_control(
            principal=_principal(self.alice), session=self.session,
            device=self.device, target_user=self.bob,
            controller_binding_id=self.controller_binding,
            host_binding_id=uuid.uuid4(),
            capabilities=["pointer.move", "pointer.button"],
            room_name=self.session.room_name,
            controller_identity=f"{self.alice.id}_{self.session.id}",
            controller_sid="PA_ctrl")
        grants.decide(grant.public_id, principal=_principal(self.bob),
                      allow=True, capabilities=["pointer.move"],
                      expected_version=grant.version)
        grant.refresh_from_db()
        nonce = grants.issue_consent_nonce(grant.public_id, device=self.device)
        grant.refresh_from_db()
        return grants.activate(grant.public_id, device=self.device,
                               consent_nonce=nonce,
                               expected_version=grant.version,
                               capture_generation=3, local_confirm=True)

    def _lease(self, **over):
        kwargs = dict(device=self.device, epoch=self.grant.epoch,
                      expected_version=self.grant.version,
                      nonce=uuid.uuid4().hex)
        kwargs.update(over)
        return leases.mint_lease(self.grant.public_id, **kwargs)


class ActivationTicketTests(LeaseBase):

    def test_a_ticket_describes_the_row_not_the_request(self):
        token = leases.mint_activation(self.grant, device=self.device)
        claims = leases.verify_activation(
            token, expected_grant_id=self.grant.public_id,
            expected_epoch=self.grant.epoch)

        self.assertEqual(claims["grant_version"], self.grant.version)
        self.assertEqual(claims["capabilities"], ["pointer.move"])
        self.assertEqual(claims["device_id"], str(self.device.id))
        self.assertEqual(claims["device_thumbprint"],
                         str(self.device.key_thumbprint))
        self.assertEqual(claims["room_name"], self.session.room_name)
        self.assertEqual(claims["protocol"], 1)

    def test_the_approved_capability_subset_is_what_is_signed(self):
        """The host narrowed pointer.button away; the ticket must agree.

        A ticket carrying the *requested* set rather than the approved one
        would hand the companion a capability the owner declined.
        """
        token = leases.mint_activation(self.grant, device=self.device)
        claims = leases.verify_activation(token)
        self.assertNotIn("pointer.button", claims["capabilities"])

    def test_ticket_expiry_is_the_grant_hard_expiry(self):
        token = leases.mint_activation(self.grant, device=self.device)
        claims = leases.verify_activation(token)
        self.assertEqual(claims["exp"],
                         int(self.grant.expires_at.timestamp()))

    def test_a_non_active_grant_has_no_ticket(self):
        grants.terminate(self.grant.public_id, reason="user_stop",
                         actor_user=self.bob)
        self.grant.refresh_from_db()
        with self.assertRaises(grants.GrantError):
            leases.mint_activation(self.grant, device=self.device)

    def test_a_ticket_for_another_epoch_is_refused(self):
        token = leases.mint_activation(self.grant, device=self.device)
        with self.assertRaises(grants.GrantError) as caught:
            leases.verify_activation(token, expected_epoch=uuid.uuid4())
        self.assertEqual(caught.exception.code, "activation_stale_epoch")

    def test_a_ticket_signed_by_another_key_is_refused(self):
        """T20. Unknown kid must be rejected, not merely fail to verify.

        Both matter: a token signed by a stranger's key with *our* kid fails
        the signature; one with its own kid fails here, earlier and with a
        clearer answer.
        """
        stranger = Ed25519PrivateKey.generate()
        forged = jwt.encode(
            {"iss": "x", "aud": leases.AUDIENCE, "grant_id": str(uuid.uuid4()),
             "epoch": str(uuid.uuid4()), "grant_version": 1, "protocol": 1,
             "iat": int(time.time()), "nbf": int(time.time()),
             "exp": int(time.time()) + 600},
            stranger, algorithm="EdDSA",
            headers={"typ": leases.ACTIVATION_TYP, "kid": "not-our-kid"})
        with self.assertRaises(grants.GrantError) as caught:
            leases.verify_activation(forged)
        self.assertEqual(caught.exception.code, "activation_unknown_key")

    def test_an_hmac_token_cannot_impersonate_a_ticket(self):
        """🔴 The classic JWT confusion: a symmetric token offered where an
        asymmetric one is expected. The fixed algorithm list is what stops
        it, so assert it rather than assuming PyJWT's defaults."""
        forged = jwt.encode(
            {"aud": leases.AUDIENCE, "grant_id": str(self.grant.public_id),
             "epoch": str(self.grant.epoch), "grant_version": 1, "protocol": 1,
             "iat": int(time.time()), "nbf": int(time.time()),
             "exp": int(time.time()) + 600},
            "secret", algorithm="HS256",
            headers={"typ": leases.ACTIVATION_TYP, "kid": leases.key_id()})
        with self.assertRaises(grants.GrantError):
            leases.verify_activation(forged)

    def test_a_device_token_is_not_an_activation_ticket(self):
        """Wrong `typ` must be refused even when correctly signed by us."""
        token = jwt.encode(
            {"aud": leases.AUDIENCE, "iat": int(time.time()),
             "exp": int(time.time()) + 600},
            leases._signing_key(), algorithm="EdDSA",
            headers={"typ": "shiksha-device+jwt", "kid": leases.key_id()})
        with self.assertRaises(grants.GrantError):
            leases.verify_activation(token)


class LeaseBoundTests(LeaseBase):

    def test_a_lease_lasts_at_most_three_seconds(self):
        """T06. The whole safety property in one assertion."""
        token, _ = self._lease()
        claims = leases.verify_lease(token)
        self.assertLessEqual(claims["exp"] - claims["iat"],
                             leases.LEASE_TTL_SECONDS)

    def test_a_lease_echoes_the_outstanding_nonce(self):
        """T17. Without this a captured older reply would renew authority."""
        nonce = uuid.uuid4().hex
        token, _ = self._lease(nonce=nonce)
        claims = leases.verify_lease(token, expected_nonce=nonce)
        self.assertEqual(claims["nonce"], nonce)

        with self.assertRaises(grants.GrantError) as caught:
            leases.verify_lease(token, expected_nonce=uuid.uuid4().hex)
        self.assertEqual(caught.exception.code, "lease_nonce_mismatch")

    def test_a_missing_or_short_nonce_is_refused(self):
        for bad in (None, "", "short"):
            with self.subTest(nonce=bad):
                with self.assertRaises(grants.GrantError) as caught:
                    self._lease(nonce=bad)
                self.assertEqual(caught.exception.code, "lease_nonce_required")

    def test_lease_sequence_numbers_strictly_increase(self):
        """The companion rejects lower OR EQUAL seq, so a repeat would make
        it discard a legitimate renewal and pause."""
        seqs = []
        for _ in range(2):
            token, _ = self._lease()
            seqs.append(leases.verify_lease(token)["lease_seq"])
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertLess(seqs[0], seqs[1])

    def test_a_forged_longer_lease_is_refused(self):
        """A lease is only a bound if the bound is checked."""
        now = int(time.time())
        forged = jwt.encode(
            {"iss": "x", "aud": leases.AUDIENCE,
             "grant_id": str(self.grant.public_id),
             "epoch": str(self.grant.epoch), "lease_seq": 99,
             "nonce": uuid.uuid4().hex, "protocol": 1,
             "iat": now, "nbf": now, "exp": now + 600},
            leases._signing_key(), algorithm="EdDSA",
            headers={"typ": leases.LEASE_TYP, "kid": leases.key_id()})
        with self.assertRaises(grants.GrantError) as caught:
            leases.verify_lease(forged)
        self.assertIn("deadline", caught.exception.detail)

    def test_a_lease_binds_to_its_activation_ticket(self):
        ticket = leases.mint_activation(self.grant, device=self.device)
        token, _ = self._lease(activation_jwt=ticket)
        claims = leases.verify_lease(token)
        self.assertEqual(claims["activation_hash"],
                         leases.activation_hash(ticket))

        other = leases.mint_activation(self.grant, device=self.device)
        self.assertNotEqual(claims["activation_hash"],
                            leases.activation_hash(other))


class LeaseRefusalTests(LeaseBase):

    def test_no_lease_for_a_revoked_grant(self):
        grants.terminate(self.grant.public_id, reason="user_stop",
                         actor_user=self.bob)
        with self.assertRaises(grants.GrantError) as caught:
            self._lease()
        self.assertEqual(caught.exception.code, "not_active")

    def test_no_lease_for_a_paused_grant(self):
        grants.pause(self.grant.public_id,
                     expected_version=self.grant.version,
                     actor_user=self.bob)
        with self.assertRaises(grants.GrantError) as caught:
            self._lease(expected_version=self.grant.version + 1)
        self.assertEqual(caught.exception.code, "not_active")

    def test_no_lease_on_a_stale_epoch(self):
        """T08. Pause/resume mints a new epoch; the old one must die."""
        with self.assertRaises(grants.GrantError) as caught:
            self._lease(epoch=uuid.uuid4())
        self.assertEqual(caught.exception.code, "stale_epoch")

    def test_no_lease_on_a_stale_version(self):
        with self.assertRaises(grants.GrantError) as caught:
            self._lease(expected_version=self.grant.version + 7)
        self.assertEqual(caught.exception.code, "version_conflict")

    def test_no_lease_for_a_revoked_device(self):
        """T11. Revocation must bite on the next renewal, not at ticket
        expiry — otherwise unlinking a device leaves up to ten minutes of
        authority outstanding."""
        PairedDevice.objects.filter(pk=self.device.pk).update(
            status=PairedDevice.STATUS_REVOKED)
        with self.assertRaises(grants.GrantError) as caught:
            self._lease()
        self.assertEqual(caught.exception.code, "device_revoked")

    def test_no_lease_for_another_devices_grant(self):
        other = PairedDevice.objects.create(
            owner=self.bob, name="other", platform="windows", arch="x64",
            public_jwk={"kty": "OKP"}, key_thumbprint=f"t-{uuid.uuid4().hex[:8]}",
            status=PairedDevice.STATUS_PAIRED)
        with self.assertRaises(grants.GrantError) as caught:
            self._lease(device=other)
        self.assertEqual(caught.exception.status, 404)

    def test_the_kill_flag_stops_new_leases(self):
        """T13. Read from the database per renewal, never from the ticket."""
        conf = GlobalSettings.load()
        conf.collaboration_control_enabled = False
        conf.save(update_fields=["collaboration_control_enabled"])
        with self.assertRaises(grants.GrantError) as caught:
            self._lease()
        self.assertEqual(caught.exception.status, 404)

    def test_an_expired_grant_yields_no_lease(self):
        RemoteControlGrant.objects.filter(pk=self.grant.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(grants.GrantError) as caught:
            self._lease()
        self.assertEqual(caught.exception.status, 410)

    def test_a_lapsed_controller_binding_stops_renewal(self):
        from django.core.cache import cache
        cache.delete(f"collab:binding:{self.controller_binding}")
        with self.assertRaises(grants.GrantError) as caught:
            self._lease()
        self.assertEqual(caught.exception.code, "binding_lapsed")

    def test_a_stale_controller_heartbeat_stops_renewal(self):
        """T14. The 3s rule is stricter than the 6s record TTL on purpose:
        a participant who vanished must not keep control for a whole TTL."""
        self._write_binding(self.controller_binding, age=5.0)
        with self.assertRaises(grants.GrantError) as caught:
            self._lease()
        self.assertEqual(caught.exception.code, "binding_stale")

    def test_a_binding_without_a_heartbeat_stamp_is_treated_as_stale(self):
        """Records written before RC-008 added the stamp. Guessing 'fresh'
        would authorise input on the strength of a missing field."""
        import json
        from django.core.cache import cache
        record = self._write_binding(self.controller_binding)
        record.pop("heartbeat_at")
        cache.set(f"collab:binding:{self.controller_binding}",
                  json.dumps(record), timeout=60)
        with self.assertRaises(grants.GrantError) as caught:
            self._lease()
        self.assertEqual(caught.exception.code, "binding_stale")

    def test_an_unreachable_binding_store_refuses_rather_than_assumes(self):
        """"Backend dependencies unavailable means no renewal." Failing open
        would keep input flowing during exactly the outage that stops us
        noticing a disconnect."""
        with mock.patch.object(binding_store, "get",
                               side_effect=binding_store.StoreUnavailable()):
            with self.assertRaises(grants.GrantError) as caught:
                self._lease()
        self.assertEqual(caught.exception.status, 503)

    def test_renewals_are_rate_limited(self):
        errors = []
        for _ in range(6):
            try:
                self._lease()
            except grants.GrantError as exc:
                errors.append(exc.status)
        self.assertIn(429, errors)


@override_settings(COLLABORATION_CONTROL_SIGNING_KEY="")
class SigningKeyTests(LeaseBase):

    def test_the_derived_key_is_stable_across_calls(self):
        """Otherwise a redeploy, or simply a second worker, would invalidate
        every outstanding ticket."""
        self.assertEqual(leases.key_id(), leases.key_id())
        first = leases.mint_activation(self.grant, device=self.device)
        leases.verify_activation(first)

    def test_a_configured_pem_is_used_in_preference(self):
        pem = Ed25519PrivateKey.generate().private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        derived_kid = leases.key_id()
        with override_settings(COLLABORATION_CONTROL_SIGNING_KEY=pem):
            self.assertNotEqual(leases.key_id(), derived_kid)

    def test_the_public_key_is_publishable(self):
        pem = leases.public_key_pem()
        self.assertIn("BEGIN PUBLIC KEY", pem)
        self.assertNotIn("PRIVATE", pem)
