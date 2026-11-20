"""RC-008 part 2a — the consent state machine.

These test observable *authorization behaviour*, not the shape of the helper
that implements it. The task prompt is explicit about the difference: *"Do not
write a test that only mirrors an implementation helper while omitting
observable authorization behavior."* So every assertion below is about what a
caller can and cannot make the machine do — what state a grant lands in, who
may drive it, and what is written down — rather than about which private
function got called.

⚠ Engine coverage: this module runs under ``config.settings_test`` (SQLite),
which makes ``select_for_update`` a silent no-op. The *concurrent* claims —
two controllers approving one device, one winner — are therefore **not**
proven here. They live in ``tests_collaboration_races.py``, which runs on the
real PostgreSQL 16 harness RC-008 part 1 built. A green run of this file is
not evidence about locking.

Mapped to the security test plan: T01 (browser approval mints no native
authority), T03 (stale version), T04 (device/room mismatch), T07 (one live
grant per device), T09 (capability supersets), T12 (stop wins), T15 (lazy
expiry), T21 (kill flag).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from global_settings.models import GlobalSettings

from .collaboration import devices as device_svc
from .collaboration import grants
from .collaboration.policy import Principal
from .models import (
    CollaborationAuditEvent,
    GroupSession,
    PairedDevice,
    RemoteControlGrant,
)

CAPS = ["pointer.button", "pointer.move", "pointer.scroll"]
_UNSET = object()


def _session(host, *, session_type="instant"):
    now = timezone.localtime()
    return GroupSession.objects.create(
        host=host, topic="Collab", scheduled_date=now.date(),
        scheduled_time=now.time().replace(microsecond=0),
        duration_minutes=60, session_type=session_type, status="live",
        short_code=uuid.uuid4().hex[:12], room_name=f"room-{uuid.uuid4().hex[:8]}",
        room_started_at=timezone.now(),
    )


def _principal(user, context="learner"):
    return Principal(user=user, auth_sid=f"sid-{user.id}", context=context,
                     profile_id=None)


class GrantMachineBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user(
            username="rc008_alice", email="a@rc008.test", password="pw")
        cls.bob = User.objects.create_user(
            username="rc008_bob", email="b@rc008.test", password="pw")
        cls.carol = User.objects.create_user(
            username="rc008_carol", email="c@rc008.test", password="pw")

    def setUp(self):
        super().setUp()
        self.session = _session(self.alice)
        self.device = PairedDevice.objects.create(
            owner=self.bob, name="Bob's laptop", platform="windows", arch="x64",
            public_jwk={"kty": "OKP"}, key_thumbprint=f"t-{uuid.uuid4().hex[:8]}",
            status=PairedDevice.STATUS_PAIRED,
        )
        conf = GlobalSettings.load()
        conf.collaboration_bindings_enabled = True
        conf.collaboration_control_enabled = True
        conf.save(update_fields=["collaboration_bindings_enabled",
                                 "collaboration_control_enabled"])

    # -- helpers ----------------------------------------------------------

    def _request(self, *, controller=None, target=None, session=None,
                 device=None, capabilities=_UNSET):
        # NB: a sentinel, not `capabilities or CAPS` — an empty list is a
        # meaningful argument here and `or` would silently replace it.
        return grants.request_control(
            principal=_principal(controller or self.alice),
            session=session or self.session,
            device=device or self.device,
            target_user=target or self.bob,
            controller_binding_id=uuid.uuid4(),
            host_binding_id=uuid.uuid4(),
            capabilities=(list(CAPS) if capabilities is _UNSET
                          else capabilities),
        )

    def _approve(self, grant, *, capabilities=None):
        return grants.decide(
            grant.public_id, principal=_principal(self.bob), allow=True,
            capabilities=capabilities or list(CAPS),
            expected_version=grant.version,
        )

    def _activate(self, grant):
        nonce = grants.issue_consent_nonce(grant.public_id, device=self.device)
        grant.refresh_from_db()
        return grants.activate(
            grant.public_id, device=self.device, consent_nonce=nonce,
            expected_version=grant.version, capture_generation=3,
            local_confirm=True,
        )


class RequestTests(GrantMachineBase):

    def test_a_request_does_not_reserve_the_device(self):
        """Asking must not lock anyone else out; only approval reserves.

        Otherwise a controller could park a request on a device and deny
        every other participant access to it without the owner ever agreeing.
        """
        self._request()
        other = self._request(controller=self.carol)
        self.assertEqual(other.status, grants.REQUESTED)
        self.assertEqual(
            RemoteControlGrant.objects.filter(
                device=self.device, status=grants.REQUESTED).count(), 2)

    def test_one_pending_request_per_controller_and_device(self):
        self._request()
        with self.assertRaises(grants.GrantError) as caught:
            self._request()
        self.assertEqual(caught.exception.code, "request_pending")

    def test_controlling_your_own_account_is_refused(self):
        device = PairedDevice.objects.create(
            owner=self.alice, name="Alice's laptop", platform="windows",
            arch="x64", public_jwk={"kty": "OKP"},
            key_thumbprint=f"t-{uuid.uuid4().hex[:8]}",
            status=PairedDevice.STATUS_PAIRED)
        with self.assertRaises(grants.GrantError) as caught:
            self._request(target=self.alice, device=device)
        self.assertEqual(caught.exception.code, "self_control_denied")

    def test_a_device_the_target_does_not_own_is_refused(self):
        """Naming a stranger's device must not push a prompt at its owner."""
        carols = PairedDevice.objects.create(
            owner=self.carol, name="Carol's laptop", platform="windows",
            arch="x64", public_jwk={"kty": "OKP"},
            key_thumbprint=f"t-{uuid.uuid4().hex[:8]}",
            status=PairedDevice.STATUS_PAIRED)
        with self.assertRaises(grants.GrantError) as caught:
            self._request(device=carols)
        self.assertEqual(caught.exception.code, "device_not_owned_by_target")

    def test_a_revoked_device_cannot_be_requested(self):
        self.device.status = PairedDevice.STATUS_REVOKED
        self.device.save(update_fields=["status"])
        with self.assertRaises(grants.GrantError) as caught:
            self._request()
        self.assertEqual(caught.exception.code, "device_revoked")

    def test_an_expired_request_frees_the_slot_lazily(self):
        """A deadline is not a state until a transaction writes it down.

        The partial unique index carries no NOW(); nothing may infer expiry.
        So a second request must both succeed *and* leave the first row
        transitioned to `expired` as a side effect of taking the lock.
        """
        first = self._request()
        RemoteControlGrant.objects.filter(pk=first.pk).update(
            request_expires_at=timezone.now() - timedelta(seconds=1))

        second = self._request()
        self.assertEqual(second.status, grants.REQUESTED)
        first.refresh_from_db()
        self.assertEqual(first.status, grants.EXPIRED)
        self.assertEqual(first.reason, "deadline")


