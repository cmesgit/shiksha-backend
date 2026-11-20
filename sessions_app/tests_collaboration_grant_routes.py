"""RC-008 part 2d — the control-grant HTTP surface.

Two things these have to prove that the service-layer suites cannot:

1. **The two authentication planes are actually separate.** A device
   credential must not drive a browser route and a signed-in user must not
   drive a device one. That is what makes "only the companion can activate"
   a property of the system rather than of the service layer alone.
2. **The wiring is real.** Every device-plane test here mints a genuine
   access token and signs a genuine ES256 DPoP proof — reusing the RC-007
   helpers rather than mocking — because part 2b's seam-ordering bug showed
   what a suite of refusals alone hides: every negative test passed over a
   path that could never once succeed.

So the happy paths are asserted first, and the refusals after.
"""

from __future__ import annotations

import time
import uuid

import jwt
from django.test import override_settings
from django.utils import timezone

from global_settings.models import GlobalSettings
from sessions_app.collaboration import devices as device_svc
from sessions_app.collaboration import dpop
from sessions_app.collaboration import grants as grant_svc
from sessions_app.collaboration.policy import Principal
from sessions_app.models import PairedDevice

from .tests_collaboration_pairing import CANONICAL, make_proof, new_key, public_jwk
from .tests_collaboration_policy import BASE, ORIGIN, CollaborationTestBase


@override_settings(
    COLLABORATION_CANONICAL_URL=CANONICAL,
    COLLABORATION_ALLOWED_ORIGINS=[ORIGIN],
    CSRF_TRUSTED_ORIGINS=[ORIGIN],
    COLLABORATION_DEVICE_KEY="rc008-routes-device-key",
)
class GrantRouteBase(CollaborationTestBase):

    def setUp(self):
        super().setUp()
        conf = GlobalSettings.load()
        conf.collaboration_bindings_enabled = True
        conf.collaboration_control_enabled = True
        conf.save(update_fields=["collaboration_bindings_enabled",
                                 "collaboration_control_enabled"])

        self.key = new_key()
        self.thumbprint = dpop.jwk_thumbprint(public_jwk(self.key))
        self.device = PairedDevice.objects.create(
            owner=self.student2, name="Student2 desk", platform="windows",
            arch="x64", public_jwk=public_jwk(self.key),
            key_thumbprint=self.thumbprint,
            status=PairedDevice.STATUS_PAIRED)

        self.controller_binding = str(uuid.uuid4())
        self._store_binding(self.controller_binding, self.student)
        self.grant = self._service_grant()

    # -- fixtures ---------------------------------------------------------

    def _store_binding(self, binding_id, user):
        """A live binding record, written straight to the store.

        The HTTP bind flow is RC-006's and is tested there; reproducing it
        here would couple these tests to LiveKit roster patching for no
        extra coverage.
        """
        import json
        from django.core.cache import cache
        cache.set(f"collab:binding:{binding_id}", json.dumps({
            "binding_id": binding_id,
            "user_id": str(user.id),
            "auth_sid": "sess-1",
            "context": "learner",
            "profile_id": "p1",
            "session_kind": "group",
            "session_id": str(self.session.id),
            "room_name": self.session.room_name,
            "identity": f"{user.id}_{self.session.id}",
            "participant_sid": f"PA_{user.username}",
            "capabilities": ["pointer.move"],
            "created_at": time.time(),
            "heartbeat_at": time.time(),
        }), timeout=60)

    def _service_grant(self):
        """A requested grant, built through the service layer.

        🔴 Not through POST grants/ — that route cannot succeed yet; see
        ``test_creating_a_grant_reports_the_rc009_gap``.
        """
        return grant_svc.request_control(
            principal=Principal(user=self.student, auth_sid="sess-1",
                                context="learner", profile_id="p1"),
            session=self.session, device=self.device,
            target_user=self.student2,
            controller_binding_id=self.controller_binding,
            host_binding_id=uuid.uuid4(),
            capabilities=["pointer.move", "pointer.button"],
            room_name=self.session.room_name,
            controller_identity=f"{self.student.id}_{self.session.id}",
            controller_sid=f"PA_{self.student.username}")

    # -- device plumbing --------------------------------------------------

    def device_headers(self, path, method="POST"):
        """A real access token plus a real DPoP proof bound to it."""
        token = device_svc.mint_access_token(self.device, self.thumbprint)
        proof = make_proof(self.key, method=method, path=path,
                           access_token=token)
        return {"HTTP_AUTHORIZATION": f"DPoP {token}", "HTTP_DPOP": proof}

    def device_post(self, path, body):
        return self.client.post(f"{BASE}{path}", body, format="json",
                                **self.device_headers(path.lstrip("/")))

    def device_get(self, path):
        return self.client.get(
            f"{BASE}{path}", **self.device_headers(path.lstrip("/"), "GET"))

    # -- browser plumbing -------------------------------------------------

    def as_target(self):
        return self.client_for(self.student2)

    def as_controller(self):
        return self.client_for(self.student)


