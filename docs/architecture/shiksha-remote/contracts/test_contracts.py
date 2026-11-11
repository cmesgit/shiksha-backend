"""Conformance tests for the RC-002 frozen input contract.

These do two jobs. They prove the Python reference validator agrees with
``fixtures.json``, and — more importantly — they prove the fixtures are a
usable conformance corpus at all, so the JS and Rust validators have something
trustworthy to be tested against rather than a file nobody ever executed.

Run standalone (no Django, no pytest, no third-party dependency):

    python docs/architecture/shiksha-remote/contracts/test_contracts.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reference_validator import (  # noqa: E402
    KEY_ALLOWLIST,
    Reject,
    TOPIC_CEILINGS,
    topic_for,
    validate_input,
    validate_raw,
)

_HERE = Path(__file__).resolve().parent
FIXTURES = json.loads((_HERE / "fixtures.json").read_text())
SCHEMA = json.loads((_HERE / "input.schema.json").read_text())


class GoldenFixtures(unittest.TestCase):
    def test_every_valid_fixture_is_accepted(self):
        for case in FIXTURES["valid"]:
            with self.subTest(case["name"]):
                msg = case["message"]
                validate_input(msg, topic=topic_for(msg["kind"]))

    def test_every_invalid_fixture_is_rejected_for_the_stated_reason(self):
        for case in FIXTURES["invalid"]:
            with self.subTest(case["name"]):
                msg = case["message"]
                topic = topic_for(msg["kind"]) if isinstance(msg.get("kind"), str) else "shiksha.rc.input.v1"
                with self.assertRaises(Reject) as ctx:
                    validate_input(msg, topic=topic)
                self.assertEqual(
                    ctx.exception.reason,
                    case["reason"],
                    f"{case['name']}: rejected, but for the wrong contract reason — "
                    "a cross-language corpus is only useful if all three "
                    "implementations agree on WHY something is refused",
                )

    def test_every_invalid_raw_fixture_is_rejected_before_parsing(self):
        for case in FIXTURES["invalid_raw"]:
            with self.subTest(case["name"]):
                raw = (bytes.fromhex(case["raw_hex"]) if "raw_hex" in case
                       else case["raw"].encode("utf-8"))
                with self.assertRaises(Reject) as ctx:
                    validate_raw(raw, topic=case["topic"])
                self.assertEqual(ctx.exception.reason, case["reason"])

    def test_corpus_is_not_trivially_small(self):
        """Guards against a future edit silently emptying the corpus."""
        self.assertGreaterEqual(len(FIXTURES["valid"]), 14)
        self.assertGreaterEqual(len(FIXTURES["invalid"]), 29)
        self.assertGreaterEqual(len(FIXTURES["invalid_raw"]), 5)
        covered = {c["message"]["kind"] for c in FIXTURES["valid"]}
        self.assertEqual(covered, set(SCHEMA["properties"]["kind"]["enum"]),
                         "every v1 kind needs at least one positive vector")

    def test_every_reason_code_is_documented_and_used(self):
        documented = set(FIXTURES["_reasons"])
        used = ({c["reason"] for c in FIXTURES["invalid"]}
                | {c["reason"] for c in FIXTURES["invalid_raw"]})
        self.assertEqual(used - documented, set(), "undocumented reason code")
        self.assertEqual(documented - used, set(), "documented reason never exercised")


class ContractInvariants(unittest.TestCase):
    def test_keyboard_allowlist_is_frozen_at_101(self):
        """The list is finite and enumerated on purpose.

        realtime-event-contracts.md forbids mapping arbitrary scan codes, so
        the size changing is a contract change requiring a version bump — not
        a convenience edit.
        """
        self.assertEqual(len(KEY_ALLOWLIST), 101)
        for required in ("KeyA", "KeyZ", "Digit0", "Numpad9", "F1", "F12", "NumpadEnter"):
            self.assertIn(required, KEY_ALLOWLIST)
        for forbidden in ("F13", "PrintScreen", "ScrollLock", "Pause", "ContextMenu"):
            self.assertNotIn(forbidden, KEY_ALLOWLIST)

    def test_no_dangerous_capability_exists_in_the_contract(self):
        """ADR-06: there is no shell/filesystem/clipboard/elevate capability."""
        caps = set(SCHEMA["$defs"]["capabilities"]["enum"])
        self.assertEqual(
            caps,
            {"pointer.move", "pointer.button", "pointer.scroll",
             "keyboard.physical", "keyboard.text"},
        )
        for forbidden in ("shell", "filesystem", "clipboard", "elevate",
                          "secure_attention", "unattended"):
            self.assertNotIn(forbidden, caps)

    def test_motion_ceiling_is_tighter_than_the_reliable_lane(self):
        self.assertLess(TOPIC_CEILINGS["shiksha.rc.motion.v1"],
                        TOPIC_CEILINGS["shiksha.rc.input.v1"])

    def test_js_safe_integer_ceiling_is_the_sequence_bound(self):
        """A seq above 2^53-1 silently loses precision in JS, so it is refused.

        This is the cross-language trap RC-002 exists to catch: Python and Rust
        would both carry 2^53 fine, and only the browser would quietly corrupt
        it.
        """
        base = dict(FIXTURES["valid"][0]["message"])
        base["seq"] = 9007199254740991
        validate_input(base, topic="shiksha.rc.motion.v1")
        base["seq"] = 9007199254740992
        with self.assertRaises(Reject) as ctx:
            validate_input(base, topic="shiksha.rc.motion.v1")
        self.assertEqual(ctx.exception.reason, "bad_seq")

    def test_non_finite_coordinates_are_refused(self):
        """JSON cannot carry NaN, but a native producer building a dict can."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            msg = dict(FIXTURES["valid"][0]["message"])
            msg["body"] = {"u": bad, "v": 0.5}
            with self.assertRaises(Reject) as ctx:
                validate_input(msg, topic="shiksha.rc.motion.v1")
            self.assertEqual(ctx.exception.reason, "out_of_range")

    def test_no_shape_valid_packet_can_reach_its_lane_ceiling(self):
        """Why the size check lives before the parser, not after it.

        Every field in the envelope is bounded, so the largest conforming
        packet is far under its ceiling. A post-parse size check would be
        unreachable code that reads like a protection. Asserted here so the
        reasoning survives — if a future field makes this fail, the ceiling
        became reachable and the contract needs rethinking.
        """
        import json as _json
        for case in FIXTURES["valid"]:
            msg = case["message"]
            topic = topic_for(msg["kind"])
            size = len(_json.dumps(msg, separators=(",", ":"),
                                   ensure_ascii=False).encode("utf-8"))
            self.assertLess(size, TOPIC_CEILINGS[topic], case["name"])

    def test_oversize_blob_is_refused_without_being_parsed(self):
        blob = b'{"pad":"' + b"y" * 5000 + b'"}'
        with self.assertRaises(Reject) as ctx:
            validate_raw(blob, topic="shiksha.rc.input.v1")
        self.assertEqual(ctx.exception.reason, "oversize")

    def test_non_finite_literal_is_refused_at_the_json_layer(self):
        """json.loads accepts NaN by default; the wire contract must not."""
        raw = (b'{"v":1,"grant_id":"00000000-0000-4000-8000-000000000001",'
               b'"epoch":"00000000-0000-4000-8000-000000000002","seq":1,'
               b'"challenge":"AAAAAAAAAAAAAAAAAAAAAA","capture_generation":0,'
               b'"kind":"move","body":{"u":NaN,"v":0.5}}')
        with self.assertRaises(Reject) as ctx:
            validate_raw(raw, topic="shiksha.rc.input.v1")
        self.assertEqual(ctx.exception.reason, "bad_json")

if __name__ == "__main__":
    unittest.main(verbosity=2)
