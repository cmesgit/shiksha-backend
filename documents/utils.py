# Contributor-badge helper for the Explore library.
#
# Reuses the forum's proven author_badge (deterministic colour/initials from
# the username, degrades to username), then layers the Explore-owned
# DocumentProfile headline/institution on top so a contributor card can show a
# credential line. Everything is getattr-guarded so a badge always renders.

from forum.utils import author_badge as _forum_badge

from .constants import DOC_PALETTE


def _initials(name):
    words = [w for w in str(name or "").split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _color_for(key):
    s = str(key or "")
    total = sum(ord(c) for c in s)
    return DOC_PALETTE[total % len(DOC_PALETTE)]


def contributor_badge(user):
    """The identity blob shown next to a document / on a contributor card.

    Built on the forum badge (so display_name/avatar stay consistent across
    the site) but with the Explore DocumentProfile's headline/institution
    preferred for the credential line and a DOC_PALETTE colour."""
    if user is None:
        return {
            "id": "", "username": "", "name": "Unknown", "initials": "?",
            "color": "#125027", "title": "", "institution": "", "avatar_url": "",
        }

    base = _forum_badge(user)
    profile = getattr(user, "document_profile", None)
    headline = (getattr(profile, "headline", "") or "").strip() if profile else ""
    institution = (getattr(profile, "institution", "") or "").strip() if profile else ""

    return {
        "id": user.username,
        "username": user.username,
        "name": base["display_name"],
        "initials": base["initials"] or _initials(base["display_name"]),
        "color": _color_for(user.username),
        "title": headline or base["credential"],
        "institution": institution,
        "avatar_url": base["avatar_url"],
    }
