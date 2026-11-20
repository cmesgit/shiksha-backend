"""RC-009a — host bindings and the companion's identity.

The acceptance criteria this file is written against, in the task's own
words: "no attendance or capacity inflation; no camera/mic/subscription
privilege; viewer/spectator cannot mint companion token; stale token cannot
grant input".

🔴 The sharpest of those is the **asymmetry** between excluding and
trusting. An `rcdev_` prefix must be enough to keep a participant OUT of
human accounting (fail-safe — a forgery only excludes itself from a count it
was never entitled to join), and must be nowhere near enough to let one IN
as a trusted companion (that needs a live registered host binding whose
device matches). Getting those two backwards in either direction is a real
vulnerability, so both are asserted explicitly.
"""

from __future__ import annotations

import time
import uuid
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from global_settings.models import GlobalSettings

from .collaboration import companion_tokens, host_bindings
from .collaboration.policy import Principal
from .models import GroupSession, PairedDevice


def _principal(user, context="learner", sid=None, profile="p1"):
    return Principal(user=user, auth_sid=sid or f"sid-{user.id}",
                     context=context, profile_id=profile)


GOOD_DISPLAY = {
    "display_id": "\\\\.\\DISPLAY1",
    "pixel_width": 1920,
    "pixel_height": 1080,
    "rotation": 0,
    "dpi_scale": 1.25,
    "desktop_bounds": {"x": -1920, "y": 0, "width": 1920, "height": 1080},
    "source": "display",
}


class HostBindingBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username="hb_owner", email="o@hb.test", password="pw")
        cls.peer = User.objects.create_user(
            username="hb_peer", email="p@hb.test", password="pw")

    def setUp(self):
        super().setUp()
        cache.clear()
        now = timezone.localtime()
        self.session = GroupSession.objects.create(
            host=self.owner, topic="HB", scheduled_date=now.date(),
            scheduled_time=now.time().replace(microsecond=0),
            duration_minutes=60, session_type="instant", status="live",
            short_code=uuid.uuid4().hex[:12],
            room_name=f"room-{uuid.uuid4().hex[:8]}",
            room_started_at=timezone.now())
        self.device = PairedDevice.objects.create(
            owner=self.owner, name="Owner desk", platform="windows",
            arch="x64", public_jwk={"kty": "OKP"},
            key_thumbprint=f"t-{uuid.uuid4().hex[:8]}",
            status=PairedDevice.STATUS_PAIRED)
        conf = GlobalSettings.load()
        conf.collaboration_bindings_enabled = True
        conf.collaboration_control_enabled = True
        conf.save(update_fields=["collaboration_bindings_enabled",
                                 "collaboration_control_enabled"])

    def _create(self, **over):
        kwargs = dict(principal=_principal(self.owner), session=self.session,
                      device=self.device, browser_binding_id=uuid.uuid4(),
                      owner_display_name="Owner")
        kwargs.update(over)
        return host_bindings.create(**kwargs)

    def _ready(self):
        hb_id, _ = self._create()
        host_bindings.mark_joining(hb_id, device=self.device,
                                   display=dict(GOOD_DISPLAY))
        host_bindings.mark_ready(hb_id, device=self.device,
                                 participant_sid="PA_companion",
                                 track_sid="TR_screen",
                                 capture_generation=3,
                                 verify_sid=lambda *a: True)
        return hb_id


