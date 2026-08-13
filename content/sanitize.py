# PLACEMENT: backend/content/sanitize.py
#
# Server-side HTML sanitization for CMS bodies (defense-in-depth: authors
# are staff, but a stolen admin session shouldn't become stored XSS).
#
# Uses `nh3` (Rust ammonia bindings — the maintained successor to bleach)
# when installed; degrades to a strict tag-stripping regex fallback so the
# app never hard-fails if the dependency is missing. `pip install nh3` is
# strongly recommended and listed in README_CONTENT.md.

import logging
import re

logger = logging.getLogger(__name__)

try:
    import nh3

    _HAS_NH3 = True
except ImportError:  # pragma: no cover - environment-dependent
    nh3 = None
    _HAS_NH3 = False
    logger.warning(
        "content.sanitize: `nh3` is not installed — falling back to a "
        "conservative regex sanitizer. Run `pip install nh3`."
    )

# Generous allowlist: everything the chapter fragments legitimately use
# (headings, tables, figures, native <details> accordions, styled spans),
# nothing executable.
ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "col",
    "colgroup", "dd", "details", "div", "dl", "dt", "em", "figcaption",
    "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "kbd",
    "li", "mark", "ol", "p", "pre", "q", "s", "section", "small", "span",
    "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "u", "ul",
}

_GLOBAL_ATTRS = {"class", "id", "style", "title", "role"}
ALLOWED_ATTRIBUTES = {
    "*": _GLOBAL_ATTRS,
    "a": _GLOBAL_ATTRS | {"href", "target"},  # rel is managed via link_rel
    "img": _GLOBAL_ATTRS | {"src", "alt", "width", "height", "loading"},
    "td": _GLOBAL_ATTRS | {"colspan", "rowspan"},
    "th": _GLOBAL_ATTRS | {"colspan", "rowspan", "scope"},
    "details": _GLOBAL_ATTRS | {"open"},
    "col": _GLOBAL_ATTRS | {"span"},
}

ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel"}

# Much smaller allowlist for homepage body/list-item fields — those feed a
# fixed, designed React layout, so only inline formatting is allowed
# (no headings/images/tables/blocks): an author can add emphasis or a link,
# but literally cannot restructure the page around it.
RESTRICTED_ALLOWED_TAGS = {"a", "b", "br", "em", "i", "li", "ol", "s", "strong", "u", "ul"}
RESTRICTED_ALLOWED_ATTRIBUTES = {
    "a": {"href", "target"},
}

# TipTap-authored content addresses elements by data-* (e.g. custom nodes,
# alignment, syntax-highlighted code blocks) and accessibility relies on
# aria-*; neither is executable, so both pass through on every tag.
GENERIC_ATTRIBUTE_PREFIXES = {"data-", "aria-"}

_SCRIPTISH = re.compile(
    r"<\s*(script|style|iframe|object|embed|form|input|button|link|meta)"
    r"[^>]*>.*?<\s*/\s*\1\s*>|<\s*(script|style|iframe|object|embed|form|"
    r"input|button|link|meta)[^>]*/?>",
    re.IGNORECASE | re.DOTALL,
)
_EVENT_ATTR = re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URL = re.compile(r"(href|src)\s*=\s*([\"']?)\s*javascript:[^\"'>\s]*\2", re.IGNORECASE)


def clean_html(html):
    """Sanitize an HTML fragment for storage/render. Idempotent."""
    if not html:
        return html or ""
    if _HAS_NH3:
        return nh3.clean(
            html,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRIBUTES,
            url_schemes=ALLOWED_URL_SCHEMES,
            link_rel="noopener noreferrer",
            generic_attribute_prefixes=GENERIC_ATTRIBUTE_PREFIXES,
        )
    # Fallback: strip executable constructs; keep everything else intact.
    out = _SCRIPTISH.sub("", html)
    out = _EVENT_ATTR.sub("", out)
    out = _JS_URL.sub(r'\1=\2#\2', out)
    return out


def clean_html_restricted(html):
    """Sanitize to the homepage's inline-only allowlist (see
    RESTRICTED_ALLOWED_TAGS) — for body/list-item fields that feed a fixed
    layout rather than a free-form article body."""
    if not html:
        return html or ""
    if _HAS_NH3:
        return nh3.clean(
            html,
            tags=RESTRICTED_ALLOWED_TAGS,
            attributes=RESTRICTED_ALLOWED_ATTRIBUTES,
            url_schemes=ALLOWED_URL_SCHEMES,
            link_rel="noopener noreferrer",
        )
    # Fallback: same executable-construct stripping as clean_html(), but
    # this path can't enforce the smaller tag allowlist without nh3 — still
    # far better than storing raw, unsanitized input.
    out = _SCRIPTISH.sub("", html)
    out = _EVENT_ATTR.sub("", out)
    out = _JS_URL.sub(r'\1=\2#\2', out)
    return out
