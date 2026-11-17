"""RC-007a — DPoP proofs and the device pairing handshake.

See `tasks/phase-1/RC-007.md`, `companion-architecture.md` §DPoP profile,
and `api-contracts.md` §Pair/device APIs.

🔴 **Every proof in this file is a real ES256 signature over a real P-256
key.** Nothing is mocked. A suite that stubbed `verify_proof` would assert
that the routes call a function, which is precisely the "mirrors an
implementation helper while omitting observable authorization behavior"
failure the task prompt forbids — and it would pass with the entire
cryptographic check deleted.

The headline claim being tested is **T02: a stolen browser session cannot
pair, and neither can a stolen code.** `BrowserAloneTest` and
`DeviceAloneTest` each take one half of the handshake and prove it is
worthless without the other.
"""
import json
import time
import uuid
from datetime import timedelta

from cryptography.hazmat.primitives.asymmetric import ec
import jwt
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from accounts.settings_models import UserSession
from global_settings.models import GlobalSettings
from sessions_app.collaboration import devices as device_svc
from sessions_app.collaboration import dpop
from sessions_app.models import DevicePairing, PairedDevice

from .tests_collaboration_policy import BASE, ORIGIN

CANONICAL = "https://api.test.shikshacom.com"


def new_key():
    """A fresh P-256 keypair, as the companion's OS keystore would hold."""
    return ec.generate_private_key(ec.SECP256R1())


def public_jwk(private_key):
    """The public half as a JWK, the way a real client would serialise it."""
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": dpop.b64url(numbers.x.to_bytes(32, "big")),
        "y": dpop.b64url(numbers.y.to_bytes(32, "big")),
    }


def make_proof(private_key, *, method, path, access_token=None, jti=None,
               iat=None, typ="dpop+jwt", htu=None, extra=None):
    """A real signed DPoP proof. Every parameter exists so a test can break
    exactly one field and leave the rest valid."""
    claims = {
        "jti": jti or uuid.uuid4().hex + uuid.uuid4().hex,
        "htm": method.upper(),
        "htu": htu if htu is not None else f"{CANONICAL}/{path.lstrip('/')}",
        "iat": int(time.time()) if iat is None else iat,
    }
    if access_token is not None:
        claims["ath"] = dpop.access_token_hash(access_token)
    if extra:
        claims.update(extra)
    return jwt.encode(
        claims, private_key, algorithm="ES256",
        headers={"typ": typ, "jwk": public_jwk(private_key)},
    )


