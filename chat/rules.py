"""RuleEngine — 6 Unique Flow Types + Horny Deflection.

6 Flow Types (randomly selected per conversation):
  1. Friendly:  greeting → gender+age → country → friendly → playful → snap
  2. Night:     greeting → gender+age → country → alone_night → horny → snap
  3. Deep:      greeting → gender+age → country → deeper → bonding → snap
  4. Fun:       greeting → gender+age → country → fun_question → tease → snap
  5. Direct:    greeting → gender+age → country → direct_ask → snap
  6. Romance:   greeting → gender+age → country → romantic → flirty → snap

Each flow has unique step 3/4/5 reply sets for variety.

Horny Deflection:
  - User sends horny message → bot deflects once → continues sequence
"""

from __future__ import annotations

import os
import re
import random
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .common_scan import (
    scan_message,
    detect_gender_age,
    detect_user_name,
    detect_age_gender,
    detect_greeting,
    detect_country_question,
    detect_user_facts,
)
from .geo_handler import CountryRotator
from .database.loader import load_pipe_db, load_evoflow_rules, load_promote_extract
from .horny_flirty_db import match_horny, match_flirty
from .persona import answer_user, react_to_statement
from .style_analyzer import mirror_reply, analyze_style, StyleProfile
from .tg_brain import TgBrain

# ---------------------------------------------------------------------------
# Round-robin txt rotators (ask_snap / share_snap / snap ids) with hot-reload and
# persisted index (.idx files in data/). No repeats before a full cycle.
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_OUTPUT_DIR = _DATA_DIR / "output"

def _load_pool_from_file(name: str, fallback: list) -> list:
    """Load a simple pool from data/output/<name>.txt, falling back to *fallback*."""
    path = _OUTPUT_DIR / name
    if not path.is_file():
        return list(fallback)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f if l.strip()]
        return lines if lines else list(fallback)
    except Exception:
        return list(fallback)

_CHAT_DEBUG_LOCK = threading.Lock()
_CHAT_DEBUG_FILE = _DATA_DIR / "logs" / "chat_debug.log"
_CHAT_DEBUG_MAX  = 1_500_000
# Ensure debug log directory exists once at import time
_CHAT_DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)


def _log_esc(s: str) -> str:
    """Make one field safe for a single-line log entry (no raw line breaks)."""
    return (s.replace("\\", "\\\\")
             .replace("\n", "\\n")
             .replace("\r", "\\r")
             .replace("\t", "\\t"))

# 3-STEP SNAP SEQUENCE pools (map): 1. horny.txt (HORNY_REPLY) ->
# 2. ask_snap.txt (ask snap) -> 3. share_snap.txt (reveal). Round-robin with
# hot-reload and persisted index (.idx files in data/). No repeats before a
# full cycle. Legacy data/output/ask_snap_backup.txt + data/share_snap_backup.txt stay in place
# as fallbacks; the loaders fall back to hardcoded pools when a file is gone.
ASKSC_FILE    = _DATA_DIR / "output" / "ask_snap.txt"
ASKSC_LEGACY  = _DATA_DIR / "output" / "ask_snap_backup.txt"

ASKSC_IDX     = _DATA_DIR / ".ask_snap.idx"
SCSHARE_FILE  = _DATA_DIR / "output" / "share_snap.txt"
SCSHARE_LEGACY = _DATA_DIR / "share_snap_backup.txt"
SCSHARE_IDX   = _DATA_DIR / ".share_snap.idx"
SNAPUSER_FILE = _DATA_DIR / "snap_ids.txt"
SNAPUSER_IDX  = _DATA_DIR / ".snap_ids.idx"

ASKSC_FALLBACK = [
    "you got snap?",
    "got sc?",
    "u on snap?",
    "you use snapchat?",
    "are you on sc?",
]
SCSHARE_FALLBACK = [
    "here's my snap: %username%",
    "snap: %username%",
    "add me: %username%",
    "come to sc -> %username%",
]
SNAPUSER_FALLBACK = [
    "eva_chill",
    "eva_21",
    "eva_69",
    "eva_xx",
    "eva_love",
]

# If a share_snap line has no %username%, append the username at the end.
SCSHARE_AUTO_APPEND_USERNAME = False

# Two-step gate: True = tease now, share on the next message; False = share
# immediately after the positive reply.
SHARE_ON_NEXT = False

# Persona (spec: always F, age 20, country via CountryRotator).
BOT_GENDER = "f"
BOT_AGE = 20

# Age-ask lines used by the funnel when the user's age is still unknown and
# their message did not match any input/output category.
_AGE_ASK_POOL = _load_pool_from_file("age_ask.txt", [
    f"f {BOT_AGE} u? age?",
    f"f {BOT_AGE}, age??",
    f"f {BOT_AGE} asl?",
    f"f {BOT_AGE} wbu? age..",
    f"f {BOT_AGE} ur age?",
    f"f {BOT_AGE} u? how old r u?",
])

# Country-ask lines — asked ONCE per conversation (see funnel guards). Every
# line ends in "?" so the answer-frame recognises the user's reply.
_COUNTRY_ASK_POOL = _load_pool_from_file("country_ask.txt", [
    "where r u from?",
    "ur from where?",
    "which country r u from?",
    "so where are u from?",
    "where abouts u from?",
])

# Gender-ask lines — asked ONCE per conversation (GREETING collection order:
# age -> gender -> country, each one at a time).
_GENDER_ASK_POOL = _load_pool_from_file("gender_ask.txt", [
    "u a guy or a girl?",
    "m or f? 👀",
    "guy or girl btw?",
    "u a boy or girl?",
])

# When the user dodges the country question too, reply a light beat and move
# on to flirty -> snap instead of interrogating them again.
_COUNTRY_DODGE_POOL = _load_pool_from_file("country_dodge.txt", [
    "haha okay",
    "fine fine",
    "hmm ok",
    "okay fair",
    "hehe okay",
])

_POSITIVE_WORDS = frozenset({
    "yes", "yep", "yess", "yeah", "yh", "sure", "ok", "okay",
    "okk", "ofc", "def", "down", "yup",
})


class RoundRobinFile:
    """Thread-safe round-robin over lines of a txt file.

    - Hot-reloads when the file's mtime changes (next call picks it up).
    - Persists the current index to ``idx_file`` across restarts.
    - Falls back to ``fallback`` lines when the file is missing/empty.
    """

    def __init__(self, file_path: Path, fallback: list,
                 idx_file: Optional[Path] = None,
                 fallback_file: Optional[Path] = None):
        self.file_path = file_path
        self.fallback_file = fallback_file
        self.fallback = [l.strip() for l in fallback if l.strip()]
        self.idx_file = idx_file
        self._lock = threading.RLock()
        self._mt = 0.0
        self._lines: List[str] = []
        self._i = 0
        self._load_lines(initial=True)
        self._load_index()

    @staticmethod
    def _read(path: Path) -> List[str]:
        lines = []
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    s = raw.strip()
                    if s:
                        lines.append(s)
        except OSError:
            pass
        return lines

    def _load_lines(self, initial=False):
        try:
            mt = self.file_path.stat().st_mtime
        except FileNotFoundError:
            mt = 0.0
        if initial or mt != self._mt:
            self._mt = mt
            lines = self._read(self.file_path)
            if not lines and self.fallback_file is not None:
                lines = self._read(self.fallback_file)
            self._lines = lines if lines else self.fallback
            if self._i >= len(self._lines):
                self._i = 0

    def _load_index(self):
        if not self.idx_file:
            return
        try:
            i = int(self.idx_file.read_text(encoding="utf-8").strip())
            if 0 <= i < max(1, len(self._lines)):
                self._i = i
        except Exception:
            pass

    def _save_index(self):
        if not self.idx_file:
            return
        try:
            self.idx_file.parent.mkdir(parents=True, exist_ok=True)
            self.idx_file.write_text(str(self._i), encoding="utf-8")
        except Exception:
            pass

    def next(self) -> str:
        """Return the current line and advance (wraps around)."""
        with self._lock:
            self._load_lines()
            if not self._lines:
                self._lines = self.fallback
            out = self._lines[self._i]
            self._i = (self._i + 1) % len(self._lines)
            self._save_index()
            return out


_ASKSC_ROT = RoundRobinFile(ASKSC_FILE, ASKSC_FALLBACK, ASKSC_IDX,
                            fallback_file=ASKSC_LEGACY)
_SCSHARE_ROT = RoundRobinFile(SCSHARE_FILE, SCSHARE_FALLBACK, SCSHARE_IDX,
                              fallback_file=SCSHARE_LEGACY)
_SNAP_ROT = RoundRobinFile(SNAPUSER_FILE, SNAPUSER_FALLBACK, SNAPUSER_IDX)


def next_asksc_prompt() -> str:
    """Next 'ask for snap' prompt (round-robin, persisted)."""
    return _ASKSC_ROT.next()


def next_snap_username() -> str:
    """Next snap username pool entry (round-robin, persisted)."""
    return _SNAP_ROT.next()


def next_scshare_line(username: Optional[str] = None) -> str:
    """Next 'share snap' line, with %username% replaced.

    The username comes from the caller or, by default, from data/snap_ids.txt
    (round-robin).  Pass a username to pin a specific ID.
    """
    line = _SCSHARE_ROT.next()
    uname = username or next_snap_username()
    if "%username%" in line.lower():
        line = line.replace("%username%", uname).replace("%USERNAME%", uname)
    elif SCSHARE_AUTO_APPEND_USERNAME:
        if uname not in line:
            line = f"{line} {uname}"
    return line


def _share_snap(state: Dict[str, Any]) -> str:
    """Emit the snap share (or two-step tease when SHARE_ON_NEXT is set)."""
    if SHARE_ON_NEXT:
        state["pending_scshare"] = True
        return "cool, ping me once more and I'll drop it 😉"
    state["pending_scshare"] = False
    state["snap_pivoted"] = True
    return next_scshare_line()


def _is_positive(text: str) -> bool:
    """Cheap positive/yes detector used only for snap confirmation."""
    t = text.casefold().strip()
    words = set(re.split(r"[^a-z]+", t))
    return bool(words & _POSITIVE_WORDS)


_QUESTION_LEAD = re.compile(
    r"^(is it|is that|isnt it|isn't it|isnt that|isn't that|are u|are you|"
    r"r u|do u|do you|does u|does|did u|did you|can u|can you|would u|"
    r"would you|could u|could you|will u|will you|should u|should we|"
    r"should i|wouldnt u|wouldn't u|dont u|don't u|arent u|aren't u|"
    r"wont u|won't u|cant u|can't u|u wanna|u want|u up|u free|u down|"
    r"u got|u have|u there|u around|u awake|u good|u ok|u ready|u right|"
    r"have u|have you|do we|shall we|wanna|want|whos|who's|whats|wats|"
    r"wat bout|what bout|what about|how bout|how about|bout u|"
    r"what|which|when|where|who|why|how|hru|hw u|how u|howz|"
    r"wyd|wat u doing|what u doing|what u doin|ne1|any1|any\b|"
    r"u\b|y\b|wai\b|fr\b|may i|let u know)\b",
    re.I,
)


