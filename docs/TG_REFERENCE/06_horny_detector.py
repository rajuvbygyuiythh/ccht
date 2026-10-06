"""
EVA BOT v7.0 — Horny Detector
Uses 5 independent detection methods to determine if user is horny.
Returns confidence score 0.0 - 1.0 and specific triggers.
"""
import re
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import clean_text, log

# ===== METHOD 1: KEYWORD DETECTION =====

HORNY_KEYWORDS = {
    # Direct horny words (high weight)
    "horny": 0.85,
    "horney": 0.85,
    "so horny": 0.92,
    "turned on": 0.88,
    "turned me on": 0.90,
    "in the mood": 0.72,
    "aroused": 0.80,
    "excited": 0.45,
    "so hard": 0.82,
    "so wet": 0.82,
    "throbbing": 0.78,
    "dripping": 0.75,
    "pulsing": 0.65,

    # Sexual body parts (medium-high weight)
    "dick": 0.70,
    "cock": 0.72,
    "penis": 0.68,
    "pussy": 0.72,
    "boobs": 0.55,
    "boob": 0.55,
    "tits": 0.60,
    "ass": 0.50,
    "nipples": 0.62,
    "thighs": 0.48,
    "boner": 0.72,
    "erection": 0.70,

    # Sexual acts (high weight)
    "fuck": 0.70,
    "fucking": 0.72,
    "suck": 0.62,
    "sucking": 0.62,
    "lick": 0.55,
    "licking": 0.55,
    "blowjob": 0.78,
    "bj": 0.65,
    "handjob": 0.68,
    "69": 0.60,
    "rim": 0.65,
    "creampie": 0.80,
    "anal": 0.68,
    "doggy": 0.60,
    "missionary": 0.45,
    "cowgirl": 0.55,

    # Orgasm (very high weight)
    "cum": 0.80,
    "cumming": 0.85,
    "cum inside": 0.90,
    "orgasm": 0.82,
    "moan": 0.58,
    "moaning": 0.58,
    "groan": 0.55,
    "scream": 0.40,

    # Dirty/sexy words (medium weight)
    "sexy": 0.45,
    "dirty": 0.48,
    "naughty": 0.50,
    "kinky": 0.55,
    "fantasy": 0.52,
    "fantasize": 0.55,
    "jerk": 0.50,
    "masturbat": 0.65,
    "stroke": 0.45,
    "spank": 0.55,
    "bondage": 0.62,
    "bdsm": 0.65,
    "roleplay": 0.48,
    "rp": 0.35,
    "sext": 0.55,
    "sexting": 0.60,

    # Nude/undress (medium-high weight)
    "nude": 0.65,
    "naked": 0.62,
    "undress": 0.65,
    "strip": 0.55,
    "bare": 0.45,
    "exposed": 0.50,
    "lingerie": 0.45,
    "panties": 0.48,
    "thong": 0.50,
    "bra": 0.35,

    # Flirty escalation (lower weight)
    "daddy": 0.48,
    "mommy": 0.42,
    "baby girl": 0.40,
    "good girl": 0.45,
    "bad girl": 0.42,
    "kiss": 0.30,
    "makeout": 0.38,
    "make out": 0.38,
    "cuddle": 0.25,
    "spoon": 0.28,
    "touch": 0.25,
    "feel": 0.20,

    # Content/link related (medium weight - user wants more)
    "send nudes": 0.65,
    "send nude": 0.65,
    "send me a pic": 0.50,
    "show me ur": 0.50,
    "show me your": 0.50,
    "what are u wearing": 0.55,
    "what are you wearing": 0.55,
    "what u wearing": 0.55,
    "i want to see you": 0.48,
    "i wanna see you": 0.48,
    "let me see": 0.45,
    "can i see": 0.42,
}

# ===== METHOD 2: PHRASE PATTERNS =====

