"""Comprehensive Chat Database Loader.

Loads keyword->reply pairs from external .txt files.
Format: "keyword ||| reply" (pipe-separated).

Source files from: ev b/local_db/
"""

from __future__ import annotations
import os
import random
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------

_DB_DIR = os.path.join("C:", os.sep, "Users", "papi", "Desktop", "ev b", "local_db")

_DB_FILES = {
    "flirty":       "04_flirty_safe.txt",
    "flirty_extra": "04b_flirty_extra.txt",
    "normal_chat":  "06_normal_chat.txt",
    "engagement":   "07_engagement_hooks.txt",
    "curiosity":    "08_curiosity_triggers.txt",
    "greetings":    "01_greetings.txt",
    "smalltalk":    "03_smalltalk.txt",
    "identity":     "02_identity_profile.txt",
    "objections":   "05_links_objections.txt",
    "multilang":    "09_multilang.txt",
    "adult_curated":"70_adult_consensual_curated.txt",
}

_MEGA_FILE = "10_million_pairs.txt"
_MEGA_SAMPLE_SIZE = 5000

# ---------------------------------------------------------------------------
# Internal storage
# ---------------------------------------------------------------------------

_DB: Dict[str, Dict[str, List[str]]] = {}
_LOADED = False


def _load_file(filename: str) -> Dict[str, List[str]]:
    """Load a single file and return keyword→[replies] dict."""
    filepath = os.path.join(_DB_DIR, filename)
    result: Dict[str, List[str]] = {}
    
    if not os.path.exists(filepath):
        return result
    
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|||" not in line:
                continue
            parts = line.split("|||")
            if len(parts) < 2:
                continue
            keyword = parts[0].strip().lower()
            reply = parts[1].strip()
            if keyword and reply:
                if keyword not in result:
                    result[keyword] = []
                result[keyword].append(reply)
    
    return result


def _load_all():
    """Load all database files."""
    global _DB, _LOADED
    if _LOADED:
        return
    
    for category, filename in _DB_FILES.items():
        _DB[category] = _load_file(filename)
    
    _load_mega_sample()
    
    _LOADED = True


def _load_mega_sample():
    """Randomly sample N entries from the 10M-pair file (too large to load fully)."""
    filepath = os.path.join(_DB_DIR, _MEGA_FILE)
    if not os.path.exists(filepath):
        return
    
    import random as _rand
    sampled: Dict[str, List[str]] = {}
    count = 0
    
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or "|||" not in line:
                continue
            parts = line.split("|||")
            if len(parts) < 2:
                continue
            keyword = parts[0].strip().lower()
            reply = parts[1].strip()
            if not keyword or not reply:
                continue
            count += 1
            if len(sampled) < _MEGA_SAMPLE_SIZE:
                if keyword not in sampled:
                    sampled[keyword] = []
                sampled[keyword].append(reply)
            else:
                idx = _rand.randint(0, count - 1)
                if idx < _MEGA_SAMPLE_SIZE:
                    k = _rand.choice(list(sampled.keys()))
                    if sampled[k]:
                        sampled[k].pop()
                        if not sampled[k]:
                            del sampled[k]
                    if keyword not in sampled:
                        sampled[keyword] = []
                    sampled[keyword].append(reply)
    
    _DB["mega_pairs"] = sampled


def match_keyword(user_message: str, category: Optional[str] = None) -> Optional[Tuple[str, str, str]]:
    """Match user message against database.
    
    Args:
        user_message: user's message
        category: specific category to search, or None for all
    
    Returns:
        (category, keyword, reply) or None
    """
    _load_all()
    msg = user_message.lower().strip()
    
    categories = [category] if category else _DB.keys()
    
    for cat in categories:
        if cat not in _DB:
            continue
        for keyword, replies in _DB[cat].items():
            if keyword in msg or msg in keyword:
                return (cat, keyword, random.choice(replies))
    
    return None


def get_reply(user_message: str) -> Optional[str]:
    """Get a reply for user message from database.
    
    Returns reply string or None if no match.
    """
    result = match_keyword(user_message)
    if result:
        return result[2]
    return None


def get_random_reply(category: str) -> str:
    """Get a random reply from a category."""
    _load_all()
    if category in _DB and _DB[category]:
        all_replies = []
        for replies in _DB[category].values():
            all_replies.extend(replies)
        if all_replies:
            return random.choice(all_replies)
    return ""


def get_engagement_hook() -> str:
    """Get a random engagement hook reply."""
    return get_random_reply("engagement")


def get_curiosity_trigger() -> str:
    """Get a random curiosity trigger reply."""
    return get_random_reply("curiosity")


def get_flirty_reply() -> str:
    """Get a random flirty reply."""
    reply = get_random_reply("flirty")
    if not reply:
        reply = get_random_reply("flirty_extra")
    return reply


def get_normal_reply() -> str:
    """Get a normal chat reply."""
    return get_random_reply("normal_chat")


def get_any_reply() -> str:
    """Get a random reply from any category (for fallback)."""
    _load_all()
    all_replies = []
    for data in _DB.values():
        for replies in data.values():
            all_replies.extend(replies)
    return random.choice(all_replies) if all_replies else "lol"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats() -> Dict[str, int]:
    """Return database statistics."""
    _load_all()
    result = {}
    total_keywords = 0
    total_replies = 0
    for cat, data in _DB.items():
        kw_count = len(data)
        reply_count = sum(len(v) for v in data.values())
        result[f"{cat}_keywords"] = kw_count
        result[f"{cat}_replies"] = reply_count
        total_keywords += kw_count
        total_replies += reply_count
    result["total_keywords"] = total_keywords
    result["total_replies"] = total_replies
    return result
