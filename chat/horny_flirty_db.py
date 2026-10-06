"""Horny & Flirty Keywords + Replies Database (SEPARATE FILE).

This is a standalone database for horny/flirty matching.
Keywords are grouped by category, replies are SHORT SMS style.

Usage:
    from chat.horny_flirty_db import match_horny, match_flirty, get_reply
"""

from __future__ import annotations
import re
import random
from typing import Dict, List, Optional, Tuple


# ===========================================================================
# HORNY KEYWORDS - grouped by intensity
# ===========================================================================

HORNY_KEYWORDS: Dict[str, List[str]] = {
    # Level 1: Soft horny (subtle) — everyday status words ("bored", "lonely",
    # "cant sleep", "u free") are NOT horny: people say them constantly and the
    # soft deflect would hijack innocent answers to the bot's own questions.
    "soft": [
        "touch", "wet", "ache", "tingly",
        "wish u were here", "miss ur touch",
        "need attention", "come over", "come here",
        "thinking about u",
        "what r u doing", "whats ur snap",
    ],

    # Level 2: Medium horny (direct) — compliments ("cute", "ur cute",
    # "u look good", "stunning", "kiss u") moved to FLIRTY_KEYWORDS so they
    # route to PATH B (flirty_reply.txt) not PATH A (horny.txt).
    "medium": [
        "hot", "sexy",
        "ur hot", "ur sexy",
        "like u", "like ur pic", "send pic",
        "show me", "what do u look like",
        "dm me", "can we chat", "private",
        "ur body", "ur lips", "ur figure",
        "id date u", "id smash", "would u ever",
        "cuddle", "hug u",
        "fine asf", "fine af",
        "drop dead gorgeous",
    ],

    # Level 3: Strong horny (explicit)
    "strong": [
        "horny", "feeling horny", "so horny",
        "nude", "nudes", "send nude",
        "dick", "cock", "pussy", "sex",
        "masturbate", "orgasm", "cum",
        "what are u wearing", "what u wearing",
        "strip", "naked", "body",
        "rate me", "rate my body",
        "fuck", "fucking", "fucked",
        "ride", "suck", "blowjob",
        "bedroom", "under the sheet",
        "nsfw", "18+", "onlyfans",
        "finger", "lick", "tongue",
        "hard", "turned on", "aroused",
    ],
}

# Horn level 1 reply - soft, playful
HORNY_SOFT_REPLIES: List[str] = [
    "haha same tbh",
    "me too hehe",
    "what u up to? 👀",
    "bored too lol",
    "hmu if ur bored",
    "whats up with u?",
    "same ngl",
    "haha why? 😏",
    "lol me rn",
    "nothing much u?",
    "yea same, what u doing",
    "bored af tbh",
    "omg same lol",
    "haha me rn too",
    "whats ur snap tho",
    "u free? 👀",
    "same, been bored all day",
    "lol why what are u doing",
    "tell me more",
    "hmm interesting",
    "haha fr fr",
    "same ngl 😏",
    "yea pretty much",
    "lowkey same",
    "lol same here",
]

# Horn level 2 reply - flirty, teasing
HORNY_MEDIUM_REPLIES: List[str] = [
    "aww thanks lol",
    "stop it hehe",
    "thats sweet 🥰",
    "haha really?",
    "thanks cutie",
    "u think so? 😏",
    "haha stop",
    "thats cute",
    "ur one to talk lol",
    "haha ok calm down",
    "thats flirty 👀",
    "aww stahp it",
    "u think im hot? 👀",
    "hehe thanks",
    "ok that was smooth",
    "haha u flirting with me?",
    "really tho? 🥺",
    "thats the sweetest",
    "ok wow thanks",
    "hehe ur kinda cute too",
    "lol stoppp",
    "ur making me blush",
    "ok fine ur cute too",
    "haha wow ok",
    "thats nice of u 🥰",
]

# Horn level 3 reply - deflect, redirect to snap
HORNY_STRONG_REPLIES: List[str] = [
    "haha calm down lol",
    "nah thats not me",
    "lets just chat normally",
    "haha nope",
    "lol maybe later",
    "thats too much lol",
    "nah not rn",
    "haha chill",
    "omg relax",
    "lol whats wrong with u",
    "nah im good",
    "haha wow ok then",
    "thats wild",
    "lol nope nope nope",
    "calm down bro",
    "ok thats enough lol",
    "haha stop being weird",
    "nah not happening",
    "lol what",
    "ur too much",
    "haha wtf",
    "ok bye then lol",
    "lol chill",
    "nah im not into that",
    "too early for that lol",
]