class LifecycleTests(HostBindingBase):

    def test_a_browser_cannot_cause_capture(self):
        """Creating a binding registers intent and nothing else.

        "No browser API can cause unattended capture." The owner's browser
        gets an id; only the companion, after a local selection, can advance
        it and receive a join token.
        """
        hb_id, record = self._create()
        self.assertEqual(record["state"], host_bindings.STATE_AWAITING)
        self.assertIsNone(record["capture"])
        self.assertIsNone(record["participant_sid"])

    def test_assistance_is_off_until_the_owner_enables_it(self):
        """Sharing a screen is not offering it for control."""
        _, record = self._create()
        self.assertFalse(record["allow_requests"])

    def test_the_full_path_to_ready(self):
        hb_id = self._ready()
        record = host_bindings.get(hb_id)
        self.assertEqual(record["state"], host_bindings.STATE_READY)
        self.assertEqual(record["capture"]["track_sid"], "TR_screen")
        self.assertEqual(record["capture_generation"], 3)

    def test_a_device_can_only_share_once_at_a_time(self):
        self._create()
        with self.assertRaises(host_bindings.HostBindingError) as caught:
            self._create()
        self.assertEqual(caught.exception.code, "device_busy")

    def test_you_cannot_share_someone_elses_device(self):
        with self.assertRaises(host_bindings.HostBindingError) as caught:
            self._create(principal=_principal(self.peer))
        self.assertEqual(caught.exception.code, "device_not_owned")

    def test_readiness_has_a_deadline(self):
        """An intent that never reaches a published track must not linger
        holding the device."""
        hb_id, _ = self._create()
        record = host_bindings.get(hb_id)
        record["readiness_deadline"] = time.time() - 1
        cache.set(f"collab:hostbinding:{hb_id}",
                  __import__("json").dumps(record), timeout=60)
        self.assertIsNone(host_bindings.get(hb_id))
        # ...and the device is free again.
        new_id, _ = self._create()
        self.assertNotEqual(new_id, hb_id)

    def test_a_closed_binding_cannot_restart(self):
        """"Failed/closed bindings cannot restart, so create a fresh binding
        after local approval.\""""
        hb_id = self._ready()
        host_bindings.close(hb_id, principal=_principal(self.owner))
        with self.assertRaises(host_bindings.HostBindingError) as caught:
            host_bindings.mark_joining(hb_id, device=self.device,
                                       display=dict(GOOD_DISPLAY))
        self.assertEqual(caught.exception.status, 409)

    def test_a_changed_capture_closes_the_share(self):
        """A generation change is not absorbed quietly — every grant built
        on this binding was authorised against the old one."""
        hb_id = self._ready()
        with self.assertRaises(host_bindings.HostBindingError) as caught:
            host_bindings.heartbeat(hb_id, device=self.device,
                                    participant_sid="PA_companion",
                                    track_sid="TR_screen",
                                    capture_generation=4)
        self.assertEqual(caught.exception.code, "capture_changed")
        self.assertEqual(host_bindings.get(hb_id)["state"],
                         host_bindings.STATE_FAILED)

    def test_a_replaced_track_closes_the_share(self):
        hb_id = self._ready()
        with self.assertRaises(host_bindings.HostBindingError):
            host_bindings.heartbeat(hb_id, device=self.device,
                                    participant_sid="PA_companion",
                                    track_sid="TR_other",
                                    capture_generation=3)
        self.assertEqual(host_bindings.get(hb_id)["state"],
                         host_bindings.STATE_FAILED)

    def test_ready_verifies_the_sid_against_livekit(self):
        """A companion asserting its own SID is a companion asserting its
        own presence."""
        hb_id, _ = self._create()
        host_bindings.mark_joining(hb_id, device=self.device,
                                   display=dict(GOOD_DISPLAY))
        with self.assertRaises(host_bindings.HostBindingError) as caught:
            host_bindings.mark_ready(hb_id, device=self.device,
                                     participant_sid="PA_lying",
                                     track_sid="TR_screen",
                                     capture_generation=1,
                                     verify_sid=lambda *a: False)
        self.assertEqual(caught.exception.code, "participant_sid_mismatch")

    def test_another_device_cannot_drive_this_binding(self):
        hb_id, _ = self._create()
        other = PairedDevice.objects.create(
            owner=self.owner, name="Other", platform="windows", arch="x64",
            public_jwk={"kty": "OKP"},
            key_thumbprint=f"t-{uuid.uuid4().hex[:8]}",
            status=PairedDevice.STATUS_PAIRED)
        with self.assertRaises(host_bindings.HostBindingError) as caught:
            host_bindings.mark_joining(hb_id, device=other,
                                       display=dict(GOOD_DISPLAY))
        self.assertEqual(caught.exception.status, 404)