class BrowserPlaneTests(GrantRouteBase):

    def test_the_target_can_read_and_approve_but_not_activate(self):
        """T01 over HTTP. The browser's reach ends at `approved`."""
        client = self.as_target()
        read = client.get(f"{BASE}/grants/{self.grant.public_id}/",
                          HTTP_ORIGIN=ORIGIN)
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["state"], "requested")

        res = self.post(client, f"/grants/{self.grant.public_id}/decision/",
                        {"allow": True, "capabilities": ["pointer.move"],
                         "expected_version": self.grant.version})
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["state"], "approved")
        self.assertIsNone(body["epoch"])
        self.assertIsNone(body["expires_at"])

    def test_declining_over_http(self):
        client = self.as_target()
        res = self.post(client, f"/grants/{self.grant.public_id}/decision/",
                        {"allow": False, "capabilities": [],
                         "expected_version": self.grant.version})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["state"], "declined")

    def test_the_controller_cannot_decide(self):
        res = self.post(self.as_controller(),
                        f"/grants/{self.grant.public_id}/decision/",
                        {"allow": True, "capabilities": ["pointer.move"],
                         "expected_version": self.grant.version})
        self.assertEqual(res.status_code, 404)

    def test_a_stranger_sees_a_404_not_a_403(self):
        """A stranger must not be able to tell a real grant id from a
        fabricated one."""
        client = self.client_for(self.outsider)
        res = client.get(f"{BASE}/grants/{self.grant.public_id}/",
                         HTTP_ORIGIN=ORIGIN)
        self.assertEqual(res.status_code, 404)

    def test_terminate_over_http(self):
        res = self.post(self.as_controller(),
                        f"/grants/{self.grant.public_id}/terminate/",
                        {"reason": "user_stop",
                         "expected_version": self.grant.version})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["state"], "revoked")

    def test_terminate_wins_over_a_stale_version(self):
        res = self.post(self.as_controller(),
                        f"/grants/{self.grant.public_id}/terminate/",
                        {"reason": "user_stop", "expected_version": 999})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["state"], "revoked")

    def test_an_unknown_body_field_is_refused(self):
        res = self.post(self.as_target(),
                        f"/grants/{self.grant.public_id}/decision/",
                        {"allow": True, "capabilities": ["pointer.move"],
                         "expected_version": self.grant.version,
                         "target_user_id": self.outsider.id})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "unknown_fields")

    def test_a_forbidden_origin_is_refused(self):
        res = self.post(self.as_target(),
                        f"/grants/{self.grant.public_id}/decision/",
                        {"allow": True, "capabilities": ["pointer.move"],
                         "expected_version": self.grant.version},
                        origin="https://evilapp.test.shikshacom.com")
        self.assertEqual(res.status_code, 403)

    def test_a_missing_csrf_token_is_refused(self):
        res = self.post(self.as_target(),
                        f"/grants/{self.grant.public_id}/decision/",
                        {"allow": True, "capabilities": ["pointer.move"],
                         "expected_version": self.grant.version},
                        csrf=None)
        self.assertEqual(res.status_code, 403)

    def test_a_missing_idempotency_key_is_refused(self):
        res = self.post(self.as_target(),
                        f"/grants/{self.grant.public_id}/decision/",
                        {"allow": True, "capabilities": ["pointer.move"],
                         "expected_version": self.grant.version},
                        idem=None)
        self.assertEqual(res.status_code, 400)

    def test_the_action_routes_are_not_swallowed_by_the_detail_route(self):
        """`<id>/decision/` must not be parsed as a grant whose id is
        "decision" — URL order is load-bearing here."""
        client = self.as_target()
        res = self.post(client, f"/grants/{self.grant.public_id}/decision/",
                        {"allow": True, "capabilities": ["pointer.move"],
                         "expected_version": self.grant.version})
        self.assertNotEqual(res.status_code, 404)


