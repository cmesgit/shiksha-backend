"""Explicit display choices for CMS copy and repeatable content."""
from django.core.exceptions import ValidationError

DISPLAY_KEYS = frozenset({
    "eyebrow", "heading", "heading_secondary", "subhead", "body",
    "cta_primary", "cta_secondary", "image", "floaters", "items",
})
DISPLAY_MODES = frozenset({"default", "custom", "hidden"})


def validate_home_display_settings(value):
    if not isinstance(value, dict):
        raise ValidationError("Display choices must be an object.")
    if set(value) - DISPLAY_KEYS:
        raise ValidationError("An unknown content field was included in the display choices.")
    if any(not isinstance(mode, str) or mode not in DISPLAY_MODES for mode in value.values()):
        raise ValidationError("Choose default, custom or hidden for each content field.")
    if value.get("heading") == "hidden":
        raise ValidationError("Keep a main heading, or hide the entire section instead.")