# After horny reply → snap hint
HORNY_SNAP_HINT_REPLIES: List[str] = [
    "im more on snap tho",
    "lets talk on snap",
    "add me on snap 👻",
    "snap is easier",
    "my snap is better",
    "im more active on snap",
    "lets continue there",
    "snap me!",
    "i dont really text here tbh",
    "its better on snap trust me",
    "i send better stuff on snap 😏",
    "snap is where its at",
    "i only use snap tbh",
    "come find me on snap",
    "hmu on snap its easier",
    "i post more on snap",
    "snap is my main app",
    "we should move to snap",
    "i check snap more",
    "way better on snap",
    "snap me lets go",
    "im always on snap",
    "texting here is boring",
    "snap is more fun",
    "i prefer snap honestly",
]


# ===========================================================================
# FLIRTY KEYWORDS - grouped by type
# ===========================================================================

FLIRTY_KEYWORDS: Dict[str, List[str]] = {
    # Compliments received
    "compliment": [
        "cute", "hot", "sexy", "beautiful", "gorgeous", "pretty",
        "ur cute", "ur hot", "ur sexy", "ur beautiful",
        "ur pretty", "ur gorgeous",
        "nice pic", "nice photo", "love ur pic",
        "ur smile", "ur eyes", "ur hair",
        "look good", "u look good", "you look good",
        "adorable", "stunning", "breathtaking",
        "ur face", "ur vibe", "ur energy",
    ],

    # Affection words
    "affection": [
        "love", "like", "miss", "want",
        "babe", "baby", "dear", "darling", "sweetheart",
        "hun", "honey", "love u", "like u", "miss u",
        "want u", "need u", "care about u",
        "ur everything", "cant stop thinking",
        "ur the best", "so grateful",
    ],

    # Date/flirty intent
    "date": [
        "date", "meet", "hangout", "hang out",
        "coffee", "dinner", "lunch", "movie",
        "go out", "take u out", "wanna meet",
        "when can we meet", "where do u live",
        "come over", "lets go somewhere",
        "weekend", "tonight", "tomorrow",
        "vacation", "trip", "adventure",
    ],

    # Teasing/playful
    "tease": [
        "flirt", "flirty", "tease", "teasing",
        "ur flirting", "ur so flirty",
        "haha stop", "ur bad", "bad girl",
        "naughty", "wild", "crazy",
        "sassy", "cheeky", "bold",
        "dare", "challenge", "bet",
        "u wish", "in ur dreams",
    ],

    # Single/flirty intent (matches the flow-map keyword list)
    "single": [
        "u single", "are u single", "r u single", "single?",
        "wanna fun", "tryna fun", "tryna have fun", "wanna have fun",
        "kiss", "kiss me", "wanna kiss",
        "u single?", "are u single?",
    ],
}

# Flirty reply for compliments
FLIRTY_COMPLIMENT_REPLIES: List[str] = [
    "aww thanks lol",
    "stop it hehe",
    "thats sweet 🥰",
    "haha really? thanks",
    "thanks cutie",
    "u think so? 😏",
    "thats cute of u",
    "aww stahp",
    "ok wow thanks",
    "hehe ur sweet",
    "thats the nicest thing",
    "u made my day 🥺",
    "ok ur making me blush",
    "haha wow ok then",
    "thanks that means alot",
    "stop being so sweet",
    "aww ur too kind",
    "hehe ok thanks",
    "thats really sweet of u",
    "omg stop 🥰",
    "u think im cute? 👀",
    "ok fine ur cute too",
    "haha thanks I guess",
    "wow ok thanks",
    "thats so nice of u",
]

# Flirty reply for affection
FLIRTY_AFFECTION_REPLIES: List[str] = [
    "haha same tbh",
    "me too hehe",
    "thats sweet",
    "aww 🥰",
    "haha aww",
    "thats cute",
    "likewise lol",
    "back at u",
    "stop it ur gonna make me cry",
    "ok thats the cutest",
    "hehe same here",
    "aww ur the best",
    "thats so sweet omg",
    "i feel the same",
    "haha ok fine me too",
    "u mean alot to me too",
    "thats really nice",
    "ok wow",
    "hehe thanks babe",
    "ur literally the best",
    "aww stoppp",
    "ok that made my heart melt",
    "haha same ngl",
    "thats so sweet omg 🥺",
    "i like u too",
]

