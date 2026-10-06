#!/usr/bin/env python3
"""eva_flow.py — EVA simplified funnel engine (diagram-exact, symmetric categories).

CORE IDEA (user-customizable, no code edits needed):

    data/input/<category>.txt   = WHAT THE USER SAYS  (trigger keywords, 1 per line)
    data/output/<category>.txt  = WHAT THE BOT REPLIES (reply lines, 1 per line)

    Same file name = same category.
    User SMS -> matched against input/<category>.txt lines
             -> category detected (+ stage/logic rules)
             -> reply picked from output/<category>.txt

Example:
    user: "m 21"  -> matches a line in input/age_gender.txt
                 -> reply from output/age_gender.txt ("f19")

Multiple detection layers (if one fails, the next one works):
    1. exact line match (raw + punctuation-stripped)
    2. word-boundary substring match (trigger >= 3 chars)
    3. slang-normalized match (u->you, wats->what is, ...)
    4. token-subset match (all trigger words present, any order)
    5. smart fallbacks: gender+age regex, country keywords
       (data/countries.txt), positive-snap regex

Extra rules kept from the original bot:
    * NO-REPEAT (pool level): every output txt pool fires AT MOST ONCE per
      conversation (greeting/age_gender/country/warm_reply/... never twice
      for the same user). Exceptions (repeat with a DIFFERENT line):
      flirty_questions.txt (funnel driver), horny.txt and
      middle_chat/flirty_reply.txt — "jokhon-i detect hobe" pools that
      keep the ask-snap loop alive.
    * MEMORY: the full conversation is kept line-by-line in
      state["transcript"] ({"n": 1, "who": "user"|"bot", "text": ...}),
      plus user_messages / bot_messages / pools_used / last_bot_pool,
      so the bot always knows what was already said.
    * HORNY CHAIN (strict): horny reply -> whatever the user says next ->
      ask_snap.txt -> positive (yes/ok/k/@/share me/whats ur ...) ->
      share_snap.txt -> END. Refusal -> one retry_nudge, second refusal
      -> polite END.
    * no line repeats inside one conversation (until pool exhausted)
    * snap usernames cycle round-robin through data/snap_ids.txt
    * %username% / {snap} / {country} placeholders in reply lines
    * reply cap (default 8) -> output/closer.txt -> END
    * revealed age < 18 permanently blocks snap sharing
    * "#" comment lines in input files are ignored
    * any NEW category pair the user adds (input/foo.txt + output/foo.txt)
      automatically joins the matching order
"""

from __future__ import annotations

import os
import re
import random
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_ROOT, "data")
INPUT_DIR = os.path.join(DATA_DIR, "input")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
SNAP_IDS_FILE = os.path.join(DATA_DIR, "snap_ids.txt")
SNAP_IDS_IDX = os.path.join(DATA_DIR, ".snap_ids.idx")
COUNTRIES_FILE = os.path.join(DATA_DIR, "countries.txt")

# After this many bot replies the bot says goodbye (closer.txt) and ends.
MAX_BOT_REPLIES = int(os.environ.get("EVA_MAX_REPLIES", "8"))

# ---------------------------------------------------------------------------
# Category system
# ---------------------------------------------------------------------------
# Matching order when scanning input/ categories (first match wins).
# Any input file NOT listed here is appended after these (custom categories).
CATEGORY_PRIORITY: List[str] = [
    "share_snap.txt",               # positive reply -> share snap -> END
    "snapchat.txt",                 # user offers their snap -> share -> END
    "horny.txt",                    # horny questions + explicit keywords
    "middle_chat/flirty_reply.txt", # flirty keywords
    "how_are_you.txt",
    "your_age.txt",
    "english_only.txt",             # strict matching (no slang passes)
    "country.txt",                  # country QUESTIONS ("where u from")
    "greeting.txt",
    "age_gender.txt",               # "m21" style (stage-gated)
    "middle_chat/busy_later.txt",   # leaving / busy words
    "middle_chat/warm_reply.txt",   # short acks / reactions
]

# Categories that end the chat with a snap share.
TERMINAL_CATEGORIES = {"share_snap.txt", "snapchat.txt"}

# Files in output/ that are never used as reply pools.
NON_POOL_OUTPUT_FILES = {"info.txt"}

# NO-REPEAT RULE: every output txt pool is used AT MOST ONCE per
# conversation/user (greeting, age_gender, country, warm_reply,
# ... never fire twice for the same person).  Only the pools
# listed here may be reused (with a DIFFERENT line each time —
# line-level no-repeat still applies):
#   flirty_questions.txt        — the funnel driver (huge pool)
#   horny.txt                   — "jokhon-i horny detect hobe" -> horny
#                                 reply, then the ask chain re-fires
#   middle_chat/flirty_reply.txt— "jokhon-i flirty detect hobe" -> flirty
#                                 reply, then the ask chain re-fires
#   ask_snap.txt                — the ask may RE-FIRE after a repeated
#                                 horny/flirty turn (different line)
#   closer.txt                  — END-state goodbye (chat is over anyway)
REPEATABLE_POOLS = {"flirty_questions.txt", "closer.txt",
                    "horny.txt", "middle_chat/flirty_reply.txt",
                    "ask_snap.txt", "horny_answer.txt"}

