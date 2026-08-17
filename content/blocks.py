# PLACEMENT: backend/content/blocks.py
#
# Server-side counterpart to shared/src/blogBlocks/schema.js — shape
# validation on write, and a plain-text extractor for reading_minutes. This
# is deliberately NOT a renderer: rendering blocks -> HTML happens in JS only
# (render.js), read time, on the client. See render.js's own header for why a
# parallel Python renderer was rejected. Keep KNOWN_BLOCK_TYPES in step with
# schema.js's KNOWN_BLOCK_TYPES export — this file has no way to import it.
#
# Same convention as counseling/guide_serializers.py: strict on write
# (unknown block type -> 400 at save time), permissive on read (nothing here
# runs on the read path; blocks_to_text() just contributes no words for a
# type it doesn't recognise, so an old backend build never fails to render
# reading_minutes for a post saved by a newer frontend).

import re

from rest_framework import serializers

KNOWN_BLOCK_TYPES = {
    "hero", "section_header", "rich_text", "callout", "faq_group",
    "table", "feature_grid", "key_terms", "image", "divider", "legacy_html",
    # Added after the Phase 6 coverage spike found these as real, recurring
    # (not speculative) patterns in the 114 legacy posts.
    "stat_grid", "timeline",
}

# The 24 tokens in schema.js's THEME_TOKENS — duplicated here for the same
# reason KNOWN_BLOCK_TYPES is: this file can't import a .js module.
THEME_TOKENS = {
    "ink", "ink2", "muted", "paper", "paper2", "rule", "white",
    "accent", "accent2", "accent-lt",
    "coral", "coral-lt", "gold", "gold-lt", "purple", "purple-lt",
    "blue", "blue-lt", "rose", "rose-lt", "green", "green-lt", "red", "red-lt",
}

_TAG_RE = re.compile(r"<[^>]+>")
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def validate_blocks(blocks):
    """Raised as a DRF ValidationError so a bad payload comes back as a 400,
    not an opaque 500 the first time something tries to read it."""
    if not isinstance(blocks, list):
        raise serializers.ValidationError("body_blocks must be a list.")
    for i, block in enumerate(blocks):
        if not isinstance(block, dict) or "t" not in block:
            raise serializers.ValidationError(
                f"body_blocks[{i}] must be an object with a 't' key."
            )
        if block["t"] not in KNOWN_BLOCK_TYPES:
            raise serializers.ValidationError(
                f"body_blocks[{i}]: unknown block type {block['t']!r}. "
                f"Known types: {sorted(KNOWN_BLOCK_TYPES)}"
            )
    return blocks


def validate_theme(theme):
    """Hex-only, mirroring normalizeTheme() in schema.js. This is a
    stored-data-quality guard, not a rendering safety net — the JS side
    already refuses to emit anything but a validated hex value regardless of
    what's in the DB, but rejecting garbage here means a broken theme is a
    400 at save time instead of a silent gap discovered later."""
    if not isinstance(theme, dict):
        raise serializers.ValidationError("body_theme must be an object.")
    for key, value in theme.items():
        if key not in THEME_TOKENS:
            raise serializers.ValidationError(
                f"body_theme: unknown token {key!r}. Known tokens: {sorted(THEME_TOKENS)}"
            )
        if not isinstance(value, str) or not _HEX_RE.match(value.strip()):
            raise serializers.ValidationError(
                f"body_theme[{key!r}] must be a hex color string, got {value!r}."
            )
    return theme


def blocks_to_text(blocks):
    """Plain-text extraction for reading_minutes — walks every string field
    in the tree (recursively, since list-type fields hold small objects) and
    strips inline HTML tags, matching how body_html's own word count already
    ignores markup. Unknown block types are not special-cased: their string
    fields still get walked, so reading_minutes degrades gracefully for a
    post saved by a newer frontend build rather than under-counting it."""
    if not isinstance(blocks, list):
        return ""
    words = []

    def walk(node):
        if isinstance(node, str):
            words.append(_TAG_RE.sub(" ", node))
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for block in blocks:
        walk(block)
    return " ".join(words)
