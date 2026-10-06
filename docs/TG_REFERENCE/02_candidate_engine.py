"""Candidate Generation Engine — Detect → Generate → Match → Reply

Generates multiple reply candidates from ALL sources simultaneously,
instead of the old cascade (try A, if fail try B...).

Sources:
  1. Deterministic (brain direct_reply) — fastest, most reliable for known intents
  2. Curated DB match — high quality local pairs
  3. Fast Matcher (exact/keyword/FTS5/mega) — broad coverage
  4. DeepSeek API — best for complex/unique messages
  5. Emotion-based fallback — contextual
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

import config
from utils import log


@dataclass
class Candidate:
    reply: str
    source: str
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self):
        return f"Candidate({self.source}: {self.reply[:40]}... conf={self.confidence:.2f})"


def _dedup_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Remove duplicate/similar replies, keep highest confidence."""
    seen: dict[str, Candidate] = {}
    for c in candidates:
        key = c.reply.lower().strip().rstrip("?.!")
        if key in seen:
            if c.confidence > seen[key].confidence:
                seen[key] = c
        else:
            seen[key] = c
    return list(seen.values())


def _collect_deterministic_candidates(
    analysis: Any,
    profile_name: str,
    last_question: str,
    last_bot_reply: str,
    allow_adult: bool,
) -> list[Candidate]:
    """Source 1: Deterministic replies from brain direct_reply."""
    from core.message_brain import direct_reply

    candidates = []
    reply, source = direct_reply(
        analysis,
        profile_name=profile_name,
        last_question=last_question,
        last_bot_reply=last_bot_reply,
        allow_adult=allow_adult,
    )
    if reply:
        candidates.append(Candidate(
            reply=reply,
            source="deterministic",
            confidence=0.95,
            metadata={"intent": analysis.intent, "brain_source": source},
        ))
    return candidates


def _collect_curated_candidates(
    text: str,
    allow_adult: bool,
) -> list[Candidate]:
    """Source 2: Curated local DB match (high quality)."""
    from db.curated_matcher import curated_match

    candidates = []
    matched = curated_match(text, allow_adult=allow_adult)
    if matched and matched["score"] >= float(getattr(config, "SMART_BRAIN_MIN_DB_CONFIDENCE", 0.78)):
        candidates.append(Candidate(
            reply=matched["reply"],
            source="curated_db",
            confidence=matched["score"],
            metadata={"tier": matched.get("tier", ""), "db_score": matched.get("score", 0)},
        ))
    return candidates


def _collect_fast_match_candidates(
    text: str,
    allow_adult: bool,
) -> list[Candidate]:
    """Source 3: Fast matcher — broad coverage from all DB tiers."""
    from db.fast_matcher import fast_match_candidates

    candidates = []
    matches = fast_match_candidates(text, allow_adult=allow_adult, top_n=3)
    for reply, tier, score in matches:
        candidates.append(Candidate(
            reply=reply,
            source=f"fast_match:{tier}",
            confidence=min(0.90, score / 100.0 if score > 1 else score),
            metadata={"tier": tier, "raw_score": score},
        ))
    return candidates


async def _collect_deepseek_candidates(
    text: str,
    conversation_history: list[tuple[str, str]],
    user_profile: dict[str, Any],
    profile_name: str,
    recent_assistant_replies: list[str],
) -> list[Candidate]:
    """Source 4: DeepSeek API — best for complex/unique messages."""
    from core.ai_client import generate_normal_reply, is_configured

    if not bool(getattr(config, "AI_ENABLED", True)) or not is_configured():
        return []

    try:
        ai_reply = await asyncio.wait_for(
            generate_normal_reply(
                text,
                history=conversation_history,
                stage=str(user_profile.get("funnel_stage", "basic_info") or "basic_info"),
                vibe=str(user_profile.get("last_vibe", "neutral") or "neutral"),
                profile_name=profile_name or str(user_profile.get("profile_name", "") or ""),
                user_country=str(user_profile.get("country", "") or ""),
                msg_count=int(user_profile.get("message_count", 0) or 0),
                link_sent=bool(user_profile.get("promo_sent", 0)),
                region_priority=str(user_profile.get("region_priority", "unknown") or "unknown"),
                recent_assistant_replies=recent_assistant_replies,
            ),
            timeout=float(getattr(config, "AI_TIMEOUT", 45)),
        )
        if ai_reply:
            return [Candidate(
                reply=ai_reply,
                source="deepseek",
                confidence=0.85,
                metadata={"model": config.AI_MODEL},
            )]
    except (Exception, asyncio.TimeoutError) as e:
        log(f"CANDIDATE_ENGINE: deepseek failed: {e}")
    return []