def _is_question(text: str) -> bool:
    """True if *text* looks like a question (trailing ? or interrogative lead).

    Catches trailing reciprocal tokens too ("bored in the crib wbu" -> True) so
    the bot's own "wbu"-style replies register as questions (answer-frame).
    """
    t = text.strip()
    return (t.endswith("?")
            or bool(_QUESTION_LEAD.match(t))
            or bool(re.search(r"(?:^|\s)(?:wbu|hbu)\s*$", t, re.I)))


# Section 5.8 STEP 1 intent classification — agreement / reaction phrases.
# Only applied to short messages (<=4 words) so ordinary statements are never
# mis-routed.
_AGREEMENT_RE = re.compile(
    r"^(me too|me 2|same here|same|saame|same lol|same ngl|same tbh|"
    r"haha same|same here lol|yeah me too|yea me too|yes me too|"
    r"ikr|indeed|agreed|totally|exactly|true|so true|true that|"
    r"fr same|for real|facts|fasho|i agree|i agree lol|yes|yep|yeah)\b",
    re.I,
)

_REACTION_RE = re.compile(
    r"^(lol|lmao|lmfao|haha|hahaha|hehe|hehehe|hmm|hm|"
    r"wow|omg|oh my|aww\b|nice|nicee|cool|ok|okay|okk|kk|"
    r"dang|damn|dam\b|no way|oh\b|ah\b|huh|funny|yikes|rip)\b",
    re.I,
)

# Section 5.8 NEW_TOPIC — a plain statement naming a concrete topic ("i like
# music", "its raining here") starts a new conversational thread instead of
# being treated as a dead-end continuation.
_TOPIC_RE = re.compile(
    r"\b(?:music|song|songs|movie|movies|film|show|series|game|games|"
    r"gaming|cricket|football|soccer|basketball|sport|food|eating|dinner|"
    r"lunch|breakfast|tea|coffee|work|job|school|college|uni|exam|"
    r"family|mom|dad|friend|friends|travel|trip|vacation|snap|tiktok|"
    r"insta|bored|sleep|tired|weather|rain|raining|rainy|cold|hot|sunny|"
    r"date|dates|relationship|"
    r"crush|hair|money|home|party|birthday)\b",
    re.I,
)


# PATH D (DRY/BORED) openers — low-energy one-liners are a topic-shift cue.
_DRY_RE = re.compile(
    r"^(ok|okay|okkk|okkkkk|kk|k|hmm|hm|mhm|meh|fine|idk|dunno|nothing|"
    r"nothing much|nvm|whatever|uh huh|alright|aight|ight|hmph)\b",
    re.I,
)

# PATH E (BUSY/DELAY) signals — the user is otherwise occupied / leaving.
_BUSY_RE = re.compile(
    r"\b(at work|at the office|working|got work|dayshift|work rn|gotta go|"
    r"gtg|brb|busy|not free|sleepy|drowsy|tired|exhausted|napping|sleeping|"
    r"heading to bed|going to bed|off to sleep)\b",
    re.I,
)

# Fallbacks when the middle_chat data files are missing (pure-Python).
_TOPIC_SHIFT_POOL = [
    "wanna know smth fun? 👀",
    "u seem quiet 😏 ok one question: whats ur type?",
]
_WAIT_NUDGE_POOL = [
    "np, text me when free 😌",
    "okay take ur time, I'll be here",
]

# Stage 6 RETRY — user declined the snap ask: one soft push, then re-ask.
_DECLINE_RE = re.compile(
    r"\b(no|nah|nope|not\s+now|no\s+thx|no\s+thanks|no\s+snap|"
    r"i\s+don'?t\s+(use|want)|don'?t\s+have\s+snap|cant\s+now|maybe\s+later|"
    r"later|not\s+really|lol\s+no|no\s+lol)\b",
    re.I,
)
_RETRY_NUDGE_POOL = _load_pool_from_file("retry_nudge.txt", [
    "come on add me na 🙄",
    "why not tho 😏 dont be shy",
    "ok but im not letting this go that easy 👀",
    "one add, no pressure... or maybe a lil pressure",
])


# Short acknowledgements for when the user is ANSWERING the bot's own
# question ("bored rn, you?" -> "im bored too" -> "haha same"). Keeps the
# conversation warm without re-scanning the answer as a fresh request.
_ANSWER_ACK_POOL = _load_pool_from_file("answer_ack.txt", [
    "haha same",
    "same here",
    "me too hehe",
    "nice",
    "okay good",
    "hehe nice",
    "i feel that",
    "good to know",
    "haha okay",
    "works",
])

# Agreement-flavoured acks for when the user AGREES with the bot ("me too",
# "same" — Section 5.8 AGREEMENT intent). Only used by the answer-frame.
_AGREEMENT_ACK_POOL = _load_pool_from_file("agreement_ack.txt", [
    "same here lol",
    "haha same",
    "me too hehe",
    "same ngl",
    "hehe same",
    "same tbh",
    "haha me too",
    "yes us both lol",
])

# A34: agreement-streak breakers. Once the streak hits 3, the bot pivots to a
# fresh-topic question (no repeat within a conversation) so a string of
# "me too" never loops on the same ack line.
_AGREEMENT_BREAKER_POOL = _load_pool_from_file("agreement_breaker.txt", [
    "lol we keep agreeing xd what do u do for fun?",
    "hahaha same energy fr, u got any hobbies?",
    "we on the same wave lol u into music?",
    "this is getting too agreeable lol what u up to tonight?",
    "we clearly think alike haha, any plans?",
    "we agree too much lol tell me something bout u",
])

# Flow fix (A37): when the LAST identity field fills the bot acknowledges the
# info (echoes the country) before the flirty gate — never jumps into a flirty
# question on the very answer that completed the profile.
_INFO_ACK_POOL = _load_pool_from_file("info_ack.txt", [
    "got it lol",
    "ahh okay got u",
    "nice nice, we can talk properly now hihi",
    "cool, finally we can vibe properly",
    "lol okay now i remember u",
    "hehe noted",
    "okay noted hihi",
    "good to know hehe",
])

# Flow fix (A37): a short answer to the bot's OWN flirty question ("slow",
# "yes") gets a teasing follow-up, never a generic ack like "hehe same".
_ANSWER_FOLLOWUP_POOL = _load_pool_from_file("answer_followup.txt", [
    "hehe good answer 😏",
    "okay i like that answer lol",
    "interesting, tell me why?",
    "haha bold answer, i respect it",
    "hehe noted, u got taste",
    "really? explain lol",
    "okay okay, i see u 😏",
    "good, so do i lol",
])

# ---------------------------------------------------------------------------
# Snap username source (backed by RoundRobinFile over data/snap_ids.txt)
# ---------------------------------------------------------------------------

def _next_snap_username() -> str:
    """Get the next snap username (round-robin, hot-reload, persisted index)."""
    return next_snap_username()


# Random snap message formats
_SNAP_FORMATS = [
    "my snp : {username}",
    "my sc : {username}",
    "sn-p : {username}",
    "s,n.ap : {username}",
    "sn..p : {username}",
    "snap : {username}",
    "sc : {username}",
    "{username}",
    "add {username}",
    "my snap : {username}",
    "im {username}",
]


def _random_snap_reply() -> str:
    """Generate a snap message with next username in cycle + random format."""
    username = _next_snap_username()
    format_str = random.choice(_SNAP_FORMATS)
    return format_str.format(username=username)


def _resolve_snap_reply(reply: str) -> str:
    """Replace {snap} placeholder with random snap message."""
    if "{snap}" in reply:
        return _random_snap_reply()
    return reply


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SNAP_OVERRIDE_KEYWORDS = frozenset({
    "snap", "snapchat", "sc", "add me", "send me your sc",
    "username", "pic", "photo", "photos", "picture",
    "send a pic", "send a photo", "show me your face",
    "show me", "send snap", "what's your snap", "your snap",
    "ছবি", "দেখাও", "add",
})

# Step 6: Snap gate replies (redirect to snap)
SNAP_GATE_REPLIES = [
    "txt me on sc its easier",
    "do u have snap? way better there",
    "snap is way better for chatting",
    "i prefer snap tbh",
    "lets move to snap",
    "add me on snap?",
    "im more active on snap",
    "{snap}",
    "we should continue this on snap",
    "its better on snap trust me",
]

# ---------------------------------------------------------------------------
# 12 Flow Definitions (randomly selected per conversation)
# ---------------------------------------------------------------------------

FLOWS = {
    "friendly": {
        "weight": 20,
        "step3": ["where r u from?", "what country?", "u from around here?"],
        "step4": ["wbu do for fun?", "what u into?", "u single?", "what keeps u busy?"],
        "step5": ["ur funny lol", "haha stop", "ok that was smooth"],
        "snap_intro": ["txt me on sc its easier", "do u have snap?"],
    },
    "night": {
        "weight": 15,
        "step3": ["where u at?", "u up late?", "what time is it there?"],
        "step4": ["u alone tonight?", "whats keeping u up?", "night owl?"],
        "step5": ["cant sleep either huh", "late night vibes", "same cant sleep"],
        "snap_intro": ["snap is better for late chats", "hmu on snap"],
    },
    "deep": {
        "weight": 10,
        "step3": ["where u from?", "what's ur story?", "tell me about urself"],
        "step4": ["whats ur biggest dream?", "what would u do with 1m $?", "u believe in soulmates?"],
        "step5": ["that's deep i like it", "ur pretty interesting", "ok i see u"],
        "snap_intro": ["we should talk more on snap", "add me on sc"],
    },
    "fun": {
        "weight": 15,
        "step3": ["where u from?", "what's fun there?", "u local?"],
        "step4": ["what u do for fun?", "fav thing to do?", "u adventurous?"],
        "step5": ["haha ur wild", "ok that sounds fun", "love that"],
        "snap_intro": ["snap is more fun", "we should snap"],
    },
    "direct": {
        "weight": 10,
        "step3": ["where u at?", "how old r u?", "u single?"],
        "step4": ["u looking for friends or more?", "what u want?", "be real what u here for"],
        "step5": ["ok straight to the point i like that", "bold", "respect"],
        "snap_intro": ["lets skip to snap", "add me on sc"],
    },
    "romance": {
        "weight": 10,
        "step3": ["where u from?", "whats ur type?", "u romantic?"],
        "step4": ["whats ur love language?", "ever been in love?", "ur ideal date?"],
        "step5": ["thats so sweet", "ur such a hopeless romantic", "i love that"],
        "snap_intro": ["we should talk on snap", "txt me on sc"],
    },
    "bold": {
        "weight": 5,
        "step3": ["where u at?", "how old?", "u single or taken?"],
        "step4": ["u down to talk?", "whats ur vibe rn?", "be honest u like attention?"],
        "step5": ["ok ur bold i like it", "haha wow", "someone's confident"],
        "snap_intro": ["snap is better for bold talks", "add me on sc"],
    },
    "mysterious": {
        "weight": 5,
        "step3": ["where u from?", "whats ur deal?", "tell me something about u"],
        "step4": ["whats the most interesting thing about u?", "u have secrets?", "whats ur hidden talent?"],
        "step5": ["ok mysterious i see u", "now im curious", "ur interesting"],
        "snap_intro": ["maybe we can talk more on snap", "add me"],
    },
    "sweetie": {
        "weight": 5,
        "step3": ["where u from?", "whats ur name?", "how r u today?"],
        "step4": ["what makes u happy?", "whats the best thing that happened to u?", "u a morning person?"],
        "step5": ["ur so sweet", "ok i love that", "thats adorable"],
        "snap_intro": ["we should stay in touch on snap", "txt me on sc"],
    },
    "chill": {
        "weight": 5,
        "step3": ["where u at?", "whats up?", "u busy?"],
        "step4": ["whats the vibe?", "u chilling?", "what u watching?"],
        "step5": ["same tbh", "nice sounds chill", "relatable"],
        "snap_intro": ["snap is easy for chatting", "add me on sc"],
    },
    "seductive": {
        "weight": 5,
        "step3": ["where u at?", "u up late?", "how old r u?"],
        "step4": ["whats ur type?", "u like attention?", "what turns u on?"],
        "step5": ["ok ur cute", "haha stop flirting", "ur trouble"],
        "snap_intro": ["snap is better for this", "hmu on sc"],
    },
    "curious": {
        "weight": 5,
        "step3": ["where u from?", "what do u do?", "how old?"],
        "step4": ["tell me something interesting", "whats the craziest thing u done?", "u believe in fate?"],
        "step5": ["thats wild", "no way", "ok im intrigued"],
        "snap_intro": ["we should keep talking on snap", "add me"],
    },
}


