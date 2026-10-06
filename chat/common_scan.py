"""Regex-based message classifier (scanner) for EVA.

Each scanner is a compiled regex.  The function ``scan_message`` checks a
user message against every scanner and returns a list of matched category
tags.  The first match (by list order) is considered the *primary* intent.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Gender + Age detection
# ---------------------------------------------------------------------------

# Gender+Age detection, incl. separator variants: "m", "M 21", "M.19",
# "m-24", "M28", "m21."
_GENDER_AGE_RE = re.compile(
    r"^([mMfF])[.\-\s]*(\d{1,2})?\b",
    re.IGNORECASE,
)

# Gender letter + age right after it, NOT at message start:
# "im m 22", "i am f 19", "i'm m 22", "hey m21", "yeah f 22", "hi f 20"
# (the compact "hey m21" case must be checked BEFORE the standalone-age
# scan, otherwise "21" wins and the gender is lost).
_IAM_GENDER_AGE_RE = re.compile(
    r"\b(?:i'?m|i\s*am)\s+([mf])\s*(\d{1,2})\b",
    re.IGNORECASE,
)

_GENDER_AGE_ANYWHERE_RE = re.compile(
    r"(?<![\w'])\b([mf])\s*(\d{1,2})\b",
    re.IGNORECASE,
)

# Age then gender: "22 m", "16 f", "19.m", "21 male", "20 female", "18 girl"
_AGE_GENDER_RE = re.compile(
    r"\b(\d{1,2})[.\-\s]*(m|f|male|female|boy|girl|guy)\b",
    re.IGNORECASE,
)

# Word-form gender with optional "a": "im a girl", "i am male", "me a guy",
# "i'm a boy", "im female" -> gender only, age (if any) handled separately.
_WORD_GENDER_RE = re.compile(
    r"\b(?:i'?m|i\s*am|im|me)\s+(?:a\s+)?(girl|guy|boy|male|female|woman|man|chick|lad|lady|gal|dude)\b",
    re.IGNORECASE,
)

# Bare gender letter token (not a contraction tail like "i'm"): "m", "f",
# "hi m", "hey f", "ok f", "me f". Excludes "im"/"am"/"if"/"of"/"9m" via the
# combined lookbehind on word chars and apostrophes.
_BARE_GENDER_LETTER_RE = re.compile(
    r"(?<![\w'])\b([mf])\b",
    re.IGNORECASE,
)

# Explicit words alone anywhere: "male", "female"
_BARE_GENDER_WORD_RE = re.compile(
    r"\b(male|female)\b",
    re.IGNORECASE,
)

# Age-only detection: "im 39", "i'm 25", "i am 22", "25", "age 30"
_AGE_ONLY_RE = re.compile(
    r"(?:^|\b)(?:i'?m|i\s*am|age|im)\s*(\d{1,2})\b",
    re.IGNORECASE,
)

# Standalone age number anywhere in the message, as its own token:
# "39", "bored, 25", "just 21 yo", "sry 21"
_STANDALONE_AGE_RE = re.compile(
    r"(?<![\d])(\d{1,2})(?![\d])",
)

# Pattern for "age and from", "age from", "age and country" etc
_AGE_FROM_RE = re.compile(
    r"(?:^|\b)(?:age|ur\s*age|your\s*age)\s*(?:and|&|\+)\s*(?:from|country|location)",
    re.IGNORECASE,
)

# Pattern for standalone "age", "age?" requests
_AGE_REQUEST_RE = re.compile(
    r"(?:^|\b)(?:age|ur\s*age|your\s*age|how\s*old|r\s*u|ur)\??(?:$|\s)",
    re.IGNORECASE,
)

_GENDER_WORDS = ("girl", "female", "woman", "chick", "lady", "gal")

def detect_gender_age(user_message: str) -> Optional[Tuple[str, Optional[int]]]:
    """Detect gender and optional age from a short message.

    Returns (gender, age) or None.
    Examples:
        "m"        -> ("male", None)
        "M 21"     -> ("male", 21)
        "f"        -> ("female", None)
        "F 19"     -> ("female", 19)
        "hi m"     -> ("male", None)
        "hey f"    -> ("female", None)
        "hey m21"  -> ("male", 21)
        "hi f 20"  -> ("female", 20)
        "im a girl"-> ("female", None)
        "22 m"     -> ("male", 22)
        "im 39"    -> (None, 39)  -- age only
        "i'm 25"   -> (None, 25)  -- age only
        "39"       -> (None, 39)  -- standalone age
    """
    msg = user_message.strip()
    msg_lower = msg.lower()

    # 1. Start-anchored gender+age: "m", "M 21", "f 19", "m22"
    m = _GENDER_AGE_RE.match(msg_lower)
    if m:
        gender = "male" if m.group(1).lower() == "m" else "female"
        age = int(m.group(2)) if m.group(2) else None
        return (gender, age)

    # 2. Gender letter + age after other words ("im m 22", "hey m21",
    #    "hi f 20", "yeah f 22"). Age range-guarded so things like "f2f"
    #    never produce a bogus 2-year-old.
    for rx in (_IAM_GENDER_AGE_RE, _GENDER_AGE_ANYWHERE_RE):
        m = rx.search(msg_lower)
        if m:
            age = int(m.group(2))
            if 13 <= age <= 99:
                gender = "male" if m.group(1).lower() == "m" else "female"
                return (gender, age)

    # 3. Age then gender: "22 m", "16 f", "19.m", "21 male", "18 girl"
    m = _AGE_GENDER_RE.search(msg_lower)
    if m:
        age = int(m.group(1))
        if 13 <= age <= 99:
            gender = "male" if str(m.group(2)).lower() in ("m", "male", "guy") \
                else "female"
            return (gender, age)

    # 4. Word-form gender: "im a girl", "i am male", "me a guy", "i'm a boy"
    m = _WORD_GENDER_RE.search(msg_lower)
    if m:
        word = m.group(1).lower()
        gender = "female" if word in _GENDER_WORDS else "male"
        return (gender, None)

    # 5. Bare gender letter token, not a contraction tail: "m", "f", "hi m",
    #    "hey f", "ok f", "me f"
    m = _BARE_GENDER_LETTER_RE.search(msg_lower)
    if m:
        gender = "male" if m.group(1).lower() == "m" else "female"
        return (gender, None)

    # 6. Explicit word anywhere: "male", "female"
    m = _BARE_GENDER_WORD_RE.search(msg_lower)
    if m:
        return ("male" if m.group(1).lower() == "male" else "female", None)

    # 7. Age-only: "im 39", "i'm 25", "i am 22", "age 30"
    m = _AGE_ONLY_RE.search(msg_lower)
    if m:
        age = int(m.group(1))
        if 13 <= age <= 99:
            return (None, age)

    # 8. Standalone age: "39", "25", "bored, 21"
    m = _STANDALONE_AGE_RE.search(msg_lower)
    if m:
        age = int(m.group(1))
        if 13 <= age <= 99:
            return (None, age)

    return None


# ---------------------------------------------------------------------------
# Scanner definitions  —  each is (category_tag, compiled_regex)
# ---------------------------------------------------------------------------

GREETING_SCAN = (
    "greeting",
    re.compile(
        r"\b("
        r"hi+|hey+|hello+|hlw|hlo|yo+|sup|wassup|what'?s?\s*up"
        r"|good\s*(morning|evening|night|afternoon)"
        r"|gm|gn|hru|how\s*r\s*u|how\s*are\s*u|how\s*you\s*doing"
        r"|how'?s?\s*it\s*going|kemon\s*achho|ki\s*khobor"
        r"|howdy|hiya|heya|salaam|asalam"
        r")\b",
        re.IGNORECASE,
    ),
)

AGE_SCAN = (
    "age_question",
    re.compile(
        r"\b("
        r"how\s*old\s*(r\s*u|are\s*you)"
        r"|ur\s*age|your\s*age|age\s*\?"
        r"|what'?s?\s*your\s*age"
        r"|what\s*is\s*your\s*age"
        r")\b",
        re.IGNORECASE,
    ),
)

NAME_SCAN = (
    "name_question",
    re.compile(
        r"\b("
        r"what'?s?\s*(ur|your)\s*name"
        r"|who\s*r\s*u|ur\s*name|your\s*name"
        r"|tell\s*me\s*(ur|your)\s*name"
        r"|name\s*\?"
        r"|what\s*is\s*your\s*name"
        r")\b",
        re.IGNORECASE,
    ),
)

LOCATION_SCAN = (
    "location_question",
    re.compile(
        r"\b("
        r"where\s*(r\s*u|are\s*you|you|u)\s*(from)?"
        r"|ur\s*(country|location|city)"
        r"|your\s*(country|location|city)"
        r"|where\s*do\s*u\s*live"
        r"|whereabouts"
        r"|where\s*from"
        r"|r\s*u\s*from"
        r"|u\s*from"
        r"|from\s*(r\s*u|where|you|\?)"
        r")\b",
        re.IGNORECASE,
    ),
)

HOW_ARE_YOU_SCAN = (
    "how_are_you",
    re.compile(
        r"\b("
        r"how\s*(r\s*u|are\s*u|are\s*you)"
        r"|how'?s?\s*it\s*going"
        r"|how\s*you\s*doing"
        r"|hru|kemon\s*achho|ki\s*khobor"
        r"|how\s*are\s*you\s*doing"
        r")\b",
        re.IGNORECASE,
    ),
)

FLIRT_SCAN = (
    "flirt",
    re.compile(
        r"\b("
        r"do\s*u\s*like\s*me"
        r"|you\s*(like|love)\s*me"
        r"|ur\s*cute|you'?re?\s*cute"
        r"|beautiful|gorgeous|pretty"
        r"|i\s*(like|love)\s*you"
        r"|babe|baby|dear|darling|sweetheart"
        r"|flirt|flirty"
        r"|do\s*u\s*have\s*a\s*bf|are\s*u\s*taken"
        r"|single|boyfriend|girlfriend"
        r")\b",
        re.IGNORECASE,
    ),
)

HORNY_SCAN = (
    "horny",
    re.compile(
        r"\b("
        r"horny|hot|sexy|dm\b|date\b|alone"
        r"|bored|feeling\s*horny"
        r"|what\s*are\s*u\s*wearing"
        r"|dirty|naughty"
        r")\b",
        re.IGNORECASE,
    ),
)

SNAP_REQUEST_SCAN = (
    "snap_request",
    re.compile(
        r"\b("
        r"snap|snapchat|sc\b|add\s*me|send\s*(me\s*)?your\s*sc"
        r"|username|pic|photo|photos|picture"
        r"|send\s*a\s*(pic|photo|picture|selfie)"
        r"|can\s*i\s*see\s*(u|you)"
        r"|show\s*(me\s*)?(ur|your)\s*(face|body|self)"
        r"|send\s*snap"
        r"|what'?s?\s*your\s*snap"
        r"|your\s*snap"
        r"|ছবি|দেখাও"
        r")\b",
        re.IGNORECASE,
    ),
)

SEXTING_SCAN = (
    "sexting",
    re.compile(
        r"\b("
        r"send\s*nude|nudes?|dick|cock|pussy|sex"
        r"|masturbat|orgasm|cum\b|cumming"
        r"|fuck\b|fucking|fucked"
        r"|rate\s*(me|my\s*body)"
        r")\b",
        re.IGNORECASE,
    ),
)

OBJECTION_SCAN = (
    "objection",
    re.compile(
        r"\b("
        r"scam|fake|bot|not\s*real"
        r"|why\s*(r\s*u|are\s*you)\s*here"
        r"|what\s*do\s*u\s*want"
        r"|boring|waste\s*(of\s*)?time"
        r"|bye|goodbye|gtg|gotta\s*go"
        r"|no\s*thanks|nah|nope|pass"
        r")\b",
        re.IGNORECASE,
    ),
)

SOCIAL_MEDIA_SCAN = (
    "social_media_request",
    re.compile(
        r"\b("
        r"instagram|insta|ig\b|facebook|fb\b"
        r"|whatsapp|wa\b|telegram|tg\b"
        r"|discord|twitter|tiktok"
        r")\b",
        re.IGNORECASE,
    ),
)

CONTENT_REQUEST_SCAN = (
    "content_request",
    re.compile(
        r"\b("
        r"sell|content|onlyfans|of\b|fans\s*only"
        r"|do\s*u\s*(sell|have)\s*(content|of)"
        r"|do\s*anything\s*like\s*that"
        r")\b",
        re.IGNORECASE,
    ),
)

THANK_SCAN = (
    "thanks",
    re.compile(
        r"\b("
        r"thank|thx|ty|thanks|tysm|thank\s*u|thank\s*you"
        r")\b",
        re.IGNORECASE,
    ),
)

CONFUSED_SCAN = (
    "confused",
    re.compile(
        r"\b("
        r"what|idk|confused|huh|wha|hm\?|hmm\?|i\s*don'?t\s*get\s*it"
        r"|what\s*do\s*you\s*mean"
        r")\b",
        re.IGNORECASE,
    ),
)


# Master list — order matters (first match wins primary tag)
ALL_SCANNERS = [
    GREETING_SCAN,
    HOW_ARE_YOU_SCAN,
    AGE_SCAN,
    NAME_SCAN,
    LOCATION_SCAN,
    SNAP_REQUEST_SCAN,
    SEXTING_SCAN,
    CONTENT_REQUEST_SCAN,
    SOCIAL_MEDIA_SCAN,
    HORNY_SCAN,
    FLIRT_SCAN,
    OBJECTION_SCAN,
    THANK_SCAN,
    CONFUSED_SCAN,
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_message(user_message: str) -> Tuple[Optional[str], List[str]]:
    """Classify *user_message*.

    Returns
    -------
    ``(primary_tag, all_tags)`` where *primary_tag* is the first matching
    category (or ``None`` if nothing matched) and *all_tags* is the full
    list of matched categories.
    """
    all_tags: List[str] = []
    primary: Optional[str] = None

    for tag, pattern in ALL_SCANNERS:
        if pattern.search(user_message):
            all_tags.append(tag)
            if primary is None:
                primary = tag

    return primary, all_tags


_NAME_INTRO_RE = re.compile(
    r"(?:i\s+am|\bi'm\b|\bim\b|my\s+name\s+is|call\s+me)\s+"
    r"([A-Za-z][A-Za-z'.-]{1,14})"
)
_NAME_HERE_RE = re.compile(r"^\s*([A-Z][a-z]{1,14})\s+here\b")

# Words that commonly follow "i am" but are NOT names.
_NAME_STOPWORDS = frozenset({
    "a", "not", "new", "back", "here", "sorry", "good", "fine", "ok", "okay",
    "horny", "bored", "tired", "busy", "single", "alone", "done", "so",
    "very", "just", "sure", "down", "sad", "happy", "hungry", "broke",
    "from", "gaming", "looking", "trying", "kinda", "kind", "super", "really",
    "literally", "actually", "m", "f", "boy", "girl", "guy", "guys",
    "random", "lost", "tipsy", "drunk", "sober", "free", "busy", "online",
})


def detect_user_name(user_message: str) -> Optional[str]:
    """Best-effort capture of a user's self-introduced name.

    Matches "i am <Name>", "my name is <Name>", "call me <Name>" and the
    "<Name> here" opener. Names that merely ride along ("i am so tired",
    "i am horny") are rejected so a pseudo-name never gets stored.
    """
    msg = user_message.strip()
    if not msg:
        return None
    m = _NAME_INTRO_RE.search(msg.lower())
    candidate = None
    if m:
        candidate = m.group(1)
    else:
        m2 = _NAME_HERE_RE.match(msg)
        if m2:
            candidate = m2.group(1).lower()
    if not candidate:
        return None
    base = candidate.lstrip("'").strip(".'-").lower()
    if not base or len(base) < 2 or base in _NAME_STOPWORDS:
        return None
    return base.capitalize()


# ---------------------------------------------------------------------------
# Rich memory capture (context engine): facts the user volunteers mid-chat.
# Each pattern returns a (key, value) so rules.py can store them in the
# conversation state and reuse them in later replies via {hobby}/{music}/
# {pet}/{job} placeholders. Only clearly volunteer-specific phrases match;
# short stop-words ("u", "you", "it") are rejected so a pseudo-fact is never
# remembered.
# ---------------------------------------------------------------------------

_FACT_STOPWORDS = frozenset({
    "u", "ur", "you", "your", "ya", "me", "my", "it", "that", "this",
    "lol", "omg", "tbh", "ngl", "fr", "rn", "wbu", "hbu", "so", "too",
})

_FACT_PET_RE = re.compile(
    r"\bi(?:'?m|s)?\s*(?:have|got|own)\s+(?:a|an|my)?\s*"
    r"(dog|cat|puppy|kitten|parrot|bird|fish|rabbit|hamster|turtle|snake)\b",
    re.IGNORECASE,
)
_FACT_MUSIC_RE = re.compile(
    r"\bi\s+(?:listen to|am into|love|like)\s+(\w+)\s+music\b",
    re.IGNORECASE,
)
_FACT_HOBBY_RE = re.compile(
    r"\bi\s+(?:love|like|enjoy|am into|am obsessed with)\s+([\w\s-]{3,24}?)(?=[\s.,!?]|$)",
    re.IGNORECASE,
)
_FACT_JOB_RE = re.compile(
    r"\bi\s+(?:work|wannna work|want to work)\s+(?:as|at|in)?\s*([\w\s-]{2,16})",
    re.IGNORECASE,
)
_FACT_FAV_RE = re.compile(
    r"\bmy\s+fav(?:ourite|orite)?\s+\w+\s+is\s+([\w\s-]{2,24}?)(?=[\s.,!?]|$)",
    re.IGNORECASE,
)


def detect_user_facts(user_message: str) -> dict:
    """Capture details the user volunteers (pet/music/hobby/job/favourite).

    Returns a mapping like ``{"pet": "dog", "music": "rap", "hobby": "gaming"}``
    or an empty dict. Values are lowercase, trimmed and filtered through
    ``_FACT_STOPWORDS`` so "i like u" never becomes a remembered fact.
    """
    msg = user_message.strip()
    if not msg:
        return {}
    facts: dict = {}
    m = _FACT_PET_RE.search(msg)
    if m:
        facts["pet"] = m.group(1).lower()
    m = _FACT_MUSIC_RE.search(msg)
    if m:
        facts["music"] = m.group(1).strip().lower()
    m = _FACT_HOBBY_RE.search(msg)
    if m:
        val = m.group(1).strip().lower()
        if val and len(val) >= 3 and val.split()[0] not in _FACT_STOPWORDS:
            facts["hobby"] = val
    m = _FACT_JOB_RE.search(msg)
    if m:
        val = m.group(1).strip(" '.").lower()
        val = re.sub(r"^(?:a|an)\s+", "", val)
        if val and len(val) >= 2 and val.split()[0] not in _FACT_STOPWORDS:
            facts["job"] = val
    m = _FACT_FAV_RE.search(msg)
    if m:
        val = m.group(1).strip(" '.").lower()
        if val and len(val) >= 2 and val.split()[0] not in _FACT_STOPWORDS:
            facts["fav"] = val
    return facts


# ---------------------------------------------------------------------------
# User First SMS Router helpers (spec: greeting / age-gender / country).
# Thin wrappers over the scanners above so the pattern detector can route
# the very first SMS in priority order: age/gender -> country -> greeting.
# ---------------------------------------------------------------------------

# Compact age/gender forms need a word boundary so "message" never matches.
_AGE_GENDER_COMPACT_RE = re.compile(r"\b(m|f)\s*(\d{2})\b", re.IGNORECASE)

# Full-word forms: "male 22", "female 19", "22 male".
_AGE_GENDER_WORD_RE = re.compile(
    r"\b((?:male|female))\s*(\d{1,2})\b|\b(\d{1,2})\s*(male|female)\b",
    re.IGNORECASE,
)


def detect_age_gender(text: str):
    """Return {"gender": ..., "age": ...} or None (spec Task 1)."""
    if not text or not text.strip():
        return None
    m = _AGE_GENDER_COMPACT_RE.search(text)
    if m:
        try:
            age = int(m.group(2))
        except (TypeError, ValueError):
            age = None
        if age is not None and not 13 <= age <= 99:
            return None
        return {"gender": "male" if m.group(1).lower() == "m" else "female",
                "age": age}
    m = _AGE_GENDER_WORD_RE.search(text)
    if m:
        word = (m.group(1) or m.group(4) or "").lower()
        num = m.group(2) or m.group(3)
        try:
            age = int(num) if num else None
        except (TypeError, ValueError):
            age = None
        if age is not None and not 13 <= age <= 99:
            return None
        gender = "female" if word == "female" else "male"
        return {"gender": gender, "age": age}
    found = detect_gender_age(text)
    if found:
        gender, age = found
        if gender is None and age is None:
            return None
        return {"gender": gender, "age": age}
    return None


def detect_greeting(text: str) -> bool:
    """True when *text* contains a greeting token (spec Task 1)."""
    if not text or not text.strip():
        return False
    return bool(GREETING_SCAN[1].search(text))


def detect_country_question(text: str) -> bool:
    """True when *text* asks where the bot is from (spec Task 1)."""
    if not text or not text.strip():
        return False
    if bool(LOCATION_SCAN[1].search(text)):
        return True
    t = text.strip().lower()
    if re.search(r"\bfrom\s*\?+\s*$", t):
        return True
    if re.match(r"^\s*from\s*\?*\s*$", t):
        return True
    return False