class SerialisationTests(GrantRouteBase):

    def test_the_grant_body_carries_no_secrets(self):
        """"Do not return device credential, signed activation, lease or
        other participant context secrets to room viewers.\""""
        client = self.as_target()
        raw = client.get(f"{BASE}/grants/{self.grant.public_id}/",
                         HTTP_ORIGIN=ORIGIN).content.decode()
        for forbidden in ("consent_nonce_hash", "activation_jwt", "lease_jwt",
                          "auth_session_id", "credential_hash", "sess-1"):
            self.assertNotIn(forbidden, raw)

    def test_the_shape_matches_the_contract(self):
        client = self.as_target()
        body = client.get(f"{BASE}/grants/{self.grant.public_id}/",
                          HTTP_ORIGIN=ORIGIN).json()
        for field in ("id", "version", "state", "session", "controller",
                      "target", "device", "capabilities", "expires_at",
                      "request_expires_at", "reason", "epoch",
                      "capture_generation", "track_sid"):
            self.assertIn(field, body)


class DevicePlaneTests(GrantRouteBase):

    def _approve(self):
        grant_svc.decide(self.grant.public_id,
                         principal=Principal(user=self.student2,
                                             auth_sid="sess-2",
                                             context="learner",
                                             profile_id="p1"),
                         allow=True, capabilities=["pointer.move"],
                         expected_version=self.grant.version)
        self.grant.refresh_from_db()

    def test_a_device_read_carries_a_consent_nonce_once_approved(self):
        """The nonce is what lets the companion show its dialog and then
        activate in one pass. Before approval there must not be one."""
        before = self.device_get(f"/grants/{self.grant.public_id}/")
        self.assertEqual(before.status_code, 200, before.content)
        self.assertIsNone(before.json()["consent_nonce"])

        self._approve()
        after = self.device_get(f"/grants/{self.grant.public_id}/")
        self.assertEqual(after.status_code, 200, after.content)
        self.assertTrue(after.json()["consent_nonce"])

    def test_a_device_can_activate_and_receives_a_ticket_and_lease(self):
        """🔴 The happy path. Without this the suite would be refusals only
        — the exact shape of bug part 2b shipped and caught."""
        self._approve()
        nonce = self.device_get(
            f"/grants/{self.grant.public_id}/").json()["consent_nonce"]
        self.grant.refresh_from_db()

        res = self.device_post(f"/grants/{self.grant.public_id}/activate/", {
            "consent_nonce": nonce,
            "expected_version": self.grant.version,
            "capture_generation": 3,
            "local_confirm": True,
            "lease_nonce": uuid.uuid4().hex,
        })
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["grant"]["state"], "active")
        self.assertTrue(body["activation_jwt"])
        self.assertTrue(body["lease_jwt"])

        claims = jwt.decode(body["lease_jwt"], options={
            "verify_signature": False})
        self.assertLessEqual(claims["exp"] - claims["iat"], 3)

    def test_a_device_can_renew_a_lease_without_an_idempotency_key(self):
        """🔴 Deliberately exempt. Replaying a cached lease is precisely
        what the three-second deadline exists to prevent."""
        self._approve()
        nonce = self.device_get(
            f"/grants/{self.grant.public_id}/").json()["consent_nonce"]
        self.grant.refresh_from_db()
        self.device_post(f"/grants/{self.grant.public_id}/activate/", {
            "consent_nonce": nonce,
            "expected_version": self.grant.version,
            "capture_generation": 3, "local_confirm": True,
            "lease_nonce": uuid.uuid4().hex})
        self.grant.refresh_from_db()

        res = self.device_post(f"/grants/{self.grant.public_id}/lease/", {
            "epoch": str(self.grant.epoch),
            "expected_version": self.grant.version,
            "nonce": uuid.uuid4().hex})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()["lease_jwt"])

    def test_activation_without_local_confirm_is_refused_over_http(self):
        self._approve()
        nonce = self.device_get(
            f"/grants/{self.grant.public_id}/").json()["consent_nonce"]
        self.grant.refresh_from_db()
        res = self.device_post(f"/grants/{self.grant.public_id}/activate/", {
            "consent_nonce": nonce,
            "expected_version": self.grant.version,
            "capture_generation": 3, "local_confirm": False,
            "lease_nonce": uuid.uuid4().hex})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "local_confirm_required")