# ---------------------------------------------------------------------------
# Input / Output TXT Matching Engine
# ---------------------------------------------------------------------------

_COUNTRY_KEYWORDS = {
    "usa": "usa", "united states": "usa", "america": "usa", "american": "usa", "us": "usa",
    "canada": "canada", "canadian": "canada",
    "uk": "uk", "united kingdom": "uk",
    "england": "uk", "scotland": "uk", "wales": "uk", "british": "uk",
    "australia": "australia", "au": "australia", "aussie": "australia", "aus": "australia",
    "new zealand": "new zealand", "nz": "new zealand",
    "germany": "germany", "german": "germany", "deutschland": "germany",
    "france": "french", "french": "france",
    "spain": "spain", "spanish": "spain",
    "italy": "italy", "italian": "italy",
    "netherlands": "netherlands", "dutch": "netherlands",
    "belgium": "belgium", "sweden": "sweden", "norway": "norway",
    "denmark": "denmark", "finland": "finland",
    "switzerland": "switzerland", "austria": "austria",
    "ireland": "ireland", "irish": "ireland",
    "poland": "poland", "portugal": "portugal", "greek": "greece", "greece": "greece",
    "japan": "japan", "japanese": "japan",
    "korea": "south korea", "south korea": "south korea", "korean": "south korea",
    "singapore": "singapore", "hong kong": "hong kong",
    "taiwan": "taiwan", "philippines": "philippines", "filipino": "philippines",
    "india": "india", "indian": "india",
    "pakistan": "pakistan", "pakistani": "pakistan",
    "bangladesh": "bangladesh", "bangladeshi": "bangladesh",
    "nepal": "nepal", "nepali": "nepal",
    "sri lanka": "sri lanka", "sinhalese": "sri lanka",
    "malaysia": "malaysia", "malaysian": "malaysia",
    "thailand": "thailand", "thai": "thailand",
    "vietnam": "vietnam", "vietnamese": "vietnam",
    "indonesia": "indonesia", "indonesian": "indonesia",
    "israel": "israel", "israeli": "israel",
    "uae": "uae", "dubai": "uae", "emirati": "uae",
    "saudi arabia": "saudi arabia", "saudi": "saudi arabia",
    "brazil": "brazil", "brazilian": "brazil",
    "argentina": "argentina", "argentinian": "argentina",
    "mexico": "mexico", "mexican": "mexico",
    "chile": "chile", "colombia": "colombia", "colombian": "colombia",
    "nigeria": "nigeria", "nigerian": "nigeria",
    "south africa": "south africa", "egypt": "egypt", "egyptian": "egypt",
    "kenya": "kenya", "ghana": "ghana", "morocco": "morocco", "moroccan": "morocco",
}


def _country_from_text(text: str) -> Optional[str]:
    lower = " " + text.lower() + " "
    for keyword, country in _COUNTRY_KEYWORDS.items():
        if f" {keyword} " in lower:
            return country
    return None


# Slang/typo expansion used only for "understanding" during trigger matching
# (never applied to the reply text itself).
_SLANG_TOKEN_MAP = {
    "u": "you",
    "r": "are",
    "ur": "your",
    "yr": "your",
    "yur": "your",
    "im": "i am",
    "ims": "i am",
    "wat": "what",
    "wats": "what is",
    "wut": "what",
    "wts": "what is",
    "wat's": "what is",
    "what's": "what is",
    "how's": "how is",
    "where's": "where is",
    "whr": "where",
    "wbu": "what about you",
    "wby": "what about you",
    "hbu": "how about you",
    "dunno": "do not know",
    "dont": "do not",
    "cant": "can not",
    "wont": "will not",
    "aint": "am not",
    "gonna": "going to",
    "kinda": "kind of",
}


def _norm_tokens(text: str) -> List[str]:
    """Lowercase token list with slang/contraction expansion applied."""
    out: List[str] = []
    for tok in re.findall(r"[a-z0-9']+", text.casefold()):
        expanded = _SLANG_TOKEN_MAP.get(tok)
        if expanded is not None:
            out.extend(expanded.split())
            continue
        tok = tok.strip("'")
        expanded = _SLANG_TOKEN_MAP.get(tok)
        if expanded is not None:
            out.extend(expanded.split())
        elif tok:
            out.append(tok)
    return out


def _norm_text(text: str) -> str:
    return " ".join(_norm_tokens(text))