HORNY_PHRASES = [
    (r"\b(make|making)\s+me\s+(so\s+)?horny\b", 0.85),
    (r"\b(i['']?m|i\s+am)\s+(so\s+)?horny\b", 0.85),
    (r"\b(i['']?m|i\s+am)\s+(so\s+)?(hard|wet)\b", 0.80),
    (r"\b(i|you)\s+(make|got)\s+me\s+(so\s+)?(hard|wet|horny)\b", 0.82),
    (r"\b(i|you)\s+turn(ed)?\s+me\s+on\b", 0.75),
    (r"\b(i\s+want\s+(to\s+)?(fuck|suck|lick|eat|taste|feel|touch|kiss))\b", 0.65),
    (r"\b(i\s+wanna\s+(fuck|suck|lick|eat|taste|feel|touch|kiss))\b", 0.65),
    (r"\b(let\s+me\s+(fuck|suck|lick|eat|taste|feel|touch))\b", 0.62),
    (r"\b(i\s+(want|need)\s+(you|u|ur|your)\s+(so\s+)?(bad|rn|right\s+now))\b", 0.55),
    (r"\b(thinking\s+about|think\s+about)\s+(you|u|us|fucking)\b", 0.52),
    (r"\b(i\s+(want|need)\s+(to\s+)?(be\s+)?inside\s+(you|u))\b", 0.70),
    (r"\b(come|get)\s+(here|over|on\s+top|in\s+bed)\b", 0.40),
    (r"\b(i\s+(can['']?t|cannot)\s+stop\s+thinking\s+about)\b", 0.55),
    (r"\b(i\s+had\s+a\s+(dirty|sexy|wet|hot|naughty)\s+dream)\b", 0.60),
    (r"\b(dream(t|ed)?\s+about\s+(you|u|us|fucking))\b", 0.55),
    (r"\b(i\s+(wish|want)\s+(you|u)\s+(were|was)\s+(here|with\s+me|in\s+my\s+bed))\b", 0.50),
    (r"\b(i['']?m\s+(all\s+)?(alone|in\s+bed|laying|laying\s+down))\b", 0.25),
    (r"\b(what\s+(would|do)\s+(you|u)\s+(do|wanna\s+do)\s+(to\s+me|if\s+you\s+were\s+here))\b", 0.55),
    (r"\b(what\s+(would|do)\s+(you|u)\s+(do|wanna\s+do)\s+(to\s+me))\b", 0.55),
    (r"\b(send\s+me\s+(a\s+)?(nude|naked|sexy|dirty|hot)\s+(pic|photo|picture|selfie))\b", 0.60),
    (r"\b(i\s+(wanna|want\s+to)\s+(see|watch)\s+(you|u)\s+(cum|orgasm|finish))\b", 0.75),
    (r"\b(make\s+me\s+(cum|moan|scream|beg|wet|drip))\b", 0.72),
]

# ===== METHOD 3: CONTEXT ESCALATION =====

# How many horny keywords found in recent messages
HORNY_CONTEXT_WINDOW = 5

# ===== METHOD 4: LENGTH/TONE =====
# Short, direct messages with explicit words = more horny
# Long, descriptive horny messages = very horny

# ===== METHOD 5: REPLY-TO-HORNY-BAIT =====
# If we recently sent a horny escalation message and user engaged positively


def detect_keyword(text: str) -> tuple[float, list[str]]:
    """Method 1: Keyword scoring. Returns (confidence, triggers)."""
    if not config.HORNY_DETECT_KEYWORD:
        return 0.0, []

    t = clean_text(text)
    score = 0.0
    triggers = []
    matches = 0

    for keyword, weight in HORNY_KEYWORDS.items():
        if keyword in t:
            score = max(score, weight)
            triggers.append(keyword)
            matches += 1

    # Multiple matches increase confidence
    if matches >= 5:
        score = min(1.0, score + 0.10)
    elif matches >= 3:
        score = min(1.0, score + 0.05)

    return score, triggers


def detect_phrase(text: str) -> tuple[float, list[str]]:
    """Method 2: Phrase pattern matching. Returns (confidence, triggers)."""
    if not config.HORNY_DETECT_PHRASE:
        return 0.0, []

    t = text.lower().strip()
    score = 0.0
    triggers = []

    for pattern, weight in HORNY_PHRASES:
        if re.search(pattern, t):
            if weight > score:
                score = weight
            triggers.append(pattern[:50])

    return score, triggers