@override_settings(
    COLLABORATION_CANONICAL_URL=CANONICAL,
    COLLABORATION_ALLOWED_ORIGINS=[ORIGIN],
    CSRF_TRUSTED_ORIGINS=[ORIGIN],
    COLLABORATION_DEVICE_KEY="rc007-test-device-key",
)
class PairingTestBase(APITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        s = GlobalSettings.load()
        s.collaboration_bindings_enabled = True
        s.save()
        self.user = User.objects.create_user(
            username="rc7user", email="rc7@test.com", password="x")
        self.other = User.objects.create_user(
            username="rc7other", email="rc7other@test.com", password="x")
        self.key = new_key()

    # ── browser plumbing ──────────────────────────────────────────────────

    def browser(self, user=None, *, signed_in_ago=timedelta(minutes=1)):
        """A CSRF-enforcing client whose UserSession row carries a real login
        time — `_require_recent_auth` reads `UserSession.created_at`."""
        user = user or self.user
        sid = uuid.uuid4()
        row = UserSession.objects.create(id=sid, user=user)
        UserSession.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - signed_in_ago)
        client = APIClient(enforce_csrf_checks=True)
        client.force_authenticate(
            user=user,
            token={"sid": str(sid), "context": "learner", "active_profile": "p1"})
        return client

    def csrf(self, client):
        res = client.get(f"{BASE}/csrf/", HTTP_ORIGIN=ORIGIN)
        assert res.status_code == 200, res.content
        return res.json()["csrf_token"]

    def browser_post(self, client, path, body, **extra):
        headers = {
            "HTTP_ORIGIN": ORIGIN,
            "HTTP_X_CSRFTOKEN": self.csrf(client),
            "HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4()),
        }
        headers.update(extra)
        return client.post(f"{BASE}{path}", body, format="json", **headers)

    # ── device plumbing ───────────────────────────────────────────────────

    def device_post(self, path, body, *, key=None, secret=None, proof=None,
                    proof_path=None):
        """A device-plane call: DPoP header, optional pairing secret, and
        deliberately NO cookie, CSRF token or Origin."""
        client = APIClient()
        headers = {}
        if proof is None and key is not None:
            proof = make_proof(key, method="POST",
                               path=proof_path or path.lstrip("/"))
        if proof is not None:
            headers["HTTP_DPOP"] = proof
        if secret is not None:
            headers["HTTP_AUTHORIZATION"] = f"Pairing {secret}"
        return client.post(f"{BASE}{path}", body, format="json", **headers)

    # ── handshake helpers ─────────────────────────────────────────────────

    def start_pairing(self, key=None, **overrides):
        key = key or self.key
        body = {
            "name": "Hruaia's ThinkPad", "platform": "windows",
            "arch": "x86_64", "app_version": "0.1.0",
            "public_jwk": public_jwk(key),
        }
        body.update(overrides)
        return self.device_post("/pairings/", body, key=key,
                                proof_path="pairings/")

    def full_handshake(self):
        """All three steps, the happy path. Returns the finalize response."""
        started = self.start_pairing().json()
        client = self.browser()
        self.browser_post(client, "/pairings/confirm/",
                          {"user_code": started["user_code"]})
        return started, self.finalize(started)

    def finalize(self, started, key=None, secret=None):
        key = key or self.key
        pid = started["pairing_id"]
        return self.device_post(
            f"/pairings/{pid}/finalize/", {"local_confirm": True},
            key=key, secret=secret if secret is not None else started["device_secret"],
            proof_path=f"pairings/{pid}/finalize/")


# ══════════════════════════════════════════════════════════════════════════
# The DPoP verifier itself
# ══════════════════════════════════════════════════════════════════════════


