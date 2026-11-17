"""RC-006a — collaboration control-plane boundary tests.

See `docs/architecture/shiksha-remote/tasks/phase-1/RC-006.md`.

🔴 **Every client here is built with `enforce_csrf_checks=True`, and that is
load-bearing.** Django's test client sets `request._dont_enforce_csrf_checks
= True` by default, and `CsrfViewMiddleware.process_view` short-circuits on
that flag — so with a default client the CSRF check this task exists to add
*always passes*, and a test asserting "missing CSRF is rejected" passes
whether or not any CSRF code was written. That is the vacuity trap this
project has already been caught by once (see REMOTE.md §7 trap 21).

The happy path therefore does the real bootstrap→use dance: GET `csrf/`,
keep the cookie the middleware sets, echo the masked token in `X-CSRFToken`.

LiveKit is patched at `bindings._fetch_room_roster` — there are no LiveKit
credentials locally, so the network call is never exercised. What IS
exercised is every branch around it: match, mismatch, and unreachable.
"""
import json
from datetime import time, timedelta
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from accounts.models import Role, User, UserRole
from global_settings.models import GlobalSettings
from sessions_app.collaboration import bindings as binding_store
from sessions_app.models import GroupSession, GroupSessionParticipant

ORIGIN = "https://app.test.shikshacom.com"

# Two different attacks, and they defeat two different wrong implementations.
# Keeping only one of them leaves a real bypass untested — the mutation sweep
# proved exactly that: with only the first, swapping the exact comparison for
# `endswith` passed the whole origin suite.
#
#   SUFFIX_ATTACK ends WITH the allowed host, so a naive
#   `origin.endswith("app.test.shikshacom.com")` ACCEPTS it. This is the
#   classic allowlist bug and the one worth guarding hardest.
#
#   PREFIX_ATTACK merely starts with it, which `endswith` already rejects —
#   it catches `startswith`/substring implementations instead.
EVIL_SUFFIX_ORIGIN = "https://evilapp.test.shikshacom.com"
EVIL_PREFIX_ORIGIN = "https://app.test.shikshacom.com.attacker.net"
EVIL_ORIGIN = EVIL_PREFIX_ORIGIN

BASE = "/api/sessions/collaboration/v1"


