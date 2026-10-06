"""Session health — load saved accounts into the browser, detect blind sessions.

A saved browser session (``storage_state.json`` + ``metadata.json``) is what
lets an account open chitchat.gg **already logged in** — no password, no
captcha, no login flow.  But a session can die (cookies expired), be empty
(0 bytes / no cookies), be corrupt (bad JSON) or be missing entirely.  Such a
session is *blind*: nothing the browser can load, so a restore attempt fails
late, after a full browser launch, with a vague error.

This module is the single place that answers, **offline and before any
browser launches**:

* is this session loadable?  (:func:`inspect_storage_state`,
  :func:`classify_health`, :func:`session_health`)
* what should the bot do with it?  (:func:`plan_for_account` →
  ``restore`` / ``repair`` / ``skip``)
* do we have ``email:password`` to repair it?  (:func:`load_credentials`,
  :func:`attach_credentials` — ``accounts.txt`` in the project root or cwd,
  ``EVA_ACCOUNTS_FILE`` overrides)
* was the restore really authenticated?  (:data:`PAGE_AUTH_JS` +
  :func:`interpret_auth_probe` + :func:`verify_page`)

Everything here is **standard library only** so it runs in tests, in the CLI
tools and inside the frozen Windows build.  Nothing in this module touches the
network; the only optional browser use is :func:`verify_page` (live check).

Log wording used by callers: a blind session is reported as
``BLIND SESSION`` so it is grep-able in the GUI log.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

ROOT_DIR = Path(__file__).resolve().parent.parent

HEALTH_OK = "ok"
HEALTH_EXPIRING = "expiring"
HEALTH_EXPIRED = "expired"
HEALTH_EMPTY = "empty"
HEALTH_CORRUPT = "corrupt"
HEALTH_MISSING = "missing"

#: states where the browser must NOT be launched from the session
BLIND_STATES = (HEALTH_MISSING, HEALTH_EMPTY, HEALTH_CORRUPT, HEALTH_EXPIRED)

DEFAULT_WARN_DAYS = 3.0
DEAD_SUBDIR = "_dead"
BANNED_FLAG_FILENAME = ".banned"

#: env overrides (also used by the tests)
SESSIONS_DIR_ENV = "EVA_SESSIONS_DIR"
ACCOUNTS_FILE_ENV = "EVA_ACCOUNTS_FILE"

#: domains that carry the chitchat.gg login
AUTH_DOMAIN_HINTS = ("chitchat.gg",)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _now() -> float:
    return time.time()


def _as_epoch(value: Any) -> Optional[float]:
    """Cookie ``expires`` → epoch seconds.  ``-1``/absent = session cookie."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    # Values that are clearly milliseconds (Playwright never writes these, but
    # hand-made files sometimes do) are normalised.
    if number > 10_000_000_000:
        number /= 1000.0
    return number


def _is_auth_domain(domain: Any) -> bool:
    text = str(domain or "").lower()
    return any(hint in text for hint in AUTH_DOMAIN_HINTS)


def project_root() -> Path:
    return ROOT_DIR


# --------------------------------------------------------------------------- #
# Storage state inspection
# --------------------------------------------------------------------------- #