class InputOutputEngine:
    """Caches input/ trigger phrases and output/ replies from *.txt files.

    Priority-1 matcher:
      * input/<name>.txt is split on newlines into trigger phrases (one per
        line).
      * output/<name>.txt is split on newlines/'|' into candidate replies
        (chosen from the same file name as the matched input file).
      * On match, a random reply from the output file is returned and the
        caller's normal flow is bypassed. If nothing matches, engines fall
        back to the 6-step state machine.
    """

    def __init__(self, project_root: str) -> None:
        self.input_dir = os.path.join(project_root, "data", "input")
        self.output_dir = os.path.join(project_root, "data", "output")
        self.input_rules: Dict[str, List[str]] = {}
        self.output_replies: Dict[str, List[str]] = {}
        self.last_match: Optional[Tuple[str, str]] = None
        self._exact_sets: Dict[str, set] = {}
        self._subpat: Dict[str, "re.Pattern[str]"] = {}
        self._norm_text_cache: Dict[str, str] = {}
        self._norm_subpat: Dict[str, "re.Pattern[str]"] = {}
        self._subset_sets: Dict[str, frozenset] = {}
        # Last line issued per output file, so a pool never repeats its own
        # previous line back-to-back (small pools like greeting/snap).
        self._last_pick: Dict[str, str] = {}
        # Track which file and line number was last picked for debug output.
        self.last_picked_file: str = ""
        self.last_picked_line_num: int = 0
        self.reload_files()

    def reload_files(self) -> None:
        """Load all input/output txt files into memory once (not per-message)."""
        self.input_rules.clear()
        self.output_replies.clear()

        if not os.path.isdir(self.input_dir) or not os.path.isdir(self.output_dir):
            return

        for fname in os.listdir(self.input_dir):
            if not fname.lower().endswith(".txt"):
                continue
            full = os.path.join(self.input_dir, fname)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    raw = f.read()
            except Exception as e:
                print(f"[InputOutputEngine] Error loading {full}: {e}")
                continue
            triggers = [
                t.strip().lower()
                for t in re.split(r"[\r\n]+", raw)
                if t.strip()
            ]
            if triggers:
                self.input_rules[fname.lower()] = triggers

        self._rebuild_caches()

        for fname in os.listdir(self.output_dir):
            if not fname.lower().endswith(".txt"):
                continue
            full = os.path.join(self.output_dir, fname)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    raw = f.read()
            except Exception as e:
                print(f"[InputOutputEngine] Error loading {full}: {e}")
                continue
            replies = [
                r.strip()
                for r in re.split(r"[\r\n|]+", raw)
                if r.strip()
            ]
            if replies:
                self.output_replies[fname.lower()] = replies

    def _rebuild_caches(self) -> None:
        """Precompute per-trigger match data (pure memoization, no behavior
        change) so per-message matching stays fast even with large pools."""
        self._exact_sets = {
            fname: set(trigs) for fname, trigs in self.input_rules.items()
        }
        self._subpat = {}
        self._norm_text_cache = {}
        self._norm_subpat = {}
        self._subset_sets = {}
        for fname, trigs in self.input_rules.items():
            for trigger in trigs:
                if len(trigger) >= 3:
                    self._subpat[trigger] = re.compile(
                        r"\b" + re.escape(trigger) + r"\b")
                nt = _norm_text(trigger)
                self._norm_text_cache[trigger] = nt
                # Guard: skip normalized matching for triggers whose meaning
                # would be inflated by dropping punctuation ("ur @" would
                # normalize to just "your" and match every "ur ..." message).
                norm_safe = len(nt.split()) >= 2 or trigger.isalnum()
                if norm_safe and len(nt) >= 3:
                    self._norm_subpat[trigger] = re.compile(
                        r"\b" + re.escape(nt) + r"\b")
                tset = frozenset(
                    t for t in _norm_tokens(trigger) if len(t) >= 3)
                if len(tset) >= 2:
                    self._subset_sets[trigger] = tset

    def find_match(self, user_msg: str,
                   exclude: Optional[set] = None,
                   used_map: Optional[Dict[str, List[str]]] = None
                   ) -> Optional[Tuple[str, str]]:
        """Return (reply, rule_file) from a matched output file, or None.

        Every text file under input/ is searched (optionally excluding the
        given category names, used to keep broad pools away from live chat).
        Matching order:
          1. Exact trigger match (original and punctuation-stripped).
          2. Word-boundary substring match, only for triggers that are long
             enough that they cannot trivially collide with common words.
          3. Slang/typo-expanded ('u' -> 'you', 'wats' -> 'what is', ...)
             exact + word-boundary match, so "wats ur age?" still finds the
             "what is your age" trigger and "where u from" finds country.
          4. Token-subset fallback: every significant (>=3 chars) trigger
             word appears in the message, so word order or extra words do
             not matter ("21 from germany hi" still matches "im 21").
        On any match a random reply from the same-named output file is used.

        ``used_map`` (per-conversation, ``{fname: [issued replies]}``) makes
        the pool session-no-repeat: a line already said in this conversation
        is shied away from until the pool is exhausted.
        """
        to_skip = exclude or frozenset()
        raw_msg = user_msg.lower().strip()
        simple_msg = re.sub(r"[^\w\s]", " ", raw_msg)
        simple_msg = " ".join(simple_msg.split())
        norm_toks = _norm_tokens(user_msg)
        norm_msg = " ".join(norm_toks)
        norm_set = set(norm_toks)
        country_hint = _country_from_text(user_msg)
        self.last_match = None

        for fname, triggers in self.input_rules.items():
            if fname in to_skip:
                continue
            exact = self._exact_sets.get(fname)
            if exact is None:
                continue
            if raw_msg in exact or simple_msg in exact:
                hit = raw_msg if raw_msg in exact else simple_msg
                self.last_match = (hit, "exact")
                reply = self._pick_reply(fname, country_hint, used_map)
                if reply:
                    return reply, fname

        for fname, triggers in self.input_rules.items():
            if fname in to_skip:
                continue
            for trigger in triggers:
                # Skip too-short triggers for substring matching to avoid
                # false positives (e.g. "hi" inside "this", "m" everywhere).
                pat = self._subpat.get(trigger)
                if pat is None:
                    continue
                if pat.search(raw_msg) or pat.search(simple_msg):
                    self.last_match = (trigger, "substring")
                    reply = self._pick_reply(fname, country_hint, used_map)
                    if reply:
                        return reply, fname

        # Pass 3: normalized (slang/typo-expanded) exact + substring match.
        # english_only.txt is excluded here: its English-looking triggers ("i like
        # you") would otherwise hijack ordinary English messages and get the
        # "english only" fallback lines. Literal foreign phrases still match
        # in pass 2.
        norm_cache = self._norm_text_cache
        for fname, triggers in self.input_rules.items():
            if fname in to_skip or fname == "english_only.txt":
                continue
            for trigger in triggers:
                nt = norm_cache.get(trigger)
                if nt is None:
                    nt = _norm_text(trigger)
                    norm_cache[trigger] = nt
                if len(nt) < 3:
                    continue
                if norm_msg == nt:
                    self.last_match = (trigger, "normalized")
                    reply = self._pick_reply(fname, country_hint, used_map)
                    if reply:
                        return reply, fname
                npat = self._norm_subpat.get(trigger)
                if npat is not None and npat.search(norm_msg):
                    self.last_match = (trigger, "normalized")
                    reply = self._pick_reply(fname, country_hint, used_map)
                    if reply:
                        return reply, fname

        # Pass 4: token-subset fallback (order-insensitive). Only for triggers
        # that carry a meaning-bearing token (>=4 chars): pure function-word
        # triggers ("how are you") otherwise swallow short messages that merely
        # share common words ("how tall are u"), which the brain answers better.
        # english_only.txt excluded (see pass 3) to protect English messages.
        for fname, triggers in self.input_rules.items():
            if fname in to_skip or fname == "english_only.txt":
                continue
            for trigger in triggers:
                tset = self._subset_sets.get(trigger)
                if tset is None:
                    continue
                if not any(len(t) >= 4 for t in tset):
                    continue
                if tset.issubset(norm_set):
                    self.last_match = (trigger, "subset")
                    reply = self._pick_reply(fname, country_hint, used_map)
                    if reply:
                        return reply, fname

        return None

    def _pick_reply(self, fname: str, country_hint: Optional[str] = None,
                    used_map: Optional[Dict[str, List[str]]] = None
                    ) -> Optional[str]:
        replies = self.output_replies.get(fname)
        if not replies:
            return None
        cand = replies
        if fname == "country.txt" and country_hint:
            matches = [r for r in replies if country_hint in r.lower()]
            if matches:
                cand = matches
        # Consecutive no-repeat: never immediately re-issue the previous line
        # from this pool (small pools like greeting/snap benefit the most).
        fresh = ([r for r in cand if r != self._last_pick.get(fname)]
                 if len(cand) > 1 else cand)
        # Session no-repeat: stay away from lines already used in THIS
        # conversation until the usable pool is exhausted.
        used_list = used_map.get(fname) if used_map else None
        if used_list:
            never = [r for r in fresh if r not in used_list]
            if never:
                fresh = never
        chosen = random.choice(fresh)
        self._last_pick[fname] = chosen
        # Track the line number for debug output.
        self.last_picked_file = fname
        try:
            self.last_picked_line_num = replies.index(chosen) + 1
        except ValueError:
            self.last_picked_line_num = 0
        if used_map:
            used_map[fname] = (used_list or []) + [chosen]
        if "%username%" in chosen:
            chosen = chosen.replace("%username%", next_snap_username())
        if "{country}" in chosen:
            chosen = chosen.replace("{country}", country_hint or "usa")
        return chosen

    def country_reply(self, country: str) -> Optional[str]:
        """Country reply from data/output/country.txt, preferring a line
        that mentions the user's country; fallback to any line in the file.
        """
        replies = self.output_replies.get("country.txt")
        if not replies:
            return None
        chosen = None
        if country:
            matches = [r for r in replies if country in r.lower()]
            if matches:
                chosen = random.choice(matches)
        if chosen is None:
            chosen = random.choice(replies)
        if "{country}" in chosen:
            chosen = chosen.replace("{country}", country or "usa")
        if "%username%" in chosen:
            chosen = chosen.replace("%username%", next_snap_username())
        return chosen


def _load_pool_lines(project_root: str, fname: str, subdir: str = "") -> List[str]:
    """Load one harvested pool file (data/output/[subdir/]<fname>).

    ``subdir`` hosts reply paths split by intent, e.g. ``middle_chat/`` with
    ``horny_reply.txt`` (HORNY path), ``flirty_reply.txt`` (FLIRTY path) and ``warm_reply.txt``
    (NORMAL path). Returns [] if the file is missing so every caller keeps a
    pure-Python fallback.
    """
    path = os.path.join(project_root, "data", "output", subdir, fname)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh.read().splitlines()]
        return [ln for ln in lines if ln]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# RuleEngine
# ---------------------------------------------------------------------------