# Flirty reply for date intent
FLIRTY_DATE_REPLIES: List[str] = [
    "haha maybe sometime",
    "we'll see lol",
    "haha where?",
    "maybe hehe",
    "that sounds fun",
    "down for that",
    "we should! 👀",
    "when tho?",
    "haha ok interesting",
    "ur asking me out? 👀",
    "haha wow smooth",
    "maybe if ur lucky",
    "ill think about it lol",
    "that sounds like a plan",
    "ok fine lets do it",
    "haha ur bold",
    "down! when?",
    "ok that actually sounds fun",
    "haha wow ok",
    "sure why not lol",
    "ur so direct lol",
    "ok lets go!",
    "haha alright then",
    "that could be fun",
    "maybe one day 👀",
]

# Flirty reply for teasing
FLIRTY_TEASE_REPLIES: List[str] = [
    "haha u started it",
    "no u 😏",
    "haha stop",
    "ur so bad lol",
    "pot calling kettle black",
    "haha omg",
    "lol whatever",
    "u think so?",
    "haha wow ok",
    "ur the one being flirty",
    "lol says who",
    "haha rly now?",
    "ok fine u win",
    "ur impossible lol",
    "haha good one",
    "lol ok ok",
    "u got me there",
    "haha touché",
    "fine whatever lol",
    "ur so annoying sometimes",
    "haha stahp",
    "ok that was good",
    "lol ur the worst",
    "haha im done with u",
    "ok fine no u",
]


# ===========================================================================
# PUBLIC API
# ===========================================================================

def match_horny(user_message: str) -> Optional[str]:
    """Match horny keywords and return level.

    Returns: "soft", "medium", "strong", or None
    Uses word-boundary matching to avoid false positives (e.g. "shot" != "hot").
    """
    msg = user_message.lower().strip()

    # Check strong first (highest priority)
    for kw in HORNY_KEYWORDS["strong"]:
        if re.search(r'\b' + re.escape(kw) + r'\b', msg):
            return "strong"

    # Check medium
    for kw in HORNY_KEYWORDS["medium"]:
        if re.search(r'\b' + re.escape(kw) + r'\b', msg):
            return "medium"

    # Check soft
    for kw in HORNY_KEYWORDS["soft"]:
        if re.search(r'\b' + re.escape(kw) + r'\b', msg):
            return "soft"

    return None


def match_flirty(user_message: str) -> Optional[str]:
    """Match flirty keywords and return category.

    Returns: "compliment", "affection", "date", "tease", or None
    Uses word-boundary matching to avoid false positives.
    """
    msg = user_message.lower().strip()

    for category, keywords in FLIRTY_KEYWORDS.items():
        for kw in keywords:
            if re.search(r'\b' + re.escape(kw) + r'\b', msg):
                return category

    return None


def get_reply(level_or_category: str, is_horny: bool = True) -> str:
    """Get random reply based on level/category.

    Args:
        level_or_category: horny level or flirty category
        is_horny: True for horny replies, False for flirty
    """
    if is_horny:
        if level_or_category == "strong":
            return random.choice(HORNY_STRONG_REPLIES)
        elif level_or_category == "medium":
            return random.choice(HORNY_MEDIUM_REPLIES)
        else:
            return random.choice(HORNY_SOFT_REPLIES)
    else:
        if level_or_category == "compliment":
            return random.choice(FLIRTY_COMPLIMENT_REPLIES)
        elif level_or_category == "affection":
            return random.choice(FLIRTY_AFFECTION_REPLIES)
        elif level_or_category == "date":
            return random.choice(FLIRTY_DATE_REPLIES)
        elif level_or_category == "tease":
            return random.choice(FLIRTY_TEASE_REPLIES)
        else:
            return random.choice(FLIRTY_COMPLIMENT_REPLIES)


# ===========================================================================
# STATISTICS
# ===========================================================================

def stats() -> Dict[str, int]:
    """Return keyword count stats."""
    horny_count = sum(len(v) for v in HORNY_KEYWORDS.values())
    flirty_count = sum(len(v) for v in FLIRTY_KEYWORDS.values())
    return {
        "horny_keywords": horny_count,
        "flirty_keywords": flirty_count,
        "horny_replies_soft": len(HORNY_SOFT_REPLIES),
        "horny_replies_medium": len(HORNY_MEDIUM_REPLIES),
        "horny_replies_strong": len(HORNY_STRONG_REPLIES),
        "flirty_replies": len(FLIRTY_COMPLIMENT_REPLIES) + len(FLIRTY_AFFECTION_REPLIES) + len(FLIRTY_DATE_REPLIES) + len(FLIRTY_TEASE_REPLIES),
    }
