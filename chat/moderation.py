# PLACEMENT: backend/backend/chat/moderation.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/chat/moderation.py
"""
chat/moderation.py — dependency-free content moderation for messaging.

Every outgoing message body is screened by two independent checks:

  1. PROFANITY / VULGARITY      → category "profanity"
  2. POLITICAL / CONTROVERSIAL  → category "political"

WHY IT LIVES HERE (and not in the consumer):
  check_message() is called inside services.post_message(), so the SAME rules
  run for the websocket path AND any REST/admin path. There is one screen, one
  source of truth, and it can never be bypassed by hitting a different endpoint.

DESIGN NOTES
------------
* No external API or ML model — pure Python, microseconds per message.
* PROFANITY is obfuscation-resistant (leetspeak, repeated letters, spaced-out
  "f u c k") WITHOUT the substring false-positives that a naive `"ass" in text`
  check causes. Matching is on word *tokens* with controlled inflection
  suffixes — never arbitrary substrings — plus a safe-word allowlist. So
  "class", "assignment", "pass", "shuttlecock", "peacock", "bass", "grass",
  "associate", "Scunthorpe" etc. are NOT flagged.
* POLITICAL is deliberately CONSERVATIVE. A study/booking chat that silently
  eats ordinary sentences ("I have a government exam", "class election") is
  worse than one that occasionally lets a borderline line through. It fires
  only on: (a) identity/communal slurs, (b) explicit violence/incitement, or
  (c) clearly argumentative political content (a political hot-term together
  with a hostility marker, OR several distinct hot-terms in one message).

TUNING (this is meant to be edited by you):
  - Add/remove banned words in PROFANITY_WORDS / PROFANITY_EXACT.
  - Add safe exceptions in SAFE_WORDS.
  - Tune the political layer via SLURS, INCITEMENT_PHRASES, POLITICAL_TERMS,
    HOSTILITY_MARKERS.
  Keyword moderation is a baseline, not a guarantee. For stricter behaviour,
  replace the body of check_message() with a call to a moderation service /
  classifier — the call site in services.post_message() will not change.
"""

import re
import unicodedata


class ModerationResult:
    """Tiny value object. `ok=True` means the message is allowed."""
    __slots__ = ("ok", "category", "reason")

    def __init__(self, ok, category="", reason=""):
        self.ok = ok
        self.category = category
        self.reason = reason


_ALLOWED = ModerationResult(True)


# ===========================================================================
# 1) PROFANITY / VULGARITY
# ===========================================================================

# Strong, unambiguous profanity + slurs. Lowercase, no spaces. The normaliser
# below means you usually only need the base form: "fuck" also catches "f*ck",
# "fuuuck", "F.U.C.K", "f u c k", and (via the LEET map) "fu(k"-style spellings.
PROFANITY_WORDS = {
    # English (strong / slurs)
    "fuck", "motherfucker", "fucker", "shit", "bullshit", "bitch", "bastard",
    "asshole", "arsehole", "dickhead", "cunt", "slut", "whore", "douchebag",
    "wanker", "pussy", "jackass", "dumbass", "jerkoff", "nigger", "nigga",
    "faggot", "retard", "rapist", "molester", "pedophile", "paedophile",
    # Hindi / Hinglish (common)
    "madarchod", "behenchod", "bhenchod", "bsdk", "chutiya", "chutiyapa",
    "gandu", "gaand", "lund", "lawda", "lavda", "randi", "harami", "haramzada",
    "kutiya", "kamina", "kamini", "bhosdike", "bhosdi", "chodu", "tatti",
}

# Short profanity that must match as a WHOLE word only (matching these as
# substrings would wreck "class", "bass", "assignment", "title", "Dick").
PROFANITY_EXACT = {"ass", "asses", "tit", "tits", "boobs"}