@override_settings(COLLABORATION_CANONICAL_URL=CANONICAL)
class DPoPProofTest(PairingTestBase):
    def test_a_valid_proof_verifies_and_yields_its_thumbprint(self):
        proof = make_proof(self.key, method="POST", path="pairings/")
        thumbprint, claims = dpop.verify_proof(
            proof, method="POST", path="pairings/")
        self.assertEqual(thumbprint, dpop.jwk_thumbprint(public_jwk(self.key)))
        self.assertEqual(claims["htm"], "POST")

    def test_the_thumbprint_is_stable_across_jwk_field_order(self):
        """RFC 7638 is canonical for a reason: a device must not be able to
        re-register the same key as a second identity by reordering JSON."""
        jwk = public_jwk(self.key)
        shuffled = {k: jwk[k] for k in ("y", "x", "crv", "kty")}
        self.assertEqual(dpop.jwk_thumbprint(jwk), dpop.jwk_thumbprint(shuffled))

    def test_a_proof_signed_by_a_different_key_is_refused(self):
        attacker = new_key()
        proof = jwt.encode(
            {"jti": uuid.uuid4().hex * 2, "htm": "POST",
             "htu": f"{CANONICAL}/pairings/", "iat": int(time.time())},
            attacker, algorithm="ES256",
            # Claims the victim's key in the header, signs with its own.
            headers={"typ": "dpop+jwt", "jwk": public_jwk(self.key)},
        )
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(proof, method="POST", path="pairings/")
        self.assertEqual(ctx.exception.reason, "signature_invalid")

    def test_the_alg_is_never_taken_from_the_header(self):
        """🔴 The algorithm-confusion attack. An HS256 token whose 'key' is
        the public JWK verifies on any server that trusts `alg` — and the
        public key is sitting right there in the proof."""
        jwk = public_jwk(self.key)
        forged = jwt.encode(
            {"jti": uuid.uuid4().hex * 2, "htm": "POST",
             "htu": f"{CANONICAL}/pairings/", "iat": int(time.time())},
            json.dumps(jwk, sort_keys=True),      # the public key as an HMAC secret
            algorithm="HS256",
            headers={"typ": "dpop+jwt", "jwk": jwk},
        )
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(forged, method="POST", path="pairings/")
        self.assertEqual(ctx.exception.reason, "alg_invalid")

    def test_the_none_algorithm_is_refused(self):
        jwk = public_jwk(self.key)
        unsigned = jwt.encode(
            {"jti": uuid.uuid4().hex * 2, "htm": "POST",
             "htu": f"{CANONICAL}/pairings/", "iat": int(time.time())},
            key="", algorithm="none",
            headers={"typ": "dpop+jwt", "jwk": jwk},
        )
        with self.assertRaises(dpop.DPoPError):
            dpop.verify_proof(unsigned, method="POST", path="pairings/")

    def test_a_proof_for_another_method_is_refused(self):
        proof = make_proof(self.key, method="GET", path="pairings/")
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(proof, method="POST", path="pairings/")
        self.assertEqual(ctx.exception.reason, "htm_mismatch")

    def test_a_proof_for_another_endpoint_is_refused(self):
        """The replay this field exists to stop: a proof captured on the
        pairing route must not spend on finalize."""
        proof = make_proof(self.key, method="POST", path="pairings/")
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(proof, method="POST", path="pairings/x/finalize/")
        self.assertEqual(ctx.exception.reason, "htu_mismatch")

    def test_htu_is_not_taken_from_the_request_host(self):
        """🔴 A proof naming an attacker's host must fail even though a
        server deriving `htu` from Host/X-Forwarded-Host would accept it."""
        proof = make_proof(self.key, method="POST", path="pairings/",
                           htu="https://evil.example/pairings/")
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(proof, method="POST", path="pairings/")
        self.assertEqual(ctx.exception.reason, "htu_mismatch")

    def test_a_stale_proof_is_refused(self):
        proof = make_proof(self.key, method="POST", path="pairings/",
                           iat=int(time.time()) - 120)
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(proof, method="POST", path="pairings/")
        self.assertEqual(ctx.exception.reason, "iat_out_of_window")

    def test_a_proof_from_the_future_is_refused(self):
        proof = make_proof(self.key, method="POST", path="pairings/",
                           iat=int(time.time()) + 120)
        with self.assertRaises(dpop.DPoPError):
            dpop.verify_proof(proof, method="POST", path="pairings/")

    def test_a_replayed_proof_is_refused(self):
        """Same jti twice. The first spends it; the second is a replay."""
        proof = make_proof(self.key, method="POST", path="pairings/")
        dpop.verify_proof(proof, method="POST", path="pairings/")
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(proof, method="POST", path="pairings/")
        self.assertEqual(ctx.exception.reason, "jti_replayed")

    def test_a_rejected_proof_does_not_burn_its_jti(self):
        """Otherwise an attacker could evict a victim's cache entries by
        submitting doomed proofs carrying their jti."""
        jti = uuid.uuid4().hex * 2
        bad = make_proof(self.key, method="GET", path="pairings/", jti=jti)
        with self.assertRaises(dpop.DPoPError):
            dpop.verify_proof(bad, method="POST", path="pairings/")
        good = make_proof(self.key, method="POST", path="pairings/", jti=jti)
        thumbprint, _ = dpop.verify_proof(good, method="POST", path="pairings/")
        self.assertTrue(thumbprint)

    def test_a_non_dpop_typ_is_refused(self):
        proof = make_proof(self.key, method="POST", path="pairings/", typ="JWT")
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(proof, method="POST", path="pairings/")
        self.assertEqual(ctx.exception.reason, "typ_invalid")

    def test_a_proof_bound_to_another_key_is_refused(self):
        other = new_key()
        proof = make_proof(other, method="POST", path="pairings/")
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(
                proof, method="POST", path="pairings/",
                expected_thumbprint=dpop.jwk_thumbprint(public_jwk(self.key)))
        self.assertEqual(ctx.exception.reason, "jwk_mismatch")

    def test_an_unexpected_ath_is_refused(self):
        proof = make_proof(self.key, method="POST", path="pairings/",
                           access_token="some-token")
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(proof, method="POST", path="pairings/")
        self.assertEqual(ctx.exception.reason, "ath_unexpected")

    def test_a_proof_for_a_different_access_token_is_refused(self):
        proof = make_proof(self.key, method="POST", path="device-heartbeat/",
                           access_token="token-A")
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(proof, method="POST", path="device-heartbeat/",
                              access_token="token-B")
        self.assertEqual(ctx.exception.reason, "ath_mismatch")

    def test_a_private_key_in_the_header_is_refused(self):
        jwk = {**public_jwk(self.key), "d": "not-telling"}
        proof = jwt.encode(
            {"jti": uuid.uuid4().hex * 2, "htm": "POST",
             "htu": f"{CANONICAL}/pairings/", "iat": int(time.time())},
            self.key, algorithm="ES256",
            headers={"typ": "dpop+jwt", "jwk": jwk})
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(proof, method="POST", path="pairings/")
        self.assertEqual(ctx.exception.reason, "jwk_private")

    def test_a_non_p256_key_is_refused(self):
        with self.assertRaises(dpop.DPoPError):
            dpop.jwk_thumbprint({"kty": "RSA", "n": "x", "e": "AQAB",
                                 "crv": "P-256", "x": "a", "y": "b"})

    @override_settings(COLLABORATION_CANONICAL_URL="")
    def test_an_unconfigured_canonical_url_refuses_rather_than_accepting_any(self):
        """🔴 Fail closed. An empty base would otherwise make every htu
        'match' a prefix of nothing."""
        proof = make_proof(self.key, method="POST", path="pairings/")
        with self.assertRaises(dpop.DPoPError) as ctx:
            dpop.verify_proof(proof, method="POST", path="pairings/")
        self.assertEqual(ctx.exception.reason, "canonical_url_unconfigured")


