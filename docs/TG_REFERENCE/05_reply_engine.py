"""
EVA BOT v7.1 â€” Local-only layered reply engine.
All replies come from local files, regex/category templates, and local fallbacks.
No API client is imported or called.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
import re
from pathlib import Path

import config
from ui.identity_variants import pick
from utils import clean_text, log

BASE = Path(__file__).resolve().parent.parent

# ===== Identity / profile =====
IDENTITY = {
    "name": ["eva", "im eva", "eva here", "my names eva", "just eva"],
    "age": ["21", "im 21", "me? 21", "twenty one", "21, you?"],
    "location": ["usa", "from the usa", "im from the us", "the usa", "usa, you?"],
    "single": ["yeah, im single", "single rn", "yep, im single", "im single", "yeah, no boyfriend"],
    "job": ["i keep that private"],
    "real": ["some messages are software generated", "i use reply software sometimes"],
}

ID_PATTERNS = [
    (r"\b(what is|whats|what s|ur|your)\s*name\b|\bwho\s*are\s*(u|you)\b|\bname\?*$", "name"),
    (r"\b(how old|age|ur age|your age|how young)\b", "age"),
    (r"\bwhere\s*(are\s*)?(u|you)\s*from\b|\bwhere\s*do\s*(u|you)\s*live\b", "location"),
    (r"\b(are\s*(u|you)\s*single|single\?|u\s*single|boyfriend|bf|relationship|taken|married)\b", "single"),
    (r"\b(what do (u|you) do|ur job|your job|work|study|college|school)\b", "job"),
    (r"\b(are\s*(u|you)\s*real|fake|catfish|bot|ai)\b", "real"),
]

# Sexual/adult terms. This is routing only; it does not generate text.
ADULT_KEYWORDS = re.compile(
    r"\b(horny|horney|sexy|sex|dirty|naughty|kinky|fantasy|masturbat|jerk|"
    r"nude|nudes|naked|explicit|adult|18\+|hookup|fuck|fucking|suck|sucking|"
    r"lick|licking|blowjob|bj|dick|cock|pussy|boob|boobs|ass|tits|penis|"
    r"vagina|cum|cumming|orgasm|moan|spank|bdsm|wet|hard|send nudes|"
    r"send me a pic|show me ur|what are u wearing)\b",
    re.I,
)

FLIRTY_KEYWORDS = re.compile(
    r"\b(cute|beautiful|pretty|gorgeous|sweet|handsome|nice|miss|love|"
    r"like (u|you)|crush|date|single|baby|babe|dear|kiss|lips|smile|"
    r"eyes|voice|vibe|mood|wyd|wuu2|chilling|bored|talk|chat|hru|how are)\b",
    re.I,
)

CATEGORIES = {
    "greeting": [r"\b(hi+|hey+|hello+|heyy+|hlw|hii+|sup|yo|wasup|howdy)\b"],
    "good_morning": [r"\b(good\s*morning|gm|morning|gmorning|mornin)\b"],
    "good_night": [r"\b(good\s*night|gn|night|sweet\s*dreams|gnight)\b"],
    "how_are_you": [r"\b(how\s*are\s*(u|you)|hru|how r u|you good|u good)\b"],
    "wyd": [r"\b(wyd|what\s*(are|r)\s*(u|you)\s*doing|what u doing|what u up to)\b"],
    "thanks": [r"\b(thanks|thank u|thank you|ty|appreciate)\b"],
    "bye": [r"\b(bye|goodbye|gtg|talk later|see ya|cya)\b"],
}

CAT_REPLIES = {
    "greeting": ["heyy how are u?", "hey there wyd?", "heyy nice to meet u", "hi lol hows ur day?"],
    "good_morning": ["good morning lol slept good?", "gm, how did u sleep?", "morning, what u doing today?"],
    "good_night": ["good night, sleep good", "gn sweet dreams", "night lol text me later"],
    "how_are_you": ["im good, just chilling. u?", "im alright lol what about u?", "good thanks, what u up to?"],
    "wyd": ["just chilling rn, u?", "texting u lol, wyd?", "not much, what about u?"],
    "thanks": ["ofc lol", "anytime", "youre welcome"],
    "bye": ["bye lol text me later", "okay talk later", "see u later"],
}

ADULT_FALLBACKS = [
    "slow down lol tell me about u first", "maybe, but i wanna know u first",
    "u got my attention lol", "tell me more but dont rush me", "lol you are bold",
]

NORMAL_FALLBACKS = [
    "haha really? tell me more", "wyd rn?", "lol what made u say that?", "mm okay what are u doing?",
    "tell me something about u", "interesting lol keep going", "u seem cool, tell me more",
    "so what else is going on?", "thats cute ngl", "lol i get u",
]


def is_adult_message(text: str) -> bool:
    return bool(ADULT_KEYWORDS.search(text or ""))


def is_flirty_message(text: str) -> bool:
    return bool(FLIRTY_KEYWORDS.search(text or ""))


def is_identity_question(text: str) -> bool:
    return any(re.search(pat, text or "", re.I) for pat, _ in ID_PATTERNS)


def _category_reply(t: str) -> tuple[str | None, str]:
    for cat, patterns in CATEGORIES.items():
        for pat in patterns:
            if re.search(pat, t, re.I):
                return random.choice(CAT_REPLIES[cat]), f"cat:{cat}"
    return None, ""



async def generate_adult_local_reply(text, user_profile=None, horny_score=0.0):
    """Adult/intimate route. No age gate in v10."""
    from db.fast_matcher import fast_match
    reply, tier, ms = fast_match(text, allow_adult=True, scope="adult")
    if reply:
        log(f"ADULT_LOCAL_ONLY tier={tier} ms={ms:.1f}")
        return reply, f"adult_local:{tier}"
    return random.choice(ADULT_FALLBACKS), "adult_local:fallback"

async def generate_reply(text, msg_count=0, horny_score=0.0, user_profile=None, ai_config=None):
    """Generate a reply using local-only routing."""
    t = clean_text(text)
    if not t:
        return random.choice(NORMAL_FALLBACKS), "fallback_empty"

    # Layer 1: identity questions are answered from local templates.
    for pat, key in ID_PATTERNS:
        if re.search(pat, t, re.I):
            return pick({"real": "authenticity"}.get(key, key)), f"identity:{key}"

    adult_intent = is_adult_message(text) or horny_score >= 0.55
    allow_adult = True  # No age gate in v10

    # Layer 2: hard age gate before any adult DB search.
    # If age was already asked once, do not ask again; hold with a safe neutral reply.
    # Adult DB — no age gate in v10
    if adult_intent and allow_adult:
        from db.fast_matcher import fast_match
        reply, tier, ms = fast_match(text, allow_adult=True, scope="adult")
        if reply:
            log(f"ADULT_LOCAL tier={tier} ms={ms:.1f}")
            return reply, f"adult_local:{tier}"
        return random.choice(ADULT_FALLBACKS), "adult_fallback"

    # Layer 4: exact/fuzzy safe DB for normal and flirty chat.
    from db.fast_matcher import fast_match
    reply, tier, ms = fast_match(text, allow_adult=False, scope="safe")
    if reply:
        log(f"SAFE_LOCAL tier={tier} ms={ms:.1f}")
        return reply, f"safe_local:{tier}"

    # Layer 5: regex category templates.
    reply, source = _category_reply(t)
    if reply:
        return reply, source

    # Layer 6: first-message/general fallback.
    if msg_count <= 2:
        return random.choice(["heyy how are u?", "hey lol whats up?", "hi, nice to meet u"]), "fallback_intro"
    if is_flirty_message(text):
        return random.choice(["lol youre sweet", "maybe i like that", "u got a cute vibe", "haha dont make me blush"]), "fallback_flirty"
    if horny_score > 0.3 and allow_adult:
        return random.choice(ADULT_FALLBACKS), "fallback_warm"
    if horny_score > 0.3:
        return hold_until_age_reply(), "fallback_warm_safe"
    return random.choice(NORMAL_FALLBACKS), "fallback_normal"


async def generate_pic_caption(ai_config=None, stage="flirty"):
    captions = {
        "flirty": ["okay fair, here u go", "since u sent one, my turn lol", "cute, now my turn"],
        "adult": ["only because u got me blushing", "okay just one, dont rush me", "u got me in a playful mood"],
        "promo": ["one last one for u", "here u go, check the rest there", "last one for now lol"],
    }
    return random.choice(captions.get(stage, ["here u go"]))


IDENTITY_REPLIES = IDENTITY
get_reply = generate_reply