class CapabilityTests(GrantMachineBase):

    def test_pointer_button_requires_pointer_move(self):
        with self.assertRaises(grants.GrantError) as caught:
            self._request(capabilities=["pointer.button"])
        self.assertEqual(caught.exception.code, "unsupported_capability")
        self.assertEqual(caught.exception.status, 422)

    def test_an_unknown_capability_is_refused(self):
        for bad in (["shell"], ["clipboard"], ["elevate"], ["unattended"]):
            with self.subTest(capability=bad):
                with self.assertRaises(grants.GrantError) as caught:
                    self._request(capabilities=bad)
                self.assertEqual(caught.exception.code, "unsupported_capability")

    def test_an_empty_request_is_refused(self):
        with self.assertRaises(grants.GrantError):
            self._request(capabilities=[])

    def test_capabilities_are_stored_sorted_and_deduplicated(self):
        grant = self._request(
            capabilities=["pointer.move", "pointer.move", "pointer.button"])
        self.assertEqual(grant.capabilities,
                         ["pointer.button", "pointer.move"])

    def test_approval_cannot_widen_what_was_requested(self):
        grant = self._request(capabilities=["pointer.move"])
        with self.assertRaises(grants.GrantError) as caught:
            self._approve(grant, capabilities=["pointer.move",
                                               "keyboard.physical"])
        self.assertEqual(caught.exception.code, "capability_superset")
        grant.refresh_from_db()
        self.assertEqual(grant.status, grants.REQUESTED)

    def test_approval_may_narrow(self):
        grant = self._request()
        approved = self._approve(grant, capabilities=["pointer.move"])
        self.assertEqual(approved.capabilities, ["pointer.move"])