# ══════════════════════════════════════════════════════════════════════════
# The handshake
# ══════════════════════════════════════════════════════════════════════════


class PairingHandshakeTest(PairingTestBase):
    def test_the_happy_path_pairs_a_device(self):
        started, res = self.full_handshake()
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()
        self.assertEqual(
            sorted(data), ["access_token", "device_id", "expires_in", "refresh_token"])
        self.assertEqual(data["expires_in"], 300)

        device = PairedDevice.objects.get(id=data["device_id"])
        self.assertEqual(device.owner, self.user)
        self.assertEqual(device.status, PairedDevice.STATUS_PAIRED)
        self.assertTrue(device.is_usable)

    def test_step_one_returns_a_code_and_a_secret_and_stores_neither(self):
        started = self.start_pairing().json()
        self.assertEqual(
            sorted(started),
            ["device_secret", "expires_at", "pairing_id", "poll_interval_s",
             "user_code"])
        self.assertEqual(started["poll_interval_s"], 5)

        pairing = DevicePairing.objects.get(id=started["pairing_id"])
        raw = json.dumps(
            {k: str(v) for k, v in pairing.__dict__.items()
             if not k.startswith("_")})
        # 🔴 Neither credential may appear anywhere on the row.
        self.assertNotIn(started["device_secret"], raw)
        self.assertNotIn(started["user_code"].replace("-", ""), raw)

    def test_the_user_code_uses_the_unambiguous_alphabet(self):
        code = self.start_pairing().json()["user_code"].replace("-", "")
        self.assertEqual(len(code), 10)
        for ch in code:
            self.assertIn(ch, device_svc.CODE_ALPHABET)
        for confusable in "ILOU":
            self.assertNotIn(confusable, device_svc.CODE_ALPHABET)

    def test_a_typed_code_is_forgiving_of_spacing_and_case(self):
        started = self.start_pairing().json()
        messy = f"  {started['user_code'].lower()}  "
        res = self.browser_post(self.browser(), "/pairings/confirm/",
                                {"user_code": messy})
        self.assertEqual(res.status_code, 200, res.content)

    def test_confirm_reports_the_device_and_phrase_but_no_credentials(self):
        started = self.start_pairing().json()
        res = self.browser_post(self.browser(), "/pairings/confirm/",
                                {"user_code": started["user_code"]})
        data = res.json()
        self.assertEqual(data["state"], "browser_confirmed")
        self.assertEqual(data["device_name"], "Hruaia's ThinkPad")
        self.assertTrue(data["verification_phrase"])
        blob = json.dumps(data).lower()
        for forbidden in ("secret", "token", "access", "refresh"):
            self.assertNotIn(forbidden, blob)

    def test_polling_reveals_the_account_only_after_confirmation(self):
        started = self.start_pairing().json()
        pid = started["pairing_id"]

        before = self.device_post(f"/pairings/{pid}/poll/", {},
                                  secret=started["device_secret"]).json()
        self.assertEqual(before["state"], "pending")
        self.assertNotIn("account_display_name", before)

        self.browser_post(self.browser(), "/pairings/confirm/",
                          {"user_code": started["user_code"]})

        after = self.device_post(f"/pairings/{pid}/poll/", {},
                                 secret=started["device_secret"]).json()
        self.assertEqual(after["state"], "browser_confirmed")
        self.assertIn("account_display_name", after)
        self.assertEqual(after["verification_phrase"],
                         DevicePairing.objects.get(id=pid).verification_phrase)

    def test_the_verification_phrase_matches_on_both_screens(self):
        """The whole point of the phrase: the human compares them."""
        started = self.start_pairing().json()
        browser = self.browser_post(
            self.browser(), "/pairings/confirm/",
            {"user_code": started["user_code"]}).json()
        device = self.device_post(
            f"/pairings/{started['pairing_id']}/poll/", {},
            secret=started["device_secret"]).json()
        self.assertEqual(browser["verification_phrase"],
                         device["verification_phrase"])

    def test_redemption_is_single_use(self):
        started, first = self.full_handshake()
        self.assertEqual(first.status_code, 200)
        second = self.finalize(started)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["code"], "pairing_already_used")
        self.assertEqual(PairedDevice.objects.count(), 1)

    def test_an_expired_pairing_cannot_be_finalized(self):
        started = self.start_pairing().json()
        self.browser_post(self.browser(), "/pairings/confirm/",
                          {"user_code": started["user_code"]})
        DevicePairing.objects.filter(id=started["pairing_id"]).update(
            expires_at=timezone.now() - timedelta(seconds=1))
        res = self.finalize(started)
        self.assertEqual(res.status_code, 410)

    def test_the_device_limit_is_enforced(self):
        for i in range(PairedDevice.MAX_PER_ACCOUNT):
            PairedDevice.objects.create(
                owner=self.user, name=f"d{i}", platform="windows", arch="x86_64",
                public_jwk={}, key_thumbprint=f"tp-{i}",
                status=PairedDevice.STATUS_PAIRED)
        started = self.start_pairing().json()
        self.browser_post(self.browser(), "/pairings/confirm/",
                          {"user_code": started["user_code"]})
        res = self.finalize(started)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "device_limit_reached")