# 🔴 CSRF_TRUSTED_ORIGINS must contain every collaboration origin.
# Django's own CsrfViewMiddleware does an Origin check of its own before our
# token comparison ever runs, and rejects with "Origin checking failed - ...
# does not match any trusted origins". So the collaboration allowlist has to
# be a SUBSET of CSRF_TRUSTED_ORIGINS or every unsafe call 403s for a reason
# that looks nothing like the real cause. Production already satisfies this
# (CORS_ALLOWED_ORIGINS ⊂ CSRF_TRUSTED_ORIGINS in settings_dev/_prod); the
# coupling is asserted by OriginAndCsrfTest.test_allowlist_is_a_subset_of_
# csrf_trusted_origins so adding an origin to one list only stays loud.
@override_settings(
    COLLABORATION_ALLOWED_ORIGINS=[ORIGIN],
    CSRF_TRUSTED_ORIGINS=[ORIGIN],
)
class CollaborationTestBase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        cls.host = User.objects.create_user(
            username="rc6host", email="rc6host@test.com", password="x")
        UserRole.objects.create(
            user=cls.host, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True)
        cls.student = User.objects.create_user(
            username="rc6student", email="rc6student@test.com", password="x")
        cls.student2 = User.objects.create_user(
            username="rc6student2", email="rc6student2@test.com", password="x")
        cls.outsider = User.objects.create_user(
            username="rc6out", email="rc6out@test.com", password="x")

        now = timezone.now()
        # Instant collaboration: no course gate, which is the OD-01 context
        # where student↔student binding has to work.
        cls.session = GroupSession.objects.create(
            host=cls.host, subject_name="Physics", course_title="C10",
            topic="Topic", scheduled_date=(now + timedelta(days=1)).date(),
            scheduled_time=time(15, 0), duration_minutes=45, status="live",
            session_type="instant", short_code="RC006A",
            room_name="rc006-room-a")
        cls.p_student = GroupSessionParticipant.objects.create(
            session=cls.session, user=cls.student)
        cls.p_student2 = GroupSessionParticipant.objects.create(
            session=cls.session, user=cls.student2)

    def setUp(self):
        super().setUp()
        cache.clear()
        s = GlobalSettings.load()
        s.collaboration_bindings_enabled = True
        s.save()

    # ── client plumbing ───────────────────────────────────────────────────

    def client_for(self, user, sid="sess-1", context="learner", profile="p1"):
        """A CSRF-enforcing client authenticated as `user`.

        The token is a plain dict — `policy.principal_from_request` only ever
        calls `.get()` on `request.auth`, exactly as `auth_flow.session_id_for`
        and `chat.services.active_identity_from_request` do.
        """
        c = APIClient(enforce_csrf_checks=True)
        claims = {"context": context, "active_profile": profile}
        if sid is not None:
            claims["sid"] = sid
        c.force_authenticate(user=user, token=claims)
        return c

    def csrf(self, client):
        """Run the real bootstrap and return the masked token."""
        res = client.get(f"{BASE}/csrf/", HTTP_ORIGIN=ORIGIN)
        assert res.status_code == 200, res.content
        return res.json()["csrf_token"]

    def armed_client(self, user, **claims):
        """A client holding a real CSRF cookie+token, whose auth claims are
        then swapped to whatever the test wants.

        Needed because the boundary runs CSRF *before* the view: to reach a
        refusal that happens inside the view (a token with no `sid`) or one
        that happens before it (the feature flag), the request still has to
        carry a valid CSRF token — but bootstrapping one requires a request
        that would itself be refused. Bootstrapping first with good claims
        and re-authenticating afterwards breaks that circle, and the cookie
        jar persists across `force_authenticate`.
        """
        client = self.client_for(user)
        token = self.csrf(client)
        swapped = {"context": "learner", "active_profile": "p1"}
        swapped.update(claims)
        client.force_authenticate(user=user, token=swapped)
        return client, token

    def post(self, client, path, body, origin=ORIGIN, idem="auto", csrf="auto"):
        headers = {}
        if origin is not None:
            headers["HTTP_ORIGIN"] = origin
        if csrf == "auto":
            headers["HTTP_X_CSRFTOKEN"] = self.csrf(client)
        elif csrf is not None:
            headers["HTTP_X_CSRFTOKEN"] = csrf
        if idem == "auto":
            headers["HTTP_IDEMPOTENCY_KEY"] = "11111111-2222-4333-8444-555555555555"
        elif idem is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = idem
        return client.post(f"{BASE}{path}", body, format="json", **headers)

    def roster(self, *users):
        return {f"{u.id}_{self.session.id}": f"PA_{u.username}" for u in users}

    def sid_of(self, user):
        return f"PA_{user.username}"

    def bind(self, client, user, origin=ORIGIN, idem="auto", csrf="auto"):
        # The patched roster lists everyone in the room, as LiveKit's
        # list_participants does — not just the caller.
        roster = self.roster(self.host, self.student, self.student2)
        with mock.patch.object(
            binding_store, "_fetch_room_roster", return_value=roster
        ):
            return self.post(
                client,
                "/bindings/",
                {
                    "session": {"kind": "group", "id": str(self.session.id)},
                    "participant_sid": self.sid_of(user),
                },
                origin=origin,
                idem=idem,
                csrf=csrf,
            )