def inspect_storage_state(path: Any) -> Dict[str, Any]:
    """Read a ``storage_state.json`` and summarise it (never raises).

    Returns a dict with ``exists/error/cookies/origins/auth_cookies/...``;
    ``cookies`` counts every cookie, ``auth_cookies`` only the ones for the
    chitchat.gg domain (the ones that actually keep the login).
    """
    info: Dict[str, Any] = {
        "path": str(path or ""),
        "exists": False,
        "error": "",
        "cookies": 0,
        "auth_cookies": 0,
        "session_cookies": 0,
        "live_auth_cookies": 0,
        "expired_auth_cookies": 0,
        "origins": 0,
        "local_storage_items": 0,
        "earliest_expiry": None,
        "latest_expiry": None,
        "days_left": None,
        "now": _now(),
        "size": 0,
    }
    if not path:
        info["error"] = "no storage_state path"
        return info
    file_path = Path(path)
    try:
        info["exists"] = file_path.is_file()
    except OSError:
        info["exists"] = False
    if not info["exists"]:
        info["error"] = "file not found"
        return info
    try:
        info["size"] = file_path.stat().st_size
    except OSError:
        info["size"] = 0
    try:
        raw = file_path.read_text(encoding="utf-8", errors="replace")
        payload = json.loads(raw)
    except Exception as exc:
        info["error"] = f"unreadable: {type(exc).__name__}: {exc}"
        return info
    if not isinstance(payload, dict):
        info["error"] = "unexpected JSON shape (not an object)"
        return info

    now = info["now"]
    earliest: Optional[float] = None
    latest: Optional[float] = None
    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        cookies = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        info["cookies"] += 1
        expires = _as_epoch(cookie.get("expires"))
        if expires is None:
            info["session_cookies"] += 1
        if _is_auth_domain(cookie.get("domain")) or _is_auth_domain(cookie.get("url")):
            info["auth_cookies"] += 1
            if expires is None:
                # A session cookie is only alive while the browser is running;
                # for a *restored* profile it means "still usable today".
                info["live_auth_cookies"] += 1
            elif expires > now:
                info["live_auth_cookies"] += 1
                earliest = expires if earliest is None else min(earliest, expires)
                latest = expires if latest is None else max(latest, expires)
            else:
                info["expired_auth_cookies"] += 1

    origins = payload.get("origins")
    if isinstance(origins, list):
        info["origins"] = len(origins)
        for origin in origins:
            try:
                items = origin.get("localStorage") or origin.get("local_storage") or []
                info["local_storage_items"] += len(items)
            except AttributeError:
                continue

    info["earliest_expiry"] = earliest
    info["latest_expiry"] = latest
    if earliest is not None:
        info["days_left"] = round((earliest - now) / 86400.0, 2)
    return info


def classify_health(info: Dict[str, Any], warn_days: float = DEFAULT_WARN_DAYS) -> Dict[str, Any]:
    """Turn an inspection dict into ``{'state', 'reason', 'blind', ...}``."""
    warn_days = float(warn_days if warn_days is not None else DEFAULT_WARN_DAYS)
    if not info:
        return {"state": HEALTH_MISSING, "reason": "no storage state", "blind": True,
                "info": info or {}}
    if info.get("error") and not info.get("exists"):
        state, reason = HEALTH_MISSING, info.get("error") or "file not found"
    elif info.get("error"):
        state, reason = HEALTH_CORRUPT, info.get("error") or "unreadable"
    elif not info.get("cookies"):
        state, reason = HEALTH_EMPTY, "no cookies in the saved state"
    elif not info.get("live_auth_cookies"):
        state, reason = HEALTH_EXPIRED, (
            f"all {info.get('auth_cookies', 0)} chitchat cookie(s) expired")
    else:
        days_left = info.get("days_left")
        if days_left is not None and days_left <= warn_days:
            state, reason = HEALTH_EXPIRING, (
                f"login cookie expires in {days_left:.2f} day(s)")
        else:
            state, reason = HEALTH_OK, "session looks loadable"
    health = {
        "state": state,
        "reason": reason,
        "blind": state in BLIND_STATES,
        "cookies": info.get("cookies", 0),
        "auth_cookies": info.get("auth_cookies", 0),
        "live_auth_cookies": info.get("live_auth_cookies", 0),
        "session_cookies": info.get("session_cookies", 0),
        "days_left": info.get("days_left"),
        "checked_at": _now(),
        "path": info.get("path", ""),
        "info": info,
    }
    return health