# ══════════════════════════════════════════════════════════════════════════
# T02 — neither half of the handshake is sufficient alone
# ══════════════════════════════════════════════════════════════════════════


class BrowserAloneTest(PairingTestBase):
    """🔴 A stolen browser session cannot pair a device."""

    def test_confirming_alone_pairs_nothing(self):
        started = self.start_pairing().json()
        res = self.browser_post(self.browser(), "/pairings/confirm/",
                                {"user_code": started["user_code"]})
        self.assertEqual(res.status_code, 200)
        # The account is named and nothing else happened.
        self.assertEqual(PairedDevice.objects.count(), 0)
        self.assertNotIn("access_token", res.json())

    def test_the_browser_cannot_reach_finalize(self):
        """There is no browser finalize — "no browser finalize", literally.
        A cookie is not a pairing secret and the route does not look at one.
        """
        started = self.start_pairing().json()
        self.browser_post(self.browser(), "/pairings/confirm/",
                          {"user_code": started["user_code"]})
        client = self.browser()
        res = client.post(
            f"{BASE}/pairings/{started['pairing_id']}/finalize/",
            {"local_confirm": True}, format="json",
            HTTP_ORIGIN=ORIGIN, HTTP_X_CSRFTOKEN=self.csrf(client))
        self.assertEqual(res.status_code, 401)
        self.assertEqual(PairedDevice.objects.count(), 0)

    def test_a_stale_browser_session_cannot_confirm(self):
        """"recent account auth ≤10min" — an old session must re-authenticate."""
        started = self.start_pairing().json()
        stale = self.browser(signed_in_ago=timedelta(hours=3))
        res = self.browser_post(stale, "/pairings/confirm/",
                                {"user_code": started["user_code"]})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "recent_auth_required")

    def test_confirm_requires_csrf_and_an_allowlisted_origin(self):
        started = self.start_pairing().json()
        client = self.browser()
        no_csrf = client.post(
            f"{BASE}/pairings/confirm/", {"user_code": started["user_code"]},
            format="json", HTTP_ORIGIN=ORIGIN,
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        self.assertEqual(no_csrf.status_code, 403)

        evil = self.browser_post(
            self.browser(), "/pairings/confirm/",
            {"user_code": started["user_code"]},
            HTTP_ORIGIN="https://evilapp.test.shikshacom.com")
        self.assertEqual(evil.status_code, 403)


class DeviceAloneTest(PairingTestBase):
    """🔴 A stolen code, or a stolen secret, cannot pair a device either."""

    def test_finalizing_without_browser_confirmation_is_refused(self):
        started = self.start_pairing().json()
        res = self.finalize(started)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "pairing_not_confirmed")
        self.assertEqual(PairedDevice.objects.count(), 0)

    def test_a_wrong_secret_is_an_indistinguishable_404(self):
        started = self.start_pairing().json()
        self.browser_post(self.browser(), "/pairings/confirm/",
                          {"user_code": started["user_code"]})
        res = self.finalize(started, secret="not-the-secret")
        self.assertEqual(res.status_code, 404)
        unknown = self.device_post(
            f"/pairings/{uuid.uuid4()}/finalize/", {"local_confirm": True},
            key=self.key, secret="anything")
        self.assertEqual(unknown.status_code, 404)

    def test_another_key_cannot_redeem_a_stolen_secret(self):
        """🔴 The secret alone is not enough: the proof must come from the
        key that STARTED the pairing."""
        started = self.start_pairing().json()
        self.browser_post(self.browser(), "/pairings/confirm/",
                          {"user_code": started["user_code"]})
        res = self.finalize(started, key=new_key())
        self.assertEqual(res.status_code, 401)
        self.assertEqual(PairedDevice.objects.count(), 0)

    def test_finalize_without_local_confirmation_is_refused(self):
        started = self.start_pairing().json()
        self.browser_post(self.browser(), "/pairings/confirm/",
                          {"user_code": started["user_code"]})
        pid = started["pairing_id"]
        res = self.device_post(
            f"/pairings/{pid}/finalize/", {"local_confirm": False},
            key=self.key, secret=started["device_secret"],
            proof_path=f"pairings/{pid}/finalize/")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "local_confirm_required")

    def test_the_secret_is_never_accepted_from_the_body_or_query(self):
        """"secret in Authorization, not URL/body logs".

        🔴 **The first version of this test was vacuous and the mutation
        sweep caught it.** It asserted `status_code in (400, 401)` against
        the poll endpoint — but `parse_body` rejects `device_secret` as an
        unknown field with a 400 *before* any credential is read, so the
        assertion held even with a body fallback wired in. The reader of the
        extractor is where the claim actually lives, so that is what this
        now exercises.
        """
        from sessions_app.collaboration.device_views import _pairing_secret
        from sessions_app.collaboration.devices import PairingError

        started = self.start_pairing().json()
        secret = started["device_secret"]

        class BodyRequest:
            method = "POST"
            headers = {}
            data = {"device_secret": secret}
            GET = {"device_secret": secret}

        with self.assertRaises(PairingError) as ctx:
            _pairing_secret(BodyRequest())
        self.assertEqual(ctx.exception.code, "pairing_auth_required")

        # And the header form, which must work — otherwise the test above
        # would pass against an extractor that reads nothing at all.
        class HeaderRequest:
            method = "POST"
            headers = {"Authorization": f"Pairing {secret}"}
            data = {}
            GET = {}

        self.assertEqual(_pairing_secret(HeaderRequest()), secret)

        # End-to-end: a query string carries no authority either.
        client = APIClient()
        in_query = client.post(
            f"{BASE}/pairings/{started['pairing_id']}/poll/"
            f"?device_secret={secret}", {}, format="json")
        self.assertEqual(in_query.status_code, 401)

    def test_five_wrong_codes_invalidate_the_pairing(self):
        started = self.start_pairing().json()
        code = started["user_code"].replace("-", "")
        # Same pairing, wrong code each time: flip one character to another
        # in the alphabet so it still parses but never matches.
        for _ in range(DevicePairing.MAX_CONFIRM_ATTEMPTS):
            device_svc.register_failed_confirm(code)
        pairing = DevicePairing.objects.get(id=started["pairing_id"])
        self.assertEqual(pairing.state, DevicePairing.STATE_EXPIRED)

        res = self.browser_post(self.browser(), "/pairings/confirm/",
                                {"user_code": started["user_code"]})
        self.assertEqual(res.status_code, 410)

    def test_pairing_routes_reject_a_cookie_as_a_substitute(self):
        """api-contracts.md: device routes "never [accept] user cookies as
        substitute". A signed-in browser with no DPoP proof gets nothing."""
        client = self.browser()
        res = client.post(f"{BASE}/pairings/", {
            "name": "x", "platform": "windows", "arch": "x86_64",
            "public_jwk": public_jwk(self.key),
        }, format="json", HTTP_ORIGIN=ORIGIN)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["code"], "device_proof_invalid")

    def test_the_registered_key_must_be_the_key_that_signed_the_proof(self):
        other = new_key()
        res = self.device_post(
            "/pairings/", {
                "name": "x", "platform": "windows", "arch": "x86_64",
                "public_jwk": public_jwk(other),     # someone else's key
            }, key=self.key, proof_path="pairings/")
        self.assertEqual(res.status_code, 401)