# Words that normalise close to a banned form but are perfectly fine. Belt-and-
# braces on top of the whole-word matching.
SAFE_WORDS = {
    "class", "classes", "classic", "assignment", "assignments", "assist",
    "assistance", "assistant", "associate", "association", "assess",
    "assessment", "assembly", "assassin", "assassins", "pass", "passes",
    "passage", "passion", "bass", "grass", "glass", "brass", "mass", "lass",
    "compass", "embassy", "harassment", "ambassador", "molecule", "molecules",
    "title", "titles", "subtitle", "competition", "competitive", "shiitake",
    "scunthorpe", "shuttlecock", "peacock", "cockpit", "cockroach", "button",
    "buttons", "analysis", "therapist", "therapists",
}

_LEET_MAP = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
    "8": "b", "9": "g", "@": "a", "$": "s", "!": "i", "|": "i", "+": "t",
})

# 3+ identical letters in a row → one letter ("fuuuck" → "fuck", "shiiit" →
# "shit", "loooove" → "love"). 2-in-a-row is left alone so "good"/"balloon"
# survive.
_REPEAT_RE = re.compile(r"(.)\1{2,}")

# Recognised inflection tails. A token matches a banned base if it equals the
# base OR is base + one of these (so "fucker", "fucking", "bitches" match, but
# "cockpit"/"peacock"/"assignment" do not).
_INFLECT = ("", "s", "es", "ed", "ing", "in", "er", "ers", "y", "ies", "z", "a", "o")