def session_dir_for(account_or_dir: Any) -> Optional[Path]:
    """Best-effort session directory from an account dict / metadata / path."""
    if account_or_dir is None:
        return None
    if isinstance(account_or_dir, (str, os.PathLike)):
        path = Path(account_or_dir)
        if path.is_dir():
            return path
        if path.is_file():
            return path.parent
        return path
    if isinstance(account_or_dir, dict):
        for key in ("session_dir", "folder_path"):
            value = account_or_dir.get(key)
            if value:
                return Path(str(value))
        storage = account_or_dir.get("storage_state_path")
        if storage:
            return Path(str(storage)).parent
    return None


def session_storage_path(account_or_dir: Any) -> Optional[Path]:
    """Resolve ``storage_state.json`` for an account dict / directory."""
    if isinstance(account_or_dir, dict):
        direct = account_or_dir.get("storage_state_path")
        if direct:
            return Path(str(direct))
    session_dir = session_dir_for(account_or_dir)
    if session_dir is None:
        return None
    for name in ("storage_state.json",):
        candidate = session_dir / name
        if candidate.is_file():
            return candidate
    # metadata may point at a differently named file
    meta = session_dir / "metadata.json"
    if meta.is_file():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            name = data.get("storage_state")
            if name:
                return session_dir / str(name)
        except Exception:
            pass
    return session_dir / "storage_state.json"


def read_metadata(account_or_dir: Any) -> Dict[str, Any]:
    """Read a session's ``metadata.json`` (empty dict when missing/broken)."""
    session_dir = session_dir_for(account_or_dir)
    if session_dir is None:
        return {}
    path = session_dir / "metadata.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def session_health(account_or_dir: Any, warn_days: float = DEFAULT_WARN_DAYS) -> Dict[str, Any]:
    """Health for one account dict / session directory."""
    storage = session_storage_path(account_or_dir)
    info = inspect_storage_state(storage) if storage else inspect_storage_state(None)
    health = classify_health(info, warn_days=warn_days)
    health["session_dir"] = str(session_dir_for(account_or_dir) or "")
    if isinstance(account_or_dir, dict):
        health["email"] = str(account_or_dir.get("email") or "")
    return health


def is_blind(account_or_dir: Any, warn_days: float = DEFAULT_WARN_DAYS) -> bool:
    return bool(session_health(account_or_dir, warn_days=warn_days).get("blind"))


def is_banned(account_or_dir: Any) -> bool:
    session_dir = session_dir_for(account_or_dir)
    if session_dir is None:
        return False
    return (session_dir / BANNED_FLAG_FILENAME).exists()


# --------------------------------------------------------------------------- #
# Session roots discovery
# --------------------------------------------------------------------------- #

def default_session_roots(base_dir: Any = None) -> List[Path]:
    """Every place sessions can live, most specific first.

    ``EVA_SESSIONS_DIR`` (env) wins; otherwise both known layouts are scanned —
    the historical root ``account_sessions/`` and the pool layout
    ``data/account_sessions/`` — because the bot has written to both.
    """
    roots: List[Path] = []
    env = str(os.environ.get(SESSIONS_DIR_ENV) or "").strip()
    if env:
        # An explicit override replaces the default locations entirely (it may
        # list several roots separated by os.pathsep).
        for part in env.split(os.pathsep):
            part = part.strip()
            if part:
                roots.append(Path(part).expanduser())
        if base_dir is not None:
            roots.append(Path(base_dir))
        return _dedupe_roots(roots)
    if base_dir is not None:
        base = Path(base_dir)
        roots.append(base if base.name == "account_sessions" else base / "account_sessions")
        roots.append(base / "data" / "account_sessions")
    else:
        roots.append(ROOT_DIR / "account_sessions")
        roots.append(ROOT_DIR / "data" / "account_sessions")
        cwd = Path.cwd()
        if cwd != ROOT_DIR:
            roots.append(cwd / "account_sessions")
    return _dedupe_roots(roots)


