"""TgBrain — reference EVA Telegram bot's stage reply rules, ported.

Loads the stage JSON reply databases copied from the reference project
(data/local_db/stage_rules/*_reply_rules.json) and answers general chat with
priority-ordered, token-based matching — the same idea the reference used
(flow first, prioritized rule DB after).

Contrast with the InputOutputEngine (flat input->output txt categories):
TgBrain runs ONLY as a fallback for messages that no specific flat category
matched, and only from mid/late chat (the fixed funnel keeps control of the
age / gender / snap-ask / snap-share rules).

Matching:
  * every question phrase is tokenized into significant tokens (>2 chars,
    stopwords removed) and matched against the message token set;
  * score = best question overlap ratio; rule picked by score desc, then
    priority desc, ties broken randomly;
  * a question must be matched by ALL of its significant tokens (no loose
    sub-string false positives) and hit at least a 0.75 threshold;
  * `short_message` rules additionally require a short incoming message.

Graceful degradation: if no stage rules are present the brain returns None and
the caller keeps its previous fallback, so the bot never depends on this data.
"""

from __future__ import annotations

import json
import os
import random
import re
from typing import Dict, List, Optional, Tuple

_BLOCK_HINTS = (
    "casereddisp",
    "{main_link}",
    "{link}",
    "onlyfans",
    "fansly",
    "tg.me",
    "/s/a",
)

_STOP = {
    "a", "an", "the", "and", "or", "for", "of", "to", "in", "on", "at", "by",
    "is", "am", "are", "was", "were", "be", "been", "do", "did", "does",
    "u", "ur", "you", "your", "i", "my", "me", "im", "it", "its", "we",
    "not", "no", "yes", "ok", "k", "so", "but", "as", "if", "then", "than",
    "rn", "lol", "tbh", "omg", "hbu", "wbu", "hru", "ya", "yeah", "yep",
    "oh", "hey", "hi", "just", "really", "right", "about", "here", "there",
    "what", "when", "why", "how", "who", "where", "which", "let", "get",
    "like", "wanna", "gonna", "think", "know",
}

_TOKEN_RE = re.compile(r"[a-z'][a-z']+|[a-z]")

# Minimum score / effects: fewer than this => no match (caller keeps fallback).
_MIN_SCORE = 0.75


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall(text.lower())]


def _significant(tokens: List[str]) -> List[str]:
    return [t for t in tokens if len(t) >= 3 and t not in _STOP]


class _Rule:
    __slots__ = ("cat", "priority", "short_only", "questions", "answers", "sig")

    def __init__(self, cat: str, priority: int, short_only: bool,
                 questions: List[List[str]], answers: List[str]) -> None:
        self.cat = cat
        self.priority = priority
        self.short_only = short_only
        self.questions = questions          # each: full normalized tokens
        self.sig = [_significant(q) for q in questions]  # significant tokens
        self.answers = answers


class TgBrain:
    def __init__(self, rules_dir: str) -> None:
        self.rules: List[_Rule] = []
        self._index: Dict[str, List[Tuple[_Rule, int]]] = {}
        self._load(rules_dir)

    def _load(self, rules_dir: str) -> None:
        if not os.path.isdir(rules_dir):
            return
        for fname in sorted(os.listdir(rules_dir)):
            if not fname.lower().endswith(".json"):
                continue
            full = os.path.join(rules_dir, fname)
            try:
                with open(full, encoding="utf-8", errors="ignore") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"[TgBrain] load error {full}: {e}")
                continue
            rules = data.get("rules") or []
            cat = data.get("category") or os.path.splitext(fname)[0]
            for r in rules:
                if not r.get("enabled", True):
                    continue
                qs = r.get("questions") or []
                ans = r.get("answers") or []
                if not qs or not ans:
                    continue
                q_toks: List[List[str]] = []
                for q in qs:
                    qt = _tokens(q)
                    if not qt:
                        continue
                    st = _significant(qt)
                    if not st:
                        continue
                    q_toks.append(qt)
                if not q_toks:
                    continue
                clean_ans = []
                for a in ans:
                    a = a.strip()
                    if not a or "{" in a or "}" in a:
                        continue
                    if any(b in a.lower() for b in _BLOCK_HINTS):
                        continue
                    clean_ans.append(a)
                if not clean_ans:
                    continue
                short_only = (r.get("match_type") == "short_message")
                priority = int(r.get("priority") or 0)
                # per-question blocked words check
                keep_q = [(q, s) for q, s in zip(q_toks, [
                    _significant(q) for q in q_toks]) if not any(
                        b in " ".join(q) for b in _BLOCK_HINTS)]
                if not keep_q:
                    continue
                rule = _Rule(cat, priority, short_only,
                             [q for q, _ in keep_q], clean_ans)
                base = len(self.rules)
                self.rules.append(rule)
                for qi, s in enumerate(rule.sig):
                    for tok in set(s):
                        self._index.setdefault(tok, []).append((rule, qi))

    @property
    def enabled(self) -> bool:
        return bool(self.rules)

    def match(self, user_msg: str, answers: Optional[List[str]] = None,
              _only_cat: Optional[str] = None) -> Optional[str]:
        """Return a reply string, or None if nothing scored well enough."""
        if not self.rules:
            return None
        toks = _tokens(user_msg)
        if not toks:
            return None
        msg_all = set(toks)
        msg_sig = set(_significant(toks))
        if not msg_sig:
            return None
        candidates: Dict[int, float] = {}
        for tok in msg_sig:
            for rule, qi in self._index.get(tok, ()):
                if rule not in candidates:
                    candidates[rule] = 0.0
        if not candidates:
            return None
        best_score = 0.0
        best_rule: Optional[_Rule] = None
        for rule in candidates:
            qs = rule.questions
            sigs = rule.sig
            sc = 0.0
            for q, s in zip(qs, sigs):
                if not s:
                    continue
                inter = len(set(s) & msg_all)
                ratio = inter / len(s)
                # whole-phrase presence bonus for full matches
                if ratio == 1.0 and set(q).issubset(msg_all):
                    ratio = 1.05
                if ratio > sc:
                    sc = ratio
            if rule.short_only and len(toks) > 5:
                sc = 0.0
            if sc > 1.0:
                sc = 1.0
            candidates[rule] = sc
            if sc >= _MIN_SCORE and sc > best_score:
                best_score = sc
                best_rule = rule
            elif sc == best_score and best_rule is not None \
                    and rule.priority > best_rule.priority:
                best_rule = rule
        if best_rule is None:
            return None
        if _only_cat is not None and best_rule.cat != _only_cat:
            return None
        pool = best_rule.answers if answers is None else answers
        return random.choice(pool)

    def describe(self) -> List[Dict[str, object]]:
        out: Dict[str, int] = {}
        for r in self.rules:
            out[r.cat] = out.get(r.cat, 0) + 1
        return [{"category": k, "rules": v} for k, v in sorted(out.items())]