"""Serial-numbered account manager for the Context-Pool edition.

Loads accounts from ``account_sessions/`` (or a user-specified directory) and
assigns each one a **serial number** (1, 2, 3, …) so the total count is trivial
to read in logs and the GUI.  Three input formats are supported:

1. **Session folders** (existing Camoufox format) — each subfolder contains
   ``metadata.json``, ``fingerprint.json``, ``storage_state.json``,
   ``history.json``.  This is the primary format and what the bot creates.
2. **Token format** — a single ``tokens.txt`` file with one token per line
   (or ``email:token`` pairs).  Each token becomes an account entry with a
   synthetic storage state.
3. **Cookie format** — a single ``cookies.txt`` file in Netscape
   ``name\tvalue\tdomain\tpath\texpiry\t...`` format, or JSON arrays.

The manager is a **registry**: every account has a status
(``idle``, ``active``, ``resting``, ``banned``, ``exhausted``) and is assigned
to context workers round-robin.  Banned accounts are never re-offered.

Phase 13: accounts also get a **session health** check (see
``browser/session_health.py``) and, when ``accounts.txt`` holds matching
credentials, a *repair* plan.  A *blind* session (missing / empty / corrupt /
expired cookies) is never handed to a worker that can only restore — it is
either repaired by logging in again, or skipped with a clear reason.

Design goals
------------
* Zero external dependencies (stdlib only).
* Thread-safe (a ``threading.Lock`` guards the registry).
* Backward compatible with the existing ``account_session_store`` helpers.
* Easy total count: ``len(manager)`` or ``manager.summary()``.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Status constants
# --------------------------------------------------------------------------- #

STATUS_IDLE = "idle"
STATUS_ACTIVE = "active"
STATUS_RESTING = "resting"
STATUS_BANNED = "banned"
STATUS_EXHAUSTED = "exhausted"
STATUS_FAILED = "failed"
#: session cannot be loaded (see browser/session_health.py) and no repair
STATUS_BLIND = "blind"

_ALL_STATUSES = (
    STATUS_IDLE, STATUS_ACTIVE, STATUS_RESTING,
    STATUS_BANNED, STATUS_EXHAUSTED, STATUS_FAILED, STATUS_BLIND,
)

#: statuses a worker must never be given
_SKIP_STATUSES = (STATUS_BANNED, STATUS_EXHAUSTED, STATUS_FAILED, STATUS_BLIND)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class AccountEntry:
    """A single account loaded by the manager."""

    serial: int                        # 1-based serial number shown to the user
    account_key: str                   # stable unique id (folder name / hash)
    email: str = ""
    account_type: str = "session"      # session | token | cookie
    folder_path: Optional[str] = None  # session-folder format
    storage_state_path: Optional[str] = None
    fingerprint_path: Optional[str] = None
    proxy: Optional[Dict[str, Any]] = None
    restore_url: str = "https://app.chitchat.gg/start/new"
    status: str = STATUS_IDLE
    last_used: float = 0.0
    chat_count: int = 0
    fail_count: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)
    # ---- Phase 13: session health + repair plan ----
    health: Dict[str, Any] = field(default_factory=dict)
    blind: bool = False
    plan: Dict[str, Any] = field(default_factory=dict)
    has_credentials: bool = False

    # ---- convenience ----
    @property
    def label(self) -> str:
        """Human-readable label: ``#001 email``."""
        return f"#{self.serial:03d} {self.email or self.account_key}"

    def to_display(self) -> Dict[str, Any]:
        """Compact dict for GUI/log display."""
        return {
            "serial": self.serial,
            "email": self.email,
            "type": self.account_type,
            "status": self.status,
            "chats": self.chat_count,
            "health": (self.health or {}).get("state", ""),
            "blind": self.blind,
            "has_credentials": self.has_credentials,
        }


# --------------------------------------------------------------------------- #
# Loader helpers (pure functions, no state)
# --------------------------------------------------------------------------- #