def _dedupe_roots(roots: Iterable[Any]) -> List[Path]:
    out: List[Path] = []
    seen: set = set()
    for root in roots:
        try:
            key = str(Path(root).resolve())
        except OSError:
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        out.append(Path(root))
    return out


def find_session_dirs(roots: Optional[Sequence[Any]] = None, *, include_dead: bool = False) -> List[Path]:
    """All ``account_*`` session folders across every root (deduped by key)."""
    directories: List[Path] = []
    seen: set = set()
    for root in (roots if roots is not None else default_session_roots()):
        base = Path(root)
        if not base.is_dir():
            continue
        for entry in sorted(base.glob("account_*")):
            if not entry.is_dir():
                continue
            if not include_dead and entry.parent.name == DEAD_SUBDIR:
                continue
            key = entry.name
            if key in seen:
                continue
            seen.add(key)
            directories.append(entry)
    return directories


# --------------------------------------------------------------------------- #
# Credentials (accounts.txt)
# --------------------------------------------------------------------------- #

def default_accounts_files() -> List[Path]:
    """``accounts.txt`` locations: env override, project root, current dir."""
    files: List[Path] = []
    env = str(os.environ.get(ACCOUNTS_FILE_ENV) or "").strip()
    if env:
        files.append(Path(env).expanduser())
    files.append(ROOT_DIR / "accounts.txt")
    cwd_file = Path.cwd() / "accounts.txt"
    if cwd_file != files[-1]:
        files.append(cwd_file)
    return files


def load_credentials(paths: Optional[Iterable[Any]] = None) -> Dict[str, Dict[str, str]]:
    """Parse ``accounts.txt`` → ``{email_lower: {'email','password','proxy'}}``.

    Supported line formats (``#`` comments and blanks are ignored)::

        email:password
        email:password:proxy_server        (proxy_server may also be host:port)
        email:password:proxy:user:pass
        email|password
    """
    out: Dict[str, Dict[str, str]] = {}
    for path in (paths if paths is not None else default_accounts_files()):
        file_path = Path(path)
        if not file_path.is_file():
            continue
        try:
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            separator = "|" if (":" not in line and "|" in line) else ":"
            parts = [part.strip() for part in line.split(separator)]
            if len(parts) < 2 or not parts[0]:
                continue
            email, password = parts[0], parts[1]
            if not password:
                continue
            entry: Dict[str, str] = {"email": email, "password": password,
                                     "source": str(file_path)}
            if len(parts) >= 3 and parts[2]:
                if len(parts) >= 5:
                    entry["proxy"] = f"{parts[2]}:{parts[3]}:{parts[4]}"
                elif len(parts) == 4:
                    entry["proxy"] = f"{parts[2]}:{parts[3]}"
                else:
                    entry["proxy"] = parts[2]
            out[email.lower()] = entry
    return out


def credentials_for(account_or_email: Any,
                    credentials: Optional[Dict[str, Dict[str, str]]] = None
                    ) -> Optional[Dict[str, str]]:
    """Find the credentials matching a session account (by email)."""
    if isinstance(account_or_email, dict):
        email = str(account_or_email.get("email") or "")
        password = account_or_email.get("password") or ""
        if email and password:
            return {"email": email, "password": str(password)}
    else:
        email = str(account_or_email or "")
    if not email:
        return None
    table = credentials if credentials is not None else load_credentials()
    return table.get(email.strip().lower())


def attach_credentials(account: Dict[str, Any],
                       credentials: Optional[Dict[str, Dict[str, str]]] = None
                       ) -> Dict[str, Any]:
    """Add the matching password to an account dict (in memory only).

    The password is never written to ``metadata.json``.
    """
    if not isinstance(account, dict):
        return account
    if account.get("password"):
        return account
    found = credentials_for(account, credentials)
    if found:
        account["password"] = str(found.get("password") or "")
        account["credentials_source"] = str(found.get("source") or "")
        if found.get("proxy") and not account.get("saved_proxy"):
            account["saved_proxy"] = found.get("proxy")
    return account


