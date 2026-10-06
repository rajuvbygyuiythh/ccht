"""Analyze user typing style so EVA can mirror it."""

from __future__ import annotations

import random
import re
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Emoji detection
# ---------------------------------------------------------------------------

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)


def _count_emojis(text: str) -> int:
    return len(_EMOJI_RE.findall(text))


# ---------------------------------------------------------------------------
# Slang / abbreviation detection
# ---------------------------------------------------------------------------

_SLANG_PATTERNS = {
    "u": re.compile(r"\bu\b"),
    "r": re.compile(r"\br\b"),
    "ya": re.compile(r"\bya\b"),
    "gonna": re.compile(r"\bgonna\b"),
    "wanna": re.compile(r"\bwanna\b"),
    "gotta": re.compile(r"\bgotta\b"),
    "lol": re.compile(r"\blol\b", re.IGNORECASE),
    "haha": re.compile(r"\bhaha\b", re.IGNORECASE),
    "ngl": re.compile(r"\bngl\b", re.IGNORECASE),
    "fr": re.compile(r"\bfr\b", re.IGNORECASE),
    "rn": re.compile(r"\brn\b", re.IGNORECASE),
    "idk": re.compile(r"\bidk\b", re.IGNORECASE),
    "hmu": re.compile(r"\bhmu\b", re.IGNORECASE),
    "smh": re.compile(r"\bsmh\b", re.IGNORECASE),
    "bruh": re.compile(r"\bbruh\b", re.IGNORECASE),
}


def _count_periods(text: str) -> int:
    return text.count(".")


def _count_exclamations(text: str) -> int:
    return text.count("!")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class StyleProfile:
    """Snapshot of a user's detected typing style."""

    __slots__ = (
        "avg_word_count",
        "emoji_density",
        "slang_score",
        "uses_periods",
        "uses_exclamations",
        "avg_message_length",
        "slang_set",
    )

    def __init__(self) -> None:
        self.avg_word_count: float = 5.0
        self.emoji_density: float = 0.0
        self.slang_score: float = 0.0
        self.uses_periods: bool = False
        self.uses_exclamations: bool = False
        self.avg_message_length: float = 30.0
        self.slang_set: set = set()


def analyze_style(messages: List[str]) -> StyleProfile:
    """Build a StyleProfile from a list of user messages."""
    profile = StyleProfile()
    if not messages:
        return profile

    word_counts: List[int] = []
    emoji_counts: List[int] = []
    total_slang_hits = 0
    slang_hits: set = set()
    period_count = 0
    excl_count = 0
    total_len = 0

    for msg in messages:
        words = msg.split()
        word_counts.append(len(words))
        emoji_counts.append(_count_emojis(msg))
        total_len += len(msg)
        period_count += _count_periods(msg)
        excl_count += _count_exclamations(msg)
        for slug, pat in _SLANG_PATTERNS.items():
            if pat.search(msg):
                total_slang_hits += 1
                slang_hits.add(slug)

    n = len(messages)
    profile.avg_word_count = sum(word_counts) / n
    profile.emoji_density = sum(emoji_counts) / max(n, 1)
    profile.slang_score = total_slang_hits / max(n, 1)
    profile.uses_periods = period_count >= n * 0.3
    profile.uses_exclamations = excl_count >= n * 0.2
    profile.avg_message_length = total_len / n
    profile.slang_set = slang_hits

    return profile


def mirror_reply(
    reply: str,
    style: StyleProfile,
    *,
    user_message: str = "",
) -> str:
    """Adjust *reply* to mirror the user's detected style.

    Mirrors:
      - word count (trimmed / expanded to match)
      - slang tokens (u/r/ya instead of you/are/you)
      - emoji frequency
      - punctuation preference
    """
    result = reply

    # --- Slang mirroring ---------------------------------------------------
    if style.slang_score > 0.5:
        result = result.replace("you are ", "u r ")
        result = result.replace("you're ", "u r ")
        result = result.replace("you ", "u ")
        result = result.replace(" are ", " r ")
        result = result.replace(" your ", " ur ")
        result = result.replace(" do you ", " do u ")
        result = result.replace(" are you ", " r u ")

    # --- Punctuation -------------------------------------------------------
    if not style.uses_periods:
        # Remove trailing periods
        result = result.rstrip(".")
    if not style.uses_exclamations:
        result = result.rstrip("!")

    # --- Emoji mirroring ---------------------------------------------------
    user_emoji_count = _count_emojis(user_message) if user_message else 0
    if user_emoji_count > 0 and style.emoji_density < 0.3:
        # User used an emoji but they're generally low-emoji; allow 1 max
        pass  # keep reply as-is (likely already has 0-1)
    elif user_emoji_count == 0 and style.emoji_density < 0.1:
        # Strip any emoji from reply
        result = _EMOJI_RE.sub("", result)

    # --- Word-count trimming ------------------------------------------------
    # Only trim if reply is noticeably longer than user's typical messages.
    # Never trim below 8 words — keeps replies coherent.
    MIN_WORDS = 8
    target_words = max(MIN_WORDS, int(style.avg_word_count) + 2)
    current_words = result.split()
    if len(current_words) > target_words + 4:
        result = " ".join(current_words[:target_words])

    return result.strip()