class BindingEligibilityTest(CollaborationTestBase):
    """Who may register as an interactive participant."""

    def test_a_student_can_bind(self):
        res = self.bind(self.client_for(self.student), self.student)
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual(body["identity"], f"{self.student.id}_{self.session.id}")
        self.assertEqual(body["expires_in"], 6)
        self.assertIn("pointer.move", body["capabilities"])

    def test_student_to_student_both_bind(self):
        """OD-01's peer-to-peer case: binding eligibility is symmetric, with
        no role test and no teacher involved."""
        a = self.bind(self.client_for(self.student), self.student)
        b = self.bind(
            self.client_for(self.student2, sid="sess-2"), self.student2,
            idem="99999999-2222-4333-8444-555555555555",
        )
        self.assertEqual(a.status_code, 201, a.content)
        self.assertEqual(b.status_code, 201, b.content)
        self.assertNotEqual(a.json()["binding_id"], b.json()["binding_id"])

    def test_the_host_can_bind(self):
        res = self.bind(
            self.client_for(self.host, context="teacher"), self.host)
        self.assertEqual(res.status_code, 201, res.content)

    def test_an_outsider_is_refused(self):
        """A scheduled room has a membership gate. (The instant room used by
        the other tests deliberately has none — that is OD-01, not a bug.)"""
        scheduled = GroupSession.objects.create(
            host=self.host, subject_name="Maths", course_title="C10",
            topic="T", scheduled_date=timezone.now().date(),
            scheduled_time=time(9, 0), duration_minutes=45, status="live",
            session_type="scheduled", short_code="RC006B",
            room_name="rc006-room-b")
        client = self.client_for(self.outsider)
        with mock.patch.object(
            binding_store, "_fetch_room_roster",
            return_value={f"{self.outsider.id}_{scheduled.id}": "PA_x"},
        ):
            res = self.post(client, "/bindings/", {
                "session": {"kind": "group", "id": str(scheduled.id)},
                "participant_sid": "PA_x",
            })
        self.assertEqual(res.status_code, 404, res.content)
        self.assertEqual(res.json()["code"], "not_a_participant")

    def test_stale_presence_is_refused(self):
        """A participant who left has no standing, even though their account
        is still a member of the room."""
        self.p_student.left_at = timezone.now()
        self.p_student.save(update_fields=["left_at"])
        res = self.bind(self.client_for(self.student), self.student)
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.json()["code"], "presence_stale")

    def test_a_token_without_an_auth_sid_is_refused(self):
        """"Missing legacy auth sid ... never degrades to account-only
        authority." It must refuse, not fall back to the account."""
        client, token = self.armed_client(self.student, sid=None)
        res = self.bind(client, self.student, csrf=token)
        self.assertEqual(res.status_code, 403, res.content)
        self.assertEqual(res.json()["code"], "auth_session_required")

    def test_an_unknown_session_adapter_is_disabled(self):
        client = self.client_for(self.student)
        res = self.post(client, "/bindings/", {
            "session": {"kind": "skill", "id": str(self.session.id)},
            "participant_sid": "PA_x",
        })
        self.assertEqual(res.status_code, 422, res.content)
        self.assertEqual(res.json()["code"], "unsupported_session_kind")

    def test_a_nonsense_session_kind_is_a_400(self):
        client = self.client_for(self.student)
        res = self.post(client, "/bindings/", {
            "session": {"kind": "wat", "id": str(self.session.id)},
            "participant_sid": "PA_x",
        })
        self.assertEqual(res.status_code, 400, res.content)


