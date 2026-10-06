"""Signal Alignment Scorer — Detect → Generate → Match → Reply

Scores candidate replies against detected message signals
(vibe, emotion, intent, horny, region) to find the best match.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
from dataclasses import dataclass, field
from typing import Any

import config
from utils import clean_reply, log


@dataclass
class ScoredCandidate:
    reply: str
    source: str
    signal_score: float
    relevance_score: float
    personality_score: float
    source_confidence: float
    penalty: float
    total_score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self):
        return (
            f"ScoredCandidate({self.source}: {self.reply[:35]}... "
            f"total={self.total_score:.3f} signal={self.signal_score:.2f} "
            f"rel={self.relevance_score:.2f})"
        )


_VIBE_REPLY_MAP: dict[str, set[str]] = {
    "playful": {"lol", "haha", "same", "fair", "cute", "sweet", "funny", "interesting"},
    "curious": {"maybe", "tell me", "what", "why", "how", "interesting", "cool"},
    "adult": {"bold", "slow down", "trust", "chill", "earn", "maybe"},
    "warm": {"sweet", "nice", "like", "good", "love", "thanks", "welcome"},
    "skeptical": {"fair", "real", "human", "prove", "trust", "honest"},
    "cold": {"quiet", "boring", "dry", "boring", "mystery", "say more"},
    "neutral": {"got it", "fair", "same", "interesting", "cool"},
    "objection": {"fine", "okay", "no pressure", "chill", "talk"},
}

_EMOTION_EMPOWER: dict[str, set[str]] = {
    "sad": {"rough", "sorry", "hear", "bad", "hurt", "alone", "empty"},
    "lonely": {"alone", "miss", "someone", "here", "talk", "chat", "empty"},
    "angry": {"frustrating", "annoying", "mad", "hate", "fair", "yeah", "ugh"},
    "anxious": {"stress", "worry", "nervous", "panic", "breathe", "calm", "okay"},
    "tired": {"rest", "sleep", "exhausted", "drained", "long day", "sleepy"},
    "bored": {"same", "boring", "nothing", "chilling", "something", "fun", "dull"},
    "happy": {"good", "great", "love", "amazing", "awesome", "glad", "nice"},
    "excited": {"cool", "wait", "ready", "go", "nice", "awesome", "finally"},
    "affectionate": {"sweet", "like", "love", "miss", "cute", "same", "back"},
    "flirty": {"sweet", "cute", "blush", "kiss", "date", "single", "same"},
    "sexual": {"bold", "chill", "slow", "trust", "first", "maybe", "trust"},
    "skeptical": {"fair", "real", "human", "bot", "prove", "honest", "trust"},
}

_PERSONALITY_MARKERS: dict[str, set[str]] = {
    "sweet": {"sweet", "cute", "nice", "love", "thank", "hug", "baby"},
    "funny": {"lol", "haha", "lmao", "joke", "funny", "hilarious", "ridiculous"},
    "bold": {"damn", "fire", "hot", "bold", "wild", "crazy", "insane"},
    "shy": {"maybe", "umm", "idk", "not sure", "shy", "nervous", "quiet"},
    "mysterious": {"secret", "maybe", "later", "surprise", "hmm", "interesting"},
    "romantic": {"love", "heart", "miss", "beautiful", "gorgeous", "sweetheart"},
    "wild": {"crazy", "wild", "insane", "fire", "lit", "insane", "wild"},
}


def _normalize_for_scoring(text: str) -> str:
    t = (text or "").lower().strip()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _signal_alignment(
    reply: str,
    vibe: str,
    emotion: str,
    emotion_confidence: float,
    analysis: Any,
) -> float:
    """How well does the reply match the detected vibe + emotion? 0.0 - 1.0"""
    score = 0.50
    r_tokens = set(_normalize_for_scoring(reply).split())
    vibe_terms = _VIBE_REPLY_MAP.get(vibe, set())
    if r_tokens & vibe_terms:
        score += 0.15

    emotion_terms = _EMOTION_EMPOWER.get(emotion, set())
    if r_tokens & emotion_terms and emotion_confidence >= 0.55:
        score += 0.12

    if analysis:
        if analysis.intent.startswith("emotion_") or analysis.intent.endswith("_statement"):
            if emotion in ("sad", "lonely", "angry", "anxious", "tired"):
                empathy_words = {"sorry", "rough", "hear", "bad", "rest", "stress", "breathe", "calm"}
                if r_tokens & empathy_words:
                    score += 0.15

        if analysis.is_question and reply.endswith("?"):
            score += 0.05
        if not analysis.is_question and not reply.endswith("?"):
            score += 0.03

    if vibe in ("skeptical", "objection") and any(w in r_tokens for w in {"fair", "real", "trust", "honest", "human"}):
        score += 0.10

    if vibe == "playful" and any(w in r_tokens for w in {"lol", "haha", "cute", "funny"}):
        score += 0.08

    if vibe == "curious" and any(w in r_tokens for w in {"maybe", "tell", "interesting", "cool"}):
        score += 0.08

    return min(1.0, max(0.0, score))


def _relevance_score(reply: str, analysis: Any, text: str) -> float:
    """How relevant is the reply to what the user said? 0.0 - 1.0"""
    score = 0.50
    if not analysis:
        return score

    r_tokens = set(_normalize_for_scoring(reply).split())
    u_tokens = set(_normalize_for_scoring(text).split())
    overlap = len(r_tokens & u_tokens)
    if overlap >= 2:
        score += 0.15
    elif overlap >= 1:
        score += 0.08

    if analysis.intent in ("greeting", "how_are_you", "wyd") and any(
        w in r_tokens for w in {"hey", "hi", "hello", "good", "chilling", "doing"}
    ):
        score += 0.12

    if analysis.intent == "ask_name" and any(w in r_tokens for w in {"eva", "name", "im"}):
        score += 0.15

    if analysis.entity_type and analysis.entity_value:
        if analysis.entity_value.lower() in r_tokens:
            score += 0.12

    if analysis.is_question and "?" in reply:
        score += 0.08
    elif not analysis.is_question and "?" not in reply:
        score += 0.05

    if analysis.is_short and len(reply.split()) <= 4:
        score += 0.08
    elif not analysis.is_short and len(reply.split()) >= 3:
        score += 0.05

    return min(1.0, max(0.0, score))


def _personality_fit(reply: str, user_profile: dict | None) -> float:
    """Does the reply match the bot's personality? 0.0 - 1.0"""
    score = 0.60
    personality = str(getattr(config, "BOT_PERSONALITY", "funny") or "funny").lower()
    markers = _PERSONALITY_MARKERS.get(personality, set())
    r_tokens = set(_normalize_for_scoring(reply).split())
    if r_tokens & markers:
        score += 0.15

    word_count = len(reply.split())
    if word_count <= int(getattr(config, "MAX_REPLY_WORDS", 12)):
        score += 0.10
    else:
        score -= 0.10

    if reply and reply[0].islower():
        score += 0.05

    return min(1.0, max(0.0, score))