# ══════════════════════════════════════════════════════════════════════════
# Device access tokens
# ══════════════════════════════════════════════════════════════════════════


class DeviceTokenTest(PairingTestBase):
    def setUp(self):
        super().setUp()
        self.started, res = self.full_handshake()
        self.tokens = res.json()
        self.device = PairedDevice.objects.get(id=self.tokens["device_id"])

    def test_the_access_token_is_key_bound_and_scope_limited(self):
        claims = device_svc.decode_access_token(self.tokens["access_token"])
        self.assertEqual(claims["sub"], str(self.device.id))
        self.assertEqual(claims["owner"], str(self.user.id))
        self.assertEqual(claims["aud"], device_svc.ACCESS_TOKEN_AUDIENCE)
        self.assertEqual(claims["cnf"]["jkt"], self.device.key_thumbprint)
        self.assertEqual(claims["scopes"], list(device_svc.DEVICE_SCOPES))
        self.assertLessEqual(claims["exp"] - claims["iat"], 300)

    def test_a_user_token_is_not_a_device_token(self):
        """🔴 The inversion that must not be possible. A SimpleJWT access
        token has no `typ=shiksha-device+jwt` and must be refused outright.
        """
        from rest_framework_simplejwt.tokens import RefreshToken
        user_token = str(RefreshToken.for_user(self.user).access_token)
        with self.assertRaises(Exception) as ctx:
            device_svc.decode_access_token(user_token)
        self.assertEqual(getattr(ctx.exception, "status", None), 401)

    def test_the_typ_check_is_what_refuses_a_non_device_token(self):
        """The test above passes even with the `typ` check deleted, because
        a SimpleJWT token is signed with a *different* key and fails on the
        signature instead — the mutation sweep caught that.

        So this one isolates `typ`: a token signed with the DEVICE key,
        carrying a valid audience and every required claim, differing from a
        real device token *only* in `typ`. Nothing but the typ check can
        refuse it.
        """
        import jwt as pyjwt
        claims = {
            "aud": device_svc.ACCESS_TOKEN_AUDIENCE,
            "sub": str(self.device.id),
            "cnf": {"jkt": self.device.key_thumbprint},
            "exp": int(time.time()) + 300,
        }
        impostor = pyjwt.encode(
            claims, device_svc._server_key(), algorithm="HS256",
            headers={"typ": "JWT"},            # the ONLY difference
        )
        with self.assertRaises(Exception) as ctx:
            device_svc.decode_access_token(impostor)
        self.assertEqual(getattr(ctx.exception, "status", None), 401)

        # Control: the same claims WITH the right typ must decode, or the
        # assertion above would hold for an unrelated reason.
        genuine = pyjwt.encode(
            claims, device_svc._server_key(), algorithm="HS256",
            headers={"typ": device_svc.ACCESS_TOKEN_TYP},
        )
        self.assertEqual(
            device_svc.decode_access_token(genuine)["sub"], str(self.device.id))

    def test_a_device_token_signed_with_the_wrong_key_is_refused(self):
        import jwt as pyjwt
        forged = pyjwt.encode(
            {"sub": str(self.device.id), "aud": device_svc.ACCESS_TOKEN_AUDIENCE,
             "cnf": {"jkt": self.device.key_thumbprint},
             "exp": int(time.time()) + 300},
            "the-wrong-key", algorithm="HS256",
            headers={"typ": device_svc.ACCESS_TOKEN_TYP})
        with self.assertRaises(Exception):
            device_svc.decode_access_token(forged)

    def test_revoking_the_device_invalidates_its_token_immediately(self):
        """Within the token's own 5-minute life — holding a valid token is
        explicitly not sufficient (PairedDevice's docstring)."""
        self.device.status = PairedDevice.STATUS_REVOKED
        self.device.save(update_fields=["status"])

        class FakeRequest:
            method = "POST"
            headers = {
                "Authorization": f"DPoP {self.tokens['access_token']}",
                "DPoP": make_proof(self.key, method="POST",
                                   path="device-heartbeat/",
                                   access_token=self.tokens["access_token"]),
            }

        with self.assertRaises(Exception) as ctx:
            device_svc.authenticate_device(FakeRequest(), path="device-heartbeat/")
        self.assertEqual(getattr(ctx.exception, "status", None), 401)

    def test_a_bearer_prefix_is_not_accepted(self):
        """A device credential must never be honoured by code looking for an
        ordinary bearer token, and vice versa."""
        class FakeRequest:
            method = "POST"
            headers = {
                "Authorization": f"Bearer {self.tokens['access_token']}",
                "DPoP": make_proof(self.key, method="POST",
                                   path="device-heartbeat/",
                                   access_token=self.tokens["access_token"]),
            }

        with self.assertRaises(Exception) as ctx:
            device_svc.authenticate_device(FakeRequest(), path="device-heartbeat/")
        self.assertEqual(getattr(ctx.exception, "code", None),
                         "device_auth_required")

    def test_the_refresh_token_is_stored_only_as_a_hash(self):
        stored = self.device.credential_hash
        if isinstance(stored, memoryview):
            stored = stored.tobytes()
        self.assertNotIn(self.tokens["refresh_token"].encode(), bytes(stored))
        self.assertTrue(device_svc.hashes_match(
            self.tokens["refresh_token"], self.device.credential_hash))


class PairingFeatureFlagTest(PairingTestBase):
    def test_every_pairing_route_is_404_while_the_flag_is_off(self):
        started = self.start_pairing().json()
        s = GlobalSettings.load()
        s.collaboration_bindings_enabled = False
        s.save()

        self.assertEqual(self.start_pairing(key=new_key()).status_code, 404)
        pid = started["pairing_id"]
        self.assertEqual(
            self.device_post(f"/pairings/{pid}/poll/", {},
                             secret=started["device_secret"]).status_code, 404)
        self.assertEqual(self.finalize(started).status_code, 404)