# Categories handled exclusively by their own dedicated logic — never
# via the regular category scan (horny_answer = the 2-line question
# reply path below).
SPECIAL_CATEGORIES = {"horny_answer.txt"}

# Leet/typo fallback for direct horny QUESTIONS — only applied when the
# message carries a "?" ("h0rny?", "dtf??", "in the m0od?" ...).
_HORNY_QUESTION_LEET_RE = [
    re.compile(r"\bh[o0*]rn[yi1]\b"),
    re.compile(r"\bdtf\b"),
    re.compile(r"\bturned? on\b"),
    re.compile(r"\bin (the )?m[o0]od\b"),
    re.compile(r"\bu down\b"),
    re.compile(r"\bwet\b"),
]

# ADVANCED DETECTION: leet/obfuscated horny keywords (h0rny, s3x, n0des,
# f*ck ...) caught by regex on top of the txt trigger files.
_HORNY_LEET_RE = [
    re.compile(r"\bh[o0*]rn[yi1]\b"),
    re.compile(r"\bs[e3]x[yi]?\b"),
    re.compile(r"\bs[e3]xt(ing|in|me)?\b"),
    re.compile(r"\bn[o0]{1,2}d[e3]s?\b"),
    re.compile(r"\bd[i1]ck\b"),
    re.compile(r"\bc[o0]ck\b"),
    re.compile(r"\bp[u*]ss[yi]?\b"),
    re.compile(r"\bc[u*]m\b"),
    re.compile(r"\bf[u*]{1,2}ck\w*\b"),
    re.compile(r"\bnsfw\b"),
    re.compile(r"\b18\+\b"),
    re.compile(r"\bo[n]?ly ?fans\b"),
    re.compile(r"\bdtf\b"),
    re.compile(r"\bmake ?out\b"),
    re.compile(r"\bhook ?up\b"),
]

# Stages: NEW -> WAIT_AGE_GENDER -> WAIT_COUNTRY -> MIDDLE -> END


# ---------------------------------------------------------------------------
# File loading helpers
# ---------------------------------------------------------------------------

def _load_triggers(path: str) -> List[str]:
    """Input file -> trigger lines. One trigger per line; '#' = comment."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
    except OSError:
        return []
    return [t.strip().lower() for t in re.split(r"[\r\n]+", raw)
            if t.strip() and not t.strip().startswith("#")]


def _load_replies(path: str) -> List[str]:
    """Output file -> reply lines. One reply per line ('|' also splits)."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
    except OSError:
        return []
    return [r.strip() for r in re.split(r"[\r\n|]+", raw) if r.strip()]


class _RoundRobin:
    """Persistent round-robin over a txt file (snap usernames)."""

    def __init__(self, path: str, idx_path: str, fallback: List[str]) -> None:
        self.path = path
        self.idx_path = idx_path
        self.fallback = fallback or ["eva_chill"]
        self._items = _load_replies(path)
        self._index = 0
        try:
            with open(idx_path, "r", encoding="utf-8") as f:
                self._index = int(f.read().strip() or "0")
        except (OSError, ValueError):
            pass

    def next(self) -> str:
        items = self._items or self.fallback
        item = items[self._index % len(items)]
        self._index = (self._index + 1) % max(len(items), 1)
        try:
            with open(self.idx_path, "w", encoding="utf-8") as f:
                f.write(str(self._index))
        except OSError:
            pass
        return item


# ---------------------------------------------------------------------------
# Text normalization (slang/typo expansion — understanding only, never
# applied to the reply text itself)
# ---------------------------------------------------------------------------
_SLANG_TOKEN_MAP = {
    "u": "you", "ur": "your", "yr": "your", "yur": "your",
    "r": "are", "im": "i am", "ims": "i am",
    "wat": "what", "wats": "what is", "wut": "what", "wts": "what is",
    "whats": "what is", "what's": "what is",
    "whr": "where", "wbu": "what about you", "wby": "what about you",
    "hbu": "how about you", "sc": "snapchat",
}

_TOKEN_SPLIT = re.compile(r"[^a-z0-9+]+")

def _norm_tokens(text: str) -> List[str]:
    return [_SLANG_TOKEN_MAP.get(t, t)
            for t in _TOKEN_SPLIT.split(text.lower()) if t]

def _norm_text(text: str) -> str:
    return " ".join(_norm_tokens(text))

def _simple_text(text: str) -> str:
    s = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(s.split())