def _calculate_penalty(reply: str, recent_assistant_replies: list[str]) -> float:
    """Calculate penalty for anti-repeat, length issues. 0.0 - 1.0 (higher = worse)"""
    penalty = 0.0
    r_normalized = _normalize_for_scoring(reply)
    for recent in (recent_assistant_replies or []):
        if _normalize_for_scoring(recent) == r_normalized:
            penalty += 0.50
            break
        recent_words = set(_normalize_for_scoring(recent).split())
        reply_words = set(r_normalized.split())
        if recent_words and reply_words:
            jaccard = len(recent_words & reply_words) / len(recent_words | reply_words)
            if jaccard > 0.70:
                penalty += 0.30
                break
            elif jaccard > 0.50:
                penalty += 0.15
                break

    word_count = len(reply.split())
    if word_count > int(getattr(config, "MAX_REPLY_WORDS", 12)):
        penalty += 0.15

    if len(reply) < 3:
        penalty += 0.10

    generic_replies = {"yeah", "same", "fair", "lol true", "got it", "ok", "okay", "hmm"}
    if r_normalized in generic_replies:
        penalty += 0.20

    return min(1.0, max(0.0, penalty))


def score_candidates(
    candidates: list[Any],
    analysis: Any,
    vibe_info: dict[str, Any],
    emotion_info: Any,
    horny_score: float,
    recent_assistant_replies: list[str],
    user_profile: dict | None = None,
) -> list[ScoredCandidate]:
    """Score and rank all candidates against detected signals.

    Weights:
      source_confidence: 25%
      signal_alignment:  30%
      relevance:         25%
      personality:       10%
      penalty:          -10%
    """
    scored: list[ScoredCandidate] = []

    for c in candidates:
        signal = _signal_alignment(
            c.reply,
            vibe_info.get("vibe", "neutral"),
            emotion_info.primary if hasattr(emotion_info, "primary") else "neutral",
            emotion_info.confidence if hasattr(emotion_info, "confidence") else 0.45,
            analysis,
        )
        relevance = _relevance_score(c.reply, analysis, analysis.raw if hasattr(analysis, "raw") else "")
        personality = _personality_fit(c.reply, user_profile)
        penalty = _calculate_penalty(c.reply, recent_assistant_replies)

        total = (
            c.confidence * 0.25
            + signal * 0.30
            + relevance * 0.25
            + personality * 0.10
            - penalty * 0.10
        )
        total = max(0.0, min(1.0, total))

        scored.append(ScoredCandidate(
            reply=c.reply,
            source=c.source,
            signal_score=round(signal, 3),
            relevance_score=round(relevance, 3),
            personality_score=round(personality, 3),
            source_confidence=round(c.confidence, 3),
            penalty=round(penalty, 3),
            total_score=round(total, 3),
            metadata=c.metadata if hasattr(c, "metadata") else {},
        ))

    scored.sort(key=lambda s: s.total_score, reverse=True)
    return scored


def log_scoring(uid: int | str, scored: list[ScoredCandidate], top_n: int = 3):
    """Log top N scored candidates for debugging."""
    if not scored:
        log(f"PIPELINE uid={uid} no candidates")
        return
    for i, s in enumerate(scored[:top_n]):
        log(
            f"PIPELINE uid={uid} #{i+1} src={s.source} "
            f"total={s.total_score:.3f} signal={s.signal_score:.2f} "
            f"rel={s.relevance_score:.2f} pers={s.personality_score:.2f} "
            f"conf={s.source_confidence:.2f} pen={s.penalty:.2f} "
            f"reply={s.reply[:50]}"
        )
