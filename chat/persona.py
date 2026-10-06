"""EVA persona layer: answer the user's questions and react to their talk.

Code-only realism. Runs only AFTER the flat input->output TXT matcher has
already had its chance, so the data-driven replies (age/country/identity)
always win:

  * ``answer_user``        — answers common questions users ask the bot
    (favourites, hobbies, relationship status, "are you real", name...)
    with a consistent persona, so the funnel no longer ignores a direct
    question and the post-snap brain gives bad replies to such questions.
  * ``react_to_statement`` — reacts to personal disclosures ("i love gaming",
    "i play cod") with short hooks that reference the topic, instead of the
    funnel pivoting straight to a scripted question.

Both return the reply or None (caller keeps the normal flow).
"""

from __future__ import annotations

import random
import re
from typing import Optional, Tuple

_SPACE_RE = re.compile(r"\s+")

_TOKEN_RE = re.compile(r"[a-z0-9']+")

# Words that should never be picked as a topic from the user's message.
_STOP_TOPICS = frozenset({
    "u", "ur", "you", "your", "ya", "me", "my", "yo", "lol", "rn", "tbh",
    "omg", "wbu", "hbu", "fr", "ngl", "this", "that", "it", "its", "fun",
    "stuff", "things", "how", "why", "what", "when", "where", "which",
    "do", "get", "go",
})


def _norm(text: str) -> str:
    return _SPACE_RE.sub(" ", text.lower().strip())


def _subject(words: str, max_words: int = 2) -> Optional[str]:
    """First meaningful tokens of a captured phrase, or None."""
    toks = [t for t in words.split()
            if t and len(t) >= 3 and t not in _STOP_TOPICS]
    if not toks:
        return None
    return " ".join(toks[:max_words])[:24]


# ---------------------------------------------------------------------------
# QUESTION -> ANSWER  (key, regex on normalized msg, answers)
# Order matters: keep the most specific rules first; the first hit wins.
# ---------------------------------------------------------------------------

PERSONA_RULES = [
    ("fav_sport", r"whats? ur (?:fav|favourite|favorite) sport\b", [
        "im not that athletic honestly 😅 but i watch sometimes, u?",
        "i like watching more than playing lol u into sports?",
        "i used to play back in school, now im just a spectator 😅 wbu?",
    ]),
    ("fav_music", r"whats? ur (?:fav|favourite|favorite) (?:music|song|artist)\b", [
        "depends on the vibe 🎧 what do u listen to?",
        "i listen to everything tbh, u got a go to?",
    ]),
    ("fav_movie", r"whats? ur (?:fav|favourite|favorite) (?:movie|film)\b", [
        "horror and comedy mostly, love a creepy movie 😅",
        "i rewatch old movies when im bored lol u?",
    ]),
    ("fav_food", r"whats? ur (?:fav|favourite|favorite) (?:food|dish)\b", [
        "im a foodie lol pasta and sushi all day",
        "anything spicy tbh, what about u?",
    ]),
    ("fav_game", r"whats? ur (?:fav|favourite|favorite) (?:game|video game)\b", [
        "i play sometimes, mostly chill games. u?",
        "whatever my friends drag me into 😅 wbu?",
    ]),
    ("fav_show", r"whats? ur (?:fav|favourite|favorite) (?:show|anime|series)\b", [
        "i binge shows when im bored lol what do u watch?",
        "depends on the mood, u got recs? 😏",
    ]),
    ("fav_gen", r"whats? ur (?:fav|favourite|favorite) (\w+)", [
        "hmm hard one lol u go first",
        "depends on my mood honestly 😅 wbu?",
    ]),
    ("single", r"r u (single|taken|free|committed)\b", [
        "single and free 😏 what about u?",
        "no lol im single, u?",
    ]),
    ("have_gf", r"do (?:u|you) have a (?:bf|gf|boyfriend|girlfriend|husband|wife)\b", [
        "no lol im single",
        "no",
    ]),
    ("like_me", r"do u (?:like|love) me\b", [
        "maybe 😏 u seem fun",
        "how could i not lol",
    ]),
    ("are_you_real", r"(?:are|r) (?:u|you) (?:a )?(?:bot|robot|real|human|ai|ai)\b", [
        "im as real as it gets 😏",
        "real enough for u to be curious hehe",
        "what do u think? 😏",
    ]),
    ("name", r"whats? (?:ur|your) name\b", [
        "eva 😌 wbu?",
    ]),
    ("what_doing", r"(?:what|wat) (?:are|r|do) (?:u|you) (?:doing|do)\b", [
        "just chilling honestly 😅 u?",
        "nothing much, floating lol u?",
    ]),
    # "u like music?" / "u play games?" — asks the bot, echoes the topic.
    ("u_like_subj", r"\b(?:u|you) (?:like|love|play|watch) (\w+)", [
        "i love {sub} ngl 😅 wbu?",
        "{sub}? yesss what about u?",
        "hmm depends 😏 u into {sub}?",
    ]),
    # "do u play/watch/like/listen to <topic>?" — echoes the topic.
    ("do_you", r"do u (play|watch|like|love|listen to|follow|enjoy) (\w+)", [
        "{sub}? think i like it, do u do it a lot?",
        "lowkey into {sub} ngl 😅",
        "{sub}? thats cute, u into it much?",
    ]),
]

_QUESTION_RE = [(key, re.compile(pat), answers)
                for key, pat, answers in PERSONA_RULES]


def answer_user(msg: str) -> Optional[Tuple[str, str]]:
    """Return (rule_key, answer) for a question the persona can answer."""
    norm = _norm(msg)
    for key, pat, answers in _QUESTION_RE:
        m = pat.search(norm)
        if not m:
            continue
        if key in ("do_you", "u_like_subj"):
            sub = _subject(m.group(2) if key == "do_you" else m.group(1))
            if not sub:
                continue
            return f"{key}|{sub}", random.choice(answers).format(sub=sub)
        return key, random.choice(answers)
    return None


# ---------------------------------------------------------------------------
# STATEMENT -> REACTION  (self-disclosure re/hooks)
# ---------------------------------------------------------------------------

_REACT_RULES = [
    (r"\bi really (like|love|enjoy|hate) (\w+)", 2),
    (r"\bi (like|love|enjoy) (\w+)", 2),
    (r"\bi (play|watch|read|cook|draw|write|study|work) (\w+)", 2),
    (r"\bi(?:'m|m) (?:into|learning|obsessed with|starting) (\w+)", 1),
    (r"\bi have (\w+)", 1),
    (r"\bive been (\w+)", 1),
    (r"\bmy (?:fav|favourite|favorite) (\w+)", 1),
]

_REACT_TEMPLATES = [
    "{sub}? ok tell me more 😏",
    "{sub}, thats actually cool hehe",
    "no way, {sub}? i feel that",
    "{sub}? how long u been into it?",
    "ok {sub} is a vibe ngl",
    "{sub}? what got u into that?",
]

_REACT_RE = [(re.compile(pat), group) for pat, group in _REACT_RULES]


def react_to_statement(msg: str) -> Optional[str]:
    """Return a short topic-aware hook for a self-disclosure, or None."""
    norm = _norm(msg)
    for pat, group in _REACT_RE:
        m = pat.search(norm)
        if not m:
            continue
        sub = _subject(m.group(group))
        if not sub:
            continue
        return random.choice(_REACT_TEMPLATES).format(sub=sub)
    return None