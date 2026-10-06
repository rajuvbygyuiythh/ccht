"""Layered message-understanding and reply routing.

The brain uses independent engines in priority order:
1. Normalization/slang expansion
2. Exact phrase rules
3. Regex question/intent detection
4. Conversation-context resolution
5. Keyword voting
6. Curated local-database matching
7. Short safe fallback

It intentionally avoids fuzzy matching for one- and two-word messages because those
messages are too ambiguous and were the main source of unrelated replies.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, asdict
from typing import Any

import config
from db.curated_matcher import curated_match
from detection.emotion_engine import detect_emotion, emotion_reply
from detection.language_guard import detect_english_support
from core.local_nlu_engine import generate_rule_reply, has_adult_intent
from utils import clean_reply, truncate_words
from ui.identity_variants import pick

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SLASH = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9']+")

SLANG = {
    "hru": "how are you",
    "how r u": "how are you",
    "wbu": "what about you",
    "hbu": "how about you",
    "wyd": "what are you doing",
    "wya": "where are you",
    "idk": "i do not know",
    "rn": "right now",
    "nm": "not much",
    "ur": "your",
    "u": "you",
}

STOPWORDS = {
    "a", "an", "the", "is", "are", "am", "i", "me", "my", "you", "your",
    "to", "of", "in", "on", "at", "for", "and", "or", "but", "it", "this",
    "that", "do", "does", "did", "can", "could", "would", "should", "what",
    "how", "where", "when", "why", "who", "about", "right", "now",
}

NAME_STOPWORDS = {
    "nothing", "nothing much", "not much", "same", "okay", "ok", "fine", "good",
    "yes", "no", "nah", "nope", "lol", "haha", "idk", "maybe", "bored", "busy",
    "sleeping", "chilling", "working", "home", "here", "there", "what", "why",
    "nice", "cool", "awesome", "sweet", "great", "alright", "okay then",
}


@dataclass
class Analysis:
    raw: str
    normalized: str
    intent: str = "general_statement"
    confidence: float = 0.0
    methods: tuple[str, ...] = ()
    is_question: bool = False
    is_short: bool = False
    is_ambiguous: bool = False
    allow_db_match: bool = True
    should_ask_profile: bool = False
    entity_type: str = ""
    entity_value: str = ""
    context_topic: str = ""
    emotion: str = "neutral"
    emotion_confidence: float = 0.0
    emotion_intensity: str = "low"
    language_supported: bool = True
    language_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize(text: str) -> str:
    t = (text or "").lower().strip()
    t = t.replace("’", "'")
    t = re.sub(r"[^a-z0-9'?+\s]", " ", t)
    t = _SLASH.sub(" ", t).strip()

    # Expand full-message slang first, then individual tokens.
    if t in SLANG:
        return SLANG[t]
    words = t.split()
    expanded = [SLANG.get(w, w) for w in words]
    return _SLASH.sub(" ", " ".join(expanded)).strip()


def _valid_name(text: str) -> str | None:
    t = normalize(text).rstrip("?").strip()
    patterns = [
        r"^(?:i am|i'm|im|my name is|call me)\s+([a-z][a-z'-]{1,17})$",
        r"^([a-z][a-z'-]{1,17})(?:\s+(?:and you|what about you|how about you))?$",
    ]
    for pat in patterns:
        m = re.match(pat, t, re.I)
        if not m:
            continue
        name = m.group(1).strip("'-").lower()
        if name and name not in NAME_STOPWORDS:
            return name.capitalize()
    return None


def _valid_age(text: str) -> str | None:
    t = normalize(text)
    m = re.search(r"\b(1[3-9]|[2-9][0-9])\b", t)
    return m.group(1) if m else None


def _valid_location(text: str) -> str | None:
    t = normalize(text).rstrip("?").strip()
    if not t or len(t.split()) > 5:
        return None
    m = re.match(r"^(?:i am from|i'm from|im from|from|live in|in)\s+(.+)$", t)
    value = (m.group(1) if m else t).strip()
    value = re.sub(r"\s+(?:and you|what about you|how about you)\??$", "", value).strip()
    if value in NAME_STOPWORDS or value.isdigit() or len(value) < 2:
        return None
    return value.title()


def _context_topic(last_bot_reply: str, last_question: str) -> str:
    q = (last_question or "").strip().lower()
    if q in {"name", "profile_name"}:
        return "name"
    if q == "age":
        return "age"
    if q in {"country", "location"}:
        return "location"

    last = normalize(last_bot_reply)
    if re.search(r"\b(?:how are you|how are u|you good|u good)\b", last):
        return "how_are_you"
    if re.search(r"\b(?:what are you doing|what are u doing|what are you up to|what u up to|wyd)\b", last):
        return "wyd"
    if re.search(r"\b(?:your name|ur name|call you|call u)\b", last):
        return "name"
    if re.search(r"\b(?:how old|your age|ur age)\b", last):
        return "age"
    if re.search(r"\b(?:where are you from|where r u from|your location|ur location)\b", last):
        return "location"
    return ""


def _has_reciprocal(text: str) -> bool:
    return bool(re.search(r"\b(?:what about you|how about you|and you)\b|^you\??$", text))


def analyze_message(
    text: str,
    *,
    last_question: str = "",
    last_bot_reply: str = "",
    recent_user_texts: list[str] | None = None,
    msg_count: int = 0,
) -> Analysis:
    raw = text or ""
    t = normalize(raw)
    tokens = _WORD.findall(t)
    methods: list[str] = ["normalize"]
    is_question = "?" in raw or bool(re.match(r"^(what|how|where|when|why|who|can|could|do|does|did|are|is|will|would)\b", t))
    lang = detect_english_support(raw)
    emotion = detect_emotion(raw, recent_user_texts or [])
    out = Analysis(
        raw=raw,
        normalized=t,
        confidence=0.20,
        methods=(),
        is_question=is_question,
        is_short=len(tokens) <= 2,
        is_ambiguous=len(tokens) <= 1,
        allow_db_match=len(tokens) >= int(getattr(config, "SMART_BRAIN_MIN_DB_WORDS", 3)),
        should_ask_profile=False,
        context_topic=_context_topic(last_bot_reply, last_question),
        emotion=emotion.primary,
        emotion_confidence=emotion.confidence,
        emotion_intensity=emotion.intensity,
        language_supported=lang.supported,
        language_reason=lang.reason,
    )
    methods.extend([f"language:{lang.reason}", f"emotion:{emotion.primary}"])

    if bool(getattr(config, "ENGLISH_ONLY", True)) and not lang.supported:
        out.intent = "unsupported_language"
        out.confidence = 1.0
        out.allow_db_match = False
        out.methods = tuple(methods)
        return out

    # Engine 1: exact high-priority phrases.
    exact: dict[str, str] = {
        "hi": "greeting", "hii": "greeting", "hey": "greeting", "hello": "greeting",
        "hey there": "greeting", "hi there": "greeting", "yo": "greeting",
        "how are you": "how_are_you", "you good": "how_are_you",
        "what are you doing": "wyd", "what are you up to": "wyd",
        "not much": "nothing", "nothing": "nothing", "nothing much": "nothing",
        "same": "same", "same here": "same", "me too": "same",
        "i do not know": "idk", "not sure": "idk", "no idea": "idk",
        "ok": "agree", "okay": "agree", "alright": "agree", "yes": "agree",
        "no": "disagree", "nope": "disagree", "nah": "disagree",
        "lol": "reaction", "haha": "reaction", "lmao": "reaction", "hmm": "reaction",
        "nice": "acknowledgement", "cool": "acknowledgement",
        "awesome": "acknowledgement", "sweet": "acknowledgement",
        "thanks": "thanks", "thank you": "thanks", "ty": "thanks",
        "bye": "bye", "goodbye": "bye", "talk later": "bye",
        "good morning": "good_morning", "morning": "good_morning",
        "good night": "good_night", "night": "good_night",
    }
    if t in exact:
        out.intent = exact[t]
        out.confidence = 0.99
        methods.append("exact")

    # Engine 2: direct questions. These override lower-priority conversation intent.
    direct_patterns: list[tuple[str, str]] = [
        ("ask_name", r"\b(?:what(?:'s| is) your name|your name|who are you|what should i call you)\b"),
        ("ask_age", r"\b(?:how old are you|your age|what(?:'s| is) your age)\b"),
        ("ask_location", r"\b(?:where are you from|where you from|where u from|where r u from|where do you live|where u live|your location|what country are you from|what country u from)\b"),
        ("ask_single", r"\b(?:are you single|do you have a (?:boyfriend|girlfriend|partner)|are you taken)\b"),
        ("how_are_you", r"\b(?:how are you|how you doing|are you okay|you good)\b"),
        ("wyd", r"\b(?:what are you doing|what are you up to)\b"),
        ("ask_hobbies", r"\b(?:what do you do for fun|what are your hobbies|what do you like|what are you into|your interests)\b"),
        ("ask_job", r"\b(?:what do you do|what is your job|do you work|do you study|what are you studying|what do you study)\b"),
        ("ask_authenticity", r"\b(?:are you a bot|are you ai|is this automated|auto reply|real person|are you real|are u real|r u real|is this really you)\b"),
        ("ask_reason", r"^(?:why|why though|how come)\??$"),
        ("ask_explain", r"\b(?:explain|what do you mean|what does that mean|say that again)\b"),
        ("reciprocal_question", r"\b(?:what about you|how about you|and you)\b|^you\??$"),
        ("content_link", r"\b(?:send|give|drop)\s+(?:me\s+)?(?:the\s+)?link\b|\bwhere(?: can i| do i) (?:find|see)\b"),
        ("pic_request", r"\b(?:send|show|give)\s+(?:me\s+)?(?:a\s+)?(?:pic|photo|picture|selfie|video)\b"),
    ]
    for intent, pat in direct_patterns:
        if re.search(pat, t, re.I):
            out.intent = intent
            out.confidence = 0.97
            methods.append("regex")
            out.is_question = True
            break

    # Engine 3: resolve answers using the bot's previous question/message.
    q = (last_question or "").strip().lower()
    topic = out.context_topic
    reciprocal = _has_reciprocal(t)

    if q in {"name", "profile_name"}:
        name = _valid_name(raw)
        if name:
            out.intent = "answer_name_reciprocal" if reciprocal else "answer_name"
            out.entity_type = "name"
            out.entity_value = name
            out.confidence = 0.98
            methods.append("context:name")
        elif out.intent in {"nothing", "idk", "same", "agree", "disagree", "reaction"}:
            methods.append("context:name_ignored")
    elif q == "age":
        age = _valid_age(raw)
        if age:
            out.intent = "answer_age_reciprocal" if reciprocal else "answer_age"
            out.entity_type = "age"
            out.entity_value = age
            out.confidence = 0.98
            methods.append("context:age")
    elif q in {"country", "location"}:
        location = _valid_location(raw)
        if location and out.intent not in {"ask_name", "ask_age", "ask_location", "ask_single"}:
            out.intent = "answer_location_reciprocal" if reciprocal else "answer_location"
            out.entity_type = "location"
            out.entity_value = location
            out.confidence = 0.90
            methods.append("context:location")

    if topic == "how_are_you" and out.intent in {"general_statement", "reciprocal_question"}:
        if re.search(r"\b(?:bad|not good|rough|sad|down|awful|terrible)\b", t):
            out.intent = "answer_status_negative_reciprocal" if reciprocal else "answer_status_negative"
            out.confidence = 0.92
            methods.append("context:status_negative")
        elif re.search(r"\b(?:good|fine|great|okay|alright|well)\b", t) or reciprocal:
            out.intent = "answer_status_reciprocal" if reciprocal else "answer_status_positive"
            out.confidence = 0.92
            methods.append("context:status_positive")

    if topic == "wyd" and out.intent in {"general_statement", "reciprocal_question"}:
        activity = re.sub(r"\s+(?:and you|what about you|how about you)\??$", "", t).strip()
        if activity:
            out.intent = "answer_activity_reciprocal" if reciprocal else "answer_activity"
            out.entity_type = "activity"
            out.entity_value = activity
            out.confidence = 0.86
            methods.append("context:activity")

    if out.intent == "reciprocal_question":
        if topic == "name":
            out.intent = "ask_name"
        elif topic == "age":
            out.intent = "ask_age"
        elif topic == "location":
            out.intent = "ask_location"
        elif topic == "wyd":
            out.intent = "reciprocal_wyd"
        else:
            out.intent = "reciprocal_status"
        out.confidence = 0.90
        methods.append("context:reciprocal")

    # Engine 4: keyword/phrase voting for statements not already resolved.
    if out.confidence < 0.75 and out.intent in {"general_statement", "unknown_question"}:
        votes: dict[str, int] = {}
        phrase_votes: dict[str, dict[str, int]] = {
            "bored": {"bored": 5, "lonely": 4, "nothing to do": 5},
            "busy": {"busy": 5, "working": 3, "at work": 4},
            "tired": {"tired": 5, "sleepy": 4, "exhausted": 5},
            "compliment": {"cute": 3, "pretty": 4, "beautiful": 4, "nice": 2, "gorgeous": 5},
            "apology": {"sorry": 5, "my bad": 5},
            "like_you": {"like you": 5, "miss you": 5, "love you": 5},
            "sad_statement": {"sad": 5, "upset": 5, "bad day": 5, "hurt": 4},
            "anxious_statement": {"anxious": 6, "worried": 5, "stressed": 5, "nervous": 4},
            "angry_statement": {"angry": 6, "mad": 5, "annoyed": 4, "frustrated": 5},
        }
        for intent, terms in phrase_votes.items():
            score = sum(weight for term, weight in terms.items() if term in t)
            if score:
                votes[intent] = score
        if votes:
            winner, score = max(votes.items(), key=lambda item: item[1])
            if score >= 3:
                out.intent = winner
                out.confidence = min(0.89, 0.55 + score / 20)
                methods.append("keyword_vote")

    if out.intent == "general_statement" and emotion.primary != "neutral" and emotion.confidence >= 0.68:
        out.intent = f"emotion_{emotion.primary}"
        out.confidence = emotion.confidence
        methods.append("emotion_route")

    # Compound message refinement: preserve the greeting while answering the question.
    has_greeting = bool(re.match(r"^(?:hi+|hey+|hello+|yo)\b", t))
    if has_greeting and out.intent == "how_are_you":
        out.intent = "greeting_how_are_you"
        out.confidence = 0.98
        methods.append("compound")
    elif has_greeting and out.intent == "wyd":
        out.intent = "greeting_wyd"
        out.confidence = 0.98
        methods.append("compound")

    # Unknown question should never be answered with a random unrelated DB pair.
    if out.intent == "general_statement" and is_question:
        out.intent = "unknown_question"
        out.confidence = 0.65
        out.allow_db_match = False
        methods.append("question_guard")

    out.is_ambiguous = len(tokens) <= 1 and out.intent in {"general_statement", "reaction", "agree", "disagree"}
    if out.is_short:
        out.allow_db_match = False
    out.should_ask_profile = (
        not out.is_question
        and not out.is_ambiguous
        and out.intent in {"general_statement", "bored", "busy", "tired", "compliment", "like_you", "emotion_happy", "emotion_excited"}
        and msg_count >= 3
    )
    out.methods = tuple(methods)
    return out


def _owner_name() -> str:
    return str(getattr(config, "BOT_NAME", "eva") or "eva").strip().lower()


def _identity_prefix() -> str:
    return _owner_name()


def _ensure_basic_reciprocal(value: str) -> str:
    """Return one short answer ending in a single `you?`."""
    text = clean_reply(value or "").strip()
    text = re.sub(
        r"\s*[,;.!]?\s*(?:and\s+)?(?:what\s+about\s+)?(?:you|u)\s*\??\s*$",
        "",
        text,
        flags=re.I,
    ).strip(" ,;.!?")
    return f"{text}, you?" if text else "you?"


def _pending_profile_question(last_question: str) -> tuple[str, str]:
    q = (last_question or "").strip().lower()
    mapping = {
        "name": ("name", "whats ur name?"),
        "profile_name": ("name", "whats ur name?"),
        "age": ("age", "how old are u?"),
        "country": ("country", "where are u from?"),
        "location": ("country", "where are u from?"),
    }
    return mapping.get(q, ("", ""))


def _already_reasked_profile(last_bot_reply: str, field: str) -> bool:
    value = normalize(last_bot_reply)
    patterns = {
        "name": r"\b(?:what is your name|whats your name|whats ur name|your name)\b",
        "age": r"\b(?:how old are you|how old are u|how old r u|your age)\b",
        "country": r"\b(?:where are you from|where are u from|where u from|where r u from)\b",
    }
    return bool(field and re.search(patterns.get(field, r"$^"), value, re.I))


def pending_basic_field(intent: str) -> str:
    return {
        "ask_name": "name",
        "ask_age": "age",
        "ask_location": "country",
    }.get(intent, "")


def is_basic_answer_intent(intent: str) -> bool:
    return intent in {
        "answer_name", "answer_name_reciprocal",
        "answer_age", "answer_age_reciprocal",
        "answer_location", "answer_location_reciprocal",
    }


def direct_reply(
    analysis: Analysis,
    *,
    profile_name: str = "",
    last_question: str = "",
    last_bot_reply: str = "",
    allow_adult: bool = False,
) -> tuple[str | None, str]:
    """Return a high-confidence short reply, or None for later engines."""
    intent = analysis.intent

    # Persona facts are controlled locally and rotate wording. Handle them before
    # the generated local intent index so fixed database text cannot override them.
    if intent == "ask_name":
        return _ensure_basic_reciprocal(pick("name")), "brain:ask_name"
    if intent == "ask_age":
        return _ensure_basic_reciprocal(pick("age")), "brain:ask_age"
    if intent == "ask_location":
        return _ensure_basic_reciprocal(pick("location")), "brain:ask_location"
    if intent == "ask_single":
        return pick("single"), "brain:ask_single"
    if intent == "ask_authenticity":
        return pick("authenticity"), "brain:ask_authenticity"
    if intent in {"how_are_you", "greeting_how_are_you"}:
        return "im good, you?", "brain:how_are_you"
    if intent in {"wyd", "greeting_wyd"}:
        return "just chilling rn, you?", "brain:wyd"

    pending_field, pending_question = _pending_profile_question(last_question)
    if pending_field and intent in {"acknowledgement", "agree", "reaction", "general_statement"}:
        if _already_reasked_profile(last_bot_reply, pending_field):
            analysis.intent = f"pending_{pending_field}_closed"
            return random.choice(["haha okay", "okay lol", "all good"]), f"brain:pending_{pending_field}_closed"
        analysis.intent = f"pending_{pending_field}_retry"
        return pending_question, f"brain:pending_{pending_field}_retry"

    local_result = generate_rule_reply(
        analysis.raw,
        last_question=last_question,
        profile_name=profile_name,
        allow_adult=allow_adult,
    )
    if local_result and any(i in {"ask_link", "pic_request"} for i in local_result.intents):
        return None, ""
    if local_result and local_result.confidence >= float(getattr(config, "LOCAL_INTENT_MIN_CONFIDENCE", 0.78)):
        return local_result.reply, f"{local_result.source}:{'+'.join(local_result.intents)}"

    owner = _owner_name()
    identity = _identity_prefix()
    age = getattr(config, "BOT_AGE", 21)
    location = str(getattr(config, "BOT_LOCATION", "") or "").strip().lower()

    replies: dict[str, list[str]] = {
        "greeting": ["hey", "hey, im eva"],
        "greeting_how_are_you": ["im good"],
        "greeting_wyd": ["just chilling rn"],
        "how_are_you": ["im good", "doing good"],
        "wyd": ["just chilling rn", "not much rn"],
        "nothing": ["same, just chilling.", "fair, quiet day then."],
        "same": ["same here lol.", "we agree then lol."],
        "idk": ["no pressure. think about it."],
        "agree": ["got it.", "okay, noted."],
        "disagree": ["got it, no problem."],
        "reaction": ["lol.", "haha fair."],
        "acknowledgement": ["thanks", "yeah", "haha yeah"],
        "thanks": ["anytime.", "youre welcome."],
        "bye": ["talk later.", "okay, take care."],
        "good_morning": ["morning"],
        "good_night": ["good night. sleep well."],
        "bored": ["same", "same lol"],
        "busy": ["got it. text when free."],
        "tired": ["get some rest then."],
        "compliment": ["thats sweet, thank u."],
        "apology": ["all good, no worries."],
        "like_you": ["thats sweet of u."],
        "ask_reason": ["not sure what part u mean"],
        "ask_explain": ["not sure what part u mean"],
        "unsupported_language": [str(getattr(config, "ENGLISH_ONLY_REPLY", "sorry, english only here"))],
        "answer_status_positive": ["glad youre doing good."],
        "answer_status_negative": ["sorry to hear that"],
        "answer_status_reciprocal": ["glad youre good. im good too."],
        "answer_status_negative_reciprocal": ["sorry to hear that. im okay."],
        "reciprocal_status": ["im good too."],
        "reciprocal_wyd": ["just chilling rn."],
    }

    if intent == "ask_job":
        return "i keep that private", "brain:ask_job"
    if intent == "ask_hobbies":
        return "i keep that private for now", "brain:ask_hobbies"
    if intent == "pic_request":
        return "i cant send photos here, but we can chat.", "brain:pic_request"
    if intent == "content_link" and not bool(getattr(config, "SITE_REDIRECT_ENABLED", False)):
        return "no link is available here rn.", "brain:link_disabled"
    if intent in {"answer_name", "answer_name_reciprocal"} and analysis.entity_value:
        if intent.endswith("reciprocal"):
            return f"nice to meet u, {analysis.entity_value}. this is {identity}.", "brain:answer_name_reciprocal"
        return f"nice to meet u, {analysis.entity_value}.", "brain:answer_name"
    if intent in {"answer_age", "answer_age_reciprocal"} and analysis.entity_value:
        if intent.endswith("reciprocal"):
            return f"got it, {analysis.entity_value}. {owner} is {age}.", "brain:answer_age_reciprocal"
        return f"got it, {analysis.entity_value}.", "brain:answer_age"
    if intent in {"answer_location", "answer_location_reciprocal"} and analysis.entity_value:
        if intent.endswith("reciprocal"):
            return f"nice, {analysis.entity_value}. {owner} is from {location}.", "brain:answer_location_reciprocal"
        return f"nice, {analysis.entity_value}.", "brain:answer_location"
    if intent in {"answer_activity", "answer_activity_reciprocal"}:
        if intent.endswith("reciprocal"):
            return "got it. im just chilling rn.", "brain:answer_activity_reciprocal"
        return "got it.", "brain:answer_activity"
    if intent.startswith("emotion_"):
        planned = emotion_reply(intent.removeprefix("emotion_"))
        if planned:
            return planned, f"brain:{intent}"
    if intent in {"sad_statement", "anxious_statement", "angry_statement"}:
        mapping = {"sad_statement": "sad", "anxious_statement": "anxious", "angry_statement": "angry"}
        planned = emotion_reply(mapping[intent])
        if planned:
            return planned, f"brain:{intent}"
    if intent in replies:
        return random.choice(replies[intent]), f"brain:{intent}"
    return None, ""


def _basic_reciprocal_question(text: str) -> bool:
    value = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return bool(re.search(
        r"\b(?:how are you|how are u|how r u|hru|you good|u good|"
        r"wyd|what are you doing|what are u doing|what u doing|what u up to|"
        r"what(?:'s| is) your name|whats ur name|who are you|who r u|"
        r"how old are you|how old r u|your age|ur age|"
        r"where are you from|where you from|where u from|where r u from|where do you live|where u live|what country)\b",
        value,
        re.I,
    ))


def _limit_questions(reply: str, user_text: str = "") -> str:
    # A single explicit pending profile question is allowed after the user
    # acknowledges but does not answer a reciprocal basic-info question.
    candidate = clean_reply(reply or "").strip()
    if re.fullmatch(
        r"(?:whats ur name|how old are u|where are u from)\?",
        candidate,
        flags=re.I,
    ):
        return candidate

    # Only basic reciprocal questions are allowed in direct mode.
    if bool(getattr(config, "BASIC_RECIPROCAL_QUESTIONS_ENABLED", True)) and _basic_reciprocal_question(user_text):
        value = clean_reply(reply or "")
        value = re.sub(
            r"\s*[,;.!]?\s*(?:and\s+)?(?:what\s+about\s+)?(?:you|u)\s*\?\s*$",
            "",
            value,
            flags=re.I,
        ).strip(" ,;.!?")
        if "?" in value:
            value = value.split("?", 1)[0].strip(" ,;.!?")
        return f"{value}, you?" if value else "you?"

    if bool(getattr(config, "AI_ASK_FOLLOWUP_QUESTIONS", False)):
        if reply.count("?") <= 1:
            return reply
        return reply[: reply.find("?") + 1]
    value = clean_reply(reply or "")
    value = re.sub(r"\s*[,;.!]\s*(?:and\s+you|and\s+u|u|you)\s*\?\s*$", "", value, flags=re.I)
    value = re.sub(r"\s*[,;.!]\s*(?:what|what's|whats|how|why|where|when|who|which)\b[^?]*\?\s*$", "", value, flags=re.I)
    if "?" in value:
        before = value.split("?", 1)[0].strip(" ,;.!?")
        if re.match(r"^(?:what|what's|whats|how|why|where|when|who|which|do|did|are|is|can|could|would|will)\b", before, re.I):
            return "not sure what u mean"
        value = before
    return value.strip(" ,;.!?")


def validate_reply(reply: str, recent_assistant_replies: list[str] | None = None, user_text: str = "") -> str:
    reply = clean_reply(reply or "")
    reply = _limit_questions(reply, user_text=user_text)
    reply = truncate_words(reply, int(getattr(config, "MAX_REPLY_WORDS", 12)))
    recent = {normalize(x) for x in (recent_assistant_replies or []) if x}
    if normalize(reply) in recent:
        return random.choice(["yeah", "fair", "okay lol"])
    return reply or "not sure what u mean"


def _should_use_local_llm(analysis: Analysis) -> bool:
    """v7.8 is deterministic local-DB-only; no model or web API is called."""
    return False


def smart_generate_reply(
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
) -> tuple[str, str, Analysis]:
    """Run deterministic understanding, local intent DB, then curated DB fallback."""
    analysis = analyze_message(
        text,
        last_question=last_question,
        last_bot_reply=last_bot_reply,
        recent_user_texts=recent_user_texts,
        msg_count=msg_count,
    )

    reply, source = direct_reply(analysis, profile_name=profile_name, last_question=last_question, last_bot_reply=last_bot_reply, allow_adult=allow_adult)
    if reply:
        return validate_reply(reply, recent_assistant_replies, user_text=text), source, analysis

    if analysis.allow_db_match:
        matched = curated_match(text, allow_adult=allow_adult)
        if matched and matched["score"] >= float(getattr(config, "SMART_BRAIN_MIN_DB_CONFIDENCE", 0.78)):
            return validate_reply(matched["reply"], recent_assistant_replies, user_text=text), f"brain_db:{matched['tier']}:{matched['score']:.2f}", analysis

    if analysis.is_question:
        reply = "not sure what u mean"
        source = "brain:fallback_question"
    elif analysis.is_short:
        reply = "got it"
        source = "brain:fallback_short"
    else:
        reply = random.choice(["got it", "i get u", "fair enough"])
        source = "brain:fallback_statement"

    return validate_reply(reply, recent_assistant_replies, user_text=text), source, analysis


async def smart_generate_reply_async(*args, **kwargs):
    """DeepSeek for non-adult messages; local DB only for adult messages."""
    import asyncio
    from core.ai_client import generate_normal_reply, is_configured
    from core.reply_engine import is_adult_message, generate_adult_local_reply

    text = kwargs.get("text", args[0] if args else "")
    analysis = analyze_message(
        text,
        last_question=kwargs.get("last_question", ""),
        last_bot_reply=kwargs.get("last_bot_reply", ""),
        recent_user_texts=kwargs.get("recent_user_texts") or [],
        msg_count=int(kwargs.get("msg_count", 0) or 0),
    )

    adultish = bool(is_adult_message(text) or has_adult_intent(text))
    if adultish:
        reply, source = await generate_adult_local_reply(
            text,
            user_profile=kwargs.get("user_profile") or {},
            horny_score=float((kwargs.get("user_profile") or {}).get("horny_score", 0.0) or 0.0),
        )
        return validate_reply(reply, kwargs.get("recent_assistant_replies") or [], user_text=text), source, analysis

    # Basic reciprocal exchanges are deterministic so the model cannot turn
    # a simple profile question into a robotic or unrelated response.
    if analysis.intent in {
        "ask_name", "ask_age", "ask_location",
        "how_are_you", "greeting_how_are_you", "wyd", "greeting_wyd",
    }:
        basic_reply, basic_source = direct_reply(
            analysis,
            profile_name=kwargs.get("profile_name", ""),
            last_question=kwargs.get("last_question", ""),
            last_bot_reply=kwargs.get("last_bot_reply", ""),
            allow_adult=False,
        )
        if basic_reply:
            return validate_reply(
                basic_reply,
                kwargs.get("recent_assistant_replies") or [],
                user_text=text,
            ), basic_source, analysis

    # A pending basic-info exchange is deterministic so the API cannot answer
    # an acknowledgement like "nice" with a robotic generic response.
    pending_field, _pending_question = _pending_profile_question(kwargs.get("last_question", ""))
    if pending_field and analysis.intent in {"acknowledgement", "agree", "reaction", "general_statement"}:
        pending_reply, pending_source = direct_reply(
            analysis,
            profile_name=kwargs.get("profile_name", ""),
            last_question=kwargs.get("last_question", ""),
            last_bot_reply=kwargs.get("last_bot_reply", ""),
            allow_adult=False,
        )
        if pending_reply:
            return validate_reply(
                pending_reply,
                kwargs.get("recent_assistant_replies") or [],
                user_text=text,
            ), pending_source, analysis

    if bool(getattr(config, "AI_ENABLED", True)) and is_configured():
        profile = kwargs.get("user_profile") or {}
        ai_reply = await generate_normal_reply(
            text,
            history=kwargs.get("conversation_history") or [],
            stage=str(profile.get("funnel_stage", "basic_info") or "basic_info"),
            vibe=str(profile.get("last_vibe", "neutral") or "neutral"),
            profile_name=str(kwargs.get("profile_name", "") or profile.get("profile_name", "") or ""),
            user_country=str(profile.get("country", "") or ""),
            msg_count=int(kwargs.get("msg_count", 0) or 0),
            link_sent=bool(profile.get("promo_sent", 0)),
            region_priority=str(profile.get("region_priority", "unknown") or "unknown"),
            recent_assistant_replies=kwargs.get("recent_assistant_replies") or [],
        )
        if ai_reply:
            return validate_reply(ai_reply, kwargs.get("recent_assistant_replies") or [], user_text=text), "deepseek:all_non_adult", analysis

    # Reliability fallback only when DeepSeek is missing, unavailable, or rejects output.
    return await asyncio.to_thread(smart_generate_reply, *args, **kwargs)