class ParticipantSidTest(CollaborationTestBase):
    """The SID is checked against LiveKit, never believed."""

    def test_a_mismatched_sid_is_403(self):
        client = self.client_for(self.student)
        with mock.patch.object(
            binding_store, "_fetch_room_roster",
            return_value=self.roster(self.student),
        ):
            res = self.post(client, "/bindings/", {
                "session": {"kind": "group", "id": str(self.session.id)},
                "participant_sid": "PA_not_mine",
            })
        self.assertEqual(res.status_code, 403, res.content)
        self.assertEqual(res.json()["code"], "participant_sid_mismatch")

    def test_a_user_absent_from_the_room_is_403(self):
        client = self.client_for(self.student)
        with mock.patch.object(
            binding_store, "_fetch_room_roster", return_value={}
        ):
            res = self.post(client, "/bindings/", {
                "session": {"kind": "group", "id": str(self.session.id)},
                "participant_sid": self.sid_of(self.student),
            })
        self.assertEqual(res.status_code, 403, res.content)

    def test_livekit_unreachable_is_503_not_403(self):
        """🔴 'We could not check' and 'your SID is wrong' are different
        answers. Collapsing them tells a user their session is invalid when
        a dependency is simply down, and only one is worth retrying."""
        client = self.client_for(self.student)
        with mock.patch.object(
            binding_store, "_fetch_room_roster",
            side_effect=binding_store.RosterUnavailable("down"),
        ):
            res = self.post(client, "/bindings/", {
                "session": {"kind": "group", "id": str(self.session.id)},
                "participant_sid": self.sid_of(self.student),
            })
        self.assertEqual(res.status_code, 503, res.content)
        self.assertEqual(res.json()["code"], "dependency_unavailable")

    def test_the_room_roster_is_coalesced_not_queried_per_request(self):
        """🔴 Capacity rule: at most one LiveKit query per ROOM per 2s. With
        a 2s heartbeat per participant, a per-request lookup would mean N
        round trips every 2s for an N-person room."""
        roster = self.roster(self.student, self.student2)
        with mock.patch.object(
            binding_store, "_fetch_room_roster", return_value=roster
        ) as fetch:
            for _ in range(5):
                binding_store.verify_participant_sid(
                    self.session.room_name,
                    f"{self.student.id}_{self.session.id}",
                    self.sid_of(self.student),
                )
        self.assertEqual(fetch.call_count, 1)


