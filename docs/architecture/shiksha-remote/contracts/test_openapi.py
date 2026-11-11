"""Conformance tests for the generated OpenAPI document (RC-002).

RC-002's acceptance is "OpenAPI covers every API table row and JSON examples
validate". The first half is only meaningful if it is checked against the
prose rather than asserted — so this re-parses ``api-contracts.md``, extracts
every ``METHOD `path``` it declares, and compares that set with the generated
specification in both directions. A route added to the document and not to
``build_openapi.py`` fails here; so does an invented route with no basis in
the contract.

Run standalone:

    python docs/architecture/shiksha-remote/contracts/test_openapi.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CONTRACTS_MD = HERE.parent / "api-contracts.md"
SPEC = yaml.safe_load((HERE / "openapi.yaml").read_text())

# `POST \`grants/{id}/decision/\`` and the prose forms like GET \`csrf/\`.
_ROUTE_RE = re.compile(r"\b(GET|POST|PATCH|PUT|DELETE)\s+`([^`]+)`")


def declared_routes() -> set[tuple[str, str]]:
    found = set()
    for method, raw in _ROUTE_RE.findall(CONTRACTS_MD.read_text()):
        path = raw.split("?")[0].strip()          # snapshot/?binding_id=… → snapshot/
        if not path or path.startswith("/ws/"):   # the WS route is not REST
            continue
        found.add((method.lower(), "/" + path.lstrip("/")))
    return found


def spec_routes() -> set[tuple[str, str]]:
    return {(method, path)
            for path, ops in SPEC["paths"].items()
            for method in ops}


class Coverage(unittest.TestCase):
    def test_every_documented_route_is_in_the_spec(self):
        missing = declared_routes() - spec_routes()
        self.assertEqual(
            missing, set(),
            "api-contracts.md declares routes the OpenAPI document does not "
            "cover — RC-002's acceptance is explicitly that every table row is "
            "covered",
        )

    def test_the_spec_invents_no_routes(self):
        extra = spec_routes() - declared_routes()
        self.assertEqual(
            extra, set(),
            "the OpenAPI document contains routes with no basis in "
            "api-contracts.md; a producer would treat them as authoritative",
        )

    def test_route_count_is_what_the_document_describes(self):
        self.assertGreaterEqual(len(spec_routes()), 39)


class SecurityInvariants(unittest.TestCase):
    def test_every_operation_declares_security(self):
        for path, ops in SPEC["paths"].items():
            for method, op in ops.items():
                with self.subTest(f"{method} {path}"):
                    self.assertTrue(op.get("security"),
                                    "an operation with no security block defaults to open")

    def test_device_routes_never_accept_browser_cookies(self):
        """api-contracts.md: native DPoP routes do not accept cookie credentials."""
        device_only = ["/pairings/{id}/finalize/", "/host-bindings/{id}/share/",
                       "/host-bindings/{id}/ready/", "/grants/{id}/activate/",
                       "/grants/{id}/lease/", "/grants/{id}/resume/"]
        for path in device_only:
            for method, op in SPEC["paths"][path].items():
                with self.subTest(f"{method} {path}"):
                    schemes = {k for entry in op["security"] for k in entry}
                    self.assertNotIn("browserSession", schemes)

    def test_unsafe_browser_operations_require_csrf(self):
        for path, ops in SPEC["paths"].items():
            for method, op in ops.items():
                if method == "get":
                    continue
                schemes = {k for entry in op["security"] for k in entry}
                if "browserSession" not in schemes:
                    continue
                with self.subTest(f"{method} {path}"):
                    names = {p["name"] for p in op.get("parameters", [])}
                    self.assertIn("X-CSRFToken", names)

    def test_mutations_require_an_idempotency_key_except_heartbeat_and_lease(self):
        for path, ops in SPEC["paths"].items():
            for method, op in ops.items():
                if method == "get":
                    continue
                params = {p["name"]: p for p in op.get("parameters", [])}
                with self.subTest(f"{method} {path}"):
                    self.assertIn("Idempotency-Key", params)
                    exempt = path.endswith("heartbeat/") or path.endswith("lease/")
                    self.assertEqual(params["Idempotency-Key"]["required"], not exempt)

    def test_no_request_body_accepts_unknown_fields(self):
        """'All mutation bodies reject unknown fields' — enforced, not hoped."""
        for path, ops in SPEC["paths"].items():
            for method, op in ops.items():
                body = op.get("requestBody")
                if not body:
                    continue
                schema = body["content"]["application/json"]["schema"]
                if "oneOf" in schema and "properties" not in schema:
                    continue
                with self.subTest(f"{method} {path}"):
                    self.assertIs(schema.get("additionalProperties"), False)

    def test_no_schema_leaks_a_credential_field(self):
        """The Grant/Transfer shapes must never carry secrets to room viewers."""
        rendered = yaml.safe_dump(SPEC["components"]["schemas"])
        for leak in ("device_secret", "storage_key", "activation_jwt", "lease_jwt",
                     "refresh_token", "access_token"):
            self.assertNotIn(leak, rendered,
                             f"{leak} appears in a shared component schema")


class RepresentativeExamples(unittest.TestCase):
    """The three examples in api-contracts.md must match the frozen shapes."""

    def _props(self, path, method):
        op = SPEC["paths"][path][method]
        return op["requestBody"]["content"]["application/json"]["schema"]

    def test_grant_request_example_matches_the_schema(self):
        example = {"controller_binding_id": "00000000-0000-4000-8000-000000000010",
                   "host_binding_id": "00000000-0000-4000-8000-000000000011",
                   "capabilities": ["pointer.move", "pointer.button", "pointer.scroll",
                                    "keyboard.physical", "keyboard.text"]}
        schema = self._props("/grants/", "post")
        self.assertEqual(set(example), set(schema["required"]))
        self.assertEqual(set(example) - set(schema["properties"]), set())

    def test_approval_example_matches_the_schema(self):
        example = {"allow": True,
                   "capabilities": ["pointer.move", "pointer.button", "pointer.scroll"],
                   "expected_version": 1}
        schema = self._props("/grants/{id}/decision/", "post")
        self.assertEqual(set(example), set(schema["required"]))

    def test_error_example_matches_the_error_schema(self):
        example = {"code": "capture_not_ready",
                   "detail": "The owner must select and share a display in the companion.",
                   "request_id": "00000000-0000-4000-8000-000000000012"}
        schema = SPEC["components"]["schemas"]["Error"]
        self.assertEqual(set(schema["required"]), set(example))
        self.assertEqual(set(example) - set(schema["properties"]), set())

    def test_capabilities_allowlist_matches_the_input_contract(self):
        """The REST capability list and the wire contract must not diverge."""
        from reference_validator import CAPABILITIES
        rest = set(SPEC["components"]["schemas"]["CapabilityList"]["items"]["enum"])
        self.assertEqual(rest, set(CAPABILITIES))

    def test_grant_states_match_the_protocol_state_model(self):
        from reference_validator import GRANT_STATES
        rest = set(SPEC["components"]["schemas"]["Grant"]["properties"]["state"]["enum"])
        self.assertEqual(rest, set(GRANT_STATES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