# --------------------------------------------------------------------------- #
# The decision table
# --------------------------------------------------------------------------- #

ACTION_RESTORE = "restore"
ACTION_REPAIR = "repair"
ACTION_SKIP = "skip"


def plan_for_account(account: Dict[str, Any], *,
                     credentials: Optional[Dict[str, Dict[str, str]]] = None,
                     warn_days: float = DEFAULT_WARN_DAYS,
                     allow_repair: bool = True) -> Dict[str, Any]:
    """What should the bot do with this account?

    Returns ``{'action': 'restore'|'repair'|'skip', 'blind': bool,
    'health': {...}, 'reason': str, 'has_credentials': bool}``.
    """
    warn_days = float(warn_days if warn_days is not None else DEFAULT_WARN_DAYS)
    health = session_health(account, warn_days=warn_days)
    banned = bool(account.get("banned")) if isinstance(account, dict) else False
    banned = banned or is_banned(account)
    creds = credentials_for(account, credentials)
    has_creds = bool(creds and creds.get("password"))
    plan = {
        "action": ACTION_RESTORE,
        "blind": bool(health.get("blind")),
        "health": health,
        "has_credentials": has_creds,
        "banned": banned,
        "reason": health.get("reason", ""),
        "email": str((account or {}).get("email") or "") if isinstance(account, dict) else "",
    }
    if banned:
        plan.update(action=ACTION_SKIP, reason="session is flagged banned")
        return plan
    if not health.get("blind"):
        plan["reason"] = health.get("reason") or "session looks loadable"
        return plan
    # blind from here on
    if allow_repair and has_creds:
        plan.update(action=ACTION_REPAIR,
                    reason=f"BLIND SESSION ({health.get('state')}: {health.get('reason')}) "
                           f"— will log in with stored credentials and re-save")
        return plan
    plan.update(action=ACTION_SKIP,
                reason=f"BLIND SESSION ({health.get('state')}: {health.get('reason')}) "
                       f"— no credentials to repair")
    return plan


def context_pool_allowed(account: Dict[str, Any], *,
                         credentials: Optional[Dict[str, Dict[str, str]]] = None,
                         warn_days: Optional[float] = None) -> Tuple[bool, str, Dict[str, Any]]:
    """May the context pool create a browser context for this account?

    Returns ``(allowed, reason, plan)``.  A blind session that cannot be
    repaired must not get a context — it would just open an unauthenticated
    page and waste a pool slot.
    """
    plan = plan_for_account(account, credentials=credentials,
                            warn_days=DEFAULT_WARN_DAYS if warn_days is None else warn_days)
    if not plan.get("blind"):
        return True, plan.get("reason") or "session looks loadable", plan
    if plan.get("action") == ACTION_REPAIR:
        return True, "session is blind — the worker will repair it with a login", plan
    return False, plan.get("reason") or "blind session without credentials", plan


def summarize(accounts_or_dirs: Optional[Iterable[Any]] = None, *,
              warn_days: float = DEFAULT_WARN_DAYS,
              credentials: Optional[Dict[str, Dict[str, str]]] = None) -> Dict[str, Any]:
    """Count sessions per state + how many are blind/repaired-able."""
    if accounts_or_dirs is None:
        items: List[Any] = find_session_dirs()
    else:
        items = list(accounts_or_dirs)
    states = {HEALTH_OK: 0, HEALTH_EXPIRING: 0, HEALTH_EXPIRED: 0,
              HEALTH_EMPTY: 0, HEALTH_CORRUPT: 0, HEALTH_MISSING: 0}
    blind = 0
    repairable = 0
    banned = 0
    alive = 0
    for item in items:
        health = session_health(item, warn_days=warn_days)
        state = health.get("state", HEALTH_MISSING)
        states[state] = states.get(state, 0) + 1
        item_banned = is_banned(item)
        if item_banned:
            banned += 1
        if health.get("blind"):
            blind += 1
            if credentials_for(item, credentials) and not item_banned:
                repairable += 1
        elif not item_banned:
            # loadable *and* allowed to run = alive
            alive += 1
    summary = {
        "total": len(items),
        "blind": blind,
        "repairable": repairable,
        "banned": banned,
        "alive": alive,
        "states": states,
        "checked_at": _now(),
    }
    return summary