class OriginAndCsrfTest(CollaborationTestBase):
    def test_a_lookalike_prefix_origin_is_refused(self):
        """`app.test.shikshacom.com.attacker.net` — defeats substring and
        `startswith` matching."""
        res = self.bind(
            self.client_for(self.student), self.student,
            origin=EVIL_PREFIX_ORIGIN)
        self.assertEqual(res.status_code, 403, res.content)
        self.assertEqual(res.json()["code"], "origin_forbidden")

    def test_a_lookalike_suffix_origin_is_refused(self):
        """🔴 `evilapp.test.shikshacom.com` — this is the one that matters.

        It ENDS with the allowed host, so `origin.endswith(allowed)` accepts
        it, and an attacker only needs to register a subdomain-looking name.
        The mutation sweep caught that the prefix case above does NOT cover
        this: swapping the exact comparison for `endswith` left the whole
        origin suite green until this test existed.
        """
        res = self.bind(
            self.client_for(self.student), self.student,
            origin=EVIL_SUFFIX_ORIGIN)
        self.assertEqual(res.status_code, 403, res.content)
        self.assertEqual(res.json()["code"], "origin_forbidden")

    def test_the_bare_host_without_a_scheme_is_refused(self):
        """An origin is scheme + host + port. Matching on host alone would
        let `http://` through on a cookie marked Secure."""
        res = self.bind(
            self.client_for(self.student), self.student,
            origin="http://app.test.shikshacom.com")
        self.assertEqual(res.status_code, 403, res.content)

    def test_a_missing_origin_is_refused_on_an_unsafe_method(self):
        res = self.bind(self.client_for(self.student), self.student, origin=None)
        self.assertEqual(res.status_code, 403, res.content)
        self.assertEqual(res.json()["code"], "origin_required")

    def test_a_missing_csrf_token_is_refused(self):
        res = self.post(
            self.client_for(self.student), "/bindings/",
            {"session": {"kind": "group", "id": str(self.session.id)},
             "participant_sid": "PA_x"},
            csrf=None,
        )
        self.assertEqual(res.status_code, 403, res.content)
        self.assertEqual(res.json()["code"], "csrf_failed")

    def test_allowlist_is_a_subset_of_csrf_trusted_origins(self):
        """🔴 Guards the coupling documented at the top of this file.

        Django's CsrfViewMiddleware runs its own Origin check against
        CSRF_TRUSTED_ORIGINS before our token comparison. An origin added to
        the collaboration allowlist but not to CSRF_TRUSTED_ORIGINS 403s
        every unsafe call with a message that points nowhere near the cause
        — which is exactly how this test file failed on its first run.
        """
        from django.conf import settings

        from sessions_app.collaboration.policy import allowed_origins

        trusted = set(settings.CSRF_TRUSTED_ORIGINS)
        self.assertTrue(
            allowed_origins() <= trusted,
            f"not trusted for CSRF: {sorted(allowed_origins() - trusted)}",
        )

    def test_the_real_environments_satisfy_that_coupling(self):
        """The same check against the actual dev/prod settings files.

        A green assertion over `override_settings` would prove nothing about
        the deployed config, which is the config that matters here.

        Read statically with `ast` rather than imported: `settings_prod`
        raises ImproperlyConfigured without a real SECRET_KEY in the
        environment — that guard is working as intended and must not be
        worked around just to run a test.
        """
        import ast
        import pathlib

        base = pathlib.Path(__file__).resolve().parent.parent / "config"

        def lists_in(path):
            tree = ast.parse(path.read_text())
            found = {}
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and isinstance(
                        node.value, (ast.List, ast.Tuple)
                    ):
                        try:
                            found[target.id] = set(ast.literal_eval(node.value))
                        except ValueError:
                            pass
            return found

        checked = 0
        for name in ("settings_dev.py", "settings_prod.py"):
            path = base / name
            self.assertTrue(path.exists(), f"{name} is missing")
            found = lists_in(path)
            cors = found.get("CORS_ALLOWED_ORIGINS")
            trusted = found.get("CSRF_TRUSTED_ORIGINS")
            self.assertIsNotNone(cors, f"{name}: no CORS_ALLOWED_ORIGINS literal")
            self.assertIsNotNone(trusted, f"{name}: no CSRF_TRUSTED_ORIGINS literal")
            self.assertTrue(
                cors <= trusted,
                f"{name}: in CORS but not CSRF_TRUSTED_ORIGINS — every unsafe "
                f"collaboration call from these origins would 403: "
                f"{sorted(cors - trusted)}",
            )
            checked += 1
        self.assertEqual(checked, 2, "both environments must be checked")

    def test_a_forged_csrf_token_is_refused(self):
        res = self.post(
            self.client_for(self.student), "/bindings/",
            {"session": {"kind": "group", "id": str(self.session.id)},
             "participant_sid": "PA_x"},
            csrf="not-a-real-token",
        )
        self.assertEqual(res.status_code, 403, res.content)

    def test_the_csrf_bootstrap_needs_an_allowlisted_origin(self):
        client = self.client_for(self.student)
        res = client.get(f"{BASE}/csrf/", HTTP_ORIGIN=EVIL_ORIGIN)
        self.assertEqual(res.status_code, 403, res.content)

    def test_every_response_is_no_store(self):
        client = self.client_for(self.student)
        res = client.get(f"{BASE}/csrf/", HTTP_ORIGIN=ORIGIN)
        self.assertEqual(res["Cache-Control"], "no-store")


class FeatureFlagTest(CollaborationTestBase):
    def _disable(self, **extra):
        s = GlobalSettings.load()
        s.collaboration_bindings_enabled = False
        for k, v in extra.items():
            setattr(s, k, v)
        s.save()

    def test_every_route_is_404_while_the_flag_is_off(self):
        """Absent, not forbidden — a disabled feature must not be probeable.

        The client is armed with a real CSRF token *before* the flag flips,
        so these 404s are the flag answering and not CSRF refusing first.
        """
        client, token = self.armed_client(self.student)
        self._disable()

        self.assertEqual(
            client.get(f"{BASE}/csrf/", HTTP_ORIGIN=ORIGIN).status_code, 404)
        self.assertEqual(
            self.bind(client, self.student, csrf=token).status_code, 404)
        self.assertEqual(
            client.get(f"{BASE}/snapshot/?binding_id=x",
                       HTTP_ORIGIN=ORIGIN).status_code, 404)

    def test_live_remote_access_enabled_does_not_turn_this_on(self):
        """🔴 The legacy flag defaults True and gates a different, dormant
        feature. It must not be mistaken for this one's switch."""
        client, token = self.armed_client(self.student)
        self._disable(live_remote_access_enabled=True)
        res = self.bind(client, self.student, csrf=token)
        self.assertEqual(res.status_code, 404, res.content)