# ---------------------------------------------------------------------------
# Trigger matcher — the 4 txt-line passes (multiple ways to detect)
# ---------------------------------------------------------------------------
class TriggerMatcher:
    """Matches a user message against the trigger lines of one input file.

    strict=True (english_only) disables the slang/subset passes so plain
    English messages are never hijacked by foreign-language triggers.
    """

    def __init__(self, triggers: List[str], strict: bool = False,
                 extra_patterns: Optional[List["re.Pattern[str]"]] = None
                 ) -> None:
        self.strict = strict
        # '#' lines are comments — never triggers (defense in depth: the
        # loader filters them too, but direct construction must be safe).
        self.exact = {t.strip().lower() for t in triggers
                      if t.strip() and not t.strip().startswith("#")}
        # ADVANCED DETECTION: extra regex layer (leet/obfuscated variants)
        self.extra = extra_patterns or []
        self.subpat: Dict[str, "re.Pattern[str]"] = {}
        self.norm: Dict[str, str] = {}
        self.norm_subpat: Dict[str, "re.Pattern[str]"] = {}
        self.subsets: Dict[str, frozenset] = {}
        for trig in self.exact:
            if len(trig) >= 3:
                self.subpat[trig] = re.compile(r"\b" + re.escape(trig) + r"\b")
            if strict:
                continue
            nt = _norm_text(trig)
            # Guard: skip normalized matching for triggers whose meaning
            # would be inflated by dropping punctuation ("ur @" would
            # normalize to just "your" and match every message with "ur").
            norm_tokens = nt.split()
            norm_safe = (len(norm_tokens) >= 2 or trig.isalnum())
            if norm_safe and len(nt) >= 3:
                self.norm[trig] = nt
                self.norm_subpat[trig] = re.compile(
                    r"\b" + re.escape(nt) + r"\b")
            tset = frozenset(t for t in _norm_tokens(trig) if len(t) >= 3)
            if len(tset) >= 2:
                self.subsets[trig] = tset

    def match(self, user_msg: str) -> Optional[str]:
        """Return the matched trigger (or None)."""
        raw = user_msg.lower().strip()
        simple = _simple_text(user_msg)

        # Pass 1: exact (raw / punctuation-stripped)
        if raw in self.exact or simple in self.exact:
            return raw if raw in self.exact else simple

        # Pass 2: word-boundary substring (trigger >= 3 chars)
        for trig, pat in self.subpat.items():
            if pat.search(raw) or pat.search(simple):
                return trig

        # Pass 2.5 (advanced): extra regex layer (leet/obfuscated words)
        for pat in self.extra:
            if pat.search(raw) or pat.search(simple):
                return "<leet:%s>" % pat.pattern

        if self.strict:
            return None

        norm_msg = _norm_text(user_msg)
        norm_set = set(_norm_tokens(user_msg))

        # Pass 3: normalized (slang-expanded) exact + substring
        for trig, nt in self.norm.items():
            if norm_msg == nt:
                return trig
            npat = self.norm_subpat.get(trig)
            if npat is not None and npat.search(norm_msg):
                return trig

        # Pass 4: token-subset (order-insensitive)
        for trig, tset in self.subsets.items():
            if any(len(t) >= 4 for t in tset) and tset.issubset(norm_set):
                return trig
        return None


# ---------------------------------------------------------------------------
# Smart fallback detectors (layer 5 — no txt file needed)
# ---------------------------------------------------------------------------
_GA_RE = [
    re.compile(r"\b([mf])[\s\-_. ]{0,2}(\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[\s\-_. ]{0,2}([mf])\b"),
]

# --- Snap share gating (ask first, share after the user's yes) ---
# The bot must ASK first (output/ask_snap.txt). Only then does a plain
# "yes" trigger the share. A user who asks for the snap directly
# ("ur sc?", "whats ur snap") gets the share immediately.
_SNAP_ASK_RE = re.compile(
    r"(\bsnap\b|\bsc\b|\bsnapchat\b|\bsnp\b|\bsn-?p\b|\bghost\b"
    r"|\binsta\b|\binstagram\b|\btelegram\b|\btg\b|\bdiscord\b|\bdc\b|@)")
# Firm refusal of snap ("dont have snap") — never triggers a share.
_SNAP_REFUSAL_RE = re.compile(
    r"\b(dont have|don't have|do not have|no snap|not on snap|not on sc"
    r"|deleted (my )?snap|no sc)\b")
# Bare yes-words — only count AFTER the bot's ask (asked_snap=True).
# Expanded per spec: yes / ok / k / yes i got / @ / share me / whats ur ...
_YES_RE = re.compile(
    r"\b(yes+|yes i got|i got it|i got u|got it|got u|"
    r"yea+h*|yep+|yeap*|yeop+|yopp|yas|"
    r"sure|ofc|of course|"
    r"ok+|okay+|okie|okies|okey|k+|"
    r"send|send it|send it over|drop|drop it|drop ur|"
    r"share|share me|gimme|give me|"
    r"here|here you go|here u go|"
    r"add me|added|done|deal|bet|say less|fr|"
    r"im down|i am down|down|"
    r"whats ur|what's ur|whats ur snap|whats ur sc|whats ur snapchat|"
    r"ur user|ur snap|ur sc|ur username)\b")
# Negative words — after the ask, these draw one retry_nudge.txt line.
_NEGATIVE_RE = re.compile(
    r"\b(no|nope|nah|naw|not really|never|later|nvm|nevermind)\b")

# Built-in country keywords — used only when data/countries.txt is missing.
_COUNTRY_FALLBACK = {
    "usa": "usa", "us": "usa", "america": "usa", "united states": "usa",
    "uk": "uk", "england": "uk", "united kingdom": "uk", "london": "uk",
    "canada": "canada", "australia": "australia", "germany": "germany",
    "france": "france", "india": "india", "bangladesh": "bangladesh",
    "pakistan": "pakistan", "brazil": "brazil", "mexico": "mexico",
}


def _gender_age_from_text(text: str) -> Optional[Tuple[str, int]]:
    for pat in _GA_RE:
        m = pat.search(text.lower())
        if m:
            g, a = m.group(1), m.group(2)
            if g.isdigit():
                a, g = g, a
            age = int(a)
            if 13 <= age <= 99:
                return g, age
    return None


