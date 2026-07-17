# Author-badge helper: the small identity blob shown next to every question,
# answer, comment and contributor in the redesigned forum.
#
# The account User model is intentionally minimal (no display name/avatar on it),
# so display data is assembled from the forum-owned ForumProfile plus, as a
# fallback, the denormalized accounts.Identity row. Everything degrades to the
# username so a badge always renders.

from .constants import FORUM_PALETTE


def _initials(name):
    words = [w for w in str(name or "").split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _color_for(key):
    """Deterministic palette colour from a stable key (username)."""
    s = str(key or "")
    total = sum(ord(c) for c in s)
    return FORUM_PALETTE[total % len(FORUM_PALETTE)]


def _credential(user):
    """A short 'credential' line, derived from role / teacher profile."""
    tp = getattr(user, "teacher_profile", None)
    if tp is not None:
        for attr in ("current_position", "qualification", "subject_specialization"):
            val = (getattr(tp, attr, "") or "").strip()
            if val:
                return val
    try:
        if user.has_role("TEACHER"):
            return "Teacher · ShikshaCom"
        if user.has_role("COUNSELOR"):
            return "Counsellor · ShikshaCom"
    except Exception:
        pass
    return "Student · ShikshaCom"


def author_badge(user):
    """Return the display blob for a user. Cheap enough for per-row use on a
    paginated list; heavier profile joins are avoided in favour of getattr."""
    if user is None:
        return {
            "username": "", "display_name": "Unknown", "initials": "?",
            "color": "#125027", "credential": "", "avatar_url": "",
        }

    profile = getattr(user, "forum_profile", None)
    fp_name = (getattr(profile, "display_name", "") or "").strip() if profile else ""
    headline = (getattr(profile, "headline", "") or "").strip() if profile else ""

    display_name = fp_name or (user.get_full_name() or "").strip() or user.username

    avatar_url = ""
    identity = user.identities.filter(is_active=True).exclude(avatar_url="").first() \
        if hasattr(user, "identities") else None
    if identity:
        avatar_url = identity.avatar_url or ""
        if not fp_name and identity.display_name:
            display_name = identity.display_name

    return {
        "username": user.username,
        "display_name": display_name,
        "initials": _initials(display_name),
        "color": _color_for(user.username),
        "credential": headline or _credential(user),
        "avatar_url": avatar_url,
    }
