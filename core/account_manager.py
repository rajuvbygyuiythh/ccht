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

_ALL_STATUSES = (
    STATUS_IDLE, STATUS_ACTIVE, STATUS_RESTING,
    STATUS_BANNED, STATUS_EXHAUSTED, STATUS_FAILED,
)


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

    def load(self, directory: Optional[str] = None) -> int:
        """Load all accounts from *directory* (defaults to sessions_dir).

        Returns the number of accounts loaded.  Calling ``load`` again
        reloads from scratch (clears previous entries).
        """
        base = Path(directory) if directory else Path(self._sessions_dir)
        with self._lock:
            self._accounts.clear()
            self._by_key.clear()
            self._next_serial = 1
            self._round_robin_idx = 0

        if not base.is_dir():
            return 0

        # 1. Session folders (primary format).
        if base.is_dir():
            for child in sorted(base.iterdir()):
                if not child.is_dir():
                    continue
                meta = _load_session_folder(child)
                if meta is None:
                    continue
                self._add_session_account(child, meta)

        # 2. tokens.txt inside the directory.
        tokens_file = base / "tokens.txt"
        if tokens_file.is_file():
            for tok in _parse_tokens_file(tokens_file):
                self._add_token_account(tok)

        # 3. cookies.txt inside the directory.
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

    def _add_session_account(self, folder: Path, meta: Dict[str, Any]) -> None:
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
        )
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
        """Return counts by status + total."""
        with self._lock:
            counts: Dict[str, int] = {s: 0 for s in _ALL_STATUSES}
            total = 0
            for acc in self._accounts:
                counts[acc.status] = counts.get(acc.status, 0) + 1
                total += 1
            counts["total"] = total
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

    def next_idle_account(self) -> Optional[AccountEntry]:
        """Round-robin pick the next idle account.

        Skips banned / exhausted / failed accounts.  Returns ``None`` if no
        idle account is available.
        """
        with self._lock:
            n = len(self._accounts)
            if n == 0:
                return None
            skip = {STATUS_BANNED, STATUS_EXHAUSTED, STATUS_FAILED}
            for _ in range(n):
                idx = self._round_robin_idx % n
                self._round_robin_idx += 1
                acc = self._accounts[idx]
                if acc.status not in skip:
                    return acc
            return None

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
        """One-line summary string for logs."""
        s = self.summary()
        return (
            f"[Stock] total: {s['total']}  |  idle: {s[STATUS_IDLE]}  |  "
            f"active: {s[STATUS_ACTIVE]}  |  resting: {s[STATUS_RESTING]}  |  "
            f"banned: {s[STATUS_BANNED]}"
        )

    def format_list(self, max_rows: int = 20) -> str:
        """Multi-line table of accounts (first *max_rows*)."""
        lines = [f"{'#':>4}  {'Email':<35}  {'Type':<8}  {'Status':<10}  Chats"]
        lines.append("-" * 75)
        with self._lock:
            for acc in self._accounts[:max_rows]:
                lines.append(
                    f"{acc.serial:>4}  {(acc.email or acc.account_key)[:35]:<35}  "
                    f"{acc.account_type:<8}  {acc.status:<10}  {acc.chat_count}"
                )
            if len(self._accounts) > max_rows:
                lines.append(f"  ... and {len(self._accounts) - max_rows} more")
        return "\n".join(lines)