def detect_context(history: list[str]) -> float:
    """Method 3: Contextual escalation. Checks horny keyword density in recent messages."""
    if not config.HORNY_DETECT_CONTEXT or not history:
        return 0.0

    recent = history[-HORNY_CONTEXT_WINDOW:]
    horny_count = 0

    for msg in recent:
        t = clean_text(msg)
        for keyword in HORNY_KEYWORDS:
            if keyword in t:
                horny_count += 1
                break

    density = horny_count / max(len(recent), 1)

    if density >= 0.60:
        return 0.70
    elif density >= 0.40:
        return 0.50
    elif density >= 0.20:
        return 0.30
    return 0.0


def detect_tone(text: str) -> float:
    """Method 4: Message tone/length analysis."""
    t = text.strip()
    if not t:
        return 0.0

    score = 0.0

    # Longer horny messages = more invested
    if len(t) > 80:
        score += 0.10
    if len(t) > 150:
        score += 0.05

    # All caps = excited
    caps_ratio = sum(1 for c in t if c.isupper()) / max(len(t), 1)
    if caps_ratio > 0.30:
        score += 0.05

    # Multiple exclamation marks
    if t.count("!") >= 2:
        score += 0.05

    # Question about sexual stuff
    if "?" in t and any(kw in t.lower() for kw in ["horny", "sexy", "hot", "nude", "fuck", "like"]):
        score += 0.08

    return min(1.0, score)


def detect(text: str, history: list[str] | None = None) -> dict:
    """
    Main horny detection. Uses all 5 methods and returns comprehensive result.

    Returns:
        {
            "is_horny": bool,
            "confidence": float (0.0 - 1.0),
            "level": "none" | "low" | "medium" | "high" | "very_high",
            "triggers": list[str],
            "methods_used": list[str],
            "should_drop_link": bool,
        }
    """
    t = text.strip()
    if not t:
        return _result(False, 0.0, [], [])

    methods_used = []
    scores = []

    # Method 1: Keywords
    kw_score, kw_triggers = detect_keyword(t)
    if kw_score > 0:
        scores.append(kw_score)
        methods_used.append("keyword")

    # Method 2: Phrases
    ph_score, ph_triggers = detect_phrase(t)
    if ph_score > 0:
        scores.append(ph_score)
        methods_used.append("phrase")
        kw_triggers.extend(ph_triggers)

    # Method 3: Context
    ctx_score = detect_context(history or [])
    if ctx_score > 0:
        scores.append(ctx_score)
        methods_used.append("context")

    # Method 4: Tone
    tone_score = detect_tone(t)
    if tone_score > 0:
        scores.append(tone_score * 0.5)  # Tone is supplementary
        methods_used.append("tone")

    if not scores:
        return _result(False, 0.0, [], [])

    # Weighted average with keyword score having most weight
    final_confidence = max(scores)  # Use max for immediate detection

    level = _confidence_level(final_confidence)
    should_drop = (final_confidence >= config.LINK_DROP_HORNY_CONFIDENCE)

    return _result(
        final_confidence >= config.HORNY_DETECT_MIN_CONFIDENCE,
        round(final_confidence, 2),
        kw_triggers[:10],
        methods_used,
        level,
        should_drop,
    )


def _result(is_horny, confidence, triggers, methods, level=None, should_drop=False):
    if level is None:
        level = _confidence_level(confidence)
    return {
        "is_horny": is_horny,
        "confidence": confidence,
        "level": level,
        "triggers": triggers,
        "methods_used": methods,
        "should_drop_link": should_drop,
    }


def _confidence_level(conf: float) -> str:
    if conf >= 0.85:
        return "very_high"
    if conf >= 0.70:
        return "high"
    if conf >= 0.50:
        return "medium"
    if conf >= 0.30:
        return "low"
    return "none"


def is_horny(text: str, history: list[str] | None = None) -> bool:
    """Quick check: is the user horny?"""
    return detect(text, history)["is_horny"]


def should_drop_link(text: str, history: list[str] | None = None) -> bool:
    """Quick check: should we drop the link now?"""
    return detect(text, history)["should_drop_link"]