def format_summary(summary: Optional[Dict[str, Any]] = None) -> str:
    summary = summary or summarize()
    states = summary.get("states") or {}
    return (f"sessions total={summary.get('total', 0)} "
            f"alive={summary.get('alive', 0)} blind={summary.get('blind', 0)} "
            f"(repairable={summary.get('repairable', 0)}) banned={summary.get('banned', 0)} "
            f"[ok={states.get(HEALTH_OK, 0)} expiring={states.get(HEALTH_EXPIRING, 0)} "
            f"expired={states.get(HEALTH_EXPIRED, 0)} empty={states.get(HEALTH_EMPTY, 0)} "
            f"corrupt={states.get(HEALTH_CORRUPT, 0)} missing={states.get(HEALTH_MISSING, 0)}]")


# --------------------------------------------------------------------------- #
# Metadata health recording + refresh throttle
# --------------------------------------------------------------------------- #

def update_metadata(account_or_dir: Any, **fields: Any) -> bool:
    """Atomically merge *fields* into the session's ``metadata.json``."""
    session_dir = session_dir_for(account_or_dir)
    if session_dir is None:
        return False
    path = session_dir / "metadata.json"
    data: Dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    data.update({key: value for key, value in fields.items() if value is not None})
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def record_health(account_or_dir: Any, health: Optional[Dict[str, Any]] = None,
                  **extra: Any) -> bool:
    """Store the health block + timestamps in the session metadata."""
    health = health or session_health(account_or_dir)
    clean = {key: value for key, value in health.items()
             if key not in ("info", "path", "session_dir")}
    fields: Dict[str, Any] = {
        "health": clean,
        "health_state": clean.get("state"),
        "blind": bool(clean.get("blind")),
        "last_checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if clean.get("blind"):
        fields.setdefault("blind_since", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    elif existing_blind_since(account_or_dir):
        fields["blind_cleared_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    fields.update(extra)
    return update_metadata(account_or_dir, **fields)


def existing_blind_since(account_or_dir: Any) -> Optional[str]:
    meta = read_metadata(account_or_dir)
    return str(meta.get("blind_since") or "") or None


def record_repaired(account_or_dir: Any, **extra: Any) -> bool:
    """Mark a session as repaired (fresh cookies were saved)."""
    health = session_health(account_or_dir)
    fields = {
        "repaired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repair_count": int(read_metadata(account_or_dir).get("repair_count") or 0) + 1,
        "blind": False,
    }
    fields.update(extra)
    return record_health(account_or_dir, health, **fields)


def should_refresh(account_or_dir: Any, minutes: float = 10.0) -> bool:
    """True when the session state should be written again (throttle)."""
    minutes = float(minutes or 0)
    if minutes <= 0:
        return True
    meta = read_metadata(account_or_dir)
    stamp = meta.get("last_refresh_at") or meta.get("saved_at")
    if not stamp:
        return True
    parsed = _parse_timestamp(str(stamp))
    if parsed is None:
        return True
    return (_now() - parsed) >= minutes * 60.0


def _parse_timestamp(value: str) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        from datetime import datetime
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            from datetime import datetime
            return datetime.strptime(text, fmt).timestamp()
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# Pruning (never deletes, never touches banned sessions)
# --------------------------------------------------------------------------- #

def prune_dead_sessions(dirs: Optional[Iterable[Any]] = None, *, base_dir: Any = None,
                        dry_run: bool = True, warn_days: float = DEFAULT_WARN_DAYS,
                        move: bool = True) -> Dict[str, Any]:
    """Move blind/expired session folders to ``<root>/_dead/``.

    * banned sessions are never touched (they must stay for the ban stats),
    * a *dry run* returns the list without changing anything,
    * files are moved, never deleted — a human can always restore them.
    """
    session_dirs = [Path(d) for d in (dirs if dirs is not None else find_session_dirs(base_dir and [base_dir]))]
    moved: List[str] = []
    kept: List[str] = []
    skipped: List[str] = []
    for session_dir in session_dirs:
        if session_dir.parent.name == DEAD_SUBDIR:
            skipped.append(str(session_dir))
            continue
        if is_banned(session_dir):
            skipped.append(str(session_dir))
            continue
        health = session_health(session_dir, warn_days=warn_days)
        if not health.get("blind"):
            kept.append(str(session_dir))
            continue
        if dry_run or not move:
            moved.append(str(session_dir))
            continue
        destination_root = session_dir.parent / DEAD_SUBDIR
        try:
            destination_root.mkdir(parents=True, exist_ok=True)
            destination = destination_root / session_dir.name
            if destination.exists():
                destination = destination_root / f"{session_dir.name}_{int(_now())}"
            shutil.move(str(session_dir), str(destination))
            moved.append(str(destination))
        except Exception:
            skipped.append(str(session_dir))
    return {
        "dry_run": bool(dry_run),
        "would_move" if dry_run else "moved": moved,
        "kept": kept,
        "skipped": skipped,
        "count": len(moved),
    }


# --------------------------------------------------------------------------- #
# Was the restore really authenticated?  (page probe)
# --------------------------------------------------------------------------- #

#: JS probe run with ``page.evaluate``; returns plain JSON (no Playwright types).
PAGE_AUTH_JS = r"""
() => {
  const safe = (fn, fallback) => { try { return fn(); } catch (e) { return fallback; } };
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const body = norm(safe(() => document.body ? document.body.innerText : '', ''));
  const head = body.slice(0, 4000).toLowerCase();
  const has = (sel) => safe(() => !!document.querySelector(sel), false);
  const url = safe(() => location.href, '');
  const loginForm = has('input[type="password"]') || has('input[name="password"]')
                    || has('form[action*="login" i]');
  const loginWords = /(log ?in|sign ?in|sign ?up|create account|forgot password)/.test(head);
  const chatUi = has('#connected-text') || has('main ol') || has('li.select-text')
                 || has('[class*="chat"]') || has('textarea')
                 || has('[contenteditable="true"]') || has('button[class*="skip" i]');
  const authWords = /(now chatting|start text chat|say hi|skip|next chat|log ?out|logout|my profile|settings|chats)/.test(head);
  const me = has('[username]') || has('[data-username]') || has('[data-testid="my-username"]');
  const avatar = has('img[alt*="avatar" i]') || has('[class*="avatar"]');
  return {
    url: url,
    loginForm: loginForm,
    loginWords: loginWords,
    chatUi: chatUi,
    authWords: authWords,
    hasUsername: me,
    hasAvatar: avatar,
    textLength: body.length,
    title: norm(safe(() => document.title, '')).slice(0, 120)
  };
}
"""


def interpret_auth_probe(probe: Optional[Dict[str, Any]], url: str = "",
                         *, allow_unknown: bool = True) -> Tuple[Optional[bool], str]:
    """Decide whether a page is an authenticated app view.

    Returns ``(True|False|None, reason)`` — ``None`` means "cannot tell"
    (blank page, still loading), which callers treat as *retry / inconclusive*
    rather than a failed login.
    """
    probe = probe or {}
    current = str(url or probe.get("url") or "").lower()
    if any(token in current for token in ("/login", "/signin", "/sign-in", "auth/login")):
        return False, "redirected to the login page"
    if probe.get("loginForm") and not (probe.get("chatUi") or probe.get("hasUsername")):
        return False, "a password form is on the page"
    if probe.get("chatUi") or probe.get("hasUsername"):
        return True, "authenticated chat UI found"
    if probe.get("authWords") and not probe.get("loginWords"):
        return True, "authenticated app text found"
    if int(probe.get("textLength") or 0) < 40 and not probe.get("loginForm"):
        if allow_unknown:
            return None, "page has no readable content yet"
        return False, "empty page"
    if "/chat/" in current:
        return True, "chat URL reached"
    if probe.get("loginWords") or probe.get("loginForm"):
        return False, "the page asks for a login"
    if allow_unknown:
        return None, "no authenticated marker found"
    return False, "no authenticated marker found"


def verify_page(page, *, url: str = "", timeout_ms: int = 15000,
                wait_ms: int = 0) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    """Run :data:`PAGE_AUTH_JS` on a live page and interpret the result.

    Never raises: a broken page returns ``(None, reason, {})``.
    """
    probe: Dict[str, Any] = {}
    try:
        if wait_ms:
            try:
                page.wait_for_timeout(wait_ms)
            except Exception:
                pass
        probe = page.evaluate(PAGE_AUTH_JS) or {}
    except Exception as exc:
        return None, f"page probe failed: {type(exc).__name__}: {exc}", {}
    if not isinstance(probe, dict):
        return None, "page probe returned an unexpected value", {}
    status, reason = interpret_auth_probe(probe, url or str(probe.get("url") or ""))
    return status, reason, probe


# --------------------------------------------------------------------------- #
# Live check (optional — used by tools/session_doctor.py --live-check)
# --------------------------------------------------------------------------- #

def live_check(account: Dict[str, Any], *, restore_url: Optional[str] = None,
               timeout_ms: int = 30000, log=print
               ) -> Dict[str, Any]:
    """Open a session in a real headless browser and report whether it is live.

    Requires Playwright + a browser, so this is only used by the CLI tool and
    never by the runtime path.
    """
    result: Dict[str, Any] = {"email": (account or {}).get("email", ""), "status": "unchecked"}
    storage = session_storage_path(account)
    if storage is None or not Path(storage).is_file():
        result.update(status="blind", reason="no storage_state file")
        return result
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        result.update(status="unchecked", reason=f"Playwright unavailable: {exc}")
        return result
    url = restore_url or str((account or {}).get("restore_url")
                             or "https://app.chitchat.gg/start/new")
    proxy = (account or {}).get("saved_proxy") or (account or {}).get("proxy")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            options: Dict[str, Any] = {"storage_state": str(storage)}
            if isinstance(proxy, dict) and proxy.get("server"):
                options["proxy"] = proxy
            context = browser.new_context(**options)
            page = context.new_page()
            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                status, reason, probe = verify_page(page, timeout_ms=timeout_ms, wait_ms=2500)
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:
        result.update(status="unchecked", reason=f"browser error: {type(exc).__name__}: {exc}")
        return result

    if status is True:
        result.update(status="live", reason=reason)
    elif status is False:
        result.update(status="blind", reason=reason)
    else:
        result.update(status="unknown", reason=reason)
    return result


__all__ = [
    "ACTION_REPAIR", "ACTION_RESTORE", "ACTION_SKIP", "BLIND_STATES",
    "HEALTH_CORRUPT", "HEALTH_EMPTY", "HEALTH_EXPIRED", "HEALTH_EXPIRING",
    "HEALTH_MISSING", "HEALTH_OK", "PAGE_AUTH_JS",
    "attach_credentials", "classify_health", "context_pool_allowed", "credentials_for",
    "default_accounts_files", "default_session_roots", "find_session_dirs",
    "format_summary", "inspect_storage_state", "interpret_auth_probe", "is_blind",
    "is_banned", "live_check", "load_credentials", "plan_for_account",
    "prune_dead_sessions", "read_metadata", "record_health", "record_repaired",
    "session_dir_for", "session_health", "session_storage_path", "should_refresh",
    "summarize", "update_metadata", "verify_page",
]
