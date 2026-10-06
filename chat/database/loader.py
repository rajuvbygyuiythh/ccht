"""Load all EVA reply databases from filesystem.

Handles three source formats:
  1. ``|||``-delimited text files  (Source A: eva-bot/, Source B: telegram bot/eva_bot_v7/)
  2. EvaFlow JSON rule files       (Source C: EvaFlow_STAGE_SEPARATE_DB_DIRECT_RUN_v2/)
  3. ``promote_extract.txt``       (structured prompt/personality spec)

All loaders return plain Python dicts/lists so the rest of chat/ has zero
filesystem dependencies at runtime.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Paths — resolved relative to this file at import time.
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)

# External DB sources — configurable via environment variable or fallback to
# the original author's local path.  When the paths don't exist the loaders
# return empty results silently (the bot still works with built-in pools).
_RANDOM_BOT_ROOT = os.environ.get(
    "EVA_BOT_DB_ROOT",
    r"C:\Users\papi\Desktop\random bot\h io tg",
)

_SOURCE_A_DIR = os.path.join(_RANDOM_BOT_ROOT, "eva-bot")
_SOURCE_B_DIR = os.path.join(_RANDOM_BOT_ROOT, "telegram bot", "eva_bot_v7")
_SOURCE_C_DIR = os.path.join(
    _RANDOM_BOT_ROOT,
    "EvaFlow_STAGE_SEPARATE_DB_DIRECT_RUN_v2",
    "data",
    "replies",
)

# Local fallback: data/replies/ inside this project (user can copy DB files here)
_LOCAL_REPLIES_DIR = os.path.join(_PROJECT_ROOT, "data", "replies")


# ---------------------------------------------------------------------------
# |||-delimited text file parser
# ---------------------------------------------------------------------------

def _parse_pipe_file(path: str) -> List[Tuple[str, str]]:
    """Parse a ``user_msg ||| bot_reply`` text file.

    Returns a list of ``(user_msg, bot_reply)`` tuples.  Lines that don't
    contain ``|||`` or are blank are silently skipped.
    """
    pairs: List[Tuple[str, str]] = []
    if not os.path.isfile(path):
        return pairs
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n\r")
            if "|||" not in line:
                continue
            parts = line.split("|||", 1)
            if len(parts) != 2:
                continue
            user_msg = parts[0].strip()
            bot_reply = parts[1].strip()
            if user_msg and bot_reply:
                pairs.append((user_msg, bot_reply))
    return pairs


def load_pipe_db(
    *,
    source_a: bool = True,
    source_b: bool = True,
) -> Dict[str, List[str]]:
    """Load all ``|||``-delimited reply files and return a merged lookup.

    Returns
    -------
    ``{ normalised_user_msg: [reply1, reply2, ...] }``
    with *earlier* files taking precedence (source A files are loaded before
    source B, so A wins on collision).
    """
    files: List[str] = []
    if source_a:
        for name in (
            "basic_replies.txt",
            "normal_goal_10k_db.txt",
            "adult_unique_varied_15k_db.txt",
            "adult_generated_db.txt",
        ):
            # Try external source first, then local fallback
            ext_path = os.path.join(_SOURCE_A_DIR, name)
            local_path = os.path.join(_LOCAL_REPLIES_DIR, name)
            if os.path.isfile(ext_path):
                files.append(ext_path)
            elif os.path.isfile(local_path):
                files.append(local_path)
    if source_b:
        for name in (
            "basic_replies.txt",
            "learned_pairs.txt",
            "sexting_dataset.txt",
            "flirty_db.txt",
        ):
            ext_path = os.path.join(_SOURCE_B_DIR, name)
            local_path = os.path.join(_LOCAL_REPLIES_DIR, name)
            if os.path.isfile(ext_path):
                files.append(ext_path)
            elif os.path.isfile(local_path):
                files.append(local_path)

    lookup: Dict[str, List[str]] = {}
    for path in files:
        for user_msg, bot_reply in _parse_pipe_file(path):
            key = user_msg.lower().strip()
            if key not in lookup:
                lookup[key] = []
            # Avoid exact duplicate replies
            if bot_reply not in lookup[key]:
                lookup[key].append(bot_reply)
    return lookup


# ---------------------------------------------------------------------------
# EvaFlow JSON rule parser
# ---------------------------------------------------------------------------

def _load_single_evoflow_json(path: str) -> List[Dict[str, Any]]:
    """Parse one EvaFlow JSON rule file into a flat list of rule dicts."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []

    raw_rules = data.get("rules", [])
    out: List[Dict[str, Any]] = []
    for r in raw_rules:
        if not r.get("enabled", True):
            continue
        out.append(
            {
                "id": r.get("id", ""),
                "priority": r.get("priority", 100),
                "match_type": r.get("match_type", "contains_any"),
                "questions": [q.lower().strip() for q in r.get("questions", [])],
                "answers": r.get("answers", []),
                "cooldown": r.get("cooldown_seconds", 0),
                "category": r.get("category", ""),
            }
        )
    return out


def load_evoflow_rules() -> Dict[str, List[Dict[str, Any]]]:
    """Load all EvaFlow JSON rule files, grouped by category.

    Returns
    -------
    ``{ category_name: [rule_dict, ...] }``
    sorted by *priority* descending (highest-priority first).
    """
    categories: Dict[str, List[Dict[str, Any]]] = {}
    # Try external source first, then local fallback
    source_dir = _SOURCE_C_DIR if os.path.isdir(_SOURCE_C_DIR) else os.path.join(_LOCAL_REPLIES_DIR, "evoflow")
    if not os.path.isdir(source_dir):
        return categories

    for category_dir in sorted(os.listdir(source_dir)):
        cat_path = os.path.join(source_dir, category_dir)
        if not os.path.isdir(cat_path):
            continue
        for fname in sorted(os.listdir(cat_path)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(cat_path, fname)
            rules = _load_single_evoflow_json(fpath)
            if rules:
                categories.setdefault(category_dir, []).extend(rules)

    for cat in categories:
        categories[cat].sort(key=lambda r: r["priority"], reverse=True)

    return categories


# ---------------------------------------------------------------------------
# Personality / prompt spec loader
# ---------------------------------------------------------------------------

def load_promote_extract() -> str:
    """Return the raw text of promote_extract.txt (the EVA personality spec)."""
    for base in (_SOURCE_A_DIR, _LOCAL_REPLIES_DIR):
        path = os.path.join(base, "promote_extract.txt")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
    return ""


# ---------------------------------------------------------------------------
# Combined loader
# ---------------------------------------------------------------------------

def load_all_databases() -> Dict[str, Any]:
    """Convenience wrapper — loads everything and returns a single dict.

    Returns
    -------
    ``{
        "pipe_pairs":      { user_msg: [reply, ...] },
        "evoflow_rules":   { category: [rule, ...] },
        "personality":     str,
        "pipe_count":      int,   # total user_msg keys
        "evoflow_count":   int,   # total rules across all categories
    }``
    """
    pipe_pairs = load_pipe_db()
    evoflow_rules = load_evoflow_rules()
    personality = load_promote_extract()

    evoflow_count = sum(len(rules) for rules in evoflow_rules.values())

    return {
        "pipe_pairs": pipe_pairs,
        "evoflow_rules": evoflow_rules,
        "personality": personality,
        "pipe_count": len(pipe_pairs),
        "evoflow_count": evoflow_count,
    }