class EligibilityTests(GrantMachineBase):

    def test_a_peer_may_request_inside_an_instant_collaboration(self):
        """OD-01: anyone the host admitted, independent of role label."""
        grant = self._request(controller=self.carol)
        self.assertEqual(grant.status, grants.REQUESTED)

    def test_a_peer_may_not_request_in_a_scheduled_session(self):
        """OD-01: scheduled rooms are teacher-initiated only.

        🔴 Note this is the exact *opposite* of the legacy
        `group_session_policy.remote_control_enabled`, which is True for
        scheduled and False for instant. A regression that starts reading
        that attribute would invert both of these tests.
        """
        scheduled = _session(self.alice, session_type="scheduled")
        with self.assertRaises(grants.GrantError) as caught:
            self._request(controller=self.carol, session=scheduled)
        self.assertEqual(caught.exception.code, "teacher_initiated_only")

    def test_the_host_may_request_in_a_scheduled_session(self):
        scheduled = _session(self.alice, session_type="scheduled")
        grant = self._request(controller=self.alice, session=scheduled)
        self.assertEqual(grant.status, grants.REQUESTED)


class DecisionTests(GrantMachineBase):

    def test_browser_approval_does_not_activate(self):
        """T01. The single most tempting shortcut in the design.

        "approved is not a permission to inject" — a browser allow must leave
        the grant short of active, with no epoch and no expiry, until the
        companion's physical consent arrives.
        """
        grant = self._request()
        approved = self._approve(grant)
        self.assertEqual(approved.status, grants.APPROVED)
        self.assertIsNone(approved.epoch)
        self.assertIsNone(approved.expires_at)
        self.assertIsNotNone(approved.approval_expires_at)

    def test_only_the_target_may_decide(self):
        grant = self._request()
        for actor in (self.alice, self.carol):
            with self.subTest(actor=actor.username):
                with self.assertRaises(grants.GrantError) as caught:
                    grants.decide(grant.public_id, principal=_principal(actor),
                                  allow=True, capabilities=list(CAPS),
                                  expected_version=grant.version)
                # 404, not 403: a stranger learns nothing.
                self.assertEqual(caught.exception.status, 404)

    def test_a_stale_expected_version_is_refused(self):
        grant = self._request()
        with self.assertRaises(grants.GrantError) as caught:
            grants.decide(grant.public_id, principal=_principal(self.bob),
                          allow=True, capabilities=list(CAPS),
                          expected_version=grant.version + 5)
        self.assertEqual(caught.exception.code, "version_conflict")
        self.assertEqual(caught.exception.status, 409)

    def test_declining_records_the_decision_and_grants_nothing(self):
        grant = self._request()
        declined = grants.decide(
            grant.public_id, principal=_principal(self.bob), allow=False,
            capabilities=[], expected_version=grant.version)
        self.assertEqual(declined.status, grants.DECLINED)
        self.assertIsNone(declined.epoch)
        self.assertIsNotNone(declined.ended_at)

    def test_a_second_decision_is_refused(self):
        grant = self._request()
        approved = self._approve(grant)
        with self.assertRaises(grants.GrantError) as caught:
            grants.decide(grant.public_id, principal=_principal(self.bob),
                          allow=False, capabilities=[],
                          expected_version=approved.version)
        self.assertEqual(caught.exception.status, 409)

    def test_approval_is_refused_while_another_grant_holds_the_device(self):
        """T07, the single-threaded half. The concurrent half is the PG race.

        Both requests must exist *before* either is approved: once a grant
        reserves the device, ``request_control`` itself answers busy (the
        contract lists busy as a POST grants/ failure), so a second request
        could not be created afterwards to race with.
        """
        first = self._request()
        second = self._request(controller=self.carol)
        self._approve(first)
        with self.assertRaises(grants.GrantError) as caught:
            self._approve(second)
        self.assertEqual(caught.exception.code, "busy")

    def test_requesting_a_device_already_under_control_is_refused(self):
        first = self._request()
        self._approve(first)
        with self.assertRaises(grants.GrantError) as caught:
            self._request(controller=self.carol)
        self.assertEqual(caught.exception.code, "busy")

    def test_an_elapsed_request_cannot_be_approved(self):
        grant = self._request()
        RemoteControlGrant.objects.filter(pk=grant.pk).update(
            request_expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(grants.GrantError) as caught:
            self._approve(grant)
        self.assertEqual(caught.exception.status, 410)
        grant.refresh_from_db()
        self.assertEqual(grant.status, grants.EXPIRED)


class ActivationTests(GrantMachineBase):

    def test_activation_needs_the_device_the_grant_names(self):
        grant = self._request()
        self._approve(grant)
        other = PairedDevice.objects.create(
            owner=self.bob, name="Other", platform="windows", arch="x64",
            public_jwk={"kty": "OKP"},
            key_thumbprint=f"t-{uuid.uuid4().hex[:8]}",
            status=PairedDevice.STATUS_PAIRED)
        nonce = grants.issue_consent_nonce(grant.public_id, device=self.device)
        grant.refresh_from_db()
        with self.assertRaises(grants.GrantError) as caught:
            grants.activate(grant.public_id, device=other, consent_nonce=nonce,
                            expected_version=grant.version,
                            capture_generation=1, local_confirm=True)
        self.assertEqual(caught.exception.status, 404)

    def test_activation_without_local_confirm_is_refused(self):
        grant = self._request()
        self._approve(grant)
        nonce = grants.issue_consent_nonce(grant.public_id, device=self.device)
        grant.refresh_from_db()
        for value in (False, None, "true", 1):
            with self.subTest(local_confirm=value):
                with self.assertRaises(grants.GrantError) as caught:
                    grants.activate(grant.public_id, device=self.device,
                                    consent_nonce=nonce,
                                    expected_version=grant.version,
                                    capture_generation=1, local_confirm=value)
                self.assertEqual(caught.exception.code, "local_confirm_required")

    def test_a_wrong_consent_nonce_is_refused(self):
        grant = self._request()
        self._approve(grant)
        grants.issue_consent_nonce(grant.public_id, device=self.device)
        grant.refresh_from_db()
        with self.assertRaises(grants.GrantError) as caught:
            grants.activate(grant.public_id, device=self.device,
                            consent_nonce=device_svc.new_device_secret(),
                            expected_version=grant.version,
                            capture_generation=1, local_confirm=True)
        self.assertEqual(caught.exception.code, "consent_nonce_invalid")

    def test_a_consent_nonce_is_single_use(self):
        grant = self._request()
        self._approve(grant)
        first = grants.issue_consent_nonce(grant.public_id, device=self.device)
        second = grants.issue_consent_nonce(grant.public_id, device=self.device)
        self.assertNotEqual(first, second)
        grant.refresh_from_db()
        with self.assertRaises(grants.GrantError) as caught:
            grants.activate(grant.public_id, device=self.device,
                            consent_nonce=first,
                            expected_version=grant.version,
                            capture_generation=1, local_confirm=True)
        self.assertEqual(caught.exception.code, "consent_nonce_invalid")

    def test_activation_mints_an_epoch_and_a_hard_expiry(self):
        grant = self._request()
        self._approve(grant)
        active = self._activate(grant)
        self.assertEqual(active.status, grants.ACTIVE)
        self.assertIsNotNone(active.epoch)
        self.assertIsNotNone(active.expires_at)
        self.assertIsNone(active.consent_nonce_hash)
        self.assertLessEqual(
            active.expires_at, timezone.now() + grants.GRANT_MAX)

    def test_a_grant_cannot_outlive_the_room(self):
        """The room's own deadline wins when it is sooner."""
        GroupSession.objects.filter(pk=self.session.pk).update(
            room_started_at=timezone.now() - timedelta(minutes=58))
        grant = self._request()
        self._approve(grant)
        active = self._activate(grant)
        self.assertLess(active.expires_at, timezone.now() + grants.GRANT_MAX)

    def test_a_consent_nonce_is_not_issued_for_a_requested_grant(self):
        grant = self._request()
        with self.assertRaises(grants.GrantError) as caught:
            grants.issue_consent_nonce(grant.public_id, device=self.device)
        self.assertEqual(caught.exception.status, 409)


class PauseResumeTests(GrantMachineBase):

    def _active(self):
        grant = self._request()
        self._approve(grant)
        return self._activate(grant)

    def test_pausing_bumps_the_epoch_and_holds_the_reservation(self):
        active = self._active()
        before = active.epoch
        paused = grants.pause(active.public_id, expected_version=active.version,
                              actor_user=self.bob, reason="user_pause")
        self.assertEqual(paused.status, grants.PAUSED)
        self.assertNotEqual(paused.epoch, before)
        self.assertIsNotNone(paused.paused_until)

    def test_pausing_twice_is_idempotent_and_does_not_extend_the_hold(self):
        active = self._active()
        paused = grants.pause(active.public_id, expected_version=active.version,
                              actor_user=self.bob)
        held_until = paused.paused_until
        again = grants.pause(paused.public_id,
                             expected_version=paused.version,
                             actor_user=self.bob)
        self.assertEqual(again.status, grants.PAUSED)
        self.assertEqual(again.paused_until, held_until)
        self.assertEqual(again.version, paused.version)

    def test_a_browser_alone_cannot_resume(self):
        """There is no browser-only resume: intent leaves it paused."""
        active = self._active()
        paused = grants.pause(active.public_id,
                              expected_version=active.version,
                              actor_user=self.bob)
        after = grants.resume_intent(paused.public_id,
                                     principal=_principal(self.bob),
                                     expected_version=paused.version)
        self.assertEqual(after.status, grants.PAUSED)
        self.assertIsNotNone(after.resume_intent_expires_at)
        self.assertGreater(after.version, paused.version)

    def test_the_companion_cannot_resume_without_fresh_intent(self):
        active = self._active()
        paused = grants.pause(active.public_id,
                              expected_version=active.version,
                              actor_user=self.bob)
        with self.assertRaises(grants.GrantError) as caught:
            grants.issue_consent_nonce(paused.public_id, device=self.device)
        self.assertEqual(caught.exception.code, "resume_intent_required")

    def test_resuming_keeps_the_original_hard_expiry(self):
        """Resume must never extend the 10-minute ceiling."""
        active = self._active()
        original = active.expires_at
        paused = grants.pause(active.public_id,
                              expected_version=active.version,
                              actor_user=self.bob)
        intent = grants.resume_intent(paused.public_id,
                                      principal=_principal(self.bob),
                                      expected_version=paused.version)
        nonce = grants.issue_consent_nonce(intent.public_id, device=self.device)
        intent.refresh_from_db()
        resumed = grants.activate(intent.public_id, device=self.device,
                                  consent_nonce=nonce,
                                  expected_version=intent.version,
                                  capture_generation=4, local_confirm=True)
        self.assertEqual(resumed.status, grants.ACTIVE)
        self.assertEqual(resumed.expires_at, original)
        self.assertNotEqual(resumed.epoch, active.epoch)

    def test_an_elapsed_pause_hold_expires_the_grant(self):
        active = self._active()
        paused = grants.pause(active.public_id,
                              expected_version=active.version,
                              actor_user=self.bob)
        RemoteControlGrant.objects.filter(pk=paused.pk).update(
            paused_until=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(grants.GrantError):
            grants.resume_intent(paused.public_id,
                                 principal=_principal(self.bob),
                                 expected_version=paused.version)
        paused.refresh_from_db()
        self.assertEqual(paused.status, grants.EXPIRED)


class TerminationTests(GrantMachineBase):

    def _active(self):
        grant = self._request()
        self._approve(grant)
        return self._activate(grant)

    def test_stop_wins_over_a_stale_version(self):
        """T12. The one place optimistic concurrency is deliberately waived.

        A revoke that failed precisely when something else was changing the
        grant would be the worst possible behaviour for the one operation
        whose entire job is to stop control.
        """
        active = self._active()
        stopped = grants.terminate(active.public_id, reason="user_stop",
                                   expected_version=active.version + 99,
                                   actor_user=self.bob)
        self.assertEqual(stopped.status, grants.REVOKED)

    def test_terminating_is_idempotent(self):
        active = self._active()
        first = grants.terminate(active.public_id, reason="completed",
                                 actor_user=self.alice)
        second = grants.terminate(first.public_id, reason="user_stop",
                                  actor_user=self.bob)
        self.assertEqual(second.status, grants.COMPLETED)
        self.assertEqual(second.version, first.version)

    def test_a_stranger_cannot_terminate(self):
        active = self._active()
        with self.assertRaises(grants.GrantError) as caught:
            grants.terminate(active.public_id, reason="user_stop",
                             actor_user=self.carol)
        self.assertEqual(caught.exception.status, 404)

    def test_reasons_map_to_the_right_terminal_state(self):
        for reason, expected in (
            ("completed", grants.COMPLETED),
            ("user_stop", grants.REVOKED),
            ("local_stop", grants.REVOKED),
            ("session_ended", grants.REVOKED),
            ("disconnected", grants.FAILED),
            ("capture_lost", grants.FAILED),
            ("protocol_error", grants.FAILED),
        ):
            with self.subTest(reason=reason):
                active = self._active()
                stopped = grants.terminate(active.public_id, reason=reason,
                                           actor_user=self.bob)
                self.assertEqual(stopped.status, expected)
                self.assertEqual(stopped.reason, reason)

    def test_an_unknown_reason_is_refused(self):
        active = self._active()
        with self.assertRaises(grants.GrantError) as caught:
            grants.terminate(active.public_id, reason="because",
                             actor_user=self.bob)
        self.assertEqual(caught.exception.code, "invalid_reason")

    def test_revocation_does_not_depend_on_the_notification_channel(self):
        """A publish failure must not unwind a committed revocation.

        The event plane is a latency optimisation; REST is authority. If a
        dead channel layer could block a stop, the feature's worst-case
        revocation latency would be unbounded rather than ≤3s.
        """
        active = self._active()
        with mock.patch("sessions_app.collaboration.events.publish",
                        side_effect=RuntimeError("channels is down")):
            stopped = grants.terminate(active.public_id, reason="user_stop",
                                       actor_user=self.bob)
        self.assertEqual(stopped.status, grants.REVOKED)
        stopped.refresh_from_db()
        self.assertEqual(stopped.status, grants.REVOKED)

    def test_unlinking_a_device_stops_its_grants_through_the_machine(self):
        """Not a bulk UPDATE: the version must move and an audit row appear."""
        active = self._active()
        device_svc.revoke_device(self.device, reason="unlinked")
        active.refresh_from_db()
        self.assertEqual(active.status, grants.REVOKED)
        self.assertTrue(CollaborationAuditEvent.objects.filter(
            resource_id=str(active.public_id),
            event_type="grant.terminated").exists())

    def test_losing_a_binding_stops_the_grants_that_referenced_it(self):
        active = self._active()
        grants.terminate_for_binding(active.controller_binding_id)
        active.refresh_from_db()
        self.assertEqual(active.status, grants.FAILED)
        self.assertEqual(active.reason, "disconnected")


class KillFlagTests(GrantMachineBase):

    def _disable_control(self):
        conf = GlobalSettings.load()
        conf.collaboration_control_enabled = False
        conf.save(update_fields=["collaboration_control_enabled"])

    def test_control_off_hides_the_feature(self):
        self._disable_control()
        with self.assertRaises(grants.GrantError) as caught:
            self._request()
        self.assertEqual(caught.exception.status, 404)

    def test_bindings_off_also_disables_control(self):
        conf = GlobalSettings.load()
        conf.collaboration_bindings_enabled = False
        conf.save(update_fields=["collaboration_bindings_enabled"])
        with self.assertRaises(grants.GrantError) as caught:
            self._request()
        self.assertEqual(caught.exception.status, 404)

    def test_stopping_still_works_after_the_flag_goes_off(self):
        """Otherwise flipping the kill switch strands live grants instead of
        ending them — the opposite of what an operator pressed it for."""
        grant = self._request()
        self._approve(grant)
        active = self._activate(grant)
        self._disable_control()
        stopped = grants.terminate(active.public_id, reason="session_ended",
                                   actor_user=self.bob)
        self.assertEqual(stopped.status, grants.REVOKED)

    def test_activation_is_refused_once_control_is_off(self):
        grant = self._request()
        self._approve(grant)
        nonce = grants.issue_consent_nonce(grant.public_id, device=self.device)
        grant.refresh_from_db()
        self._disable_control()
        with self.assertRaises(grants.GrantError) as caught:
            grants.activate(grant.public_id, device=self.device,
                            consent_nonce=nonce,
                            expected_version=grant.version,
                            capture_generation=1, local_confirm=True)
        self.assertEqual(caught.exception.status, 404)


class AuditTests(GrantMachineBase):

    def test_every_transition_leaves_exactly_one_audit_row(self):
        grant = self._request()
        self._approve(grant)
        active = self._activate(grant)
        grants.terminate(active.public_id, reason="completed",
                         actor_user=self.alice)

        rows = CollaborationAuditEvent.objects.filter(
            resource_type="grant", resource_id=str(grant.public_id)
        ).order_by("resource_version")
        self.assertEqual(
            [r.event_type for r in rows],
            ["grant.requested", "grant.approved", "grant.activated",
             "grant.terminated"],
        )
        # One row per version, which is what makes a retried transaction
        # unable to double-log.
        versions = [r.resource_version for r in rows]
        self.assertEqual(len(versions), len(set(versions)))

    def test_audit_records_no_secrets(self):
        grant = self._request()
        self._approve(grant)
        nonce = grants.issue_consent_nonce(grant.public_id, device=self.device)
        grant.refresh_from_db()
        grants.activate(grant.public_id, device=self.device,
                        consent_nonce=nonce, expected_version=grant.version,
                        capture_generation=2, local_confirm=True)
        blob = "".join(
            str(r.metadata) + r.reason
            for r in CollaborationAuditEvent.objects.filter(
                resource_id=str(grant.public_id))
        )
        self.assertNotIn(nonce, blob)

    def test_a_legacy_grant_is_not_addressable_through_this_plane(self):
        """Legacy rows belong to the dormant teacher→student path and must
        not be reachable by passing an id to the new machine."""
        legacy = RemoteControlGrant.objects.create(
            session=self.session, controller=self.alice, target=self.bob,
            status=RemoteControlGrant.STATUS_REQUESTED,
            schema_version=RemoteControlGrant.SCHEMA_LEGACY,
            public_id=uuid.uuid4(),
        )
        with self.assertRaises(grants.GrantError) as caught:
            grants.terminate(legacy.public_id, reason="user_stop",
                             actor_user=self.bob)
        self.assertEqual(caught.exception.status, 404)
