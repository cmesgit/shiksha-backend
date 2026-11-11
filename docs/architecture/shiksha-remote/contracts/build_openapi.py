"""Generate contracts/openapi.yaml from the route table in api-contracts.md.

Generated rather than hand-written for one reason: RC-002's acceptance is
"OpenAPI covers every API table row", and a hand-maintained YAML drifts from
the prose the moment either is edited. Here the route table below is the
single declaration, `test_openapi.py` re-parses `api-contracts.md` and asserts
the two agree, so a row added to the document and not to this file fails a
test instead of being quietly missing.

Auth codes mirror the document: B = browser cookie + CSRF + exact Origin,
D = device token + RFC 9449 DPoP proof, P = pairing secret + key proof.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CAPS = ["pointer.move", "pointer.button", "pointer.scroll",
        "keyboard.physical", "keyboard.text"]
GRANT_STATES = ["requested", "approved", "active", "paused", "declined",
                "revoked", "expired", "completed", "failed"]
TERMINATE_REASONS = ["user_stop", "completed", "local_stop", "disconnected",
                     "capture_lost", "protocol_error", "session_ended"]

UUID = {"type": "string", "format": "uuid"}


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def obj(props, required=None, extra=False):
    return {"type": "object", "properties": props,
            "required": required or list(props), "additionalProperties": extra}


# ---------------------------------------------------------------------------
# (method, path, auth, operationId, summary, requestBody schema, response)
# `None` request means no body. Paths are relative to the v1 prefix.
# ---------------------------------------------------------------------------
ROUTES = [
  ("get", "csrf/", "B", "getCsrf", "Bootstrap a masked CSRF token. Safe only; grants no API access.",
   None, obj({"csrf_token": {"type": "string"}})),

  # --- pair / device -------------------------------------------------------
  ("post", "pairings/", "anon", "createPairing", "Begin pairing a new device key. Anonymous DPoP proof; no account assigned yet.",
   obj({"name": {"type": "string", "maxLength": 80}, "platform": {"enum": ["windows", "macos", "linux"]},
        "arch": {"enum": ["x64", "arm64"]}, "app_version": {"type": "string", "maxLength": 32},
        "public_jwk": {"type": "object", "additionalProperties": True}}),
   obj({"pairing_id": UUID, "device_secret": {"type": "string"}, "user_code": {"type": "string"},
        "expires_at": {"type": "string", "format": "date-time"}, "poll_interval_s": {"const": 5}})),
  ("post", "pairings/confirm/", "B", "confirmPairing", "Browser confirms a user code. Requires account auth within 10 minutes.",
   obj({"user_code": {"type": "string"}}),
   obj({"pairing_id": UUID, "device_name": {"type": "string"}, "platform": {"type": "string"},
        "key_fingerprint": {"type": "string"}, "verification_phrase": {"type": "string"},
        "state": {"const": "browser_confirmed"}})),
  ("post", "pairings/{id}/poll/", "P", "pollPairing", "Device polls pairing state. Secret travels in Authorization, never URL or body.",
   obj({}, []),
   obj({"state": {"type": "string"}, "account_display_name": {"type": ["string", "null"]},
        "verification_phrase": {"type": ["string", "null"]},
        "expires_at": {"type": "string", "format": "date-time"}}, ["state", "expires_at"])),
  ("post", "pairings/{id}/finalize/", "P", "finalizePairing", "Device redeems pairing after a local physical confirmation. One atomic redemption; no browser equivalent.",
   obj({"local_confirm": {"const": True}}),
   obj({"device_id": UUID, "access_token": {"type": "string"}, "refresh_token": {"type": "string"},
        "expires_in": {"const": 300}})),
  ("post", "device-token/", "D", "rotateDeviceToken", "Rotate device credentials. Reuse of an old refresh token revokes the device.",
   obj({"refresh_token": {"type": "string"}}),
   obj({"access_token": {"type": "string"}, "refresh_token": {"type": "string"}, "expires_in": {"const": 300}})),
  ("get", "devices/", "B", "listDevices", "Own paired devices. Never a global device directory.",
   None, obj({"devices": {"type": "array", "items": ref("DeviceSummary")}})),
  ("delete", "devices/{id}/", "B", "deleteDevice", "Unpair a device. Revokes its bindings and grants first. Idempotent.",
   None, None),
  ("post", "device-heartbeat/", "D", "deviceHeartbeat", "Device liveness. MUST NOT implicitly renew any control grant.",
   obj({"app_version": {"type": "string", "maxLength": 32}, "protocol_min": {"type": "integer"},
        "protocol_max": {"type": "integer"}, "status": {"enum": ["idle", "sharing"]}}),
   obj({"server_time": {"type": "string", "format": "date-time"},
        "minimum_version": {"type": "string"}, "disabled": {"type": "boolean"}})),

  # --- participant / capture ----------------------------------------------
  ("post", "bindings/", "B", "createBinding", "Register the current browser against a verified LiveKit participant SID. Additive to the existing join, not a new login.",
   obj({"session": ref("SessionRef"), "participant_sid": {"type": "string"}}),
   obj({"binding_id": UUID, "identity": {"type": "string"}, "participant_sid": {"type": "string"},
        "expires_in": {"const": 6}, "capabilities": {"type": "array", "items": {"enum": CAPS}}})),
  ("post", "bindings/{id}/heartbeat/", "B", "heartbeatBinding", "Renew a browser binding. Every 2s; carries no input payload.",
   obj({"participant_sid": {"type": "string"}}),
   obj({"expires_in": {"const": 6}, "server_time": {"type": "string", "format": "date-time"}})),
  ("delete", "bindings/{id}/", "B", "deleteBinding", "Release a browser binding and terminate grants referencing it.",
   None, None),
  ("post", "host-bindings/", "B", "createHostBinding", "Owner links a paired device to this room. Max one active per device.",
   obj({"binding_id": UUID, "device_id": UUID}),
   obj({"host_binding_id": UUID, "state": {"const": "awaiting_local_share"},
        "expires_at": {"type": "string", "format": "date-time"}})),
  ("get", "host-bindings/{id}/", "B|D", "getHostBinding", "Host binding state. The capture descriptor appears only after the owner exposes availability.",
   None, obj({"id": UUID, "state": ref("HostBindingState"), "owner": ref("Principal"),
              "device": ref("DeviceRef"),
              "capture": {"oneOf": [ref("CaptureDescriptor"), {"type": "null"}]}})),
  ("post", "host-bindings/{id}/share/", "D", "startHostShare", "Device reports a locally confirmed display selection and receives a screen-only room token.",
   obj({"display": ref("CaptureDescriptorInput"), "local_confirm": {"const": True}}),
   obj({"livekit_url": {"type": "string"}, "token": {"type": "string"},
        "identity": {"type": "string"}, "join_expires_in": {"const": 60}})),
  ("post", "host-bindings/{id}/ready/", "D", "hostBindingReady", "Device declares its published screen track. Server verifies the real identity, SID and track.",
   obj({"participant_sid": {"type": "string"}, "track_sid": {"type": "string"},
        "capture_generation": {"type": "integer", "minimum": 0}}),
   obj({"state": {"const": "ready"}, "capture": ref("CaptureDescriptor")})),
  ("post", "host-bindings/{id}/heartbeat/", "D", "heartbeatHostBinding", "Device liveness for an active local share. A descriptor or SID change closes the binding.",
   obj({"participant_sid": {"type": "string"}, "track_sid": {"type": "string"},
        "capture_generation": {"type": "integer", "minimum": 0}}),
   obj({"expires_in": {"const": 6}})),
  ("delete", "host-bindings/{id}/", "B|D", "deleteHostBinding", "The API stop-sharing action. Terminates grants and stops capture.",
   None, None),
  ("patch", "host-bindings/{id}/preferences/", "B|D", "setHostBindingPreferences",
   "Owner opt-in to receiving requests. Defaults false. Cannot enable capture or control.",
   obj({"allow_requests": {"type": "boolean"}}),
   obj({"host_binding_id": UUID, "allow_requests": {"type": "boolean"}})),
  ("post", "ws-ticket/", "B|D", "createWsTicket", "One-use ticket for the collaboration WebSocket. Exactly one of binding_id or host_binding_id.",
   {"type": "object", "properties": {"binding_id": UUID, "host_binding_id": UUID},
    "additionalProperties": False, "oneOf": [{"required": ["binding_id"]}, {"required": ["host_binding_id"]}]},
   obj({"ticket": {"type": "string"}, "expires_in": {"const": 30}, "ws_url": {"type": "string"}})),
  ("get", "snapshot/", "B", "getSnapshot", "Authorized view for one binding. Excludes strangers' private requests.",
   None, obj({"server_time": {"type": "string", "format": "date-time"},
              "host_bindings": {"type": "array", "items": ref("HostAvailability")},
              "grants": {"type": "array", "items": ref("Grant")},
              "transfers": {"type": "array", "items": ref("Transfer")}})),

  # --- control grants ------------------------------------------------------
  ("post", "grants/", "B", "createGrant", "Controller requests assistance. Principals are derived server-side; the body carries no user IDs.",
   obj({"controller_binding_id": UUID, "host_binding_id": UUID,
        "capabilities": ref("CapabilityList")}), ref("Grant")),
  ("get", "grants/{id}/", "B|D", "getGrant", "Grant state. A device additionally receives a short-lived consent_nonce once approved.",
   None, ref("Grant")),
  ("post", "grants/{id}/decision/", "B", "decideGrant", "Target allows or declines in the browser. Capabilities may only be reduced. This never yields an active grant.",
   obj({"allow": {"type": "boolean"}, "capabilities": ref("CapabilityListAllowEmpty"),
        "expected_version": {"type": "integer"}}), ref("Grant")),
  ("post", "grants/{id}/activate/", "D", "activateGrant", "Device activates after a local physical approval. Returns the signed activation ticket and first lease.",
   obj({"consent_nonce": {"type": "string"}, "expected_version": {"type": "integer"},
        "capture_generation": {"type": "integer", "minimum": 0},
        "local_confirm": {"const": True}, "lease_nonce": {"type": "string"}}),
   obj({"grant": ref("Grant"), "activation_jwt": {"type": "string"}, "lease_jwt": {"type": "string"}})),
  ("post", "grants/{id}/lease/", "D", "renewLease", "Renew the bounded authority lease. Once per second; never idempotency-cached.",
   obj({"epoch": UUID, "expected_version": {"type": "integer"}, "nonce": {"type": "string"}}),
   obj({"lease_jwt": {"type": "string"}})),
  ("post", "grants/{id}/pause/", "B|D", "pauseGrant", "Pause. The local gate closes before the native HTTP call. Repeating the same state is idempotent.",
   obj({"expected_version": {"type": "integer"}, "reason": {"type": "string"}}), ref("Grant")),
  ("post", "grants/{id}/resume-intent/", "B", "resumeIntentGrant", "Target records intent to resume. There is no browser-only resume; intent expires in 30s.",
   obj({"expected_version": {"type": "integer"}}), ref("Grant")),
  ("post", "grants/{id}/resume/", "D", "resumeGrant", "Device resumes after a fresh native approval. New epoch; never a reconnect path.",
   obj({"consent_nonce": {"type": "string"}, "expected_version": {"type": "integer"},
        "capture_generation": {"type": "integer", "minimum": 0},
        "local_confirm": {"const": True}, "lease_nonce": {"type": "string"}}),
   obj({"grant": ref("Grant"), "activation_jwt": {"type": "string"}, "lease_jwt": {"type": "string"}})),
  ("post", "grants/{id}/terminate/", "B|D", "terminateGrant", "Terminal stop. Idempotent, and a stale version still revokes — stop wins.",
   obj({"expected_version": {"type": "integer"}, "reason": {"enum": TERMINATE_REASONS}}), ref("Grant")),

  # --- file transfer -------------------------------------------------------
  ("post", "transfers/", "B", "createTransfer", "Offer a file to one bound recipient in the same room. Offer expires in 60s.",
   obj({"sender_binding_id": UUID, "recipient_binding_id": UUID,
        "name": {"type": "string", "maxLength": 255}, "size_bytes": {"type": "integer", "minimum": 0},
        "mime": {"type": "string"}, "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "mode": {"enum": ["realtime", "durable"]}}), ref("Transfer")),
  ("get", "transfers/{id}/", "B", "getTransfer", "Sender or recipient only. No implicit host or admin read.",
   None, ref("Transfer")),
  ("post", "transfers/{id}/decision/", "B", "decideTransfer", "Recipient accepts or rejects. No bytes may move before acceptance.",
   obj({"accept": {"type": "boolean"}, "expected_version": {"type": "integer"}}), ref("Transfer")),
  ("post", "transfers/{id}/start/", "B", "startTransfer", "Sender begins. One-use. Realtime returns the stream target; durable returns an upload lease.",
   obj({"expected_version": {"type": "integer"}}),
   obj({"transfer": ref("Transfer"), "stream_id": UUID, "topic": {"const": "shiksha.file.bytes.v1"},
        "destination_identity": {"type": "string"}, "attributes": {"type": "object", "additionalProperties": True}})),
  ("post", "transfers/{id}/receipt/", "B", "receiptTransfer", "Recipient reports the verified outcome. A sender cannot declare its own success.",
   obj({"expected_version": {"type": "integer"}, "bytes_received": {"type": "integer", "minimum": 0},
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "result": {"enum": ["verified", "failed"]}, "reason": {"type": ["string", "null"]}},
       ["expected_version", "bytes_received", "sha256", "result"]), ref("Transfer")),
  ("post", "transfers/{id}/cancel/", "B", "cancelTransfer", "Either party cancels. Idempotent; invalidates upload and download leases.",
   obj({"reason": {"type": "string"}}), ref("Transfer")),
  ("post", "transfers/{id}/upload-parts/", "B", "transferUploadParts", "Durable only. At most 20 part URLs per request, scoped to the exact object key.",
   obj({"part_numbers": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 20}}),
   obj({"parts": {"type": "array", "items": obj({"number": {"type": "integer"}, "url": {"type": "string"},
                                                 "expires_at": {"type": "string", "format": "date-time"}})},
        "upload_expires_at": {"type": "string", "format": "date-time"}})),
  ("post", "transfers/{id}/upload-complete/", "B", "transferUploadComplete", "Durable only. The server hashes the stored object itself; an ETag is not a SHA-256.",
   obj({"parts": {"type": "array", "items": obj({"number": {"type": "integer"}, "etag": {"type": "string"}})},
        "expected_version": {"type": "integer"}}),
   obj({"transfer": ref("Transfer"), "state": {"const": "scanning"}})),
  ("get", "transfers/{id}/download/", "B", "downloadTransfer", "Authorized recipient only. ACL and scan state are re-checked on every request.",
   None, None),
]

ERRORS = {
    "400": "invalid", "401": "unauthenticated", "403": "forbidden / CSRF / wrong context",
    "404": "nonexistent OR inaccessible — deliberately indistinguishable",
    "409": "conflict / stale version / busy / upgrade_required", "410": "expired",
    "413": "too_large", "422": "unsupported_capability", "429": "rate_limited (Retry-After)",
    "503": "dependency_unavailable",
}

SCHEMAS = {
  "SessionRef": obj({"kind": {"enum": ["group", "private", "live", "skill"]}, "id": UUID}),
  "Principal": obj({"user_id": UUID, "display_name": {"type": "string"}}),
  "DeviceRef": obj({"id": UUID, "name": {"type": "string", "maxLength": 80}}),
  "DeviceSummary": obj({"id": UUID, "name": {"type": "string"}, "platform": {"enum": ["windows", "macos", "linux"]},
                        "app_version": {"type": "string"}, "online": {"type": "boolean"},
                        "last_seen_at": {"type": ["string", "null"], "format": "date-time"},
                        "compatible": {"type": "boolean"}}),
  "HostBindingState": {"enum": ["awaiting_local_share", "joining", "ready", "closed", "failed"]},
  "CapabilityList": {"type": "array", "items": {"enum": CAPS}, "minItems": 1, "uniqueItems": True},
  "CapabilityListAllowEmpty": {"type": "array", "items": {"enum": CAPS}, "uniqueItems": True},
  "CaptureDescriptorInput": obj({
      "display_id": {"type": "string", "maxLength": 128},
      "pixel_width": {"type": "integer", "minimum": 1, "maximum": 32768},
      "pixel_height": {"type": "integer", "minimum": 1, "maximum": 32768},
      "rotation": {"enum": [0, 90, 180, 270]},
      "desktop_bounds": obj({"x": {"type": "integer"}, "y": {"type": "integer"},
                             "width": {"type": "integer", "minimum": 1},
                             "height": {"type": "integer", "minimum": 1}}),
      "dpi_scale": {"type": "number", "minimum": 0.25, "maximum": 8},
      "coordinate_space": {"const": "physical_pixels"}, "source": {"const": "display"}}),
  "CaptureDescriptor": obj({
      "display_id": {"type": "string", "maxLength": 128},
      "capture_generation": {"type": "integer", "minimum": 0},
      "track_sid": {"type": "string"},
      "pixel_width": {"type": "integer", "minimum": 1, "maximum": 32768},
      "pixel_height": {"type": "integer", "minimum": 1, "maximum": 32768},
      "rotation": {"enum": [0, 90, 180, 270]},
      "desktop_bounds": obj({"x": {"type": "integer"}, "y": {"type": "integer"},
                             "width": {"type": "integer", "minimum": 1},
                             "height": {"type": "integer", "minimum": 1}}),
      "dpi_scale": {"type": "number", "minimum": 0.25, "maximum": 8},
      "coordinate_space": {"const": "physical_pixels"}, "source": {"const": "display"}}),
  "HostAvailability": obj({"host_binding_id": UUID, "owner": ref("Principal"),
                           "device": ref("DeviceRef"), "ready": {"type": "boolean"},
                           "capabilities": {"type": "array", "items": {"enum": CAPS}}}),
  "Grant": obj({
      "id": UUID, "version": {"type": "integer"}, "state": {"enum": GRANT_STATES},
      "session": ref("SessionRef"), "controller": ref("Principal"), "target": ref("Principal"),
      "device": ref("DeviceRef"), "capabilities": {"type": "array", "items": {"enum": CAPS}},
      "expires_at": {"type": ["string", "null"], "format": "date-time"},
      "request_expires_at": {"type": ["string", "null"], "format": "date-time"},
      "reason": {"type": ["string", "null"]},
      "epoch": {"oneOf": [UUID, {"type": "null"}]},
      "capture_generation": {"type": ["integer", "null"]},
      "track_sid": {"type": ["string", "null"]}}),
  "Transfer": obj({
      "id": UUID, "version": {"type": "integer"}, "session": ref("SessionRef"),
      "sender": ref("Principal"), "recipient": ref("Principal"),
      "mode": {"enum": ["realtime", "durable"]}, "state": {"type": "string"},
      "name": {"type": "string", "maxLength": 255}, "mime": {"type": "string"},
      "size_bytes": {"type": "integer", "minimum": 0},
      "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
      "offer_expires_at": {"type": ["string", "null"], "format": "date-time"},
      "accepted_at": {"type": ["string", "null"], "format": "date-time"},
      "expires_at": {"type": ["string", "null"], "format": "date-time"},
      "completed_at": {"type": ["string", "null"], "format": "date-time"},
      "reason": {"type": ["string", "null"]},
      "stream_id": {"oneOf": [UUID, {"type": "null"}]}}),
  "Error": obj({"code": {"type": "string"}, "detail": {"type": "string"}, "request_id": UUID,
                "field_errors": {"type": "object", "additionalProperties": True}},
               ["code", "detail", "request_id"]),
}

SECURITY = {
    "B": [{"browserSession": []}], "D": [{"deviceDpoP": []}], "P": [{"pairingSecret": []}],
    "B|D": [{"browserSession": []}, {"deviceDpoP": []}], "anon": [{"anonymousDpoP": []}],
}


def build():
    paths: dict = {}
    for method, path, auth, op_id, summary, body, response in ROUTES:
        url = "/" + path
        entry = paths.setdefault(url, {})
        operation = {
            "operationId": op_id,
            "summary": summary,
            "tags": [path.split("/")[0] or "root"],
            "security": SECURITY[auth],
            "responses": {},
        }
        if "{id}" in path:
            operation["parameters"] = [{"name": "id", "in": "path", "required": True, "schema": UUID}]
        if path.startswith("snapshot"):
            operation["parameters"] = [{"name": "binding_id", "in": "query", "required": True, "schema": UUID}]
        if method != "get":
            headers = [{"name": "Idempotency-Key", "in": "header",
                        "required": not path.endswith("heartbeat/") and "lease/" not in path,
                        "description": "UUID. Same actor+route+key+body replays the original result; a "
                                       "changed body is 409. Retained 24h. Heartbeat and lease are exempt.",
                        "schema": UUID}]
            if auth in ("B", "B|D"):
                headers.append({"name": "X-CSRFToken", "in": "header", "required": True,
                                "schema": {"type": "string"}})
            operation.setdefault("parameters", []).extend(headers)
        if body is not None:
            operation["requestBody"] = {"required": True,
                                        "content": {"application/json": {"schema": body}}}
        if response is None:
            operation["responses"]["204"] = {"description": "No content"}
        else:
            code = "201" if (method == "post" and path in
                             ("pairings/", "bindings/", "host-bindings/", "grants/", "transfers/")) else "200"
            operation["responses"][code] = {
                "description": "Success",
                "content": {"application/json": {"schema": response}},
                "headers": {"Cache-Control": {"schema": {"const": "no-store"}}},
            }
        for status, meaning in ERRORS.items():
            operation["responses"][status] = {
                "description": meaning,
                "content": {"application/json": {"schema": ref("Error")}},
            }
        entry[method] = operation

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Shiksha Remote collaboration API v1",
            "version": "1.0.0",
            "description": (
                "Frozen by RC-002 from api-contracts.md. Generated by build_openapi.py — "
                "edit that route table, not this file. Every mutation body rejects unknown "
                "fields; every non-heartbeat mutation requires an Idempotency-Key; actions on "
                "a resource carry expected_version. Authority is never inferred from a role "
                "sent in a body."
            ),
        },
        "servers": [{"url": "https://api.shikshacom.com/api/sessions/collaboration/v1"}],
        "components": {
            "securitySchemes": {
                "browserSession": {"type": "apiKey", "in": "cookie", "name": "sessionid",
                                   "description": "Browser cookie plus explicit CSRF validation and an exact allowlisted Origin."},
                "deviceDpoP": {"type": "http", "scheme": "bearer",
                               "description": "Device access token with an RFC 9449 DPoP proof. Cookies are never accepted here."},
                "pairingSecret": {"type": "http", "scheme": "bearer",
                                  "description": "Pairing secret plus original key proof. Poll and finalize only."},
                "anonymousDpoP": {"type": "http", "scheme": "bearer",
                                  "description": "Proof of possession of a new device key. No account is assigned."},
            },
            "schemas": SCHEMAS,
        },
        "paths": paths,
    }


if __name__ == "__main__":
    spec = build()
    out = HERE / "openapi.yaml"
    with out.open("w") as fh:
        fh.write("# GENERATED by build_openapi.py — do not edit by hand.\n")
        yaml.safe_dump(spec, fh, sort_keys=False, width=100, allow_unicode=True)
    ops = sum(len(v) for v in spec["paths"].values())
    print(f"wrote {out.name}: {len(spec['paths'])} paths, {ops} operations, "
          f"{len(spec['components']['schemas'])} schemas")
    print(json.dumps({"paths": len(spec["paths"]), "operations": ops}))