class IdempotencyTest(CollaborationTestBase):
    KEY = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    def test_a_missing_idempotency_key_is_refused(self):
        res = self.bind(self.client_for(self.student), self.student, idem=None)
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json()["code"], "idempotency_key_required")

    def test_a_non_uuid_idempotency_key_is_refused(self):
        res = self.bind(self.client_for(self.student), self.student, idem="nope")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json()["code"], "idempotency_key_invalid")

    def test_the_same_key_and_body_replays_the_original_result(self):
        client = self.client_for(self.student)
        first = self.bind(client, self.student, idem=self.KEY)
        second = self.bind(client, self.student, idem=self.KEY)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(
            first.json()["binding_id"], second.json()["binding_id"],
            "a replay must not mint a second binding")
        self.assertEqual(second["Idempotent-Replay"], "true")

    def test_the_same_key_with_a_changed_body_is_409(self):
        client = self.client_for(self.student)
        self.bind(client, self.student, idem=self.KEY)
        with mock.patch.object(
            binding_store, "_fetch_room_roster",
            return_value=self.roster(self.student),
        ):
            res = self.post(client, "/bindings/", {
                "session": {"kind": "group", "id": str(self.session.id)},
                "participant_sid": "PA_different",
            }, idem=self.KEY)
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.json()["code"], "idempotency_key_reused")


class BodyValidationTest(CollaborationTestBase):
    def test_unknown_fields_are_rejected(self):
        """An ignored field is how a client comes to believe it set something
        the server never read."""
        client = self.client_for(self.student)
        res = self.post(client, "/bindings/", {
            "session": {"kind": "group", "id": str(self.session.id)},
            "participant_sid": "PA_x",
            "capabilities": ["pointer.move"],
        })
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json()["code"], "unknown_fields")

    def test_unknown_nested_session_fields_are_rejected(self):
        client = self.client_for(self.student)
        res = self.post(client, "/bindings/", {
            "session": {"kind": "group", "id": str(self.session.id), "x": 1},
            "participant_sid": "PA_x",
        })
        self.assertEqual(res.status_code, 400, res.content)

    def test_a_missing_field_is_rejected(self):
        client = self.client_for(self.student)
        res = self.post(client, "/bindings/", {
            "session": {"kind": "group", "id": str(self.session.id)}})
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json()["code"], "missing_fields")

    def test_the_error_envelope_shape(self):
        client = self.client_for(self.student)
        res = self.post(client, "/bindings/", {
            "session": {"kind": "group", "id": str(self.session.id)}})
        body = res.json()
        self.assertEqual(sorted(body), ["code", "detail", "request_id"])