class DescriptorValidationTests(HostBindingBase):

    def test_a_valid_descriptor_is_normalised(self):
        out = host_bindings.validate_descriptor(dict(GOOD_DISPLAY))
        self.assertEqual(out["coordinate_space"], "physical_pixels")
        self.assertIsNone(out["track_sid"])

    def test_track_sid_cannot_be_supplied_at_share_time(self):
        """The track does not exist until the companion has published it."""
        bad = dict(GOOD_DISPLAY, track_sid="TR_premature")
        with self.assertRaises(host_bindings.HostBindingError):
            host_bindings.validate_descriptor(bad)

    def test_window_and_tab_capture_are_refused(self):
        """"Browser tab/window capture remains view-only" — only a whole
        display can back a control grant, because only a display gives a
        verified capture-to-desktop mapping."""
        with self.assertRaises(host_bindings.HostBindingError) as caught:
            host_bindings.validate_descriptor(
                dict(GOOD_DISPLAY, source="window"))
        self.assertEqual(caught.exception.status, 422)

    def test_out_of_range_geometry_is_refused(self):
        for field, value in (
            ("pixel_width", 0), ("pixel_width", 40000),
            ("pixel_height", -1), ("rotation", 45),
            ("dpi_scale", 0.1), ("dpi_scale", 99),
            ("display_id", "x" * 200), ("display_id", ""),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(host_bindings.HostBindingError):
                    host_bindings.validate_descriptor(
                        dict(GOOD_DISPLAY, **{field: value}))

    def test_a_huge_pixel_area_is_refused(self):
        with self.assertRaises(host_bindings.HostBindingError):
            host_bindings.validate_descriptor(
                dict(GOOD_DISPLAY, pixel_width=32000, pixel_height=32000))

    def test_nonfinite_scale_is_refused(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(host_bindings.HostBindingError):
                    host_bindings.validate_descriptor(
                        dict(GOOD_DISPLAY, dpi_scale=value))

    def test_a_boolean_is_not_an_integer(self):
        """`isinstance(True, int)` is True in Python, so a bool would slip
        through a naive check and become a dimension of 1."""
        with self.assertRaises(host_bindings.HostBindingError):
            host_bindings.validate_descriptor(
                dict(GOOD_DISPLAY, pixel_width=True))


class CompanionIdentityTests(HostBindingBase):

    def test_exclusion_is_permissive_on_purpose(self):
        """🔴 Direction one: anything prefixed is kept OUT of human counts.

        Fail-safe. The worst a forgery achieves is excluding itself from a
        count it was never entitled to appear in.
        """
        for identity in (f"rcdev_{uuid.uuid4()}", "rcdev_garbage",
                         "rcdev_", "rcdev_" + "x" * 500):
            with self.subTest(identity=identity):
                self.assertTrue(companion_tokens.looks_like_companion(identity))

    def test_human_identities_are_never_mistaken_for_companions(self):
        for identity in (f"{self.owner.id}_{self.session.id}", "", None,
                         "12_34", "rcdevil_x"):
            with self.subTest(identity=identity):
                self.assertFalse(companion_tokens.looks_like_companion(identity))

    def test_trust_requires_a_live_registered_binding(self):
        """🔴 Direction two, and the acceptance criterion verbatim:
        "Prevent unknown prefixed identities being treated as valid
        companion." A well-formed prefix with no binding behind it is
        nothing."""
        orphan = companion_tokens.companion_identity(uuid.uuid4())
        self.assertTrue(companion_tokens.well_formed_companion(orphan))
        self.assertIsNone(companion_tokens.registered_companion(orphan))

    def test_a_registered_companion_resolves(self):
        hb_id = self._ready()
        identity = companion_tokens.companion_identity(hb_id)
        record = companion_tokens.registered_companion(
            identity, room_name=self.session.room_name)
        self.assertIsNotNone(record)
        self.assertEqual(record["host_binding_id"], hb_id)

    def test_a_companion_cannot_vouch_for_itself_in_another_room(self):
        hb_id = self._ready()
        identity = companion_tokens.companion_identity(hb_id)
        self.assertIsNone(companion_tokens.registered_companion(
            identity, room_name="some-other-room"))

    def test_a_closed_binding_stops_being_a_valid_companion(self):
        """Stale credential, no standing: the identity outlives the share,
        the trust does not."""
        hb_id = self._ready()
        identity = companion_tokens.companion_identity(hb_id)
        host_bindings.close(hb_id, principal=_principal(self.owner))
        record = companion_tokens.registered_companion(
            identity, room_name=self.session.room_name)
        self.assertIsNone(record)

    def test_a_store_outage_denies_rather_than_assumes(self):
        hb_id = self._ready()
        identity = companion_tokens.companion_identity(hb_id)
        with mock.patch.object(host_bindings, "get",
                               side_effect=host_bindings.StoreUnavailable()):
            self.assertIsNone(companion_tokens.registered_companion(identity))


@override_settings(LIVEKIT_API_KEY="devkey",
                   LIVEKIT_API_SECRET="devsecret-devsecret-devsecret-32")
class CompanionTokenTests(HostBindingBase):
    """The SDK is faked here so the *grants* can be inspected directly, but
    credentials must still be present — minting refuses without them, which
    is itself asserted in ``tests_host_binding_routes``."""


    def test_the_token_is_screen_only_and_room_scoped(self):
        """No camera, no microphone, one room, 60 seconds."""
        captured = {}

        class FakeGrants:
            def __init__(self, **kw):
                captured.update(kw)
                self.can_publish_sources = None
                for k, v in kw.items():
                    setattr(self, k, v)

        class FakeToken:
            def __init__(self, *a):
                pass

            def with_identity(self, i):
                captured["identity"] = i
                return self

            def with_name(self, n):
                captured["name"] = n
                return self

            def with_ttl(self, t):
                captured["ttl"] = t
                return self

            def with_grants(self, g):
                captured["grants"] = g
                return self

            def to_jwt(self):
                return "fake.jwt.value"

        hb_id = self._ready()
        record = host_bindings.get(hb_id)

        fake_api = mock.MagicMock()
        fake_api.AccessToken = FakeToken
        fake_api.VideoGrants = FakeGrants
        with mock.patch.dict("sys.modules", {"livekit.api": fake_api}):
            jwt_value, identity = companion_tokens.mint_join_token(record)

        self.assertEqual(jwt_value, "fake.jwt.value")
        self.assertEqual(identity, f"rcdev_{hb_id}")
        self.assertEqual(captured["room"], self.session.room_name)
        self.assertTrue(captured["can_publish"])
        self.assertEqual(captured["ttl"].total_seconds(), 60)
        self.assertEqual(captured["grants"].can_publish_sources,
                         ["screen_share"])

    def test_the_token_grants_subscribe_and_that_is_adr02_option_c(self):
        """🔴 Asserted so nobody "fixes" it back to False.

        LiveKit has no data/media split: can_subscribe=False makes the
        companion deaf, receiving no input at all. Media isolation is
        client-side under Option C, with authority moved into the signed
        payload. Flipping this to False would not harden anything — it would
        break the feature while appearing to harden it.
        """
        captured = {}

        class FakeGrants:
            def __init__(self, **kw):
                captured.update(kw)
                self.can_publish_sources = None

        class FakeToken:
            def __init__(self, *a):
                pass

            def with_identity(self, i):
                return self

            def with_name(self, n):
                return self

            def with_ttl(self, t):
                return self

            def with_grants(self, g):
                return self

            def to_jwt(self):
                return "x"

        record = host_bindings.get(self._ready())
        fake_api = mock.MagicMock()
        fake_api.AccessToken = FakeToken
        fake_api.VideoGrants = FakeGrants
        with mock.patch.dict("sys.modules", {"livekit.api": fake_api}):
            companion_tokens.mint_join_token(record)

        self.assertTrue(captured["can_subscribe"])
        self.assertTrue(captured["can_publish_data"])

    def test_availability_never_leaks_the_display_descriptor(self):
        """Display geometry is a fingerprint of someone's desk, not a
        room-wide fact."""
        record = host_bindings.get(self._ready())
        public = host_bindings.public_availability(record)
        blob = str(public)
        self.assertNotIn("DISPLAY1", blob)
        self.assertNotIn("1920", blob)
        self.assertNotIn("TR_screen", blob)
        self.assertIn("host_binding_id", public)
        self.assertTrue(public["ready"])


class AssistanceSwitchTests(HostBindingBase):

    def test_enabling_and_disabling(self):
        hb_id = self._ready()
        record = host_bindings.set_allow_requests(
            hb_id, allow=True, principal=_principal(self.owner))
        self.assertTrue(record["allow_requests"])
        record = host_bindings.set_allow_requests(
            hb_id, allow=False, principal=_principal(self.owner))
        self.assertFalse(record["allow_requests"])

    def test_turning_it_off_cancels_pending_requests(self):
        from .collaboration import grants as grant_svc
        from .models import RemoteControlGrant

        hb_id = self._ready()
        host_bindings.set_allow_requests(hb_id, allow=True,
                                         principal=_principal(self.owner))
        grant = grant_svc.request_control(
            principal=_principal(self.peer), session=self.session,
            device=self.device, target_user=self.owner,
            controller_binding_id=uuid.uuid4(), host_binding_id=hb_id,
            capabilities=["pointer.move"])

        host_bindings.set_allow_requests(hb_id, allow=False,
                                         principal=_principal(self.owner))
        grant.refresh_from_db()
        self.assertIn(grant.status, (grant_svc.REVOKED, grant_svc.EXPIRED))

    def test_a_peer_cannot_flip_someone_elses_switch(self):
        hb_id = self._ready()
        with self.assertRaises(host_bindings.HostBindingError) as caught:
            host_bindings.set_allow_requests(
                hb_id, allow=True, principal=_principal(self.peer))
        self.assertEqual(caught.exception.status, 404)

    def test_closing_a_share_terminates_its_grants(self):
        from .collaboration import grants as grant_svc

        hb_id = self._ready()
        host_bindings.set_allow_requests(hb_id, allow=True,
                                         principal=_principal(self.owner))
        grant = grant_svc.request_control(
            principal=_principal(self.peer), session=self.session,
            device=self.device, target_user=self.owner,
            controller_binding_id=uuid.uuid4(), host_binding_id=hb_id,
            capabilities=["pointer.move"])

        host_bindings.close(hb_id, principal=_principal(self.owner))
        grant.refresh_from_db()
        # 🔴 `revoked`, not `failed`, and that is the protocol being obeyed
        # rather than a compromise: `failed` is only reachable from
        # approved/active/paused. This grant was still `requested`, so it is
        # revoked — with `capture_lost` preserved as the reason, so the
        # audit still says what actually happened.
        self.assertEqual(grant.status, grant_svc.REVOKED)
        self.assertEqual(grant.reason, "capture_lost")

    def test_closing_a_share_terminates_an_ACTIVE_grant_as_failed(self):
        """The counterpart: once approved and activated, capture loss is a
        genuine `failed`, which is what the protocol's table says."""
        from .collaboration import grants as grant_svc

        hb_id = self._ready()
        host_bindings.set_allow_requests(hb_id, allow=True,
                                         principal=_principal(self.owner))
        grant = grant_svc.request_control(
            principal=_principal(self.peer), session=self.session,
            device=self.device, target_user=self.owner,
            controller_binding_id=uuid.uuid4(), host_binding_id=hb_id,
            capabilities=["pointer.move"])
        grant_svc.decide(grant.public_id, principal=_principal(self.owner),
                         allow=True, capabilities=["pointer.move"],
                         expected_version=grant.version)
        grant.refresh_from_db()
        nonce = grant_svc.issue_consent_nonce(grant.public_id,
                                              device=self.device)
        grant.refresh_from_db()
        grant_svc.activate(grant.public_id, device=self.device,
                           consent_nonce=nonce,
                           expected_version=grant.version,
                           capture_generation=3, local_confirm=True)

        host_bindings.close(hb_id, principal=_principal(self.owner))
        grant.refresh_from_db()
        self.assertEqual(grant.status, grant_svc.FAILED)
        self.assertEqual(grant.reason, "capture_lost")
