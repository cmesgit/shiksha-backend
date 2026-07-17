# forum/moderation.py — auto-reject scanner for forum submissions.
#
# Unlike chat.moderation.check_message() (first-match-wins, used for a live
# chat message), a forum submission can trip MORE THAN ONE category at once
# (the Moderator Panel design shows posts flagged for 2-3 reasons
# simultaneously), so scan_content() returns every category that matched
# rather than stopping at the first hit.
#
# This reuses chat.moderation's normalization helpers (leet-speak/accent
# stripping, repeat-letter collapsing) for the profanity layer, and its
# SLURS/INCITEMENT_PHRASES sets for the hate-speech layer, so the two scanners
# share one tuned profanity/slur list instead of drifting apart. The three
# categories with no chat precedent (academic fraud, self-harm incitement,
# promotional spam) are new, narrow, phrase-based checks — keyword matching
# for these is inherently approximate; false negatives are expected. This is a
# first-pass heuristic layer, not a guarantee.

import re

from chat.moderation import _strip_accents, _contains_profanity, SLURS, INCITEMENT_PHRASES

CATEGORY_ACADEMIC_FRAUD = "academic_fraud"
CATEGORY_HATE_HARASSMENT = "hate_harassment"
CATEGORY_SELF_HARM = "self_harm"
CATEGORY_PROMOTIONAL_SPAM = "promotional_spam"
CATEGORY_MISINFORMATION = "misinformation"
CATEGORY_POLITICAL = "political"

CATEGORY_LABELS = {
    CATEGORY_ACADEMIC_FRAUD: "Academic fraud",
    CATEGORY_HATE_HARASSMENT: "Hate speech / harassment",
    CATEGORY_SELF_HARM: "Self-harm incitement",
    CATEGORY_PROMOTIONAL_SPAM: "Promotional spam",
    CATEGORY_MISINFORMATION: "Misinformation",
    CATEGORY_POLITICAL: "Political / controversial",
}


def _norm_text(text):
    return " " + re.sub(r"\s+", " ", _strip_accents(text or "").lower()).strip() + " "


def _any_phrase(low, phrases):
    return any(p in low for p in phrases)


# ---------------------------------------------------------------------------
# Academic fraud — leaked papers, paid cheating, fake credentials.
# ---------------------------------------------------------------------------
ACADEMIC_FRAUD_PHRASES = [
    "leaked paper", "leaked question paper", "leaked exam paper",
    "sell answer key", "buy answer key", "answer key for sale",
    "paid exam help", "exam solutions for sale", "pay someone to take my exam",
    "take my exam for me", "exam proxy", "proxy exam", "impersonate in exam",
    "cheat in exam", "cheating in exam", "cheat during exam",
    "fake certificate", "fake marksheet", "fake degree", "buy degree",
    "buy certificate", "fake mark sheet",
]


def _academic_fraud(low):
    return _any_phrase(low, ACADEMIC_FRAUD_PHRASES)


# ---------------------------------------------------------------------------
# Hate speech / harassment — reuse chat's profanity + identity-slur/incitement
# detection (a strong, unambiguous denylist; not the fuller "political
# argument" heuristic chat also applies to hot-button topics — that's not
# forum-appropriate here, general debate on current affairs is fine).
# ---------------------------------------------------------------------------
def _hate_harassment(text, low):
    if _contains_profanity(text):
        return True
    words = set(re.findall(r"[a-z]+", low))
    if words & SLURS:
        return True
    if _any_phrase(low, INCITEMENT_PHRASES):
        return True
    return False


# ---------------------------------------------------------------------------
# Self-harm incitement — narrow, directed-at-someone phrases only. Never
# flags a bare mention of mental health or someone seeking/offering support.
# ---------------------------------------------------------------------------
SELF_HARM_PHRASES = [
    "kill yourself", "kill urself", "go kill yourself", "you should kill yourself",
    "just end it", "end your life", "you should die", "go die", "just die already",
    "nobody would miss you", "no one would miss you if you died",
]


def _self_harm(low):
    return _any_phrase(low, SELF_HARM_PHRASES)


# ---------------------------------------------------------------------------
# Promotional spam — external contact sharing + generic ad phrasing.
# ---------------------------------------------------------------------------
PROMO_PHRASES = [
    "join our telegram", "join telegram group", "join our whatsapp",
    "join whatsapp group", "dm me for", "message me on", "contact me at",
    "call now", "limited time offer", "earn money fast", "work from home earn",
    "click this link", "visit this link now",
]
_PHONE_RE = re.compile(r"\b[6-9]\d{9}\b")
_URL_RE = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.IGNORECASE)


def _promotional_spam(low, raw_text):
    if _any_phrase(low, PROMO_PHRASES):
        return True
    if _PHONE_RE.search(raw_text):
        return True
    if _URL_RE.search(raw_text):
        return True
    return False


# ---------------------------------------------------------------------------
# Misinformation — a small denylist of well-known false-claim tropes. Factual
# accuracy isn't keyword-detectable in general; this only catches the most
# common, unambiguous ones.
# ---------------------------------------------------------------------------
MISINFO_PHRASES = [
    "vaccine causes autism", "vaccines cause autism", "5g causes covid",
    "5g causes coronavirus", "covid is a hoax", "coronavirus is a hoax",
    "earth is flat", "moon landing was faked", "moon landing is fake",
    "cures cancer naturally guaranteed", "guaranteed cure for cancer",
]


def _misinformation(low):
    return _any_phrase(low, MISINFO_PHRASES)


def scan_content(title, body):
    """Scan a submission's title + body. Returns a list of matched category
    keys (empty = clean). A submission may match more than one category."""
    raw_text = f"{title or ''}\n{body or ''}"
    low = _norm_text(raw_text)

    categories = []
    if _academic_fraud(low):
        categories.append(CATEGORY_ACADEMIC_FRAUD)
    if _hate_harassment(raw_text, low):
        categories.append(CATEGORY_HATE_HARASSMENT)
    if _self_harm(low):
        categories.append(CATEGORY_SELF_HARM)
    if _promotional_spam(low, raw_text):
        categories.append(CATEGORY_PROMOTIONAL_SPAM)
    if _misinformation(low):
        categories.append(CATEGORY_MISINFORMATION)
    return categories
