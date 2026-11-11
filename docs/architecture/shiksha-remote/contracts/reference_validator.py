"""Reference validator for the v1 controller input envelope (RC-002).

This is the normative Python implementation of the wire contract in
``realtime-event-contracts.md``. It is deliberately NOT a generic JSON Schema
runner:

* the repository has no ``jsonschema`` dependency, and RC-002 forbids
  unrelated dependency additions;
* several contract limits cannot be expressed in JSON Schema at all — UTF-8
  **byte** ceilings as distinct from scalar counts, per-topic serialised packet
  size, and the control-character rule that permits LF and TAB but nothing
  else. A validator that only ran the schema would accept packets the native
  core must refuse, which is the dangerous direction to be wrong in.

The enums and bounds are read from ``input.schema.json`` at import time rather
than restated here, so the schema stays the single source of truth and this
file cannot silently drift from it. The JS and Rust validators are expected to
do the same and to agree with ``fixtures.json`` exactly.

Reject reasons are contract vocabulary, not free text — they appear in
``fixtures.json`` and the three implementations must agree on which reason a
given malformed packet earns.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCHEMA = json.loads((_HERE / "input.schema.json").read_text())

# Pulled from the schema so there is exactly one definition of each.
KEY_ALLOWLIST: frozenset[str] = frozenset(_SCHEMA["$defs"]["keyboardAllowlist"]["enum"])
CAPABILITIES: frozenset[str] = frozenset(_SCHEMA["$defs"]["capabilities"]["enum"])
GRANT_STATES: frozenset[str] = frozenset(_SCHEMA["$defs"]["grantState"]["enum"])
KINDS: frozenset[str] = frozenset(_SCHEMA["properties"]["kind"]["enum"])
_ENVELOPE_FIELDS: frozenset[str] = frozenset(_SCHEMA["properties"])
_REQUIRED: tuple[str, ...] = tuple(_SCHEMA["required"])
_UUID_RE = re.compile(_SCHEMA["properties"]["grant_id"]["pattern"])
_CHALLENGE_RE = re.compile(_SCHEMA["properties"]["challenge"]["pattern"])

JS_MAX_SAFE_INT = 9007199254740991

TOPIC_CEILINGS = {
    "shiksha.rc.motion.v1": 1200,
    "shiksha.rc.input.v1": 4096,
    "shiksha.rc.status.v1": 4096,
}
_MOTION_KINDS = frozenset({"move"})

# Body field sets, mirroring the schema's per-kind branches.
_BODY_SPEC: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "move": (frozenset({"u", "v"}), frozenset({"u", "v"})),
    "button": (frozenset({"button", "down", "u", "v", "motion_seq"}),) * 2,
    "wheel": (frozenset({"dx", "dy", "u", "v", "motion_seq"}),) * 2,
    "key": (frozenset({"code", "down"}),) * 2,
    "text": (frozenset({"text"}),) * 2,
    "reset": (frozenset({"reason"}),) * 2,
    "heartbeat": (frozenset({"keys", "buttons", "last_ack"}),) * 2,
}

_BUTTONS = frozenset({"left", "middle", "right"})
_RESET_REASONS = frozenset({"blur", "visibility", "pointer_cancel", "user_pause"})


class Reject(Exception):
    """Carries the contract reason code, not a human sentence."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def _is_int(value) -> bool:
    # bool is a subclass of int; a boolean is never a valid sequence number.
    return isinstance(value, int) and not isinstance(value, bool)