def _load_countries(path: str) -> Dict[str, str]:
    """data/countries.txt -> {keyword: country}. Lines: 'keyword=country'.
    Falls back to the built-in map when the file is missing/empty."""
    table: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                kw, _, country = line.partition("=")
                kw, country = kw.strip().lower(), country.strip().lower()
                if kw and country:
                    table[kw] = country
    except OSError:
        pass
    return table or dict(_COUNTRY_FALLBACK)


# ---------------------------------------------------------------------------
# The flow bot
# ---------------------------------------------------------------------------
class EvaFlowBot:
    """Diagram-exact funnel bot with symmetric input/output categories.

    Usage:
        bot = EvaFlowBot()                       # or EvaFlowBot(root="...")
        state = bot.new_conversation()
        reply = bot.reply("hi", state)

    After each reply (debug info):
        bot.last_file   -> output file the reply came from
        bot.last_line   -> 1-based line number in that file
        bot.last_reason -> human-readable routing reason
    """

    def __init__(self, root: Optional[str] = None) -> None:
        data_dir = os.path.join(root, "data") if root else DATA_DIR
        input_dir = os.path.join(data_dir, "input")
        output_dir = os.path.join(data_dir, "output")
        snap_ids_file = os.path.join(data_dir, "snap_ids.txt")
        snap_ids_idx = os.path.join(data_dir, ".snap_ids.idx")

        # ---- input trigger matchers (category = file name) -------------
        self.matchers: Dict[str, TriggerMatcher] = {}
        if os.path.isdir(input_dir):
            for fname in sorted(os.listdir(input_dir)):
                if fname.lower().endswith(".txt"):
                    self.matchers[fname.lower()] = TriggerMatcher(
                        _load_triggers(os.path.join(input_dir, fname)),
                        strict=(fname.lower() == "english_only.txt"),
                        # leet/obfuscated horny detection layer
                        extra_patterns=(_HORNY_LEET_RE
                                        if fname.lower() == "horny.txt"
                                        else None))
        mc_in = os.path.join(input_dir, "middle_chat")
        if os.path.isdir(mc_in):
            for fname in sorted(os.listdir(mc_in)):
                if fname.lower().endswith(".txt"):
                    self.matchers["middle_chat/" + fname.lower()] = \
                        TriggerMatcher(
                            _load_triggers(os.path.join(mc_in, fname)))

        # full matching order: priority list + any custom categories
        known = set(self.matchers)
        self.scan_order: List[str] = ([c for c in CATEGORY_PRIORITY
                                       if c in known]
                                      + sorted(known - set(CATEGORY_PRIORITY)))

        # ---- output reply pools (same names as input categories) -------
        self.pools: Dict[str, List[str]] = {}
        if os.path.isdir(output_dir):
            for fname in os.listdir(output_dir):
                if (fname.lower().endswith(".txt")
                        and fname.lower() not in NON_POOL_OUTPUT_FILES):
                    self.pools[fname.lower()] = _load_replies(
                        os.path.join(output_dir, fname))
        mc_out = os.path.join(output_dir, "middle_chat")
        if os.path.isdir(mc_out):
            for fname in os.listdir(mc_out):
                if fname.lower().endswith(".txt"):
                    self.pools["middle_chat/" + fname.lower()] = _load_replies(
                        os.path.join(mc_out, fname))

        self.snap_ids = _RoundRobin(
            snap_ids_file, snap_ids_idx,
            _load_replies(os.path.join(data_dir, "snap_ids_fallback.txt")))
        self.countries = _load_countries(
            os.path.join(data_dir, "countries.txt"))

        # debug info for the last reply
        self.last_file: str = ""
        self.last_line: int = 0
        self.last_reason: str = ""

    # ------------------------------------------------------------------
    # Public API (drop-in compatible with ChatRuleBot)
    # ------------------------------------------------------------------
    def new_conversation(self) -> Dict[str, Any]:
        return {
            "stage": "NEW",
            "flirty_state": "NEW",     # legacy-compatible view of stage
            "flow_type": "diagram",     # legacy-compatible display field
            "user_gender": None,
            "user_age": None,
            "user_country": None,
            "blocked": False,          # underage -> never share snap
            "snap_pivoted": False,     # set True on snap share (browser ends chat)
            "asked_snap": False,       # bot already asked "u got snap?"
            "ask_next": False,         # ask snap on the next bot turn
            "snap_nudges": 0,          # retry nudges after a refusal
            "sequence_step": 0,        # funnel progress (1..8, logged by GUI)
            "bot_reply_count": 0,
            "misses": 0,               # stage-progression counter
            "io_used": {},             # {pool_file: [used lines]}
            "pools_used": [],          # output txt files already used ONCE
            "last_bot_pool": "",       # pool of the bot's previous reply
            "user_messages": [],       # every user line (memory)
            "bot_messages": [],        # every bot line (memory)
            "transcript": [],          # line-by-line memory:
                                        # [{"n":1,"who":"user","text":...}, ...]
            "pending_replies": [],     # multi-message replies (compat)
        }

    def reply(self, user_message: str, state: Dict[str, Any]) -> str:
        """Generate the bot reply for *user_message* (state mutates in place)."""
        result = self._reply_core(user_message, state)
        # legacy-compatible state view: flirty_state mirrors stage
        state["flirty_state"] = state["stage"]
        # MEMORY: record the bot's line (line-by-line transcript)
        if result:
            state.setdefault("bot_messages", []).append(result)
            transcript = state.setdefault("transcript", [])
            transcript.append({"n": len(transcript) + 1,
                               "who": "bot", "text": result})
            state["last_bot_pool"] = self.last_file
        # MEMORY: the 2-line horny-question reply (answer + queued
        # horny.txt line) is recorded too, so the transcript stays
        # line-by-line complete.
        for extra in state.get("pending_replies", []):
            state.setdefault("bot_messages", []).append(extra)
            transcript = state.setdefault("transcript", [])
            transcript.append({"n": len(transcript) + 1,
                               "who": "bot", "text": extra})
            state["last_bot_pool"] = "horny.txt"
        # After a horny reply or a flirty_reply the funnel pivots: the NEXT
        # bot turn asks for snap (output/ask_snap.txt) before any share.
        if self.last_file in ("horny.txt", "middle_chat/flirty_reply.txt"):
            state["ask_next"] = True
        return result

    def _reply_core(self, user_message: str,
                    state: Dict[str, Any]) -> str:
        lines = [l.strip() for l in user_message.split("\n") if l.strip()]
        if not lines:
            return ""
        # Multi-line message: only the LAST line drives the machine.
        msg = lines[-1]
        state["user_messages"].append(msg)
        # A new user message closes any unfetched pending line (the 2nd
        # line of the horny-question answer is fetched by the worker via
        # get_pending_replies right after the 1st line).
        state["pending_replies"] = []
        # MEMORY: line-by-line transcript of the whole conversation
        transcript = state.setdefault("transcript", [])
        transcript.append({"n": len(transcript) + 1,
                           "who": "user", "text": msg})

        # ---- ALWAYS: harvest facts from the message -------------------
        ga = _gender_age_from_text(msg)
        if ga:
            if not state.get("user_gender"):
                state["user_gender"] = ga[0]
            if state.get("user_age") is None:
                state["user_age"] = ga[1]
        country = self._country_from_text(msg)
        if country:
            state["user_country"] = country

        # ---- ALWAYS: underage block (18+) -----------------------------
        age = ga[1] if ga else state.get("user_age")
        if age is not None and age < 18:
            state["blocked"] = True

        # ---- ALWAYS: END means END ------------------------------------
        if state["stage"] == "END":
            self.last_reason = "END: chat over -> closer.txt"
            reply = self._pick("closer.txt", state)
            state["bot_reply_count"] += 1
            return reply

        # ---- ALWAYS: share gating (ask first, then share) -------------
        # (a) user asks for the snap directly ("ur sc?", "whats ur snap")
        #     -> share immediately
        # (b) plain yes-words ("yes", "yeop", ...) -> share ONLY after the
        #     bot's ask (ask_snap.txt); before that they are just normal
        #     chat answers (fixes: flirty question -> "yes" wrongly shared)
        if self._should_share(msg, state):
            state["stage"] = "END"
            state["snap_pivoted"] = True
            state["sequence_step"] = 8
            state["ask_next"] = False
            if state.get("blocked"):
                state["snap_pivoted"] = False
                self.last_reason = ("positive but blocked (underage) "
                                    "-> closer.txt")
                reply = self._pick("closer.txt", state)
            else:
                cat = self._match_category(
                    msg, only=("share_snap.txt", "snapchat.txt"))
                pool = cat if cat and self.pools.get(cat) else "share_snap.txt"
                self.last_reason = (f"positive reply -> {pool} -> END")
                reply = self._pick(pool, state)
            state["bot_reply_count"] += 1
            return reply

        # ---- ALWAYS: horny QUESTION -> 2-line reply -------------------
        # The user directly asks the bot ("horny?", "hrny?", "are u
        # horny?", "rn horny?", "u dtf?" ...) — anywhere in the chat
        # (first lines or middle chat):
        #   bot line 1: yes-answer from output/horny_answer.txt
        #   bot line 2: random horny.txt line (queued as pending reply)
        # After that the funnel continues exactly as before: next user
        # message (whatever it is) -> ask_snap.txt -> positive ->
        # share_snap.txt -> END.
        if self._is_horny_question(msg):
            state["horny_detected"] = True
            state["stage"] = "MIDDLE"
            # pick the 2nd line (horny.txt) FIRST so the answer's pick is
            # last — last_file/last_line then point at the main reply
            horny_line = self._pick("horny.txt", state)
            answer = self._pick("horny_answer.txt", state)
            state["pending_replies"] = [horny_line]
            state["ask_next"] = True
            self.last_reason = ("horny question -> horny_answer.txt + "
                                "horny.txt (2 lines) -> ask chain")
            state["bot_reply_count"] += 2
            return answer

        # ---- ALWAYS: pending ask (STRICT — whatever the user said) ----
        # After a horny / flirty_reply turn the bot's next line is ALWAYS
        # the snap ask (output/ask_snap.txt), no matter what the user
        # replied (per spec: "user ki bole ta dekhar dorkar nei").
        if state.get("ask_next"):
            state["ask_next"] = False
            state["asked_snap"] = True
            state["sequence_step"] = 7
            self.last_reason = ("ask snap -> ask_snap.txt "
                                "(whatever user said)")
            reply = self._pick("ask_snap.txt", state)
            state["bot_reply_count"] += 1
            return reply

        # ---- ALWAYS: refusal handling after the ask --------------------
        # "no/nope" right after the ask  -> one retry_nudge.txt line
        # refusing again after the nudge -> polite END (closer.txt)
        if (state.get("asked_snap")
                and state.get("last_bot_pool") in
                    ("ask_snap.txt", "retry_nudge.txt")
                and _NEGATIVE_RE.search(msg.lower())):
            if state.get("snap_nudges", 0) < 1:
                state["snap_nudges"] = state.get("snap_nudges", 0) + 1
                self.last_reason = "negative after ask -> retry_nudge.txt"
                reply = self._pick("retry_nudge.txt", state)
            else:
                state["stage"] = "END"
                self.last_reason = ("refused twice after ask -> "
                                    "closer.txt -> END")
                reply = self._pick("closer.txt", state)
            state["bot_reply_count"] += 1
            return reply

        # ---- stage machine --------------------------------------------
        stage = state["stage"]
        if stage == "NEW":
            reply = self._stage_first_sms(msg, state)
        elif stage == "WAIT_AGE_GENDER":
            reply = self._stage_wait_age_gender(msg, state)
        elif stage == "WAIT_COUNTRY":
            reply = self._stage_wait_country(msg, state)
        else:  # MIDDLE
            reply = self._stage_middle(msg, state)

        state["bot_reply_count"] += 1
        return reply

    def get_pending_replies(self, state: Dict[str, Any]) -> List[str]:
        """Compatible with ChatRuleBot; this flow sends one reply at a time."""
        pending = state.get("pending_replies", [])
        state["pending_replies"] = []
        return pending

    # ------------------------------------------------------------------
    # ChatRuleBot-compatible debug/introspection API (used by the GUI,
    # browser worker logs and tools/live_chat.py)
    # ------------------------------------------------------------------
    def last_picked_info(self) -> tuple:
        """(file_path, line_number) of the last picked reply line."""
        path = ""
        if self.last_file:
            path = os.path.join(OUTPUT_DIR, *self.last_file.split("/"))
        return (path, self.last_line)

    def last_debug_summary(self) -> str:
        """One-line 'why this reply' summary of the most recent decision."""
        return self.last_reason or "no reply yet"

    def last_debug_trace(self) -> List[str]:
        """Debug lines explaining the most recent reply decision."""
        return [self.last_debug_summary()]

    @property
    def pipe_db_size(self) -> int:
        return 0

    @property
    def evoflow_rule_count(self) -> int:
        return 0

    def stats(self) -> Dict[str, Any]:
        return {
            "engine": "eva_flow (diagram funnel)",
            "categories": len(self.matchers),
            "pools": len(self.pools),
            "snap_ids": len(self.snap_ids._items or self.snap_ids.fallback),
        }

    # ------------------------------------------------------------------
    # Stage 1: FIRST SMS -> greeting.txt
    # ------------------------------------------------------------------
    def _stage_first_sms(self, msg: str, state: Dict[str, Any]) -> str:
        # "m21" straight away: skip greeting
        if _gender_age_from_text(msg) or self._hit(msg, "age_gender.txt"):
            state["stage"] = "WAIT_COUNTRY"
            state["sequence_step"] = 2
            self.last_reason = "first SMS: age/gender -> age_gender.txt"
            return self._pick("age_gender.txt", state)
        # "where u from?" straight away: answer with own country
        if self._hit(msg, "country.txt"):
            state["stage"] = "WAIT_COUNTRY"
            state["sequence_step"] = 3
            self.last_reason = "first SMS: country question -> country.txt"
            return self._pick("country.txt", state)
        # default: greeting
        state["stage"] = "WAIT_AGE_GENDER"
        state["sequence_step"] = 1
        self.last_reason = "first SMS -> greeting.txt"
        return self._pick("greeting.txt", state)

    # ------------------------------------------------------------------
    # Stage 2: waiting for age/gender -> age_gender.txt
    # ------------------------------------------------------------------
    def _stage_wait_age_gender(self, msg: str, state: Dict[str, Any]) -> str:
        # NO-REPEAT: the age/gender pool fires only ONCE per conversation
        if (_gender_age_from_text(msg)
                and self._category_available("age_gender.txt", state)):
            state["stage"] = "WAIT_COUNTRY"
            state["misses"] = 0
            state["sequence_step"] = 2
            self.last_reason = "age/gender captured -> age_gender.txt"
            return self._pick("age_gender.txt", state)
        if (self._hit(msg, "country.txt")
                and self._category_available("country.txt", state)):
            state["stage"] = "WAIT_COUNTRY"
            state["misses"] = 0
            self.last_reason = "country talk -> country.txt"
            return self._pick("country.txt", state)
        cat = self._scan_category(msg, state, skip_terminal=True,
                                  skip_age_gender=True)
        if cat:
            state["misses"] += 1
            self.last_reason = f"category match -> {cat}"
            reply = self._pick(cat, state)
            if state["misses"] >= 2:
                # too many misses: move the funnel forward
                state["stage"] = "WAIT_COUNTRY"
                state["misses"] = 0
                nxt = ("country.txt" if self._category_available(
                    "country.txt", state) else "flirty_questions.txt")
                self.last_reason += f" | progressing -> {nxt}"
                reply = self._pick(nxt, state)
            return reply
        state["misses"] += 1
        if (state["misses"] >= 2
                or not self._category_available("age_ask.txt", state)):
            # asked twice already (or age_ask spent): swap to country
            state["stage"] = "WAIT_COUNTRY"
            state["misses"] = 0
            nxt = ("country.txt" if self._category_available(
                "country.txt", state) else "flirty_questions.txt")
            self.last_reason = f"age asks exhausted -> {nxt}"
            return self._pick(nxt, state)
        self.last_reason = "waiting age/gender -> age_ask.txt"
        return self._pick("age_ask.txt", state)

    # ------------------------------------------------------------------
    # Stage 3+4: country question -> country.txt,
    #            country answer   -> flirty_questions.txt
    # ------------------------------------------------------------------
    def _stage_wait_country(self, msg: str, state: Dict[str, Any]) -> str:
        # user answers with a country ("usa") -> flirty question
        if (self._country_from_text(msg)
                and not self._hit(msg, "country.txt")):
            state["stage"] = "MIDDLE"
            state["sequence_step"] = 4
            self.last_reason = "country captured -> flirty_questions.txt"
            return self._pick("flirty_questions.txt", state)
        # user asks where the bot is from -> own country line (once only)
        if (self._hit(msg, "country.txt")
                and self._category_available("country.txt", state)):
            self.last_reason = "country question -> country.txt"
            return self._pick("country.txt", state)
        # late age/gender (once only)
        if (_gender_age_from_text(msg)
                and not state.get("user_gender")
                and self._category_available("age_gender.txt", state)):
            self.last_reason = "late age/gender -> age_gender.txt"
            return self._pick("age_gender.txt", state)
        cat = self._scan_category(msg, state, skip_terminal=True,
                                  skip_age_gender=True)
        if cat:
            state["misses"] += 1
            self.last_reason = f"category match -> {cat}"
            reply = self._pick(cat, state)
            if state["misses"] >= 2:
                state["stage"] = "MIDDLE"
                self.last_reason += " | progressing -> flirty_questions.txt"
                reply = self._pick("flirty_questions.txt", state)
            return reply
        # anything else: move the funnel forward
        state["stage"] = "MIDDLE"
        self.last_reason = "country stage fallback -> flirty_questions.txt"
        return self._pick("flirty_questions.txt", state)

    # ------------------------------------------------------------------
    # Stage 5: MIDDLE — "DETECT NEXT USER MSG" (the diagram core)
    # ------------------------------------------------------------------
    def _stage_middle(self, msg: str, state: Dict[str, Any]) -> str:
        # reply cap: say goodbye and end
        if state["bot_reply_count"] >= MAX_BOT_REPLIES:
            state["stage"] = "END"
            self.last_reason = f"max replies ({MAX_BOT_REPLIES}) -> closer.txt"
            return self._pick("closer.txt", state)

        # ---- user insists on leaving again -> polite END --------------
        # (busy_later.txt was already used once for this user)
        if (self._hit(msg, "middle_chat/busy_later.txt")
                and not self._category_available(
                    "middle_chat/busy_later.txt", state)):
            state["stage"] = "END"
            self.last_reason = "user leaving again -> closer.txt -> END"
            return self._pick("closer.txt", state)

        # category scan — used-once pools are skipped automatically,
        # share/positive handling lives above (always first)
        cat = self._scan_category(msg, state, skip_terminal=True,
                                  skip_age_gender=True)
        if cat:
            self.last_reason = f"category match -> {cat}"
            return self._pick(cat, state)

        # no match at all: next flirty question keeps the funnel moving
        self.last_reason = "no match -> flirty_questions.txt"
        return self._pick("flirty_questions.txt", state)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _hit(self, msg: str, category: str) -> bool:
        matcher = self.matchers.get(category)
        return bool(matcher and matcher.match(msg))

    def _match_category(self, msg: str,
                        only: Optional[Tuple[str, ...]] = None) -> Optional[str]:
        for cat in self.scan_order:
            if only and cat not in only:
                continue
            matcher = self.matchers.get(cat)
            if matcher and matcher.match(msg):
                return cat
        return None

    def _scan_category(self, msg: str, state: Dict[str, Any],
                       skip_terminal: bool = False,
                       skip_age_gender: bool = False) -> Optional[str]:
        """First AVAILABLE category whose triggers match the message.

        Pools already used once in this conversation are skipped
        (NO-REPEAT rule) — only REPEATABLE_POOLS can fire again.
        """
        for cat in self.scan_order:
            if skip_terminal and cat in TERMINAL_CATEGORIES:
                continue
            if cat in SPECIAL_CATEGORIES:
                continue
            if skip_age_gender and cat == "age_gender.txt":
                continue
            if not self._category_available(cat, state):
                continue
            matcher = self.matchers.get(cat)
            if matcher and matcher.match(msg):
                return cat
        return None

    def _category_available(self, cat: str, state: Dict[str, Any]) -> bool:
        """NO-REPEAT rule: a non-repeatable pool fires once per user."""
        if cat in REPEATABLE_POOLS:
            return True
        return cat not in state.setdefault("pools_used", [])

    def _is_horny_question(self, msg: str) -> bool:
        """True when the user directly ASKS the bot a horny question.

        ("horny?", "hrny?", "are u horny?", "rn horny?", "u dtf?" ...)
        Works anywhere in the chat — first lines or middle chat.
        Layer 1: input/horny_answer.txt triggers (editable txt file).
        Layer 2: leet/typo fallback ("h0rny?", "dtf?" ...) — the message
                 must carry a "?" so statements never match.
        Statements ("im horny", "send nudes") do NOT match — they keep
        the regular single horny.txt reply.
        """
        if self._hit(msg, "horny_answer.txt"):
            return True
        low = msg.lower()
        if "?" in low and any(p.search(low)
                              for p in _HORNY_QUESTION_LEET_RE):
            return True
        return False

    def _should_share(self, msg: str, state: Dict[str, Any]) -> bool:
        """Decide whether this message triggers the snap share.

        Layer 1 — firm refusal ("dont have snap") never shares.
        Layer 2 — the user asks for the bot's snap (regex or the
                  input/snapchat.txt triggers when they offer their own)
                  -> share immediately.
        Layer 3 — bare yes-words (or input/share_snap.txt triggers)
                  -> share only AFTER the bot asked (asked_snap=True).
        """
        low = msg.lower()
        if _SNAP_REFUSAL_RE.search(low):
            return False
        if _SNAP_ASK_RE.search(low) or self._hit(msg, "snapchat.txt"):
            return True
        if state.get("asked_snap") and (
                self._hit(msg, "share_snap.txt")
                or _YES_RE.search(low)
                or "@" in msg):
            return True
        return False

    def _country_from_text(self, text: str) -> Optional[str]:
        lower = " " + text.lower() + " "
        # longest keyword first so "south africa" beats "africa"
        for kw in sorted(self.countries, key=len, reverse=True):
            if f" {kw} " in lower:
                return self.countries[kw]
        return None

    def _pick(self, pool_file: str, state: Dict[str, Any]) -> str:
        """Pick a reply line from an output pool.

        NO-REPEAT (pool level): a non-repeatable output txt file is used
        only ONCE per conversation — if it was already spent, the pick is
        redirected to flirty_questions.txt so the funnel keeps moving.
        Line level: a line already used is avoided until the pool is
        exhausted, and never back-to-back.
        Records file + line number for debug output and resolves
        %username% / {snap} / {country} placeholders.
        """
        if (pool_file not in REPEATABLE_POOLS
                and pool_file in state.setdefault("pools_used", [])):
            # defense in depth: a spent pool must never fire again
            self.last_reason += (f" | {pool_file} already used once -> "
                                 "flirty_questions.txt")
            pool_file = "flirty_questions.txt"

        pool = self.pools.get(pool_file)
        if not pool:
            # graceful fallback so the bot never goes silent
            self.last_file, self.last_line = pool_file, 0
            self.last_reason += " (pool missing -> fallback)"
            return random.choice(["hii", "hey", "lol", "hmm", "oh nice"])

        used = state.setdefault("io_used", {}).setdefault(pool_file, [])
        fresh = [r for r in pool if r not in used] or pool
        # avoid the exact previous line from this pool
        if fresh and len(fresh) > 1 and used:
            no_repeat = [r for r in fresh if r != used[-1]]
            if no_repeat:
                fresh = no_repeat
        pick = random.choice(fresh)
        used.append(pick)
        # keep the used-list bounded
        if len(used) > max(len(pool) * 2, 16):
            del used[:len(used) // 2]

        # NO-REPEAT: mark this pool as spent for the conversation
        if pool_file not in REPEATABLE_POOLS:
            used_pools = state.setdefault("pools_used", [])
            if pool_file not in used_pools:
                used_pools.append(pool_file)

        self.last_file = pool_file
        try:
            self.last_line = pool.index(pick) + 1
        except ValueError:
            self.last_line = 0

        # placeholder resolution
        low = pick.lower()
        if "%username%" in low:
            pick = pick.replace("%username%", self.snap_ids.next()) \
                       .replace("%USERNAME%", self.snap_ids.next())
        if "{snap}" in low:
            pick = re.sub(r"\{snap\}", self.snap_ids.next(), pick,
                          flags=re.I)
        if "{country}" in low:
            pick = re.sub(r"\{country\}", state.get("user_country") or "usa",
                          pick, flags=re.I)
        return pick


# ---------------------------------------------------------------------------
# Quick self-test when run directly
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    bot = EvaFlowBot()
    convo = [("hi", "greeting.txt"),
             ("m21", "age_gender.txt"),
             ("from?", "country.txt"),
             ("usa", "flirty_questions.txt"),
             ("are u horny?", "horny_answer.txt"),
             ("haha", "ask_snap.txt"),
             ("yeop", "share_snap.txt")]
    state = bot.new_conversation()
    ok = True
    for user_msg, expected_file in convo:
        r = bot.reply(user_msg, state)
        status = "OK " if bot.last_file == expected_file else "FAIL"
        if bot.last_file != expected_file:
            ok = False
        print(f"[{status}] you> {user_msg!r:14} eva> {r!r}  "
              f"# {bot.last_file} line {bot.last_line}  ({bot.last_reason})")
    print(f"\nfinal stage: {state['stage']} (expected END)")
    print("SELF-TEST", "PASSED" if ok and state["stage"] == "END" else "FAILED")
    raise SystemExit(0 if ok and state["stage"] == "END" else 1)
