"""
EVA BOT v7.1 — Local DB Matcher
No API, no external model. Layered local text-db routing:
1) exact phrase
2) keyword overlap
3) SQLite FTS5
4) RapidFuzz fallback

Adult DB files are indexed separately and are only searched when allow_adult=True.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import config
from utils import clean_text, log

BASE = Path(__file__).resolve().parent.parent
INDEX_DB_PATH = BASE / "data" / "fast_index.db"
MEGA_DB_PATH = BASE / "data" / "mega_index.db"
MEGA_FILE = BASE / "local_db" / "10_million_pairs.txt"

SAFE_DB_FILES = [
    "local_db/01_greetings.txt",
    "local_db/02_identity_profile.txt",
    "local_db/03_smalltalk.txt",
    "local_db/04_flirty_safe.txt",
    "local_db/04b_flirty_extra.txt",
    "local_db/05_links_objections.txt",
    "local_db/06_normal_chat.txt",
    "local_db/07_engagement_hooks.txt",
    "local_db/08_curiosity_triggers.txt",
    "local_db/09_multilang.txt",
    "data/basic_replies.txt",
]

if config.USE_LEARNED_PAIRS_FOR_MATCHING:
    SAFE_DB_FILES.append("learned_pairs.txt")

ADULT_DB_FILES = [
    "local_db/60_adult_gate_safe.txt",
    "local_db/70_adult_consensual_curated.txt",
]

# Pair shape: {"user": str, "reply": str, "adult": bool, "source": str}
_pairs: list[dict] = []
_safe_indices: list[int] = []
_adult_indices: list[int] = []
_exact_safe: dict[str, list[int]] = defaultdict(list)
_exact_adult: dict[str, list[int]] = defaultdict(list)
_keyword_safe: dict[str, list[int]] = defaultdict(list)
_keyword_adult: dict[str, list[int]] = defaultdict(list)
_fts_ready = False
_index_built = False

_WORD_RE = re.compile(r"[a-z0-9']+")

# Mega DB FTS5 state
_mega_fts_ready = False


def _tokens(text: str) -> list[str]:
    t = clean_text(text or "")
    return [w for w in _WORD_RE.findall(t) if len(w) >= 2]


def _read_pairs(fname: str, adult: bool) -> Iterable[dict]:
    fp = BASE / fname
    if not fp.exists():
        return []
    out = []
    try:
        for line in fp.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or "|||" not in line:
                continue
            user_text, reply = line.split("|||", 1)
            user_text = clean_text(user_text)
            reply = reply.strip()
            if len(user_text) < 1 or len(reply) < 1:
                continue
            out.append({"user": user_text, "reply": reply, "adult": adult, "source": fname})
    except Exception as e:
        log(f"FAST_MATCHER: load failed {fname}: {e}")
    return out


def _add_pair(pair: dict, seen: set[tuple[str, str, bool]]) -> None:
    key = (pair["user"], pair["reply"].lower(), bool(pair["adult"]))
    if key in seen:
        return
    seen.add(key)
    idx = len(_pairs)
    _pairs.append(pair)
    adult = bool(pair["adult"])
    if adult:
        _adult_indices.append(idx)
        _exact_adult[pair["user"]].append(idx)
        kw = _keyword_adult
    else:
        _safe_indices.append(idx)
        _exact_safe[pair["user"]].append(idx)
        kw = _keyword_safe
    for token in set(_tokens(pair["user"])):
        kw[token].append(idx)


def build_index(force: bool = False) -> int:
    """Build/rebuild all local DB indexes. Call once at startup."""
    global _pairs, _safe_indices, _adult_indices, _fts_ready, _index_built
    global _exact_safe, _exact_adult, _keyword_safe, _keyword_adult

    if _index_built and not force:
        return len(_pairs)

    start = time.monotonic()
    _pairs = []
    _safe_indices = []
    _adult_indices = []
    _exact_safe = defaultdict(list)
    _exact_adult = defaultdict(list)
    _keyword_safe = defaultdict(list)
    _keyword_adult = defaultdict(list)
    seen: set[tuple[str, str, bool]] = set()

    for fname in SAFE_DB_FILES:
        for pair in _read_pairs(fname, adult=False):
            _add_pair(pair, seen)
    for fname in ADULT_DB_FILES:
        for pair in _read_pairs(fname, adult=True):
            _add_pair(pair, seen)

    _fts_ready = False
    try:
        conn = sqlite3.connect(str(INDEX_DB_PATH))
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("DROP TABLE IF EXISTS fts_pairs")
        conn.execute(
            "CREATE VIRTUAL TABLE fts_pairs USING fts5(user_text, reply, adult UNINDEXED, source UNINDEXED, idx UNINDEXED, tokenize='porter unicode61')"
        )
        conn.execute("BEGIN")
        for idx, p in enumerate(_pairs):
            conn.execute(
                "INSERT INTO fts_pairs(user_text, reply, adult, source, idx) VALUES(?,?,?,?,?)",
                (p["user"], p["reply"], 1 if p["adult"] else 0, p["source"], idx),
            )
        conn.execute("COMMIT")
        conn.close()
        _fts_ready = True
    except Exception as e:
        log(f"FAST_MATCHER: FTS unavailable, using memory/fuzzy only: {e}")

    _index_built = True
    elapsed = time.monotonic() - start
    log(
        "FAST_MATCHER: local index built — "
        f"{len(_safe_indices)} safe + {len(_adult_indices)} adult pairs in {elapsed:.2f}s"
    )
    # Build mega FTS5 in background-ish (sequential but cached)
    _build_mega_fts()
    return len(_pairs)


def _candidate_indices(scope: str, allow_adult: bool) -> list[int]:
    if scope == "safe" or not allow_adult:
        return _safe_indices
    if scope == "adult":
        return _adult_indices + _safe_indices
    return _safe_indices + (_adult_indices if allow_adult else [])


def _exact_lookup(text: str, scope: str, allow_adult: bool) -> str | None:
    t = clean_text(text or "")
    buckets: list[dict[str, list[int]]] = []
    if scope == "adult" and allow_adult:
        buckets = [_exact_adult, _exact_safe]
    elif scope == "all" and allow_adult:
        buckets = [_exact_safe, _exact_adult]
    else:
        buckets = [_exact_safe]
    for bucket in buckets:
        ids = bucket.get(t)
        if ids:
            # Prefer curated files listed earlier over noisy generated/learned files.
            source_rank = {name: i for i, name in enumerate(SAFE_DB_FILES + ADULT_DB_FILES)}
            best_rank = min(source_rank.get(_pairs[idx]["source"], 9999) for idx in ids)
            preferred = [idx for idx in ids if source_rank.get(_pairs[idx]["source"], 9999) == best_rank]
            return _pairs[random.choice(preferred)]["reply"]
    return None


def _keyword_lookup(text: str, scope: str, allow_adult: bool) -> str | None:
    words = _tokens(text)
    # One-word messages are too ambiguous for keyword matching. Exact routing or
    # deterministic short-message handlers should own them.
    if len(words) < 2:
        return None

    buckets = []
    if scope == "adult" and allow_adult:
        buckets = [_keyword_adult, _keyword_safe]
    elif scope == "all" and allow_adult:
        buckets = [_keyword_safe, _keyword_adult]
    else:
        buckets = [_keyword_safe]

    for kw_index in buckets:
        scores: dict[int, float] = defaultdict(float)
        for w in words:
            for idx in kw_index.get(w, []):
                scores[idx] += 1.0
        if not scores:
            continue
        scored = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:10]
        best_score = scored[0][1]
        # Need enough overlap, but allow short messages to match direct phrases.
        needed = max(1.0, min(len(words), 3) * 0.60)
        if best_score >= needed:
            best = [idx for idx, score in scored if score == best_score]
            return _pairs[random.choice(best)]["reply"]
    return None


def _fts5_search(text: str, scope: str, allow_adult: bool, limit: int = 5) -> list[str]:
    if not _fts_ready:
        return []
    words = _tokens(text)
    if len(words) < 2:
        return []

    try:
        # Prefix search improves typo/partial recall without calling any API.
        query = " OR ".join(f'"{w}"' for w in words[:8])
        adult_clause = "adult IN (0,1)" if allow_adult and scope == "all" else "adult=0"
        if allow_adult and scope == "adult":
            adult_clause = "adult=1"
        conn = sqlite3.connect(str(INDEX_DB_PATH))
        rows = conn.execute(
            f"SELECT reply FROM fts_pairs WHERE fts_pairs MATCH ? AND {adult_clause} ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _build_mega_fts() -> bool:
    """Build FTS5 index for the mega 10M pair database. Only builds if missing."""
    global _mega_fts_ready
    if _mega_fts_ready:
        return True
    if not MEGA_FILE.exists():
        return False
    # Skip if already built and fresh (< 24h old)
    if MEGA_DB_PATH.exists():
        age_h = (time.time() - MEGA_DB_PATH.stat().st_mtime) / 3600
        if age_h < 24:
            _mega_fts_ready = True
            return True
    try:
        log("FAST_MATCHER: building mega FTS5 index (10M pairs)...")
        t0 = time.monotonic()
        conn = sqlite3.connect(str(MEGA_DB_PATH))
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-200000")
        conn.execute("DROP TABLE IF EXISTS mega_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE mega_fts USING fts5(user_text, reply, tokenize='porter unicode61')"
        )
        sql = "INSERT INTO mega_fts(user_text, reply) VALUES(?,?)"
        cur = conn.cursor()
        cur.execute("BEGIN")
        count = 0
        batch_size = 50000
        batch = []
        for line in MEGA_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or "|||" not in line:
                continue
            parts = line.split("|||", 1)
            user_text = clean_text(parts[0])
            reply = parts[1].strip()
            if len(user_text) >= 1 and len(reply) >= 1:
                batch.append((user_text, reply))
                count += 1
                if len(batch) >= batch_size:
                    cur.executemany(sql, batch)
                    conn.commit()
                    cur.execute("BEGIN")
                    batch = []
        if batch:
            cur.executemany(sql, batch)
        conn.commit()
        conn.execute("PRAGMA optimize")
        conn.close()
        elapsed = time.monotonic() - t0
        log(f"FAST_MATCHER: mega FTS5 built — {count:,} pairs in {elapsed:.1f}s")
        _mega_fts_ready = True
        return True
    except Exception as e:
        log(f"FAST_MATCHER: mega FTS5 build failed: {e}")
        return False


def _mega_fts_search(text: str, limit: int = 5) -> list[str]:
    """Search the 10M mega FTS5 index. Returns top replies."""
    if not _mega_fts_ready:
        return []
    words = _tokens(text)
    if len(words) < 1:
        return []
    try:
        query = " OR ".join(f'"{w}"' for w in words[:6])
        conn = sqlite3.connect(str(MEGA_DB_PATH))
        rows = conn.execute(
            "SELECT reply FROM mega_fts WHERE mega_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _fuzzy_search(text: str, scope: str, allow_adult: bool, threshold: int = 64) -> str | None:
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return None

    t = clean_text(text or "")
    if len(t) < 3 or len(_tokens(t)) < 2:
        return None

    indices = _candidate_indices(scope, allow_adult)
    if not indices:
        return None

    best_score = 0
    best_indices: list[int] = []
    # 60k cap keeps worst-case latency controlled.
    for idx in indices[:60000]:
        s = fuzz.token_sort_ratio(t, _pairs[idx]["user"])
        if s > best_score:
            best_score = s
            best_indices = [idx]
        elif s == best_score:
            best_indices.append(idx)

    if best_score >= threshold and best_indices:
        return _pairs[random.choice(best_indices)]["reply"]
    return None


def fast_match(text: str, allow_adult: bool = False, scope: str = "safe") -> tuple[str | None, str, float]:
    """
    Return (reply, tier_used, elapsed_ms).

    scope:
    - safe: only non-adult DB files
    - adult: adult DB first, then safe fallback; requires allow_adult=True
    - all: safe + adult when allow_adult=True
    """
    if not _index_built:
        build_index()

    start = time.monotonic()
    if scope not in {"safe", "adult", "all"}:
        scope = "safe"

    checks = []
    if config.MATCH_EXACT_ENABLED:
        checks.append(("exact", _exact_lookup))
    if config.MATCH_PARTIAL_ENABLED:
        checks.append(("keyword", _keyword_lookup))
    for tier, fn in checks:
        reply = fn(text, scope, allow_adult)
        if reply:
            return reply, tier, (time.monotonic() - start) * 1000

    if config.MATCH_PARTIAL_ENABLED:
        fts = _fts5_search(text, scope, allow_adult, limit=5)
        if fts:
            return random.choice(fts[:3]), "fts5", (time.monotonic() - start) * 1000

    # Fallback: search the 10M mega FTS5 index
    mega = _mega_fts_search(text, limit=5)
    if mega:
        return random.choice(mega[:3]), "mega_fts5", (time.monotonic() - start) * 1000

    if config.MATCH_FUZZY_ENABLED:
        reply = _fuzzy_search(text, scope, allow_adult, threshold=int(config.MATCH_THRESHOLD))
        if reply:
            return reply, "fuzzy", (time.monotonic() - start) * 1000

    return None, "no_match", (time.monotonic() - start) * 1000


async def cached_fast_match(text: str, allow_adult: bool = False, scope: str = "safe") -> tuple[str | None, str, float]:
    # Kept for compatibility. Local memory/SQLite is already fast; Redis is optional.
    return fast_match(text, allow_adult=allow_adult, scope=scope)


def search_best(text: str, top_n: int = 3, allow_adult: bool = False, scope: str = "safe") -> list[str]:
    if not _index_built:
        build_index()
    replies: list[str] = []

    exact = _exact_lookup(text, scope, allow_adult)
    if exact:
        replies.append(exact)
    kw = _keyword_lookup(text, scope, allow_adult)
    if kw and kw not in replies:
        replies.append(kw)
    for r in _fts5_search(text, scope, allow_adult, limit=top_n * 2):
        if r not in replies:
            replies.append(r)
        if len(replies) >= top_n:
            break
    # Fallback: mega FTS5
    if len(replies) < top_n:
        for r in _mega_fts_search(text, limit=top_n * 2):
            if r not in replies:
                replies.append(r)
            if len(replies) >= top_n:
                break
    if len(replies) < top_n:
        fuzzy = _fuzzy_search(text, scope, allow_adult, threshold=55)
        if fuzzy and fuzzy not in replies:
            replies.append(fuzzy)
    return replies[:top_n]


def fast_match_candidates(text: str, allow_adult: bool = False, scope: str = "safe", top_n: int = 3) -> list[tuple[str, str, float]]:
    """Return top N candidates as [(reply, tier, score), ...] for the pipeline."""
    if not _index_built:
        build_index()

    if scope not in {"safe", "adult", "all"}:
        scope = "safe"

    candidates: list[tuple[str, str, float]] = []
    seen_replies: set[str] = set()

    exact = _exact_lookup(text, scope, allow_adult)
    if exact and exact not in seen_replies:
        candidates.append((exact, "exact", 1.0))
        seen_replies.add(exact)

    kw = _keyword_lookup(text, scope, allow_adult)
    if kw and kw not in seen_replies:
        candidates.append((kw, "keyword", 0.85))
        seen_replies.add(kw)

    fts_results = _fts5_search(text, scope, allow_adult, limit=top_n * 2)
    for r in fts_results:
        if r not in seen_replies:
            candidates.append((r, "fts5", 0.75))
            seen_replies.add(r)
        if len(candidates) >= top_n:
            break

    if len(candidates) < top_n:
        mega_results = _mega_fts_search(text, limit=top_n * 2)
        for r in mega_results:
            if r not in seen_replies:
                candidates.append((r, "mega_fts5", 0.65))
                seen_replies.add(r)
            if len(candidates) >= top_n:
                break

    if len(candidates) < top_n and config.MATCH_FUZZY_ENABLED:
        fuzzy = _fuzzy_search(text, scope, allow_adult, threshold=max(55, int(config.MATCH_THRESHOLD) - 15))
        if fuzzy and fuzzy not in seen_replies:
            candidates.append((fuzzy, "fuzzy", 0.60))
            seen_replies.add(fuzzy)

    return candidates[:top_n]


def get_stats() -> dict:
    return {
        "total_pairs": len(_pairs),
        "safe_pairs": len(_safe_indices),
        "adult_pairs": len(_adult_indices),
        "keywords": len(set(_keyword_safe) | set(_keyword_adult)),
        "fts_ready": _fts_ready,
        "mega_fts_ready": _mega_fts_ready,
        "index_built": _index_built,
        "index_db_size_mb": round(INDEX_DB_PATH.stat().st_size / 1024 / 1024, 1) if INDEX_DB_PATH.exists() else 0,
        "mega_db_size_mb": round(MEGA_DB_PATH.stat().st_size / 1024 / 1024, 1) if MEGA_DB_PATH.exists() else 0,
    }