class RuleEngine:
    """Section 5.7 FLIRTY_CHAT mode — a fixed state machine.

    Locked state order:
      GREETING      -> collect greeting / age / gender / country one at a time
      HORNY_DETECT  -> fuzzy match the user's SMS vs horny_input.txt.

      BRANCH A (horny hit):
          HORNY_REPLY (random horny_output line) -> ASKSC (random ask_snap line,
          never a flirty question, user reply not matched) -> SHARESC (random
          share_snap line, user reply not matched) -> END.

      BRANCH B (no hit):
          FLIRTY_ASK (1-2 random flirty lines) -> MIDDLE_CHAT (reply to the
          user realistically, flirty answer 3-type detected) -> ASKSC (user
          reply not matched) -> SHARESC (user reply not matched) -> END.

    Hard rules honoured:
      - Never HORNY_REPLY -> FLIRTY_ASK.
      - No line repeats within one session (pools cycle without repeats).
      - Flow is never revealed in the visible reply; STATE/REPLY debug goes to
        data/logs/chat_debug.log only.

    MIDDLE_CHAT reuses the answer logic (TXT matcher, persona answers, the
    answer-frame with topic reactions + acks, then the stage-rule brain, then
    a fresh flirty question) so late chat stays realistic.

    State per conversation:
      - flirty_state (str): GREETING / FLIRTY_ASK / MIDDLE / ASKSC / SHARESC
        / END (HORNY_REPLY is emitted when HORNY_DETECT hits and sets ASKSC
        next; SHARESC always ends the chat).
      - flow_type, horny_detected, snap_pivoted, message_count, identity
        caches, used-line lists.
    """

    def __init__(self) -> None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.io_engine = InputOutputEngine(project_root)
        self.pipe_db = load_pipe_db()
        self.evoflow_rules = load_evoflow_rules()
        self.personality_spec = load_promote_extract()
        # Flirty/playful question pool (data/output/flirty_questions.txt).
        # FLIRTY_ASK and the MIDDLE fallback draw from it.
        self.flow_questions = _load_pool_lines(project_root, "flirty_questions.txt")
        # HORNY_REPLY pool (data/output/horny.txt) and GREETING pool
        # (data/output/greeting.txt), both session-no-repeat.
        self.horny_lines = _load_pool_lines(project_root, "horny.txt")
        self.greet_lines = _load_pool_lines(project_root, "greeting.txt")
        # MIDDLE_CHAT reply paths, split by intent (data/output/middle_chat/):
        #   horny.txt  -> HORNY path reply   (HORNY_DETECT deflect, both gates)
        #   flirty_reply.txt -> FLIRTY path reply  (MIDDLE flirty fallback line)
        #   warm_reply.txt -> NORMAL path reply  (generic warm ack)
        # Each falls back to the flat pools below when its file is missing.
        self.mc_horny_lines = _load_pool_lines(project_root, "horny_reply.txt",
                                               subdir="middle_chat")
        self.mc_flirty_lines = _load_pool_lines(project_root, "flirty_reply.txt",
                                                subdir="middle_chat")
        self.mc_normal_lines = _load_pool_lines(project_root, "warm_reply.txt",
                                                subdir="middle_chat")
        self.mc_topic_shift_lines = _load_pool_lines(
            project_root, "new_topic.txt", subdir="middle_chat")
        self.mc_wait_nudge_lines = _load_pool_lines(
            project_root, "busy_later.txt", subdir="middle_chat")
        # Reference TGA stage reply brain (optional fallback for MIDDLE_CHAT).
        # Disabled gracefully if the stage_rules data is not present.
        try:
            self.rule_brain = TgBrain(
                os.path.join(project_root, "data", "local_db", "stage_rules")
            )
        except Exception as e:  # never let the brain break the bot
            print(f"[RuleEngine] TgBrain disabled: {e}")
            self.rule_brain = None
        self.last_trace: List[str] = []
        self._last_emitting_state: str = "?"

    def _trace(self, entry: str) -> None:
        """Record one debug line explaining the last reply decision."""
        self.last_trace.append(entry)

    def _debug_summary(self, emitting_state: str) -> str:
        """Reduce the last decision trace to a single 'why this reply' line.

        Consumed by chat_debug.log (and exposed via ChatRuleBot for tools/
        tests). Mirrors the flow-map so each entry pairs with a recognizable
        path: PATTERN DETECTOR first-SMS routing, the io TXT category that
        fired, KEYWORD DETECTOR PATH A/B, or the machine stage that answered.
        """
        joined = " ".join(self.last_trace)
        if "route=AGE_GENDER" in joined:
            return "route=AGE_GENDER -> age_gender.txt"
        if "route=COUNTRY" in joined:
            return "route=COUNTRY -> country.txt"
        if "route=FIRST_AGE_GENDER" in joined or "pattern: FIRST_AGE_GENDER" in joined:
            return "route=FIRST_AGE_GENDER -> age_gender.txt"
        if "route=FIRST_COUNTRY" in joined or "pattern: FIRST_COUNTRY" in joined:
            return "route=FIRST_COUNTRY -> country.txt"
        if "route=FIRST_GREETING" in joined or "pattern: FIRST_GREETING" in joined:
            return "route=FIRST_GREETING -> greeting.txt"
        if "pattern: FT-HORNY" in joined:
            return ("pattern:FT-HORNY (first SMS fast-track) -> "
                    "middle_chat/horny_reply.txt")
        if "pattern: FT-FLIRTY" in joined:
            return ("pattern:FT-FLIRTY (first SMS fast-track) -> "
                    "middle_chat/flirty_reply.txt")
        m = re.search(r"\bpattern:\s*([^ ]+) -> (\S+\.txt)", joined)
        if m:
            return (f"pattern:{m.group(1)} -> {m.group(2)} "
                    f"(first SMS, skip greeting)")
        m = re.search(
            r"\bio:\s*(\S+\.txt)\s*\[(exact|substring|fuzzy|normalized)\]",
            joined)
        if m:
            return f"io:{m.group(1)} ({m.group(2)})"
        if "HORNY_DETECT:" in joined:
            return "machine:HORNY_DETECT -> HORNY_REPLY (state ASKSC)"
        if "PATH E (BUSY)" in joined:
            return "machine:PATH E (BUSY) -> middle_chat/busy_later.txt"
        if "PATH D (DRY)" in joined:
            return "machine:PATH D (DRY) -> middle_chat/new_topic.txt"
        if "PATH B (FLIRTY)" in joined:
            return "machine:PATH B (FLIRTY) -> middle_chat/flirty_reply.txt"
        if "GREETING: collection complete -> flirty now" in joined:
            return f"machine:{emitting_state} -> flirty_questions.txt"
        if "GREETING: collect" in joined:
            return f"machine:{emitting_state} -> collect profile field"
        if "END: chat over" in joined:
            return "machine:END -> chat over (closer)"
        if "gate: pending_scshare" in joined:
            return "machine:pending-share gate -> deliver share"
        if "minor: age" in joined:
            return "security: underage (<18) -> snap share blocked"
        if "agreement+react:" in joined:
            return f"machine:{emitting_state} -> agreement+react"
        if "answer-frame:" in joined:
            return f"machine:{emitting_state} -> answer-frame"
        if "persona:" in joined:
            return f"machine:{emitting_state} -> persona answer (builtin)"
        if "react:" in joined:
            return f"machine:{emitting_state} -> react (builtin)"
        if "brain:" in joined:
            return f"machine:{emitting_state} -> brain match (rules)"
        if "middle: fallback -> FLIRTY path reply" in joined:
            return (f"machine:{emitting_state} -> fallback FLIRTY path "
                    f"(flirty_reply.txt)")
        if "middle: fallback -> warm ack" in joined:
            return (f"machine:{emitting_state} -> fallback warm ack "
                    f"(warm_reply.txt)")
        if "ASKSC:" in joined:
            return "machine:ASKSC -> ask_snap.txt (round-robin)"
        if "RETRY: decline" in joined:
            return ("machine:SHARESC -> share_snap.txt "
                    "(retry/decline)")
        if "SHARESC:" in joined:
            return "machine:SHARESC -> share_snap.txt -> END"
        return f"machine:{emitting_state} -> full trace above"

    def _pick_flow(self) -> str:
        """Pick a random flow type based on per-flow weights."""
        types = list(FLOWS.keys())
        weights = [FLOWS[t].get("weight", 10) for t in types]
        return random.choices(types, weights=weights, k=1)[0]

    def init_conversation(self) -> Dict[str, Any]:
        flow_type = self._pick_flow()
        conv_id = "{:x}{:06x}".format(
            int(time.time() * 1000), random.randint(0, 0xFFFFFF))
        return {
            "conv_id": conv_id,
            "message_count": 0,
            "user_messages": [],
            "bot_messages": [],
            "snap_pivoted": False,
            "snap_hint_sent": False,
            "snap_intro_sent": False,
            "asked_snap": False,
            "snap_nudge_sent": False,
            "pending_scshare": False,
            "blocked": False,
            "pending_replies": [],
            "user_gender": None,
            "user_age": None,
            "age_revealed": False,
            "user_country": None,
            "country_rotator": CountryRotator(),
            "user_facts": {},
            "style_profile": None,
            "sequence_step": 0,
            "flow_type": flow_type,
            "horny_detected": False,
            "awaiting_answer": False,
            "last_bot_question": None,
            "age_asked": False,
            "country_asked": False,
            "gender_asked": False,
            "identity_answers": {},
            "io_used": {},
            "recent_flirty": [],
            "recent_acks": [],
            "agreement_streak": 0,
            "react_count": 0,
            "flirty_state": "GREETING",
            "greeting_done": False,
            "stage": "NEW",
            "route": None,
            "country_replied": False,
        }

    def decide_reply(
        self,
        user_message: str,
        state: Dict[str, Any],
    ) -> str:
        emitting_state = state.get("flirty_state", "?")
        t0 = time.perf_counter()
        reply = self._decide_reply(user_message, state)
        if not reply:
            reply = "আমি এখানে আছি—আপনি কী জানতে চান?"
            self._trace("fallback: empty reply -> default line")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._last_emitting_state = emitting_state
        state["awaiting_answer"] = _is_question(reply)
        if state["awaiting_answer"]:
            state["last_bot_question"] = reply
        self._append_chat_debug(user_message, state, reply,
                                emitting_state, elapsed_ms)
        return reply

    def _append_chat_debug(self, user_message: str, state: Dict[str, Any],
                           reply: str, emitting_state: str,
                           elapsed_ms: float) -> None:
        """Append one structured entry to data/logs/chat_debug.log.

        Format: STATE: <emitting state> / REPLY: <escaped reply> followed by a
        PATH (one-line "why this reply"), a USER and META line (conv id, flow,
        next state, collection, asks, timing) and the full trace of the
        decision. Oversized logs rotate once to chat_debug.log.1 instead of
        losing history entirely.
        """
        try:
            with _CHAT_DEBUG_LOCK:
                ts = time.time()
                stamp = (time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(ts))
                         + f".{int(ts % 1 * 1000):03d}")
                head = (f"[{stamp}] STATE: {emitting_state} / "
                        f"REPLY: {_log_esc(reply)}")
                path = self._debug_summary(emitting_state)
                age = state.get("user_age")
                gender = state.get("user_gender")
                country = state.get("user_country")
                meta = (
                    f"[conv={state.get('conv_id', '?')} "
                    f"flow={state.get('flow_type')} "
                    f"state={emitting_state}->{state.get('flirty_state')} "
                    f"stage={state.get('stage', '-')} "
                    f"route={state.get('route', '-')}"
                    f" step={state.get('sequence_step')} "
                    f"greet_done={state.get('greeting_done')} "
                    f"age={age if age is not None else '-'} "
                    f"gender={gender or '-'} country={country or '-'} "
                    f"name={state.get('user_name') or '-'} "
                    f"mood={state.get('mood') or '-'} "
                    f"asks(a,g,c)={state.get('age_asked')},"
                    f"{state.get('gender_asked')},{state.get('country_asked')} "
                    f"horny={state.get('horny_detected')} "
                    f"pivoted={state.get('snap_pivoted')} "
                    f"blocked={state.get('blocked')} "
                    f"reacts={state.get('react_count')} "
                    f"ms={elapsed_ms:.1f}]"
                )
                lines = [head,
                         f"  PATH: {path}",
                         f"  USER : {_log_esc(user_message)}",
                         f"  META : {meta}"]
                for t in self.last_trace:
                    lines.append(f"    {t}")
                text = "\n".join(lines) + "\n\n"
                try:
                    if (_CHAT_DEBUG_FILE.exists()
                            and _CHAT_DEBUG_FILE.stat().st_size > _CHAT_DEBUG_MAX):
                        prev = _CHAT_DEBUG_FILE.with_name(_CHAT_DEBUG_FILE.name + ".1")
                        if prev.exists():
                            prev.unlink()
                        _CHAT_DEBUG_FILE.replace(prev)
                except OSError:
                    pass
                with open(_CHAT_DEBUG_FILE, "a", encoding="utf-8") as f:
                    f.write(text)
        except Exception:
            pass

    def _decide_reply(
        self,
        user_message: str,
        state: Dict[str, Any],
    ) -> str:
        self.last_trace = []
        lines = [line.strip() for line in user_message.split("\n") if line.strip()]

        if len(lines) > 1:
            # Multi-line message: only process the LAST meaningful line to
            # avoid state mutation from intermediate lines.  Earlier lines
            # are fact-extracted but don't advance the conversation machine.
            for line in lines[:-1]:
                # Extract facts (gender/age/country/name) without advancing state
                scan_message(line)
                ga = detect_gender_age(line)
                if ga:
                    g, a = ga
                    if g and not state.get("user_gender"):
                        state["user_gender"] = g
                    if a is not None and state.get("user_age") is None:
                        state["user_age"] = a
                dc = self._try_detect_country(line)
                if dc:
                    state["user_country"] = dc
            # Process only the last line through the full machine
            return self._decide_reply(lines[-1], state)

        msg = lines[0] if lines else user_message.strip()
        state["user_messages"].append(msg)

        primary_tag, all_tags = scan_message(msg)

        # ---- CONTEXT MEMORY ENGINE (Stage 1): save name + mood ------------
        # age/gender/country are stored above; the new fields (name, mood) are
        # filled whenever the user volunteers them. Skip-logic (already
        # present) never re-asks a known field — see _collect_missing_line.
        if state.get("user_name") is None:
            found_name = detect_user_name(msg)
            if found_name:
                state["user_name"] = found_name
                self._trace(f"memory: name -> {found_name}")
        tag = primary_tag or "neutral"
        if tag != "neutral" or "mood" not in state:
            state["mood"] = tag
        # Remember more volunteered details (pet/music/hobby/job/fav). Each
        # fact is kept only if the user has not already given a value for the
        # key, so the first volunteered value always wins.
        user_facts = state.setdefault("user_facts", {})
        new_facts = detect_user_facts(msg)
        for key, value in new_facts.items():
            if value and user_facts.get(key) is None:
                user_facts[key] = value
                self._trace(f"memory: fact {key} -> {value}")

        # ---- ALWAYS: Extract gender/age from ANY message ----
        gender_age = detect_gender_age(msg)
        age_now = None
        if gender_age:
            gender, age = gender_age
            age_now = age
            if gender and not state.get("user_gender"):
                state["user_gender"] = gender
            if age is not None and state.get("user_age") is None:
                state["user_age"] = age

        # ---- ALWAYS: Extract country from ANY message ----
        detected_country = self._try_detect_country(msg)
        if detected_country:
            state["user_country"] = detected_country
            state["country_rotator"].set_user_country(detected_country)

        # ---- ALWAYS: Underage block (18+) --------------------------------
        # A revealed age < 18 (on ANY message, even after an older sticky age)
        # locks the conversation out of snap sharing: the user never receives
        # an ask_snap/share, a direct snapchat.txt reveal or a pending share. Safe
        # chat continues.
        minor_age = (age_now if age_now is not None else state["user_age"])
        if minor_age is not None and minor_age < 18 and not state.get("blocked"):
            state["blocked"] = True
            self._trace(f"minor: age {minor_age} < 18 -> snap block")

        # ---- Style mirroring: build profile after 5+ user messages ------
        # Threshold is 5 (not 3) to avoid interfering with persona caching
        # and identity collection in early conversation turns.
        if state.get("style_profile") is None and len(state["user_messages"]) >= 5:
            try:
                profile = analyze_style(state["user_messages"])
                state["style_profile"] = profile
                self._trace(f"style: profile built (slang={profile.slang_score:.2f} words={profile.avg_word_count:.0f})")
            except Exception:
                pass

        self._trace(f"scan: primary={primary_tag!r} tags={all_tags}")
        self._trace(
            f"detect: gender_age={gender_age} country={detected_country!r} "
            f"state.age={state.get('user_age')} state.gender={state.get('user_gender')}"
        )

        # ==============================================================
        # SIMPLE LOGIC: Detect user message → reply from output file
        # ==============================================================
        st = state["flirty_state"]

        # Greeting done on first message
        if state["message_count"] == 0:
            state["greeting_done"] = True

        # ---- PENDING SHARE (deliver on next inbound) -------
        if state.get("pending_scshare") and not state.get("blocked"):
            self._trace("gate: pending_scshare -> deliver share")
            state["pending_scshare"] = False
            state["snap_pivoted"] = True
            state["flirty_state"] = "MIDDLE"
            state["sequence_step"] = 8
            reply = "as promised, " + next_scshare_line()
            state["message_count"] += 1
            state["bot_messages"].append(reply)
            return self._apply_style(reply, state, msg)
        if state.get("pending_scshare"):
            state["pending_scshare"] = False

        # ---- STAGE 0: First SMS → Input/Output TXT match ----
        # Every message goes through InputOutputEngine. If matched, reply.
        scanned = self.io_engine.find_match(
            msg, used_map=state.setdefault("io_used", {}))
        if scanned:
            reply, txt_rule = scanned
            # Underage block: a minor must never receive a snap-revealing
            # line (share_snap.txt / snapchat.txt) even via the priority-1
            # IO match — fall through to safe chat instead.
            if state.get("blocked") and txt_rule in (
                    "share_snap.txt", "snapchat.txt"):
                self._trace(f"io: {txt_rule} skipped (blocked minor)")
            else:
                self.io_engine.last_picked_file = txt_rule
                try:
                    self.io_engine.last_picked_line_num = (
                        self.io_engine.output_replies.get(txt_rule, []).index(reply) + 1)
                except (ValueError, AttributeError):
                    self.io_engine.last_picked_line_num = 0
                self._trace(f"io: {txt_rule} -> {reply!r}")
                state["message_count"] += 1
                state["bot_messages"].append(reply)
                return self._apply_style(reply, state, msg)

        # ---- END: chat is over ----
        if state.get("flirty_state") == "END":
            self._trace("END: chat over -> closer")
            reply = "ttyl, snap me 👻"
            state["message_count"] += 1
            state["bot_messages"].append(reply)
            return self._apply_style(reply, state, msg)

        # ---- ASKSC: ask snap ----
        if state.get("blocked") and state["flirty_state"] in ("ASKSC", "SHARESC"):
            state["flirty_state"] = "MIDDLE"
        if state["flirty_state"] == "ASKSC":
            self._trace("ASKSC: ask_snap line")
            state["flirty_state"] = "SHARESC"
            state["sequence_step"] = 7
            reply = self._session_unused_line(state, "asksc_used", next_asksc_prompt)
            state["message_count"] += 1
            state["bot_messages"].append(reply)
            return self._apply_style(reply, state, msg)

        # ---- SHARESC: share snap → END ----
        if state["flirty_state"] == "SHARESC":
            self._trace("SHARESC: share_snap line -> END")
            state["flirty_state"] = "END"
            state["snap_pivoted"] = True
            state["sequence_step"] = 8
            reply = self._session_unused_line(state, "sharesc_used", next_scshare_line)
            state["message_count"] += 1
            state["bot_messages"].append(reply)
            return self._apply_style(reply, state, msg)

        # ---- MIDDLE CHAT: Detect horny/flirty/normal → reply from file ----
        # PATH A: HORNY keywords → horny.txt
        if match_horny(msg):
            self._trace("PATH A (HORNY): horny.txt")
            state["horny_detected"] = True
            state["flirty_state"] = "ASKSC"
            state["sequence_step"] = 6
            reply = self._pool_choice(state, "horny", self.horny_lines or ["little bit"])
            state["message_count"] += 1
            state["bot_messages"].append(reply)
            return self._apply_style(reply, state, msg)

        # PATH B: FLIRTY keywords → flirty_reply.txt
        if match_flirty(msg):
            self._trace("PATH B (FLIRTY): flirty_reply.txt")
            reply = self._pool_choice(state, "mc_flirty", self.mc_flirty_lines or _ANSWER_FOLLOWUP_POOL)
            state["message_count"] += 1
            state["bot_messages"].append(reply)
            return self._apply_style(reply, state, msg)

        # PATH C: NORMAL → warm_reply.txt
        self._trace("PATH C (NORMAL): warm_reply.txt")
        ack = self._ack_choice(state)
        state["message_count"] += 1
        state["bot_messages"].append(ack)
        return self._apply_style(ack, state, msg)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _try_detect_country(self, msg: str) -> Optional[str]:
        return _country_from_text(msg)

    def _match_horny_input(self, msg: str) -> Optional[Tuple[str, str]]:
        """(reply, rule_file) when *msg* triggers data/input/horny.txt, else
        None. Used by HORNY_DETECT (collection-independent, Section 5.8)."""
        return self.io_engine.find_match(
            msg, exclude=set(self.io_engine.input_rules) - {"horny.txt"})

    def _wait_step_reply(self, msg: str, state: Dict[str, Any],
                           pool_name: str, fallback: str) -> str:
        """Diagram wait-step reply: prefer the pool's trigger match, else any
        pool line (session no-repeat). Never returns None/empty."""
        io = self.io_engine
        try:
            m = io.find_match(
                msg, exclude=set(io.input_rules) - {pool_name})
            if m and m[0]:
                return m[0]
        except Exception:
            pass
        try:
            line = io._pick_reply(
                pool_name, used_map=state.setdefault("io_used", {}))
            if line:
                return line
        except Exception:
            pass
        pool = io.output_replies.get(pool_name) or []
        if pool:
            return random.choice(pool)
        return fallback

    def _first_sms_reply(self, msg: str, state: Dict[str, Any],
                          gender_age: Optional[Tuple[str, Optional[int]]]) -> str:
        """User First SMS Router (spec: greeting / age-gender / country).

        Priority on the very first SMS only:
          1. age/gender ("m21", "hi m21" -> greeting SKIPPED) -> 02 + WAIT_COUNTRY
          2. country question ("from?") -> 03 + FLIRTY_ASK (main flow next)
          3. fast-track horny/flirty opener (existing behaviour, unchanged)
          4. greeting ("hi") -> 01 + WAIT_AGE_GENDER
          5. unknown -> 01 default. Never returns None/empty.
        """
        io = self.io_engine
        scan_ag = detect_age_gender(msg)
        has_age_gender = bool(scan_ag) or bool(gender_age and gender_age[0])
        has_country_q = detect_country_question(msg)
        has_greeting = detect_greeting(msg)

        def _pick(pool_name: str, fallback: str) -> str:
            try:
                line = io._pick_reply(
                    pool_name, used_map=state.setdefault("io_used", {}))
            except Exception:
                line = None
            if line:
                return line
            pool = io.output_replies.get(pool_name) or []
            if pool:
                return random.choice(pool)
            return fallback

        if has_age_gender:
            m = io.find_match(msg,
                              exclude=set(io.input_rules) - {"age_gender.txt"})
            reply = m[0] if m else _pick("age_gender.txt", "f 20")
            if scan_ag:
                if scan_ag.get("gender") and not state.get("user_gender"):
                    state["user_gender"] = scan_ag["gender"]
                if scan_ag.get("age") is not None and state.get("user_age") is None:
                    state["user_age"] = scan_ag["age"]
            state["route"] = "FIRST_AGE_GENDER"
            state["stage"] = "WAIT_COUNTRY"
            self._trace(f"pattern: FIRST_AGE_GENDER route=FIRST_AGE_GENDER -> age_gender.txt stage=WAIT_COUNTRY age={state.get('user_age')} gender={state.get('user_gender')}")
            return reply or "f 20"
        if has_country_q:
            m = io.find_match(msg,
                              exclude=set(io.input_rules) - {"country.txt"})
            reply = m[0] if m else _pick("country.txt", "uk wbu")
            state["route"] = "FIRST_COUNTRY"
            state["stage"] = "FLIRTY_Q"
            state["country_replied"] = True
            state["flirty_state"] = "FLIRTY_ASK"
            state["sequence_step"] = 1
            self._trace("pattern: FIRST_COUNTRY route=FIRST_COUNTRY -> country.txt stage=FLIRTY_Q")
            return reply or "uk wbu"
        m = io.find_match(msg,
                          exclude=set(io.input_rules) - {"country.txt"})
        if m:
            state["route"] = "FIRST_COUNTRY"
            state["stage"] = "FLIRTY_Q"
            state["country_replied"] = True
            state["flirty_state"] = "FLIRTY_ASK"
            state["sequence_step"] = 1
            self._trace(f"pattern: FIRST_COUNTRY route=FIRST_COUNTRY -> country.txt stage=FLIRTY_Q trigger={m[1]!r}")
            return m[0] or "uk wbu"
        # ---- FAST-TRACK: a direct horny/flirty opener skips the intro ----
        # Stage 0 box "DIRECT FLIRT/SEX": a first SMS aimed at the snap/intro
        # (horny trigger, or flirty keyword directed at the bot) jumps straight
        # to the mid-chat pools instead of the greeting/age/country sequence.
        if self._match_horny_input(msg):
            state["horny_detected"] = True
            state["greeting_done"] = True
            state["flirty_state"] = "ASKSC"
            state["sequence_step"] = 7
            state["route"] = "FT-HORNY"
            state["stage"] = "ASKSC"
            self._trace("pattern: FT-HORNY (first SMS) -> horny pool, state ASKSC")
            return self._pool_choice(
                state, "horny",
                self.mc_horny_lines or self.horny_lines or ["little bit 😏"])
        if self._match_flirty_input(msg):
            state["greeting_done"] = True
            state["flirty_state"] = "MIDDLE"
            state["sequence_step"] = 4
            state["route"] = "FT-FLIRTY"
            state["stage"] = "MIDDLE"
            self._trace("pattern: FT-FLIRTY (first SMS) -> flirty pool, state MIDDLE")
            return self._pool_choice(
                state, "mc_flirty",
                self.mc_flirty_lines or _ANSWER_FOLLOWUP_POOL)
        m = io.find_match(msg,
                          exclude=set(io.input_rules) - {"greeting.txt"})
        if m:
            state["route"] = "FIRST_GREETING"
            state["stage"] = "WAIT_AGE_GENDER"
            self._trace(f"pattern: FIRST_GREETING route=FIRST_GREETING -> greeting.txt stage=WAIT_AGE_GENDER trigger={m[1]!r}")
            return m[0] or "hlw,,"
        if has_greeting:
            state["route"] = "FIRST_GREETING"
            state["stage"] = "WAIT_AGE_GENDER"
            self._trace("pattern: FIRST_GREETING route=FIRST_GREETING -> greeting.txt stage=WAIT_AGE_GENDER (scanner)")
            return _pick("greeting.txt", "hlw,,")
        state["route"] = "FIRST_GREETING"
        state["stage"] = "WAIT_AGE_GENDER"
        self._trace("pattern: FIRST_GREETING route=FIRST_GREETING -> greeting.txt default stage=WAIT_AGE_GENDER")
        if self.greet_lines:
            return random.choice(self.greet_lines)
        try:
            return _pick("greeting.txt", "hlw,,")
        except Exception:
            pass
        return "hlw,,"

    def _match_flirty_input(self, msg: str) -> Optional[str]:
        """PATH B (FLIRTY) keyword detector — flirty category (compliment /
        affection / date / tease / single) when *msg* carries a flirty
        keyword, else None. Works off the FLIRTY_KEYWORDS table in
        chat/horny_flirty_db.py.

        Only a message DIRECTED at the bot counts ("ur cute" / "u single?" /
        "u look good" / "i like you") or an explicit opener ("kiss me" /
        "wanna fun"); a bare emotion word ("i love gaming", "i want pizza")
        keeps the react/topic path instead of triggering PATH B."""
        if match_flirty(msg) is None:
            return None
        m = msg.strip().lower()
        directed = bool(re.search(r"\b(u|you|ur|your)\b", m))
        explicit = any(t in m for t in ("kiss me", "wanna fun", "tryna fun",
                                        "date me"))
        return None if not (directed or explicit) else match_flirty(msg)

    def _classify_intent(self, msg: str) -> str:
        """Section 5.8 STEP 1 — classify an unmatched message's intent.

        Returns one of QUESTION / AGREEMENT / REACTION / NEW_TOPIC /
        CONTINUATION. NEW_INFO is handled by the scanner data (gender_age /
        country), not here; FLIRT/HORNY is handled by the horny.txt matcher.
        """
        if _is_question(msg):
            return "QUESTION"
        t = msg.strip().lower()
        if len(re.findall(r"[a-z']+", t)) <= 4:
            if _AGREEMENT_RE.match(t):
                return "AGREEMENT"
            if _REACTION_RE.match(t):
                return "REACTION"
        if _TOPIC_RE.search(t):
            return "NEW_TOPIC"
        return "CONTINUATION"

    def _identity_collected(self, state: Dict[str, Any]) -> bool:
        """True when any identity field (age/gender/country) is known."""
        return (state.get("user_age") is not None
                or bool(state.get("user_gender"))
                or bool(state.get("user_country")))

    def _flirty_choice(self, state: Dict[str, Any]) -> str:
        """Pick a flirty question, avoiding lines already used this conversation."""
        pool = self.flow_questions or FLOWS[state["flow_type"]]["step4"]
        recent = state.setdefault("recent_flirty", [])
        fresh = [q for q in pool if q not in recent] or pool
        pick = random.choice(fresh)
        recent.append(pick)
        del recent[:-5]
        return pick

    def _ack_choice(self, state: Dict[str, Any]) -> str:
        """NORMAL path reply — generic warm ack.

        Preferences ``data/output/middle_chat/warm_reply.txt`` over the built-in
        ``_ANSWER_ACK_POOL``. History-aware: never repeats any line in this
        conversation (pool exhaustion wraps to start).
        """
        recent = state.setdefault("recent_acks", [])
        pool = self.mc_normal_lines or _ANSWER_ACK_POOL
        fresh = [a for a in pool if a not in recent] or pool
        pick = random.choice(fresh)
        recent.append(pick)
        return pick

    def _agreement_ack_choice(self, state: Dict[str, Any]) -> str:
        """Pick an AGREEMENT-flavoured ack (5.8), sharing the ack history so
        the same warm line is never said twice in a row."""
        recent = state.setdefault("recent_acks", [])
        fresh = ([a for a in _AGREEMENT_ACK_POOL if a not in recent]
                 or _AGREEMENT_ACK_POOL)
        pick = random.choice(fresh)
        recent.append(pick)
        del recent[:-3]
        return pick

    def _agreement_reply(self, state: Dict[str, Any]) -> str:
        """A34 escalating AGREEMENT reply (streak counter).

        First agreements get a warm ack; once the streak reaches 3 the bot
        pivots to a new-topic question (no-repeat pool) so a string of
        "me too" never loops on the same line. Any non-agreement message
        resets the streak (done in ``_context_reply``).
        """
        streak = state.get("agreement_streak", 0) + 1
        state["agreement_streak"] = streak
        if streak >= 3:
            self._trace(f"agreement: streak={streak} -> new-topic breaker")
            return self._pool_choice(state, "agree_break",
                                     _AGREEMENT_BREAKER_POOL)
        self._trace(f"agreement: ack (streak={streak})")
        return self._agreement_ack_choice(state)

    def _info_ack(self, state: Dict[str, Any],
                  detected_country: Optional[str]) -> str:
        """Warm ack for a completed identity collection (flow fix A37).

        Echoes the just-given country when there is one, else falls back to a
        generic line. No repeat within a conversation.
        """
        name = (detected_country or state.get("user_country")
                or "").strip().lower()
        if name:
            pool = [f"oh nice, {name} 😍", f"u from {name}? cutee hehe",
                    f"no way, {name}? i like that lol",
                    f"{name}? big vibe ngl"]
            return self._pool_choice(state, "info_ack", pool)
        return self._pool_choice(state, "info_ack", _INFO_ACK_POOL)

    def _followup_choice(self, state: Dict[str, Any]) -> str:
        """Teasing follow-up when the user answers the bot's flirty question
        (flow fix A37). Shares the ack history so no line repeats in a row."""
        recent = state.setdefault("recent_acks", [])
        fresh = ([x for x in _ANSWER_FOLLOWUP_POOL if x not in recent]
                 or _ANSWER_FOLLOWUP_POOL)
        pick = random.choice(fresh)
        recent.append(pick)
        del recent[:-3]
        return pick

    def _pool_choice(self, state: Dict[str, Any], key: str, pool: List[str]) -> str:
        """Pick a line from a pool, never repeating within this conversation.

        When the whole pool has been used once, reuse from the start.
        Resolves {snap} placeholders before returning.
        """
        used = state.setdefault(f"{key}_used", [])
        fresh = [x for x in pool if x not in used] or pool
        pick = random.choice(fresh)
        used.append(pick)
        # Track which pool file the reply came from for debug output.
        _POOL_FILE_MAP = {
            "mc_flirty": "middle_chat/flirty_reply.txt",
            "mc_horny": "middle_chat/horny_reply.txt",
            "mc_wait_nudge": "middle_chat/busy_later.txt",
            "mc_topic_shift": "middle_chat/new_topic.txt",
        }
        if key in _POOL_FILE_MAP:
            self.io_engine.last_picked_file = _POOL_FILE_MAP[key]
            try:
                self.io_engine.last_picked_line_num = pool.index(pick) + 1
            except ValueError:
                self.io_engine.last_picked_line_num = 0
        return _resolve_snap_reply(pick)

    def _session_unused_line(self, state: Dict[str, Any], used_key: str,
                             producer) -> str:
        """Ask ``producer`` for a line, skipping lines already used in this
        conversation (Rule 5: no repeats within a session).

        Bounded retries: up to 20 attempts, then accept any line.
        """
        used = state.setdefault(used_key, [])
        reply = producer()
        for _ in range(20):
            if reply not in used:
                break
            reply = producer()
        used.append(reply)
        return reply

    def _collect_missing_line(self, state: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """GREETING collection: ask age -> gender -> country, one per turn.

        Each question is asked at most once (ask-once guards). Returns None once
        every item is collected or every ask is exhausted, so the machine can
        fall through to HORNY_DETECT.
        """
        if state.get("user_age") is None and not state.get("age_asked"):
            state["age_asked"] = True
            pick = random.choice(_AGE_ASK_POOL)
            self.io_engine.last_picked_file = "age_ask.txt"
            try:
                self.io_engine.last_picked_line_num = _AGE_ASK_POOL.index(pick) + 1
            except ValueError:
                self.io_engine.last_picked_line_num = 0
            return pick, "age"
        if not state.get("user_gender") and not state.get("gender_asked"):
            state["gender_asked"] = True
            pick = random.choice(_GENDER_ASK_POOL)
            self.io_engine.last_picked_file = "gender_ask.txt"
            try:
                self.io_engine.last_picked_line_num = _GENDER_ASK_POOL.index(pick) + 1
            except ValueError:
                self.io_engine.last_picked_line_num = 0
            return pick, "gender"
        return None

    def _flirty_lines(self, state: Dict[str, Any]) -> str:
        """FLIRTY_ASK / MIDDLE fallback: ONE random flirty question.

        Flow fix — always a single question (the ~30% two-line join was
        removed after the "bored too? do u prefer being chased slow or caught
        fast?" compound line report). No line repeats within a conversation.

        Smart flow influence: 70% from the main flirty_questions.txt pool,
        30% from the flow-specific step4 pool for variety.
        """
        used = state.setdefault("flirty_used", [])
        flow_pool = FLOWS[state["flow_type"]]["step4"]
        main_pool = self.flow_questions or []
        # Mix: 70% main pool, 30% flow-specific for variety
        if main_pool and random.random() < 0.7:
            pool = main_pool
        else:
            pool = flow_pool or main_pool
        fresh = [q for q in pool if q not in used] or pool
        pick = random.choice(fresh)
        used.append(pick)
        # Track for debug output.
        self.io_engine.last_picked_file = "flirty_questions.txt"
        try:
            self.io_engine.last_picked_line_num = pool.index(pick) + 1
        except ValueError:
            self.io_engine.last_picked_line_num = 0
        return pick

    def _middle_flirty(self, state: Dict[str, Any]) -> str:
        """FLIRTY path reply for MIDDLE_CHAT fallback.

        Prefers ``data/output/middle_chat/flirty_reply.txt``; when that file is
        missing it falls back to a single flirty question from the FLIRTY_ASK
        pool (Section 5.8 behaviour). No repeat within a conversation.
        """
        pool = self.mc_flirty_lines
        if pool:
            return self._pool_choice(state, "mc_flirty", pool)
        return self._flirty_lines(state)

    def _middle_reply(
        self,
        msg: str,
        state: Dict[str, Any],
        gender_age: Optional[Tuple[str, Optional[int]]],
        detected_country: Optional[str],
        flirty_fallback: bool = True,
    ) -> str:
        """4B / MIDDLE_CHAT loop — classify the unmatched message and route it
        into the Section 5.8 context reply.

        CONTINUATION / NEW_TOPIC statements are answered from the last-message
        context via ``_context_reply()`` — never with an identity re-ask and
        never by resetting back to GREETING collection. The machine advances
        to ASKSC afterwards.
        """
        intent = self._classify_intent(msg)
        if intent in ("CONTINUATION", "NEW_TOPIC"):
            self._trace(f"middle: {intent} -> _context_reply()")
        else:
            self._trace(f"middle: intent={intent} -> _context_reply()")
        return self._context_reply(msg, state, gender_age, detected_country,
                                   flirty_fallback)

    def _context_reply(
        self,
        msg: str,
        state: Dict[str, Any],
        gender_age: Optional[Tuple[str, Optional[int]]],
        detected_country: Optional[str],
        flirty_fallback: bool = True,
    ) -> str:
        """Section 5.8 context reply — answer from the last-message context.

        Order: AGREEMENT (A34 escalating, never a loop) -> answer-frame (the
        user answers our last question: horny answer -> horny_reply.txt,
        flirty answer -> flirty_reply.txt, else react / follow-up / warm ack)
        -> persona question answer -> topic statement react -> stage-rule
        brain -> a warm ack (or a fresh flirty question when
        ``flirty_fallback``). NEVER asks for profile fields.

        ``flirty_fallback=False`` (GREETING context replies / minor-safe
        replies) ends in a warm ack instead of a premature flirty line.
        """
        intent = self._classify_intent(msg)
        post_collect = state.get("flirty_state") != "GREETING"
        awaiting_reply = (state.get("awaiting_answer")
                          and not _is_question(msg))
        # PATH D (DRY/BORED -> new_topic.txt) + PATH E (BUSY -> busy_later.txt).
        # Flow-map Stage 3: low-energy "ok/hmm/meh" shifts topics; a user who is
        # busy/leaving gets a defer line instead of a fresh question. Both only
        # apply post-collection and never on an actual question or profile info.
        if post_collect and not gender_age and not detected_country \
                and not _is_question(msg):
            if _BUSY_RE.search(msg):
                self._trace(
                    "PATH E (BUSY): keyword hit -> middle_chat/busy_later.txt")
                return self._pool_choice(
                    state, "mc_wait_nudge",
                    self.mc_wait_nudge_lines or _WAIT_NUDGE_POOL)
            if not awaiting_reply and _DRY_RE.match(msg):
                self._trace(
                    "PATH D (DRY): keyword hit -> middle_chat/new_topic.txt")
                return self._pool_choice(
                    state, "mc_topic_shift",
                    self.mc_topic_shift_lines or _TOPIC_SHIFT_POOL)
        # When the user ANSWERS the bot's own flirty question ("do u prefer
        # being chased slow or caught fast?" -> "yes"/"slow") the reply is a
        # teasing follow-up, NOT a generic agreement ack (flow fix A37).
        agreement_answered = (intent == "AGREEMENT" and awaiting_reply
                              and post_collect)
        if (intent == "AGREEMENT" and not agreement_answered
                and not gender_age and not detected_country
                and not _is_question(msg)):
            horny_level_now = match_horny(msg)
            if horny_level_now != "strong":
                if state.get("react_count", 0) < 3:
                    reacted = react_to_statement(msg)
                    if reacted:
                        state["react_count"] = state.get("react_count", 0) + 1
                        self._trace(
                            f"agreement+react: {msg!r} -> {reacted!r}")
                        return reacted
                return self._agreement_reply(state)
        state["agreement_streak"] = 0

        if awaiting_reply and not gender_age and not detected_country:
            # FLIRTY-ANSWER 3-type detect: the user answers our flirty
            # question — horny answer -> horny_reply.txt, flirty answer ->
            # flirty_reply.txt, anything else falls to the normal handling
            # below (react / follow-up / warm ack from warm_reply.txt).
            horny_level_now = match_horny(msg)
            if horny_level_now == "strong":
                self._trace(
                    "answer-frame: horny answer -> "
                    "middle_chat/horny_reply.txt")
                return self._pool_choice(
                    state, "mc_horny",
                    self.mc_horny_lines or self.horny_lines
                    or ["little bit 😏"])
            if (post_collect and flirty_fallback
                    and len(msg.strip().split()) <= 7
                    and self._match_flirty_input(msg)):
                self._trace(
                    "answer-frame: flirty answer -> "
                    "middle_chat/flirty_reply.txt")
                return self._pool_choice(
                    state, "mc_flirty",
                    self.mc_flirty_lines or _ANSWER_FOLLOWUP_POOL)
            if horny_level_now != "strong":
                if state.get("react_count", 0) < 3:
                    reacted = react_to_statement(msg)
                    if reacted:
                        state["react_count"] = state.get("react_count", 0) + 1
                        self._trace(
                            f"answer-frame: {msg!r} -> react {reacted!r}")
                        return reacted
                if post_collect:
                    self._trace(
                        f"answer-frame: {msg!r} answered flirty question -> follow-up")
                    return self._followup_choice(state)
                self._trace(f"answer-frame: {msg!r} -> ack")
                return self._ack_choice(state)
        # PATH B (FLIRTY) keyword detector (flow-map): a fresh flirty line in
        # post-collection ("ur cute" / "u single?" / "u look good" /
        # "i like you" / "kiss me") answers with the middle_chat/flirty_reply.txt
        # fixed reply. Only for short flirty lines and only after the
        # agreement / answer-frame blocks, so it never hijacks the info
        # exchange or the pending answer to our own flirty question.
        if (post_collect and flirty_fallback and not gender_age
                and not detected_country
                and len(msg.strip().split()) <= 7):
            flirty_cat = self._match_flirty_input(msg)
            if flirty_cat:
                self._trace(
                    f"PATH B (FLIRTY): {flirty_cat} keyword hit -> "
                    f"middle_chat/flirty_reply.txt")
                return self._pool_choice(
                    state, "mc_flirty",
                    self.mc_flirty_lines or _ANSWER_FOLLOWUP_POOL)
        if _is_question(msg):
            per = answer_user(msg)
            if per:
                key, p_answer = per
                cache_key = f"persona|{key}"
                cached = state["identity_answers"].get(cache_key)
                if cached:
                    p_answer = cached
                    self._trace(f"persona: {msg!r} same question -> reuse {p_answer!r}")
                else:
                    state["identity_answers"][cache_key] = p_answer
                    self._trace(f"persona: {msg!r} -> {p_answer!r}")
                return p_answer
        if (state.get("react_count", 0) < 3
                and not gender_age and not detected_country
                and not _is_question(msg)):
            reacted = react_to_statement(msg)
            if reacted:
                state["react_count"] = state.get("react_count", 0) + 1
                self._trace(f"react: {msg!r} -> {reacted!r}")
                return reacted
        # Smart memory-aware follow-up: if we know the user's facts, use them
        # for more personalized context replies instead of generic brain/ack.
        user_facts = state.get("user_facts", {})
        if (post_collect and user_facts and not gender_age
                and not detected_country and not _is_question(msg)):
            # Pick a random known fact to reference for variety
            fact_keys = list(user_facts.keys())
            if fact_keys:
                chosen_key = random.choice(fact_keys)
                chosen_val = user_facts[chosen_key]
                self._trace(f"memory-followup: fact {chosen_key}={chosen_val!r}")
                # Generate a contextual ack that references the known fact
                acks = [
                    f"that's cool {chosen_val} sounds fun",
                    f"nice i like that about u",
                    f"ok that's actually really cool",
                    f"love that for u",
                    f"haha that's awesome",
                ]
                return random.choice(acks)
        if self.rule_brain is not None:
            brain_reply = self.rule_brain.match(msg)
            bots = state["bot_messages"]
            loop = (len(bots) >= 2
                    and brain_reply == bots[-1] == bots[-2])
            if brain_reply and not loop:
                self._trace(f"brain: -> {brain_reply!r}")
                return brain_reply
        self._trace("middle: fallback -> FLIRTY path reply"
                    if flirty_fallback
                    else "middle: fallback -> warm ack (never a profile ask)")
        return (self._middle_flirty(state) if flirty_fallback
                else self._ack_choice(state))

    def _apply_style(self, reply: str, state: Dict[str, Any], user_msg: str) -> str:
        reply = self._inject_memory(reply, state)
        profile = state.get("style_profile")
        if profile is None:
            return reply
        return mirror_reply(reply, profile, user_message=user_msg)

    def _inject_memory(self, reply: str, state: Dict[str, Any]) -> str:
        """Stage 4 dynamic selector — inject stored memory into reply text.

        ``{name}`` / ``{age}`` placeholders in any reply line are replaced
        with the captured value when known; ``{country}`` is filled for pools
        that rely on the generic token (the routed country replies use their
        own geo rotator). Remembered facts (``{hobby}`` / ``{music}`` /
        ``{pet}`` / ``{job}`` / ``{fav}``) are filled from the conversation
        facts captured by the context memory engine. ``{snap}`` is replaced
        with a random snap message. Unknown values leave the placeholder
        untouched so a line can never be returned half-processed.
        """
        if "{" not in reply:
            return reply
        if "{snap}" in reply:
            reply = _resolve_snap_reply(reply)
        name = state.get("user_name")
        if name:
            reply = reply.replace("{name}", name).replace("{Name}", name)
        age = state.get("user_age")
        if age is not None:
            reply = reply.replace("{age}", str(age))
        country = state.get("user_country")
        if country:
            reply = reply.replace("{country}", country)
        for key in ("hobby", "music", "pet", "job", "fav"):
            value = state.get("user_facts", {}).get(key)
            if value:
                reply = reply.replace("{" + key + "}", str(value))
        return reply