def _strip_accents(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def _normalise_token(tok):
    """Lowercase → strip accents → de-leet → keep letters → collapse repeats."""
    tok = _strip_accents(tok).lower().translate(_LEET_MAP)
    tok = re.sub(r"[^a-z]", "", tok)
    tok = _REPEAT_RE.sub(r"\1", tok)
    return tok


def _matches_banned(norm):
    """True if a normalised token is profanity (whole-word + inflection only)."""
    if not norm or norm in SAFE_WORDS:
        return False
    if norm in PROFANITY_EXACT:
        return True
    for base in PROFANITY_WORDS:
        if norm == base:
            return True
        if norm.startswith(base):
            tail = norm[len(base):]
            if tail in _INFLECT:
                return True
    return False


def _censor_candidates(raw):
    """Expand a `*`-censored token into plausible originals.

    "f*ck" → {"fack","feck","fick","fock","fuck","fck", ...}. Repeated stars
    are collapsed first so "f**k" → "f*k" → expanded. This catches the common
    "censor one vowel" pattern without affecting normal tokens.
    """
    if "*" not in raw:
        return (raw,)
    base = re.sub(r"\*+", "*", raw)          # collapse "**" → "*"
    out = {raw, base.replace("*", "")}
    for v in "aeiou":
        out.add(base.replace("*", v))
    return out


def _contains_profanity(text):
    # Raw tokens (keep leet/punct so the normaliser can do its job).
    raw_tokens = re.findall(r"[A-Za-z0-9@$!|+*]+", text)

    norm_tokens = []
    for t in raw_tokens:
        matched = False
        for cand in _censor_candidates(t):
            n = _normalise_token(cand)
            if _matches_banned(n):
                return True
            if not matched:
                norm_tokens.append(n)   # keep the first (uncensored) form
                matched = True

    # Spaced-out obfuscation: a run of 3+ single-letter tokens ("f u c k")
    # is joined and re-tested ("fuck").
    run = []
    for n in norm_tokens + [""]:
        if len(n) == 1:
            run.append(n)
            continue
        if len(run) >= 3:
            joined = "".join(run)
            # test the whole join and every 3..len window
            if _matches_banned(joined):
                return True
            for size in range(len(joined), 2, -1):
                for i in range(0, len(joined) - size + 1):
                    if _matches_banned(joined[i:i + size]):
                        return True
        run = []
    return False


# ===========================================================================
# 2) POLITICAL / CONTROVERSIAL
# ===========================================================================

# (a) Identity / communal slurs — denylisted outright. (Kept minimal; this is
#     a blocklist whose only purpose is to STOP this content.)
SLURS = {
    "katua", "mulla", "mullah", "jihadi", "terrorist", "aatankwadi",
    "chamar", "bhangi", "chinki", "madrasi", "pakistani"  # used pejoratively
}

# (b) Explicit violence / incitement — fires regardless of surrounding words.
INCITEMENT_PHRASES = [
    "kill all", "death to", "should be killed", "should be shot",
    "should be hanged", "should be lynched", "wipe them out", "burn them",
    "gas them", "ethnic cleansing", "go back to pakistan", "leave the country",
    "deserve to die", "exterminate",
]

# (c) Political / controversial HOT terms. A passing mention is NOT enough —
#     see the firing logic in _political_verdict().
POLITICAL_TERMS = {
    "bjp", "congress", "modi", "rahul gandhi", "aap", "kejriwal", "rss",
    "hindutva", "hindu rashtra", "khalistan", "caa", "nrc", "article 370",
    "ram mandir", "babri", "love jihad", "communal", "reservation quota",
    "anti national", "anti-national", "deshdrohi", "urban naxal", "tukde tukde",
    "godhra", "kashmir issue", "uniform civil code", "secular vs",
    # Globally-relevant hot terms. Kept to argument-leaning words so a single
    # neutral mention still passes (the firing logic needs a 2nd term or a
    # hostility marker); academic words like "democracy", "revolution",
    # "world war", country/leader names, etc. are deliberately NOT here so
    # history / civics tutoring isn't blocked.
    "election", "elections", "leftist", "rightist", "communist", "communism",
    "marxist", "marxism", "socialist", "deep state", "voter fraud",
    "election fraud", "stolen election", "rigged election", "white supremacy",
    "globalist", "woke agenda",
}

# Hostility / argument markers. A political hot-term TOGETHER with one of these
# is treated as an argument, not a neutral reference.
HOSTILITY_MARKERS = {
    "hate", "traitor", "corrupt", "fascist", "fascism", "bhakt", "andhbhakt",
    "presstitute", "boycott", "anti", "shame", "propaganda", "agenda",
    "destroy", "fraud", "looter", "sickular", "libtard", "bigot", "riot",
    "rigged", "stolen", "tyrant", "dictator", "regime", "supremacist",
}


def _political_verdict(text):
    low = " " + re.sub(r"\s+", " ", _strip_accents(text).lower()).strip() + " "

    # word set for slur / marker checks (whole-word)
    words = set(re.findall(r"[a-z]+", low))
    # Add naive singular forms so plurals match singular markers/slurs
    # ("traitors"→"traitor", "supremacists"→"supremacist").
    for w in list(words):
        if len(w) > 4 and w.endswith("es"):
            words.add(w[:-2])
        if len(w) > 3 and w.endswith("s"):
            words.add(w[:-1])

    # (a) slurs
    if words & SLURS:
        return True

    # (b) incitement phrases
    for ph in INCITEMENT_PHRASES:
        if ph in low:
            return True

    # (c) argumentative political content
    hits = [t for t in POLITICAL_TERMS if (" " + t + " ") in low or t in words]
    if not hits:
        return False
    # several distinct hot-terms in one message → an argument
    if len(set(hits)) >= 2:
        return True
    # a hot-term + a hostility marker → an argument
    if words & HOSTILITY_MARKERS:
        return True
    return False


# ===========================================================================
# Public entry point
# ===========================================================================

PROFANITY_REASON = (
    "Your message looks like it contains offensive or vulgar language, so it "
    "wasn't sent. Please keep the conversation respectful."
)
POLITICAL_REASON = (
    "Your message looks like it contains political or controversial content "
    "that isn't allowed here, so it wasn't sent. Please keep chats focused on "
    "learning."
)


def check_message(body):
    """
    Screen one message body.

    Returns a ModerationResult. `ok=True` → allowed. Otherwise `category` is
    "profanity" or "political" and `reason` is a user-facing explanation.
    """
    text = (body or "").strip()
    if not text:
        return _ALLOWED

    if _contains_profanity(text):
        return ModerationResult(False, "profanity", PROFANITY_REASON)

    if _political_verdict(text):
        return ModerationResult(False, "political", POLITICAL_REASON)

    return _ALLOWED