class PlaneSeparationTests(GrantRouteBase):

    def test_a_signed_in_user_cannot_activate(self):
        """🔴 The property the whole consent design rests on. A browser
        credential must not reach the route that opens the gate."""
        res = self.post(self.as_target(),
                        f"/grants/{self.grant.public_id}/activate/",
                        {"consent_nonce": "x", "expected_version": 1,
                         "capture_generation": 1, "local_confirm": True,
                         "lease_nonce": uuid.uuid4().hex})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["code"], "device_auth_required")

    def test_a_signed_in_user_cannot_renew_a_lease(self):
        res = self.post(self.as_controller(),
                        f"/grants/{self.grant.public_id}/lease/",
                        {"epoch": str(uuid.uuid4()), "expected_version": 1,
                         "nonce": uuid.uuid4().hex})
        self.assertEqual(res.status_code, 401)

    def test_a_device_credential_does_not_satisfy_a_browser_route(self):
        """The decision route is the target's, and a device is not a
        target — a paired desktop must not be able to approve on its
        owner's behalf."""
        path = f"grants/{self.grant.public_id}/decision/"
        res = self.client.post(
            f"{BASE}/{path}",
            {"allow": True, "capabilities": ["pointer.move"],
             "expected_version": self.grant.version},
            format="json", HTTP_ORIGIN=ORIGIN,
            **self.device_headers(path))
        self.assertIn(res.status_code, (401, 403))

    def test_a_bare_bearer_token_is_not_a_device_credential(self):
        """`authenticate_device` requires the DPoP scheme specifically, so a
        token lifted into an ordinary Authorization header buys nothing."""
        token = device_svc.mint_access_token(self.device, self.thumbprint)
        res = self.client.post(
            f"{BASE}/grants/{self.grant.public_id}/lease/",
            {"epoch": str(uuid.uuid4()), "expected_version": 1,
             "nonce": uuid.uuid4().hex},
            format="json", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(res.status_code, 401)


class FeatureFlagTests(GrantRouteBase):

    def _control_off(self):
        conf = GlobalSettings.load()
        conf.collaboration_control_enabled = False
        conf.save(update_fields=["collaboration_control_enabled"])

    def test_control_off_hides_the_routes(self):
        self._control_off()
        res = self.post(self.as_target(),
                        f"/grants/{self.grant.public_id}/decision/",
                        {"allow": True, "capabilities": ["pointer.move"],
                         "expected_version": self.grant.version})
        self.assertEqual(res.status_code, 404)

    def test_stopping_still_works_with_control_off(self):
        """Otherwise the kill switch strands live grants rather than ending
        them."""
        self._control_off()
        res = self.post(self.as_controller(),
                        f"/grants/{self.grant.public_id}/terminate/",
                        {"reason": "session_ended",
                         "expected_version": self.grant.version})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["state"], "revoked")


class Rc009GapTests(GrantRouteBase):

    def test_creating_a_grant_reports_the_rc009_gap(self):
        """🔴 POST grants/ cannot succeed until RC-009 ships host bindings.

        The request body names a *host binding*, and the target and device
        are derived from it — "no client controller/target IDs". Host
        bindings do not exist, so there is nothing to derive from. Accepting
        a device id from the body instead would be precisely the rule the
        contract forbids, so the route refuses with the contract's own
        ``capture_not_ready`` rather than inventing a resolution.

        This test exists to make that a stated, asserted limitation rather
        than a mystery 409 for whoever wires the frontend.
        """
        res = self.post(self.as_controller(), "/grants/", {
            "controller_binding_id": self.controller_binding,
            "host_binding_id": str(uuid.uuid4()),
            "capabilities": ["pointer.move"]})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "capture_not_ready")

    def test_creating_a_grant_still_checks_the_caller_owns_its_binding(self):
        """The RC-009 refusal must come *after* the caller's own binding is
        validated, or the route would leak that a binding exists."""
        res = self.post(self.as_controller(), "/grants/", {
            "controller_binding_id": str(uuid.uuid4()),
            "host_binding_id": str(uuid.uuid4()),
            "capabilities": ["pointer.move"]})
        self.assertEqual(res.status_code, 404)