def _collect_emotion_candidates(
    analysis: Any,
    vibe_info: dict[str, Any],
) -> list[Candidate]:
    """Source 5: Emotion/vibe-based fallback replies."""
    from detection.emotion_engine import emotion_reply
    from detection.vibe_engine import get_prelink_reply

    candidates = []

    if analysis.intent.startswith("emotion_"):
        emotion = analysis.intent.removeprefix("emotion_")
        planned = emotion_reply(emotion)
        if planned:
            candidates.append(Candidate(
                reply=planned,
                source="emotion",
                confidence=0.75,
                metadata={"emotion": emotion},
            ))

    intent_to_emotion = {
        "sad_statement": "sad", "anxious_statement": "anxious",
        "angry_statement": "angry", "bored": "bored",
    }
    if analysis.intent in intent_to_emotion:
        planned = emotion_reply(intent_to_emotion[analysis.intent])
        if planned:
            candidates.append(Candidate(
                reply=planned,
                source="emotion",
                confidence=0.70,
                metadata={"emotion": intent_to_emotion[analysis.intent]},
            ))

    if vibe_info.get("vibe") in ("skeptical", "objection", "cold", "playful", "curious", "adult", "warm"):
        prelink = get_prelink_reply(vibe_info)
        candidates.append(Candidate(
            reply=prelink,
            source="vibe_fallback",
            confidence=0.60,
            metadata={"vibe": vibe_info["vibe"]},
        ))

    return candidates


def _collect_fallback_candidates(
    analysis: Any,
) -> list[Candidate]:
    """Source 6: Short safe fallbacks when nothing else works."""
    if analysis.is_question:
        return [Candidate("not sure what u mean", "fallback_question", 0.30)]
    if analysis.is_short:
        return [Candidate("got it", "fallback_short", 0.25)]
    return [Candidate(
        random.choice(["got it", "i get u", "fair enough"]),
        "fallback_statement", 0.20,
    )]


async def generate_candidates(
    text: str,
    *,
    last_question: str = "",
    last_bot_reply: str = "",
    recent_user_texts: list[str] | None = None,
    recent_assistant_replies: list[str] | None = None,
    conversation_history: list[tuple[str, str]] | None = None,
    msg_count: int = 0,
    profile_name: str = "",
    user_profile: dict | None = None,
    allow_adult: bool = False,
    analysis: Any = None,
    vibe_info: dict[str, Any] | None = None,
) -> list[Candidate]:
    """Generate reply candidates from ALL sources simultaneously.

    Returns deduplicated list of candidates sorted by confidence (descending).
    """
    if not text or not text.strip():
        return [Candidate("tell me more", "empty_fallback", 0.10)]

    recent_asst = recent_assistant_replies or []
    recent_usr = recent_user_texts or []
    history = conversation_history or []
    profile = user_profile or {}

    all_candidates: list[Candidate] = []

    all_candidates.extend(_collect_deterministic_candidates(
        analysis, profile_name, last_question, last_bot_reply, allow_adult,
    ))

    all_candidates.extend(_collect_curated_candidates(text, allow_adult))

    if analysis and analysis.allow_db_match:
        all_candidates.extend(_collect_fast_match_candidates(text, allow_adult))

    basic_intents = {
        "ask_name", "ask_age", "ask_location",
        "how_are_you", "greeting_how_are_you", "wyd", "greeting_wyd",
    }
    skip_deepseek = (
        (analysis and analysis.intent in basic_intents)
        or (analysis and analysis.is_short and analysis.is_ambiguous)
        or allow_adult
    )
    if not skip_deepseek:
        ds_candidates = await _collect_deepseek_candidates(
            text, history, profile, profile_name, recent_asst,
        )
        all_candidates.extend(ds_candidates)

    if analysis:
        all_candidates.extend(_collect_emotion_candidates(analysis, vibe_info or {
            "vibe": profile.get("last_vibe", "neutral"),
            "scores": {},
            "readiness": float(profile.get("cta_ready_score", 0) or 0),
        }))

    if not all_candidates:
        all_candidates.extend(_collect_fallback_candidates(
            analysis if analysis else type("A", (), {"is_question": False, "is_short": False, "is_ambiguous": False, "intent": "general_statement"})()
        ))

    deduped = _dedup_candidates(all_candidates)
    deduped.sort(key=lambda c: c.confidence, reverse=True)

    max_candidates = int(getattr(config, "PIPELINE_MAX_CANDIDATES", 6))
    return deduped[:max_candidates]