def _load_session_folder(folder: Path) -> Optional[Dict[str, Any]]:
    """Read a session folder's metadata.json (returns None if invalid)."""
    meta_path = folder / "metadata.json"
    if not meta_path.is_file():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except Exception:
        return None
    # Validate that the storage_state file exists.
    ss_name = meta.get("storage_state", "storage_state.json")
    if not (folder / ss_name).is_file():
        return None
    meta["_folder"] = str(folder)
    meta["_storage_state_path"] = str(folder / ss_name)
    fp_name = meta.get("fingerprint", "fingerprint.json")
    if (folder / fp_name).is_file():
        meta["_fingerprint_path"] = str(folder / fp_name)
    return meta


def _parse_tokens_file(path: Path) -> List[Dict[str, Any]]:
    """Parse a tokens.txt file → list of account dicts."""
    accounts: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return accounts
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Support "email:token" or bare token.
        if ":" in line and "@" in line.split(":")[0]:
            email, token = line.split(":", 1)
            email, token = email.strip(), token.strip()
        else:
            email, token = "", line
        if not token:
            continue
        accounts.append({
            "email": email,
            "token": token,
            "_type": "token",
        })
    return accounts


def _parse_cookies_file(path: Path) -> List[Dict[str, Any]]:
    """Parse a cookies file (Netscape or JSON) → list of account dicts.

    Netscape format groups are separated by blank lines; each group becomes
    one account.  JSON format expects an array of cookie arrays.
    """
    accounts: List[Dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return accounts
    raw = raw.strip()
    # JSON array of arrays.
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except Exception:
            data = []
        if isinstance(data, list):
            for idx, group in enumerate(data, 1):
                if isinstance(group, list):
                    accounts.append({
                        "email": f"cookie-group-{idx}",
                        "cookies": group,
                        "_type": "cookie",
                    })
        return accounts
    # Netscape format: split on blank lines.
    groups: List[str] = []
    current: List[str] = []
    for line in raw.splitlines():
        if line.strip() == "":
            if current:
                groups.append("\n".join(current))
                current = []
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        current.append(line)
    if current:
        groups.append("\n".join(current))
    for idx, group in enumerate(groups, 1):
        accounts.append({
            "email": f"cookie-group-{idx}",
            "cookies_raw": group,
            "_type": "cookie",
        })
    return accounts


# --------------------------------------------------------------------------- #
# Session-health bridges (Phase 13) — lazy + never fatal
# --------------------------------------------------------------------------- #

def _health_module():
    """Import browser.session_health lazily (keeps this module importable alone)."""
    try:
        from browser import session_health
        return session_health
    except Exception:
        return None


def _session_roots(fallback_dir: str):
    """Every root to scan for sessions (env + both known layouts)."""
    module = _health_module()
    if module is not None:
        try:
            roots = module.default_session_roots()
            if roots:
                return roots
        except Exception:
            pass
    return [Path(fallback_dir)]


def _warn_days():
    """``session_management.session_expiry_warn_days`` from config.json."""
    try:
        from core.config_loader import load_session_management
        return float(load_session_management().get("session_expiry_warn_days", 3))
    except Exception:
        return 3.0


def _load_credentials():
    module = _health_module()
    if module is None:
        return {}
    try:
        return module.load_credentials()
    except Exception:
        return {}


def _credentials_for(account, credentials=None):
    module = _health_module()
    if module is None:
        return None
    try:
        return module.credentials_for(account, credentials)
    except Exception:
        return None


def _plan(account, *, warn_days=None, credentials=None):
    """Session plan for one account (restore / repair / skip)."""
    module = _health_module()
    if module is None:
        return {"action": "restore", "blind": False, "health": {},
                "reason": "session health module unavailable",
                "has_credentials": False, "banned": False}
    try:
        return module.plan_for_account(
            account,
            credentials=credentials,
            warn_days=_warn_days() if warn_days is None else warn_days,
        )
    except Exception as exc:
        return {"action": "restore", "blind": False, "health": {},
                "reason": f"health check failed: {type(exc).__name__}: {exc}",
                "has_credentials": False, "banned": False}


def _recover_session_folder(folder: Path):
    """Build metadata for a session folder whose metadata.json is missing.

    The bot has always written ``storage_state.json``; metadata is just a
    description.  A folder with a storage state is still usable, so recover it
    instead of silently dropping the account.
    """
    storage = folder / "storage_state.json"
    if not storage.is_file():
        return None
    meta: Dict[str, Any] = {
        "account_key": folder.name,
        "email": "",
        "storage_state": storage.name,
        "_recovered": True,
    }
    fingerprint = folder / "fingerprint.json"
    if fingerprint.is_file():
        meta["fingerprint"] = fingerprint.name
    meta["_storage_state_path"] = str(storage)
    if fingerprint.is_file():
        meta["_fingerprint_path"] = str(fingerprint)
    return meta


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #

class AccountManager:
    """Thread-safe registry of serial-numbered accounts."""

    def __init__(self, sessions_dir: str = "account_sessions"):
        self._sessions_dir = sessions_dir
        self._lock = threading.RLock()
        self._accounts: List[AccountEntry] = []
        self._by_key: Dict[str, AccountEntry] = {}
        self._next_serial = 1
        self._round_robin_idx = 0

    # ---- loading ----

    def load(self, directory: Optional[str] = None, *,
             warn_days: Optional[float] = None,
             credentials: Optional[Dict[str, Dict[str, str]]] = None) -> int:
        """Load accounts from *directory* or from **every** session root.

        With no argument the historical root (``account_sessions/``), the pool
        root (``data/account_sessions/``) and ``EVA_SESSIONS_DIR`` are all
        scanned, so a session saved by any part of the bot is found.  Each
        session account gets a health check and — when ``accounts.txt`` has
        matching credentials — a repair plan (``browser/session_health``).

        Returns the number of accounts loaded.  Calling ``load`` again
        reloads from scratch (clears previous entries).
        """
        if directory:
            bases = [Path(directory)]
        else:
            bases = _session_roots(self._sessions_dir)
        with self._lock:
            self._accounts.clear()
            self._by_key.clear()
            self._next_serial = 1
            self._round_robin_idx = 0

        if warn_days is None:
            warn_days = _warn_days()
        creds = credentials if credentials is not None else _load_credentials()

        # 1. Session folders (primary format) — every root, deduped by key.
        for base in bases:
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir()):
                if not child.is_dir() or child.name.startswith("_"):
                    continue
                meta = _load_session_folder(child)
                if meta is None:
                    # A folder without metadata can still be a usable session
                    # (damaged metadata.json, storage_state present).
                    recovered = _recover_session_folder(child)
                    if recovered is None:
                        continue
                    meta = recovered
                self._add_session_account(child, meta, warn_days=warn_days,
                                          credentials=creds)

        # 2. tokens.txt inside the root.
        for base in bases:
            tokens_file = base / "tokens.txt"
            if tokens_file.is_file():
                for tok in _parse_tokens_file(tokens_file):
                    self._add_token_account(tok)

        # 3. cookies.txt inside the root.
        for base in bases:
            cookies_file = base / "cookies.txt"
            if cookies_file.is_file():
                for ck in _parse_cookies_file(cookies_file):
                    self._add_cookie_account(ck)

        return len(self._accounts)

    def _register(self, entry: AccountEntry) -> None:
        with self._lock:
            if entry.account_key in self._by_key:
                return  # deduplicate
            entry.serial = self._next_serial
            self._next_serial += 1
            self._accounts.append(entry)
            self._by_key[entry.account_key] = entry

    def _add_session_account(self, folder: Path, meta: Dict[str, Any], *,
                             warn_days: Optional[float] = None,
                             credentials: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        account = {
            "email": meta.get("email", ""),
            "session_dir": str(folder),
            "storage_state_path": meta.get("_storage_state_path"),
            "banned": bool(meta.get("banned")),
        }
        plan = _plan(account, warn_days=warn_days, credentials=credentials)
        health = plan.get("health") or {}
        entry = AccountEntry(
            serial=0,  # assigned by _register
            account_key=meta.get("account_key", folder.name),
            email=meta.get("email", ""),
            account_type="session",
            folder_path=str(folder),
            storage_state_path=meta.get("_storage_state_path"),
            fingerprint_path=meta.get("_fingerprint_path"),
            proxy=meta.get("proxy"),
            restore_url=meta.get("restore_url", "https://app.chitchat.gg/start/new"),
            health=health,
            blind=bool(health.get("blind")),
            plan=plan,
            has_credentials=bool(plan.get("has_credentials")),
        )
        if entry.blind and plan.get("action") == "skip":
            entry.status = STATUS_BLIND
        if plan.get("action") == "repair":
            # the worker may log in again for this account
            found = _credentials_for(account, credentials)
            if found:
                entry.extra["password"] = found.get("password", "")
                entry.extra["credentials_source"] = found.get("source", "")
        self._register(entry)

    def _add_token_account(self, tok: Dict[str, Any]) -> None:
        import hashlib
        key = hashlib.md5(tok["token"].encode()).hexdigest()[:24]
        entry = AccountEntry(
            serial=0,
            account_key=f"token_{key}",
            email=tok.get("email", ""),
            account_type="token",
            extra={"token": tok["token"]},
        )
        self._register(entry)

    def _add_cookie_account(self, ck: Dict[str, Any]) -> None:
        import hashlib
        raw = ck.get("cookies_raw") or json.dumps(ck.get("cookies", []))
        key = hashlib.md5(raw.encode()).hexdigest()[:24]
        entry = AccountEntry(
            serial=0,
            account_key=f"cookie_{key}",
            email=ck.get("email", ""),
            account_type="cookie",
            extra=ck,
        )
        self._register(entry)

    # ---- query ----

    def __len__(self) -> int:
        with self._lock:
            return len(self._accounts)

    def all_entries(self) -> List[AccountEntry]:
        with self._lock:
            return list(self._accounts)

    def summary(self) -> Dict[str, int]:
        """Return counts by status + total (+ blind / repairable helper keys)."""
        with self._lock:
            counts: Dict[str, int] = {s: 0 for s in _ALL_STATUSES}
            total = 0
            blind = 0
            repairable = 0
            for acc in self._accounts:
                counts[acc.status] = counts.get(acc.status, 0) + 1
                total += 1
                if acc.blind:
                    blind += 1
                    if (acc.plan or {}).get("action") == "repair":
                        repairable += 1
            counts["total"] = total
            counts["blind"] = blind
            counts["repairable"] = repairable
            counts["restorable"] = total - blind
            return counts

    def get_by_serial(self, serial: int) -> Optional[AccountEntry]:
        with self._lock:
            for acc in self._accounts:
                if acc.serial == serial:
                    return acc
            return None

    def get_by_key(self, key: str) -> Optional[AccountEntry]:
        with self._lock:
            return self._by_key.get(key)

    # ---- assignment ----

    def next_idle_account(self, *, skip_blind: bool = True) -> Optional[AccountEntry]:
        """Round-robin pick the next idle account.

        Skips banned / exhausted / failed accounts and — unless
        ``skip_blind=False`` — accounts whose session is blind with no way to
        repair it (they would waste a browser launch).
        """
        with self._lock:
            n = len(self._accounts)
            if n == 0:
                return None
            skip = set(_SKIP_STATUSES)
            if not skip_blind:
                skip.discard(STATUS_BLIND)
            for _ in range(n):
                idx = self._round_robin_idx % n
                self._round_robin_idx += 1
                acc = self._accounts[idx]
                if acc.status in skip:
                    continue
                if skip_blind and acc.blind and (acc.plan or {}).get("action") == "skip":
                    continue
                return acc
            return None

    # ---- Phase 13: health / repair helpers ----

    def refresh_health(self, account_key: str, *, warn_days: Optional[float] = None,
                       credentials: Optional[Dict[str, Dict[str, str]]] = None
                       ) -> Optional[Dict[str, Any]]:
        """Re-check one account's session health and update its status/plan."""
        with self._lock:
            acc = self._by_key.get(account_key)
        if acc is None:
            return None
        account = {
            "email": acc.email,
            "session_dir": acc.folder_path,
            "storage_state_path": acc.storage_state_path,
        }
        plan = _plan(account, warn_days=warn_days, credentials=credentials)
        with self._lock:
            acc.plan = plan
            acc.health = plan.get("health") or {}
            acc.blind = bool(acc.health.get("blind"))
            acc.has_credentials = bool(plan.get("has_credentials"))
            if acc.blind and plan.get("action") == "skip":
                acc.status = STATUS_BLIND
            elif acc.status == STATUS_BLIND and not acc.blind:
                acc.status = STATUS_IDLE
        return plan

    def plan_for(self, account_key: str) -> Dict[str, Any]:
        with self._lock:
            acc = self._by_key.get(account_key)
        return dict(acc.plan) if acc is not None else {}

    def mark_blind(self, account_key: str, reason: str = "") -> bool:
        """Flag an account whose session turned out unusable at runtime."""
        with self._lock:
            acc = self._by_key.get(account_key)
            if acc is None:
                return False
            acc.blind = True
            acc.status = STATUS_BLIND
            if reason:
                acc.extra["blind_reason"] = reason
            return True

    def mark_repaired(self, account_key: str, *, storage_state_path: Optional[str] = None
                      ) -> bool:
        """Clear the blind flag after a successful credential re-login."""
        with self._lock:
            acc = self._by_key.get(account_key)
            if acc is None:
                return False
            if storage_state_path:
                acc.storage_state_path = storage_state_path
            acc.blind = False
            acc.health = {}
            acc.plan = {}
            if acc.status == STATUS_BLIND:
                acc.status = STATUS_IDLE
            return True

    def blind_entries(self) -> List[AccountEntry]:
        with self._lock:
            return [acc for acc in self._accounts if acc.blind]

    # ---- status updates ----

    def mark_status(self, account_key: str, status: str) -> bool:
        with self._lock:
            acc = self._by_key.get(account_key)
            if acc is None:
                return False
            acc.status = status
            if status == STATUS_ACTIVE:
                import time as _t
                acc.last_used = _t.time()
            return True

    def increment_chat(self, account_key: str) -> None:
        with self._lock:
            acc = self._by_key.get(account_key)
            if acc:
                acc.chat_count += 1

    def increment_fail(self, account_key: str) -> None:
        with self._lock:
            acc = self._by_key.get(account_key)
            if acc:
                acc.fail_count += 1

    def mark_banned(self, account_key: str) -> None:
        self.mark_status(account_key, STATUS_BANNED)

    def reset_to_idle(self, account_key: str) -> None:
        self.mark_status(account_key, STATUS_IDLE)

    # ---- display ----

    def format_summary(self) -> str:
        """One-line summary string for logs (includes blind-session counts)."""
        s = self.summary()
        line = (
            f"[Stock] total: {s['total']}  |  idle: {s[STATUS_IDLE]}  |  "
            f"active: {s[STATUS_ACTIVE]}  |  resting: {s[STATUS_RESTING]}  |  "
            f"banned: {s[STATUS_BANNED]}  |  blind: {s['blind']}"
        )
        if s.get("repairable"):
            line += f" (repairable: {s['repairable']})"
        return line

    def format_list(self, max_rows: int = 20) -> str:
        """Multi-line table of accounts (first *max_rows*)."""
        lines = [f"{'#':>4}  {'Email':<35}  {'Type':<8}  {'Status':<10}  "
                 f"{'Session':<10}  Chats"]
        lines.append("-" * 90)
        with self._lock:
            for acc in self._accounts[:max_rows]:
                health = (acc.health or {}).get("state", "-") or "-"
                if acc.blind and (acc.plan or {}).get("action") == "repair":
                    health += "*"
                lines.append(
                    f"{acc.serial:>4}  {(acc.email or acc.account_key)[:35]:<35}  "
                    f"{acc.account_type:<8}  {acc.status:<10}  {health:<10}  {acc.chat_count}"
                )
            if len(self._accounts) > max_rows:
                lines.append(f"  ... and {len(self._accounts) - max_rows} more")
        return "\n".join(lines)
