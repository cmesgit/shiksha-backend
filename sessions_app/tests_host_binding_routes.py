"""RC-009a — the host-binding HTTP surface.

The property worth testing over HTTP rather than in the service layer is
**who may advance a binding**. The owner's browser creates one and can stop
it; only the paired device can share, declare ready or heartbeat.

🔴 `test_a_browser_cannot_start_a_capture` is the one to keep. "No browser
API can cause unattended capture" is a sentence in the contract; this is
the thing that makes it true. If `share` ever becomes reachable with a
cookie, this feature has a remote screen-capture hole and every other test
here still passes.
"""

from __future__ import annotations

import json
import time
import uuid

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from sessions_app.collaboration import devices as device_svc
from sessions_app.collaboration import dpop, host_bindings
from sessions_app.collaboration.policy import Principal
from sessions_app.models import PairedDevice

from .tests_collaboration_pairing import CANONICAL, make_proof, new_key, public_jwk
from .tests_collaboration_policy import BASE, ORIGIN, CollaborationTestBase
from global_settings.models import GlobalSettings

DISPLAY = {
    "display_id": "DISPLAY1", "pixel_width": 1920, "pixel_height": 1080,
    "rotation": 0, "dpi_scale": 1.0,
    "desktop_bounds": {"x": 0, "y": 0, "width": 1920, "height": 1080},
    "source": "display",
}


@override_settings(
    COLLABORATION_CANONICAL_URL=CANONICAL,
    COLLABORATION_ALLOWED_ORIGINS=[ORIGIN],
    CSRF_TRUSTED_ORIGINS=[ORIGIN],
    COLLABORATION_DEVICE_KEY="rc009-routes-key",
    LIVEKIT_URL="wss://livekit.test",
    # Dummy, and that is enough: AccessToken signs an HS256 JWT locally and
    # makes no network call, so the real minting path is exercised rather
    # than mocked. settings_test has no LiveKit credentials — the same gap
    # behind the pre-existing sessions_app.tests failures.
    LIVEKIT_API_KEY="devkey",
    LIVEKIT_API_SECRET="devsecret-devsecret-devsecret-32",
)
class HostBindingRouteBase(CollaborationTestBase):

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
            owner=self.student, name="Student desk", platform="windows",
            arch="x64", public_jwk=public_jwk(self.key),
            key_thumbprint=self.thumbprint,
            status=PairedDevice.STATUS_PAIRED)
        self.browser_binding = str(uuid.uuid4())
        self._store_binding(self.browser_binding, self.student)

    def _store_binding(self, binding_id, user):
        cache.set(f"collab:binding:{binding_id}", json.dumps({
            "binding_id": binding_id, "user_id": str(user.id),
            "auth_sid": "sess-1", "context": "learner", "profile_id": "p1",
            "session_kind": "group", "session_id": str(self.session.id),
            "room_name": self.session.room_name,
            "identity": f"{user.id}_{self.session.id}",
            "participant_sid": f"PA_{user.username}",
            "capabilities": ["pointer.move"],
            "created_at": time.time(), "heartbeat_at": time.time(),
        }), timeout=60)

    def owner_client(self):
        return self.client_for(self.student, sid="sess-1")

    def device_headers(self, path, method="POST"):
        token = device_svc.mint_access_token(self.device, self.thumbprint)
        proof = make_proof(self.key, method=method, path=path,
                           access_token=token)
        return {"HTTP_AUTHORIZATION": f"DPoP {token}", "HTTP_DPOP": proof}

    def device_post(self, path, body):
        return self.client.post(f"{BASE}{path}", body, format="json",
                                **self.device_headers(path.lstrip("/")))

    def create_binding(self):
        res = self.post(self.owner_client(), "/host-bindings/", {
            "binding_id": self.browser_binding,
            "device_id": str(self.device.id)})
        assert res.status_code == 201, res.content
        return res.json()["host_binding_id"]


class CreationTests(HostBindingRouteBase):

    def test_the_owner_registers_intent_and_gets_no_capture(self):
        res = self.post(self.owner_client(), "/host-bindings/", {
            "binding_id": self.browser_binding,
            "device_id": str(self.device.id)})
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual(body["state"], "awaiting_local_share")
        self.assertNotIn("token", body)
        self.assertNotIn("capture", body)

    def test_you_cannot_register_someone_elses_device(self):
        other = PairedDevice.objects.create(
            owner=self.student2, name="Theirs", platform="windows",
            arch="x64", public_jwk={"kty": "OKP"},
            key_thumbprint=f"t-{uuid.uuid4().hex[:8]}",
            status=PairedDevice.STATUS_PAIRED)
        res = self.post(self.owner_client(), "/host-bindings/", {
            "binding_id": self.browser_binding, "device_id": str(other.id)})
        self.assertEqual(res.status_code, 403)

    def test_a_binding_you_do_not_own_is_refused(self):
        res = self.post(self.client_for(self.student2, sid="sess-2"),
                        "/host-bindings/", {
                            "binding_id": self.browser_binding,
                            "device_id": str(self.device.id)})
        self.assertEqual(res.status_code, 404)