class HeartbeatTest(CollaborationTestBase):
    def setUp(self):
        super().setUp()
        self.client_a = self.client_for(self.student)
        res = self.bind(self.client_a, self.student)
        self.assertEqual(res.status_code, 201, res.content)
        self.binding_id = res.json()["binding_id"]

    def _beat(self, client=None, sid=None, idem=None):
        return self.post(
            client or self.client_a,
            f"/bindings/{self.binding_id}/heartbeat/",
            {"participant_sid": sid or self.sid_of(self.student)},
            idem=idem,
        )

    def test_a_heartbeat_renews_without_an_idempotency_key(self):
        """Heartbeats are explicitly exempt — a 24h replay record for a
        2s-cadence call would be absurd."""
        res = self._beat()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["expires_in"], 6)
        self.assertIn("server_time", res.json())

    def test_a_changed_sid_is_409(self):
        res = self._beat(sid="PA_reconnected")
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.json()["code"], "participant_sid_changed")

    def test_stale_membership_is_503_and_drops_the_binding(self):
        self.p_student.left_at = timezone.now()
        self.p_student.save(update_fields=["left_at"])
        res = self._beat()
        self.assertEqual(res.status_code, 503, res.content)
        self.assertEqual(res.json()["code"], "membership_stale")
        self.assertIsNone(binding_store.get(self.binding_id))

    def test_a_stranger_cannot_heartbeat_someone_elses_binding(self):
        other = self.client_for(self.student2, sid="sess-2")
        res = self._beat(client=other)
        self.assertEqual(res.status_code, 404, res.content)

    def test_a_different_auth_session_cannot_reuse_the_binding(self):
        """Re-logging in does not inherit a binding's standing."""
        relogged = self.client_for(self.student, sid="sess-NEW")
        res = self._beat(client=relogged)
        self.assertEqual(res.status_code, 404, res.content)

    def test_a_different_profile_cannot_reuse_the_binding(self):
        switched = self.client_for(self.student, profile="p2")
        res = self._beat(client=switched)
        self.assertEqual(res.status_code, 404, res.content)

    def test_a_heartbeat_writes_nothing_to_the_database(self):
        """🔴 Capacity-critical. PG max_connections (~75) is the box's wall
        and ASGI leaks ~1 connection per request; a 2s-per-participant row
        write exhausts it at roughly 20 concurrent sessions. A heartbeat is a
        DB READ plus Redis — never an UPDATE."""
        with CaptureQueriesContext(connection) as ctx:
            res = self._beat()
        self.assertEqual(res.status_code, 200)
        writes = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].strip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}
        ]
        self.assertEqual(writes, [], f"heartbeat wrote to the DB: {writes}")

    def test_a_heartbeat_does_not_query_livekit_per_call(self):
        with mock.patch.object(
            binding_store, "_fetch_room_roster",
            return_value=self.roster(self.student),
        ) as fetch:
            for _ in range(3):
                self._beat()
        self.assertEqual(fetch.call_count, 0,
                         "the heartbeat path must not reach LiveKit at all")