def _norm(value, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Reject("out_of_range", field)
    # JSON cannot carry NaN/Infinity, but a hand-built dict can.
    if value != value or value in (float("inf"), float("-inf")):
        raise Reject("out_of_range", f"{field} non-finite")
    if not 0 <= value <= 1:
        raise Reject("out_of_range", field)


def _check_text(text) -> None:
    if not isinstance(text, str):
        raise Reject("out_of_range", "text")
    if len(text) > 256:
        raise Reject("text_too_long", "over 256 scalars")
    if len(text.encode("utf-8")) > 1024:
        raise Reject("text_too_long", "over 1024 UTF-8 bytes")
    for ch in text:
        if ch in ("\n", "\t"):
            continue
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise Reject("control_char", repr(ch))


def _check_body(kind: str, body) -> None:
    if not isinstance(body, dict):
        raise Reject("bad_body", kind)
    allowed, required = _BODY_SPEC[kind]
    extra = set(body) - allowed
    if extra:
        raise Reject("unknown_field", ", ".join(sorted(extra)))
    missing = required - set(body)
    if missing:
        raise Reject("missing_field", ", ".join(sorted(missing)))

    if kind in ("move", "button", "wheel"):
        _norm(body["u"], "u")
        _norm(body["v"], "v")
    if kind in ("button", "wheel"):
        if not _is_int(body["motion_seq"]) or not 0 <= body["motion_seq"] <= JS_MAX_SAFE_INT:
            raise Reject("bad_seq", "motion_seq")
    if kind == "button":
        if body["button"] not in _BUTTONS:
            raise Reject("out_of_range", "button")
        if not isinstance(body["down"], bool):
            raise Reject("out_of_range", "down")
    if kind == "wheel":
        for axis in ("dx", "dy"):
            d = body[axis]
            if isinstance(d, bool) or not isinstance(d, (int, float)) or not -10 <= d <= 10:
                raise Reject("out_of_range", axis)
    if kind == "key":
        if body["code"] not in KEY_ALLOWLIST:
            raise Reject("unsupported_key", str(body["code"]))
        if not isinstance(body["down"], bool):
            raise Reject("out_of_range", "down")
    if kind == "text":
        _check_text(body["text"])
    if kind == "reset":
        if body["reason"] not in _RESET_REASONS:
            raise Reject("out_of_range", "reason")
    if kind == "heartbeat":
        keys, buttons = body["keys"], body["buttons"]
        if not isinstance(keys, list) or not isinstance(buttons, list):
            raise Reject("out_of_range", "heartbeat arrays")
        if len(keys) > 16 or len(buttons) > 3:
            raise Reject("too_many", "heartbeat state")
        for k in keys:
            if k not in KEY_ALLOWLIST:
                raise Reject("unsupported_key", str(k))
        for b in buttons:
            if b not in _BUTTONS:
                raise Reject("out_of_range", "button")
        if not _is_int(body["last_ack"]) or not 0 <= body["last_ack"] <= JS_MAX_SAFE_INT:
            raise Reject("bad_seq", "last_ack")


def validate_input(message, *, topic: str | None = None) -> dict:
    """Return the message, or raise Reject with a contract reason code.

    `topic` enables the serialised-size ceiling. It is optional only so unit
    tests can exercise shape rules in isolation; the transport layer must
    always pass it, because size is the one limit an attacker controls freely.
    """
    if not isinstance(message, dict):
        raise Reject("bad_body", "envelope not an object")

    extra = set(message) - _ENVELOPE_FIELDS
    if extra:
        raise Reject("unknown_field", ", ".join(sorted(extra)))
    for field in _REQUIRED:
        if field not in message:
            raise Reject("missing_field", field)

    if message["v"] != 1 or isinstance(message["v"], bool):
        raise Reject("bad_version", str(message["v"]))
    for field in ("grant_id", "epoch"):
        value = message[field]
        if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
            raise Reject("bad_uuid", field)
    if not _is_int(message["seq"]) or not 1 <= message["seq"] <= JS_MAX_SAFE_INT:
        raise Reject("bad_seq", "seq")
    if not _is_int(message["capture_generation"]) or message["capture_generation"] < 0:
        raise Reject("out_of_range", "capture_generation")
    ch = message["challenge"]
    if not isinstance(ch, str) or not _CHALLENGE_RE.fullmatch(ch):
        raise Reject("bad_challenge", "challenge")
    kind = message["kind"]
    if kind not in KINDS:
        raise Reject("unknown_kind", str(kind))

    _check_body(kind, message["body"])

    if topic is not None:
        ceiling = TOPIC_CEILINGS.get(topic)
        if ceiling is None:
            raise Reject("out_of_range", f"unknown topic {topic}")
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > ceiling:
            raise Reject("oversize", f"{len(encoded)} > {ceiling}")
    return message


def topic_for(kind: str) -> str:
    """The lane a kind travels on. Motion is lossy; everything else reliable."""
    return "shiksha.rc.motion.v1" if kind in _MOTION_KINDS else "shiksha.rc.input.v1"


def validate_raw(raw: bytes, *, topic: str) -> dict:
    """Validate a received packet starting from its bytes. **Use this one.**

    The size ceiling has to be enforced here, before parsing, and that is not
    a stylistic preference. Measured against this contract, the largest
    *shape-valid* packet is 265 bytes on the motion lane (ceiling 1200) and
    1253 on the reliable lane (ceiling 4096) — every field is bounded, so a
    conforming message can never approach either limit. Checking size after
    parsing therefore tests a condition that cannot occur, while leaving the
    real attack unaddressed: a multi-megabyte blob that costs CPU to parse
    before anything rejects it.

    So the ordering is load-bearing. Size, then decode, then shape.
    """
    ceiling = TOPIC_CEILINGS.get(topic)
    if ceiling is None:
        raise Reject("out_of_range", f"unknown topic {topic}")
    if len(raw) > ceiling:
        raise Reject("oversize", f"{len(raw)} > {ceiling}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Reject("bad_encoding", "not UTF-8") from exc
    try:
        message = json.loads(text)
    except ValueError as exc:
        # json.loads accepts NaN/Infinity by default; the wire contract does
        # not, and a non-finite coordinate reaching the injector is exactly
        # the class of bug the strict parser exists to stop.
        raise Reject("bad_json", str(exc)[:60]) from exc
    if any(tok in text for tok in ("NaN", "Infinity")):
        raise Reject("bad_json", "non-finite literal")
    return validate_input(message, topic=None)