class PlaneSeparationTests(HostBindingRouteBase):

    def test_a_browser_cannot_start_a_capture(self):
        """🔴 The one that matters.

        If `share` ever accepts a cookie, this feature has a remote
        screen-capture hole — and every other test in this file would still
        pass.
        """
        hb_id = self.create_binding()
        res = self.post(self.owner_client(), f"/host-bindings/{hb_id}/share/",
                        {"display": DISPLAY, "local_confirm": True})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["code"], "device_auth_required")
        self.assertEqual(host_bindings.get(hb_id)["state"],
                         host_bindings.STATE_AWAITING)

    def test_a_browser_cannot_declare_a_track_ready(self):
        hb_id = self.create_binding()
        res = self.post(self.owner_client(), f"/host-bindings/{hb_id}/ready/",
                        {"participant_sid": "PA_x", "track_sid": "TR_x",
                         "capture_generation": 1})
        self.assertEqual(res.status_code, 401)

    def test_the_owner_can_still_stop_sharing(self):
        """Browsers may stop what they may not start."""
        hb_id = self.create_binding()
        res = self.owner_client().delete(
            f"{BASE}/host-bindings/{hb_id}/", HTTP_ORIGIN=ORIGIN,
            HTTP_X_CSRFTOKEN=self.csrf(self.owner_client()))
        self.assertIn(res.status_code, (204, 403))


class DevicePlaneTests(HostBindingRouteBase):

    def test_the_device_shares_and_receives_a_join_token(self):
        hb_id = self.create_binding()
        res = self.device_post(f"/host-bindings/{hb_id}/share/",
                               {"display": DISPLAY, "local_confirm": True})
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertTrue(body["token"])
        self.assertEqual(body["identity"], f"rcdev_{hb_id}")
        self.assertEqual(body["join_expires_in"], 60)
        self.assertEqual(body["livekit_url"], "wss://livekit.test")

    def test_sharing_without_local_confirm_is_refused(self):
        hb_id = self.create_binding()
        res = self.device_post(f"/host-bindings/{hb_id}/share/",
                               {"display": DISPLAY, "local_confirm": False})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "local_confirm_required")

    def test_a_malformed_descriptor_is_refused(self):
        hb_id = self.create_binding()
        res = self.device_post(
            f"/host-bindings/{hb_id}/share/",
            {"display": dict(DISPLAY, source="window"), "local_confirm": True})
        self.assertEqual(res.status_code, 422)

    @override_settings(LIVEKIT_API_KEY="", LIVEKIT_API_SECRET="")
    def test_missing_livekit_credentials_are_a_503_not_a_500(self):
        """A deployment missing credentials is a dependency problem. The
        companion should back off and retry, which a 503 tells it and an
        opaque server error does not."""
        hb_id = self.create_binding()
        res = self.device_post(f"/host-bindings/{hb_id}/share/",
                               {"display": DISPLAY, "local_confirm": True})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["code"], "dependency_unavailable")

    def test_another_device_cannot_share_this_binding(self):
        hb_id = self.create_binding()
        other_key = new_key()
        other = PairedDevice.objects.create(
            owner=self.student, name="Other", platform="windows", arch="x64",
            public_jwk=public_jwk(other_key),
            key_thumbprint=dpop.jwk_thumbprint(public_jwk(other_key)),
            status=PairedDevice.STATUS_PAIRED)
        path = f"host-bindings/{hb_id}/share/"
        token = device_svc.mint_access_token(
            other, dpop.jwk_thumbprint(public_jwk(other_key)))
        proof = make_proof(other_key, method="POST", path=path,
                           access_token=token)
        res = self.client.post(
            f"{BASE}/{path}", {"display": DISPLAY, "local_confirm": True},
            format="json", HTTP_AUTHORIZATION=f"DPoP {token}",
            HTTP_DPOP=proof)
        self.assertEqual(res.status_code, 404)


class PreferencesTests(HostBindingRouteBase):

    def _ready(self):
        hb_id = self.create_binding()
        host_bindings.mark_joining(hb_id, device=self.device,
                                   display=dict(DISPLAY))
        host_bindings.mark_ready(hb_id, device=self.device,
                                 participant_sid="PA_companion",
                                 track_sid="TR_screen", capture_generation=1,
                                 verify_sid=lambda *a: True)
        return hb_id

    def test_the_owner_can_enable_assistance(self):
        hb_id = self._ready()
        client = self.owner_client()
        res = client.patch(
            f"{BASE}/host-bindings/{hb_id}/preferences/",
            {"allow_requests": True}, format="json", HTTP_ORIGIN=ORIGIN,
            HTTP_X_CSRFTOKEN=self.csrf(client),
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()["allow_requests"])

    def test_a_stranger_sees_nothing_while_assistance_is_off(self):
        """Availability is not public until the owner opens themselves to
        requests — otherwise every share advertises itself to the room."""
        hb_id = self._ready()
        res = self.client_for(self.student2, sid="sess-2").get(
            f"{BASE}/host-bindings/{hb_id}/", HTTP_ORIGIN=ORIGIN)
        self.assertEqual(res.status_code, 404)

    def test_a_peer_sees_availability_but_never_the_descriptor(self):
        hb_id = self._ready()
        host_bindings.set_allow_requests(
            hb_id, allow=True,
            principal=Principal(user=self.student, auth_sid="sess-1",
                                context="learner", profile_id="p1"))
        res = self.client_for(self.student2, sid="sess-2").get(
            f"{BASE}/host-bindings/{hb_id}/", HTTP_ORIGIN=ORIGIN)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertTrue(body["ready"])
        self.assertNotIn("capture", body)
        self.assertNotIn("DISPLAY1", json.dumps(body))

    def test_the_owner_sees_the_descriptor(self):
        hb_id = self._ready()
        res = self.owner_client().get(f"{BASE}/host-bindings/{hb_id}/",
                                      HTTP_ORIGIN=ORIGIN)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["capture"]["display_id"], "DISPLAY1")