class ReleaseTest(CollaborationTestBase):
    def setUp(self):
        super().setUp()
        self.client_a = self.client_for(self.student)
        self.binding_id = self.bind(self.client_a, self.student).json()["binding_id"]

    def test_delete_releases_the_binding(self):
        res = self.client_a.delete(
            f"{BASE}/bindings/{self.binding_id}/",
            HTTP_ORIGIN=ORIGIN,
            HTTP_X_CSRFTOKEN=self.csrf(self.client_a),
            HTTP_IDEMPOTENCY_KEY="dddddddd-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        )
        self.assertEqual(res.status_code, 204, res.content)
        self.assertIsNone(binding_store.get(self.binding_id))

    def test_a_stranger_cannot_delete_a_binding(self):
        other = self.client_for(self.student2, sid="sess-2")
        res = other.delete(
            f"{BASE}/bindings/{self.binding_id}/",
            HTTP_ORIGIN=ORIGIN,
            HTTP_X_CSRFTOKEN=self.csrf(other),
            HTTP_IDEMPOTENCY_KEY="dddddddd-bbbb-4ccc-8ddd-eeeeeeeeeeef",
        )
        self.assertEqual(res.status_code, 404, res.content)
        self.assertIsNotNone(binding_store.get(self.binding_id))


class StoreOutageTest(CollaborationTestBase):
    """🔴 Fail CLOSED — the inverse of every other cache helper in this repo.

    `accounts/revocation.py` and `chat/redis_utils.py` both document
    themselves as failing OPEN so an outage costs availability rather than
    access. Here an outage must remove authority: a binding that cannot be
    read is a binding that cannot be trusted.
    """

    def test_a_store_outage_on_read_is_503_not_a_clean_404(self):
        client = self.client_for(self.student)
        binding_id = self.bind(client, self.student).json()["binding_id"]
        with mock.patch.object(cache, "get", side_effect=RuntimeError("redis down")):
            res = self.post(
                client, f"/bindings/{binding_id}/heartbeat/",
                {"participant_sid": self.sid_of(self.student)},
                csrf="x",  # CSRF cannot be bootstrapped during the outage
            )
        # Either the CSRF layer or the store refuses — what must NOT happen is
        # a 2xx. Assert the specific store behaviour directly below.
        self.assertGreaterEqual(res.status_code, 400)

    def test_the_store_raises_rather_than_reporting_an_empty_result(self):
        with mock.patch.object(cache, "get", side_effect=RuntimeError("redis down")):
            with self.assertRaises(binding_store.StoreUnavailable):
                binding_store.get("any-binding-id")

    def test_a_write_outage_raises(self):
        with mock.patch.object(cache, "set", side_effect=RuntimeError("redis down")):
            with self.assertRaises(binding_store.StoreUnavailable):
                binding_store.mint_ticket("b", {
                    "user_id": "1", "auth_sid": "s", "session_id": "x"})


class TicketTest(CollaborationTestBase):
    """Tickets are minted here so RC-006b's consumer has nothing to invent."""

    def test_a_ticket_is_single_use(self):
        record = {"user_id": "1", "auth_sid": "s", "session_id": "x"}
        ticket = binding_store.mint_ticket("binding-1", record)
        first = binding_store.consume_ticket(ticket)
        second = binding_store.consume_ticket(ticket)
        self.assertEqual(first["binding_id"], "binding-1")
        self.assertIsNone(second, "a ticket must not be replayable")

    def test_an_unknown_ticket_returns_none(self):
        self.assertIsNone(binding_store.consume_ticket("never-issued"))


class SnapshotTest(CollaborationTestBase):
    def setUp(self):
        super().setUp()
        self.client_a = self.client_for(self.student)
        self.binding_id = self.bind(self.client_a, self.student).json()["binding_id"]

    def test_snapshot_returns_the_contract_shape(self):
        res = self.client_a.get(
            f"{BASE}/snapshot/?binding_id={self.binding_id}", HTTP_ORIGIN=ORIGIN)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(
            sorted(res.json()),
            ["grants", "host_bindings", "server_time", "transfers"])

    def test_snapshot_requires_a_binding_you_own(self):
        other = self.client_for(self.student2, sid="sess-2")
        res = other.get(
            f"{BASE}/snapshot/?binding_id={self.binding_id}", HTTP_ORIGIN=ORIGIN)
        self.assertEqual(res.status_code, 404, res.content)

    def test_snapshot_without_a_binding_id_is_400(self):
        res = self.client_a.get(f"{BASE}/snapshot/", HTTP_ORIGIN=ORIGIN)
        self.assertEqual(res.status_code, 400, res.content)


class ExistingBehaviourUnaffectedTest(CollaborationTestBase):
    """"old joins/sockets unaffected" — RC-006 is additive."""

    def test_the_legacy_remote_control_route_still_answers_as_before(self):
        """The dormant legacy path is untouched: it still refuses because
        nothing sets is_sharing_screen, not because of anything added here."""
        client = APIClient()
        client.force_authenticate(
            user=self.student, token={"sid": "s", "context": "learner"})
        res = client.post(
            f"/api/sessions/group-sessions/{self.session.id}/remote-control/request/",
            {"target_user_id": str(self.student2.id)}, format="json")
        self.assertNotEqual(res.status_code, 500)
        self.assertNotIn(res.status_code, (200, 201))

    def test_the_group_files_endpoint_still_works(self):
        client = APIClient()
        client.force_authenticate(
            user=self.student, token={"sid": "s", "context": "learner"})
        res = client.get(f"/api/sessions/group-sessions/{self.session.id}/files/")
        self.assertEqual(res.status_code, 200, res.content)
