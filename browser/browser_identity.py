"""browser/browser_identity.py — one account = one permanent device profile.

Problem this solves
-------------------
A saved session is only convincing if it reopens in the *same* browser it was
created in, while N accounts running at the same time must look like N
*different* devices.  Before this module, Chromium stealth values (User-Agent,
viewport, hardware concurrency, device memory) were re-randomized on every
launch, so:

* the same account looked like a brand-new device every run, and
* two accounts could easily draw the *same* UA + viewport (same device twice).

Design
------
``account_key`` (email / session folder name)
    │  sha256(account_key + salt + version)   ← deterministic, never random()
    ▼
device identity: UA · platform · viewport · screen · DPR · window metrics ·
                 hardwareConcurrency · deviceMemory · timezone · locale
    ├─ <session_dir>/identity.json        (portable: travels with the session)
    └─ <primary_root>/_identities.json    (registry: keeps identities unique)

* Same account → same identity, forever (persisted, not re-derived).
* Different accounts → different signature (registry + salted re-derivation).
* If the session's ``fingerprint.json`` has an *observed* UA from the login run,
  that UA is adopted (the account keeps the device it was logged in with) as
  long as no other account already owns it.
* ``rotate_identity()`` gives an account a brand-new device (used after a ban).

The module is stdlib-only and works standalone: every cross-module import is
lazy and wrapped in try/except, so a missing session store never breaks it.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

IDENTITY_VERSION = 1
IDENTITY_FILE = "identity.json"
REGISTRY_FILE = "_identities.json"
LOCK_FILE = "_identities.lock"
REGISTRY_KEY = "identities"
PROFILES_DIRNAME = "browser_profiles"
PROFILE_OWNER_FILE = "owner.json"
PROFILE_MODES = ("context", "persistent")
# Default: every account gets its OWN real Chrome profile (separate cookies,
# history, cache and IndexedDB).  "context" (one shared browser) is opt-in.
DEFAULT_PROFILE_MODE = "persistent"
MAX_UNIQUE_ATTEMPTS = 200
LOCK_TIMEOUT = 5.0

# --------------------------------------------------------------------------- #
#  Device pools — expanded so hundreds of accounts get distinct devices
# --------------------------------------------------------------------------- #

UA_POOL: Tuple[str, ...] = (
    # Windows 10/11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    # Windows on ARM-ish / newer Intel strings
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
)

# (screen w, screen h, viewport w, viewport h, dpr)
SCREEN_POOL: Tuple[Tuple[int, int, int, int, float], ...] = (
    (1920, 1080, 1898, 941, 1.0),
    (1920, 1080, 1920, 955, 1.25),
    (1536, 864, 1512, 828, 1.25),
    (1600, 900, 1580, 786, 1.0),
    (1440, 900, 1420, 800, 2.0),
    (1366, 768, 1348, 641, 1.0),
    (2560, 1440, 2536, 1303, 1.0),
    (1680, 1050, 1662, 919, 1.0),
    (1280, 800, 1263, 673, 2.0),
    (1728, 1117, 1707, 981, 2.0),
    (2048, 1152, 2028, 1002, 1.0),
    (3840, 2160, 3812, 1990, 1.5),
)

HARDWARE_CONCURRENCY_POOL: Tuple[int, ...] = (4, 6, 8, 8, 12, 16)
DEVICE_MEMORY_POOL: Tuple[int, ...] = (4, 8, 8, 16)

# (locale, timezone) pairs that belong together — an English UI in a plausible
# timezone.  Kept small on purpose: a wrong locale/timezone pair is a leak.
LOCALE_TIMEZONE_POOL: Tuple[Tuple[str, str], ...] = (
    ("en-US", "America/New_York"),
    ("en-US", "America/Chicago"),
    ("en-US", "America/Denver"),
    ("en-US", "America/Los_Angeles"),
    ("en-US", "America/Phoenix"),
    ("en-GB", "Europe/London"),
    ("en-GB", "Europe/Dublin"),
    ("en-CA", "America/Toronto"),
    ("en-CA", "America/Vancouver"),
    ("en-AU", "Australia/Sydney"),
    ("en-AU", "Australia/Brisbane"),
    ("en-NZ", "Pacific/Auckland"),
    ("en-IE", "Europe/Dublin"),
    ("en-SG", "Asia/Singapore"),
    ("en-ZA", "Africa/Johannesburg"),
)

_LOCK_GUARD = threading.RLock()


# --------------------------------------------------------------------------- #
#  small helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _derive_int(seed_hex: str, salt: int, modulo: int) -> int:
    """Deterministic index from the seed (stable across processes/runs)."""
    if modulo <= 0:
        return 0
    digest = _sha(f"{seed_hex}|{salt}")
    return int(digest[:12], 16) % modulo


def _platform_for_ua(ua: str) -> str:
    if "Windows" in ua:
        return "Win32"
    if "Macintosh" in ua:
        return "MacIntel"
    if "Linux" in ua:
        return "Linux x86_64"
    return "Win32"


def _os_name_for_ua(ua: str) -> str:
    if "Windows" in ua:
        return "Windows"
    if "Macintosh" in ua:
        return "macOS"
    if "Linux" in ua:
        return "Linux"
    return "Windows"


def account_key_for(account: Any) -> str:
    """Stable key for an account dict / email string / session folder."""
    if isinstance(account, str):
        return account.strip().lower()
    if isinstance(account, dict):
        email = str(account.get("email") or "").strip()
        if email:
            return email.lower()
        for key in ("account_key", "key", "name", "username"):
            value = str(account.get(key) or "").strip()
            if value:
                return value.lower()
        session_dir = account.get("session_dir") or account.get("folder")
        if session_dir:
            return Path(str(session_dir)).name.lower()
    if account is not None:
        try:
            return Path(str(account)).name.lower()
        except Exception:
            pass
    return "unknown"


def session_dir_for(account: Any) -> Optional[Path]:
    if isinstance(account, dict):
        for key in ("session_dir", "folder", "path"):
            value = account.get(key)
            if value:
                return Path(str(value)).expanduser()
        state = account.get("storage_state_path")
        if state:
            return Path(str(state)).expanduser().parent
    return None


def primary_root() -> Path:
    """Where the identity registry lives (first session root)."""
    env = str(os.environ.get("EVA_SESSIONS_DIR") or "").strip()
    if env:
        first = env.split(os.pathsep)[0].strip()
        if first:
            return Path(first).expanduser()
    try:
        from browser.account_session_store import get_account_sessions_dir
        return Path(get_account_sessions_dir())
    except Exception:
        return Path(__file__).resolve().parent.parent / "account_sessions"


def registry_path() -> Path:
    return primary_root() / REGISTRY_FILE


def profiles_root() -> Path:
    return primary_root() / PROFILES_DIRNAME


# --------------------------------------------------------------------------- #
#  config
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "profile_mode": DEFAULT_PROFILE_MODE,
    "profiles_dir_name": PROFILES_DIRNAME,
    "unique_between_accounts": True,
    "adopt_observed_fingerprint": True,
    "rotate_on_ban": True,
    "warmup_sites": [],
    "log_identity_on_start": True,
    # never let a second account open a profile that another one created
    "owner_guard": True,
    # check the real browser values against the identity before logging in
    "verify_before_login": True,
    # True = refuse to log in when the check fails (False = warn + continue)
    "verify_strict": False,
    # optional real fingerprint-checker page ("" = probe the current page)
    "verify_url": "",
    # write the profile + identity after a successful login
    "save_profile_after_login": True,
}


def config() -> Dict[str, Any]:
    values = dict(DEFAULT_CONFIG)
    try:
        from core.config_loader import load_browser_identity
        loaded = load_browser_identity()
        if isinstance(loaded, dict):
            values.update({k: v for k, v in loaded.items() if v is not None})
    except Exception:
        pass
    env_mode = str(os.environ.get("EVA_BROWSER_PROFILE_MODE") or "").strip().lower()
    if env_mode in PROFILE_MODES:
        values["profile_mode"] = env_mode
    if str(values.get("profile_mode") or "").lower() not in PROFILE_MODES:
        values["profile_mode"] = DEFAULT_PROFILE_MODE
    return values


def enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    cfg = cfg or config()
    return bool(cfg.get("enabled", True))


def profile_mode(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg or config()
    return str(cfg.get("profile_mode") or DEFAULT_PROFILE_MODE).lower()


# --------------------------------------------------------------------------- #
#  registry (file + memory) — guarantees uniqueness across accounts
# --------------------------------------------------------------------------- #

@contextmanager
def _file_lock(path: Path, timeout: float = LOCK_TIMEOUT, log_fn=None):
    """Cooperative lock file so two processes don't derive the same identity."""
    deadline = time.time() + max(0.0, timeout)
    handle_fd = None
    while True:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle_fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle_fd, str(os.getpid()).encode("ascii", "ignore"))
            break
        except FileExistsError:
            if time.time() >= deadline:
                if log_fn:
                    log_fn(f"[Identity] registry lock busy — continuing without lock ({path.name})")
                break
            time.sleep(0.05)
        except Exception:
            break
    try:
        yield
    finally:
        if handle_fd is not None:
            try:
                os.close(handle_fd)
            except Exception:
                pass
            try:
                path.unlink()
            except Exception:
                pass


def _read_registry() -> Dict[str, Any]:
    path = registry_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": IDENTITY_VERSION, REGISTRY_KEY: {}}
    if not isinstance(raw, dict):
        return {"version": IDENTITY_VERSION, REGISTRY_KEY: {}}
    entries = raw.get(REGISTRY_KEY)
    if not isinstance(entries, dict):
        entries = {}
    raw[REGISTRY_KEY] = entries
    raw.setdefault("version", IDENTITY_VERSION)
    return raw


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _write_registry(data: Dict[str, Any]) -> None:
    data["version"] = IDENTITY_VERSION
    data["updated_at"] = _now_iso()
    _atomic_write_json(registry_path(), data)


@contextmanager
def _registry_session():
    """Locked read → mutate → write cycle."""
    with _LOCK_GUARD:
        with _file_lock(primary_root() / LOCK_FILE):
            data = _read_registry()
            yield data
            _write_registry(data)


def registry_snapshot() -> Dict[str, Any]:
    with _LOCK_GUARD:
        return dict(_read_registry().get(REGISTRY_KEY) or {})


# Everything needed to rebuild an identical device after a restart.
_REGISTRY_FIELDS = (
    "user_agent", "platform", "os", "chrome_major", "viewport", "screen",
    "avail_height", "device_scale_factor", "outer", "inner",
    "hardware_concurrency", "device_memory", "timezone_id", "locale",
    "languages", "color_scheme", "seed_salt", "rotations", "warmup_done",
    "warmup_visited", "created_at", "source", "session_dir",
    "rotation_reason", "rotated_at",
)


def _registry_entry(identity: Dict[str, Any]) -> Dict[str, Any]:
    entry = {key: identity[key] for key in _REGISTRY_FIELDS if identity.get(key) is not None}
    entry["signature"] = signature_of(identity)
    return entry


def _identity_from_entry(key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild a full identity from a registry entry (stable across restarts)."""
    identity = {key: value for key, value in entry.items() if key in _REGISTRY_FIELDS}
    identity["version"] = IDENTITY_VERSION
    identity["account_key"] = key
    identity.setdefault("locale", "en-US")
    identity.setdefault("languages", [identity["locale"], str(identity["locale"]).split("-")[0]])
    identity.setdefault("created_at", entry.get("created_at") or _now_iso())
    identity["profile_dir"] = None
    identity["source"] = entry.get("source") or "registry"
    identity["signature"] = signature_of(identity)
    return identity


# --------------------------------------------------------------------------- #
#  derivation
# --------------------------------------------------------------------------- #

def signature_of(identity: Dict[str, Any]) -> str:
    """The part of an identity that must never be shared by two accounts."""
    viewport = identity.get("viewport") or {}
    return "|".join(str(part) for part in (
        identity.get("user_agent") or "",
        identity.get("platform") or "",
        f"{viewport.get('width')}x{viewport.get('height')}",
        identity.get("hardware_concurrency") or "",
        identity.get("device_memory") or "",
        identity.get("timezone_id") or "",
        identity.get("locale") or "",
    ))


def derive_identity(account_key: str, *, salt: int = 0,
                    user_agent: Optional[str] = None) -> Dict[str, Any]:
    """Deterministically build an identity for *account_key* (no I/O)."""
    seed = _sha(f"{account_key}|v{IDENTITY_VERSION}")
    ua = user_agent or UA_POOL[_derive_int(seed, salt, len(UA_POOL))]
    screen_w, screen_h, view_w, view_h, dpr = SCREEN_POOL[
        _derive_int(seed, salt + 7, len(SCREEN_POOL))
    ]
    hw = HARDWARE_CONCURRENCY_POOL[_derive_int(seed, salt + 13, len(HARDWARE_CONCURRENCY_POOL))]
    mem = DEVICE_MEMORY_POOL[_derive_int(seed, salt + 17, len(DEVICE_MEMORY_POOL))]
    locale, timezone_id = LOCALE_TIMEZONE_POOL[
        _derive_int(seed, salt + 23, len(LOCALE_TIMEZONE_POOL))
    ]
    platform = _platform_for_ua(ua)
    outer_w = screen_w - _derive_int(seed, salt + 29, 41)
    outer_h = max(view_h, screen_h - 60 - _derive_int(seed, salt + 31, 61))
    inner_w = max(320, outer_w - 20 - _derive_int(seed, salt + 37, 41))
    inner_h = max(240, outer_h - 70 - _derive_int(seed, salt + 41, 71))
    return {
        "version": IDENTITY_VERSION,
        "account_key": account_key,
        "created_at": _now_iso(),
        "signature": "",
        "user_agent": ua,
        "platform": platform,
        "os": _os_name_for_ua(ua),
        "chrome_major": _chrome_major(ua),
        "viewport": {"width": view_w, "height": view_h},
        "screen": {"width": screen_w, "height": screen_h},
        "avail_height": max(view_h, screen_h - 40),
        "device_scale_factor": dpr,
        "outer": {"width": outer_w, "height": outer_h},
        "inner": {"width": inner_w, "height": inner_h},
        "hardware_concurrency": hw,
        "device_memory": mem,
        "timezone_id": timezone_id,
        "locale": locale,
        "languages": [locale, locale.split("-")[0]],
        "color_scheme": "light",
        "seed_salt": salt,
        "rotations": 0,
        "warmup_done": False,
        "source": "derived",
        "profile_dir": None,
        "session_dir": None,
    }


def _chrome_major(ua: str) -> Optional[int]:
    marker = "Chrome/"
    if marker not in ua:
        return None
    tail = ua.split(marker, 1)[1]
    number = tail.split(".", 1)[0]
    try:
        return int(number)
    except ValueError:
        return None


def _observed_fingerprint_from_session(account: Any) -> Optional[Dict[str, Any]]:
    """Read the *observed* UA/platform captured when the session was saved."""
    folder = session_dir_for(account)
    if folder is None:
        return None
    for name in ("fingerprint.json",):
        path = folder / name
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        observed = raw.get("observed") if isinstance(raw, dict) else None
        if isinstance(observed, dict) and observed.get("user_agent"):
            return observed
    return None


def _adopt_observed(account_key: str, observed: Dict[str, Any],
                    taken: Iterable[str]) -> Optional[Dict[str, Any]]:
    ua = str(observed.get("user_agent") or "").strip()
    if not ua or not ua.startswith("Mozilla/"):
        return None
    identity = derive_identity(account_key, salt=0, user_agent=ua)
    if signature_of(identity) in set(taken):
        return None
    identity["source"] = "observed"
    platform = str(observed.get("platform") or "").strip()
    if platform:
        identity["platform"] = platform
    language = str(observed.get("language") or "").strip()
    if language and "-" in language:
        identity["locale"] = language
        identity["languages"] = [language, language.split("-")[0]]
    for key, cast in (("hardware_concurrency", int), ("device_memory", int)):
        try:
            value = observed.get(key)
            if value:
                identity[key] = cast(value)
        except (TypeError, ValueError):
            pass
    return identity


def _unique_identity(account_key: str, taken: Iterable[str], *, salt_start: int = 0,
                     log_fn=None) -> Dict[str, Any]:
    taken_set = set(taken)
    for salt in range(salt_start, salt_start + MAX_UNIQUE_ATTEMPTS):
        candidate = derive_identity(account_key, salt=salt)
        if signature_of(candidate) not in taken_set:
            return candidate
    if log_fn:
        log_fn(f"[Identity] could not find a unique device for {account_key} "
               f"after {MAX_UNIQUE_ATTEMPTS} tries — reusing the best candidate")
    return derive_identity(account_key, salt=salt_start + MAX_UNIQUE_ATTEMPTS)


# --------------------------------------------------------------------------- #
#  public API: get / save / rotate
# --------------------------------------------------------------------------- #

def load_identity(account: Any) -> Optional[Dict[str, Any]]:
    """Return the stored identity (session folder first, then registry)."""
    key = account_key_for(account)
    folder = session_dir_for(account)
    if folder is not None:
        path = folder / IDENTITY_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("user_agent"):
                return raw
        except Exception:
            pass
    entry = registry_snapshot().get(key)
    if isinstance(entry, dict) and entry.get("user_agent"):
        return _identity_from_entry(key, entry)
    return None


def identity_for(account: Any, *, create: bool = True, cfg: Optional[Dict[str, Any]] = None,
                 log_fn=None) -> Optional[Dict[str, Any]]:
    """The account's permanent device identity (created + registered on demand)."""
    cfg = cfg or config()
    if not enabled(cfg):
        return None
    key = account_key_for(account)
    if not key or key == "unknown":
        return None

    existing = load_identity(account)
    if existing is not None:
        _attach_profile_dir(account, existing, cfg, log_fn)
        return existing
    if not create:
        return None

    unique = bool(cfg.get("unique_between_accounts", True))
    adopt = bool(cfg.get("adopt_observed_fingerprint", True))
    with _registry_session() as data:
        entries = data.setdefault(REGISTRY_KEY, {})
        # another thread may have registered it while we waited for the lock
        entry = entries.get(key)
        if isinstance(entry, dict) and entry.get("user_agent"):
            identity = _identity_from_entry(key, entry)
            _attach_profile_dir(account, identity, cfg, log_fn)
            return identity
        taken = {str(item.get("signature") or "") for item in entries.values()
                 if isinstance(item, dict)}
        if not unique:
            taken = set()
        identity = None
        if adopt:
            observed = _observed_fingerprint_from_session(account)
            if observed:
                identity = _adopt_observed(key, observed, taken)
                if identity is not None and log_fn:
                    log_fn(f"[Identity] adopting the browser {key} logged in with")
        if identity is None:
            identity = _unique_identity(key, taken, log_fn=log_fn)
        identity["signature"] = signature_of(identity)
        _attach_profile_dir(account, identity, cfg, log_fn)
        folder = session_dir_for(account)
        if folder is not None:
            identity["session_dir"] = str(folder)
        _write_identity_file(folder, identity)
        entries[key] = _registry_entry(identity)
    return identity


def save_identity(account: Any, identity: Dict[str, Any], *,
                  session_dir: Optional[Path] = None) -> Optional[Path]:
    """Persist an identity into the session folder + registry."""
    if not isinstance(identity, dict) or not identity.get("user_agent"):
        return None
    key = account_key_for(account)
    folder = Path(session_dir) if session_dir else session_dir_for(account)
    if folder is not None:
        identity["session_dir"] = str(folder)
    return _write_identity_file(folder, identity) if folder else _register_identity(key, identity)


def _write_identity_file(folder: Optional[Path], identity: Dict[str, Any]) -> Optional[Path]:
    if folder is None:
        return None
    path = Path(folder) / IDENTITY_FILE
    try:
        _atomic_write_json(path, identity)
    except Exception:
        return None
    return path


def _register_identity(key: str, identity: Dict[str, Any]) -> Optional[Path]:
    try:
        with _registry_session() as data:
            entries = data.setdefault(REGISTRY_KEY, {})
            entries[key] = _registry_entry(identity)
    except Exception:
        return None
    return registry_path()


def attach_session_dir(account: Any, identity: Optional[Dict[str, Any]]) -> None:
    """Write identity.json into the account's (possibly new) session folder."""
    if not isinstance(identity, dict):
        return
    folder = session_dir_for(account)
    if folder is None:
        return
    identity["session_dir"] = str(folder)
    _write_identity_file(folder, identity)


def rotate_identity(account: Any, *, reason: str = "",
                    cfg: Optional[Dict[str, Any]] = None, log_fn=None) -> Optional[Dict[str, Any]]:
    """Give the account a brand-new device (used after a ban)."""
    cfg = cfg or config()
    if not enabled(cfg):
        return None
    key = account_key_for(account)
    if not key or key == "unknown":
        return None
    with _registry_session() as data:
        entries = data.setdefault(REGISTRY_KEY, {})
        taken = {str(item.get("signature") or "") for item in entries.values()
                 if isinstance(item, dict)}
        current = entries.pop(key, None) or {}
        rotations = int(current.get("rotations") or 0) + 1
        identity = _unique_identity(key, taken, salt_start=rotations * 97, log_fn=log_fn)
        identity["rotations"] = rotations
        identity["source"] = "rotated"
        if reason:
            identity["rotation_reason"] = str(reason)
        identity["rotated_at"] = _now_iso()
        identity["signature"] = signature_of(identity)
        _attach_profile_dir(account, identity, cfg, log_fn)
        folder = session_dir_for(account)
        if folder is not None:
            identity["session_dir"] = str(folder)
        _write_identity_file(folder, identity)
        entries[key] = _registry_entry(identity)
    if log_fn:
        log_fn(f"[Identity] rotated the device for {key} (rotation #{rotations})"
               + (f" — {reason}" if reason else ""))
    return identity


# --------------------------------------------------------------------------- #
#  profiles (persistent mode) + warm-up
# --------------------------------------------------------------------------- #

def _attach_profile_dir(account: Any, identity: Dict[str, Any],
                        cfg: Optional[Dict[str, Any]] = None,
                        log_fn=None) -> Optional[Path]:
    """Reserve the account's own Chrome profile (or None in context mode).

    The folder is claimed for this account only: if it belongs to a different
    account, ``claim_profile`` hands out a separate folder instead — two
    accounts must never share one Chrome profile.
    """
    cfg = cfg or config()
    if profile_mode(cfg) != "persistent":
        identity["profile_dir"] = None
        return None
    folder = claim_profile(account, cfg=cfg, log_fn=log_fn)
    identity["profile_dir"] = str(folder) if folder else None
    return folder


def profile_dir_for(account: Any, cfg: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """Per-account real Chromium profile directory (persistent mode).

    The folder is derived from the account key, so every account gets its own.
    An explicit ``profile_dir`` / ``browser_profile_dir`` on the account dict
    wins (the owner guard still decides whether it may be opened).
    """
    cfg = cfg or config()
    if isinstance(account, dict):
        explicit = account.get("profile_dir") or account.get("browser_profile_dir")
        if explicit:
            return Path(str(explicit)).expanduser()
    name = str(cfg.get("profiles_dir_name") or PROFILES_DIRNAME).strip() or PROFILES_DIRNAME
    env = str(os.environ.get("EVA_PROFILES_DIR") or "").strip()
    root = Path(env).expanduser() if env else (primary_root() / name)
    key = account_key_for(account)
    if not key or key == "unknown":
        return None
    safe = "".join(ch if (ch.isalnum() or ch in "._-@") else "_" for ch in key)[:80] or "account"
    return root / safe


def profile_owner(profile_dir: Optional[Path]) -> Optional[Dict[str, Any]]:
    """Who created this profile?  ``owner.json`` lives inside the profile."""
    if not profile_dir:
        return None
    try:
        raw = json.loads((Path(profile_dir) / PROFILE_OWNER_FILE).read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def claim_profile(account: Any, *, cfg: Optional[Dict[str, Any]] = None,
                  log_fn=None) -> Optional[Path]:
    """Reserve THIS account's own profile folder — never another account's.

    A profile folder records its owner (``owner.json``).  If the folder was
    created for a different account (e.g. two accounts share a session folder,
    or a folder name was reused) a *separate* folder is chosen instead, because
    two accounts must never log in from one Chrome profile.
    """
    cfg = cfg or config()
    key = account_key_for(account)
    folder = profile_dir_for(account, cfg)
    if folder is None:
        return None
    email = str(account.get("email") or "") if isinstance(account, dict) else ""
    guard = bool(cfg.get("owner_guard", True))

    for attempt in range(6):
        candidate = folder if attempt == 0 else folder.parent / f"{folder.name}_{_sha(key + str(attempt))[:6]}"
        owner = profile_owner(candidate)
        if owner and guard and str(owner.get("account_key") or "") not in ("", key):
            if log_fn:
                log_fn(f"[Profile] ⛔ {candidate.name} belongs to another account "
                       f"({owner.get('email') or owner.get('account_key')}) — "
                       f"{key} gets its own separate profile")
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except Exception as error:
            if log_fn:
                log_fn(f"[Profile] could not create {candidate}: {error}")
            return None
        record = dict(owner or {})
        record.update({
            "account_key": key,
            "email": email,
            "profile_dir": str(candidate),
            "updated_at": _now_iso(),
            "browser": "chromium",
        })
        record.setdefault("created_at", _now_iso())
        try:
            _atomic_write_json(candidate / PROFILE_OWNER_FILE, record)
        except Exception:
            pass
        return candidate
    if log_fn:
        log_fn(f"[Profile] no free profile folder for {key} — profiles are all claimed")
    return None


def profile_is_used(profile_dir: Optional[Path]) -> bool:
    """True if the profile already holds browser data (so cookies are live)."""
    if not profile_dir:
        return False
    folder = Path(profile_dir)
    if not folder.is_dir():
        return False
    markers = ("Default/Cookies", "Default/History", "Default/Preferences",
               "Default/Local Storage", "Default/IndexedDB")
    return any((folder / marker).exists() for marker in markers)


def warmup_pending(identity: Optional[Dict[str, Any]],
                   cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    cfg = cfg or config()
    sites = [str(s).strip() for s in (cfg.get("warmup_sites") or []) if str(s).strip()]
    if not sites or not isinstance(identity, dict):
        return []
    return [] if identity.get("warmup_done") else sites


def mark_warmup_done(account: Any, identity: Optional[Dict[str, Any]],
                     visited: Optional[Iterable[str]] = None) -> None:
    if not isinstance(identity, dict):
        return
    identity["warmup_done"] = True
    identity["warmup_at"] = _now_iso()
    identity["warmup_visited"] = [str(u) for u in (visited or [])]
    save_identity(account, identity)


# --------------------------------------------------------------------------- #
#  Playwright integration
# --------------------------------------------------------------------------- #

def context_kwargs(identity: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``browser.new_context(**kwargs)`` values for this identity."""
    if not isinstance(identity, dict):
        return {}
    viewport = identity.get("viewport") or {}
    kwargs: Dict[str, Any] = {
        "user_agent": identity.get("user_agent"),
        "viewport": {"width": int(viewport.get("width") or 1280),
                     "height": int(viewport.get("height") or 800)},
        "screen": {"width": int((identity.get("screen") or {}).get("width") or 1280),
                   "height": int((identity.get("screen") or {}).get("height") or 800)},
        "device_scale_factor": float(identity.get("device_scale_factor") or 1.0),
        "locale": identity.get("locale") or "en-US",
        "timezone_id": identity.get("timezone_id") or "America/New_York",
        "color_scheme": identity.get("color_scheme") or "light",
        "is_mobile": False,
        "has_touch": False,
        "java_script_enabled": True,
        "ignore_https_errors": False,
    }
    languages = identity.get("languages")
    if isinstance(languages, list) and languages:
        kwargs["extra_http_headers"] = {
            "Accept-Language": ", ".join(
                [str(languages[0])] + [f"{lang};q={0.9 - i * 0.1:.1f}"
                                       for i, lang in enumerate(languages[1:])]
            )
        }
    return {k: v for k, v in kwargs.items() if v is not None}


def stealth_fingerprint(identity: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Dict accepted by ``browser_engine.chromium_context_kwargs/apply_chromium_stealth``."""
    if not isinstance(identity, dict):
        return {}
    return {
        "user_agent": identity.get("user_agent"),
        "viewport": identity.get("viewport"),
        "platform": identity.get("platform"),
        "hardware_concurrency": identity.get("hardware_concurrency"),
        "device_memory": identity.get("device_memory"),
        # locale/timezone so the init script and the context agree
        "locale": identity.get("locale"),
        "languages": identity.get("languages"),
        "timezone_id": identity.get("timezone_id"),
        "color_scheme": identity.get("color_scheme"),
        # stable window/screen metrics (prevents a fresh "screen size" every run)
        "screen": identity.get("screen"),
        "device_scale_factor": identity.get("device_scale_factor"),
        "avail_height": identity.get("avail_height"),
        "outer": identity.get("outer"),
        "inner": identity.get("inner"),
    }


def describe(identity: Optional[Dict[str, Any]]) -> str:
    if not isinstance(identity, dict) or not identity.get("user_agent"):
        return "no saved device profile (random fingerprint per launch)"
    viewport = identity.get("viewport") or {}
    ua = str(identity.get("user_agent"))
    chrome = identity.get("chrome_major")
    short_ua = "Chrome/" + str(chrome) if chrome else ua[:40]
    return (f"{identity.get('os') or '?'} · {short_ua} · "
            f"{viewport.get('width')}x{viewport.get('height')} · "
            f"hw {identity.get('hardware_concurrency')} · mem {identity.get('device_memory')}GB · "
            f"{identity.get('locale')}/{identity.get('timezone_id')}"
            + (f" · rotations {identity['rotations']}" if identity.get("rotations") else ""))


# Reads the values the site itself can see (navigator/screen/WebGL/canvas/tz).
PAGE_FINGERPRINT_JS = """
() => {
    const safe = (fn, fallback = null) => { try { return fn(); } catch (_) { return fallback; } };
    const gl = safe(() => {
        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
        if (!ctx) return {};
        const ext = ctx.getExtension('WEBGL_debug_renderer_info');
        return {
            vendor: ext ? ctx.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
            renderer: ext ? ctx.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
        };
    }, {});
    const canvasHash = safe(() => {
        const canvas = document.createElement('canvas');
        canvas.width = 240; canvas.height = 60;
        const ctx = canvas.getContext('2d');
        ctx.textBaseline = 'top';
        ctx.font = '16px Arial';
        ctx.fillStyle = '#f60';
        ctx.fillRect(8, 8, 120, 30);
        ctx.fillStyle = '#069';
        ctx.fillText('device-profile', 12, 12);
        const value = canvas.toDataURL();
        let hash = 0;
        for (let i = 0; i < value.length; i += 1) {
            hash = ((hash << 5) - hash) + value.charCodeAt(i);
            hash |= 0;
        }
        return String(hash >>> 0);
    });
    const tz = safe(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
    return {
        user_agent: navigator.userAgent,
        platform: navigator.platform,
        languages: navigator.languages,
        language: navigator.language,
        hardware_concurrency: navigator.hardwareConcurrency,
        device_memory: navigator.deviceMemory,
        webdriver: navigator.webdriver,
        screen: {width: screen.width, height: screen.height},
        avail_height: screen.availHeight,
        viewport: {width: window.innerWidth, height: window.innerHeight},
        device_scale_factor: window.devicePixelRatio,
        timezone: tz,
        webgl_vendor: gl.vendor,
        webgl_renderer: gl.renderer,
        canvas_hash: canvasHash,
        url: location.href,
    };
}
"""


def read_page_fingerprint(page) -> Dict[str, Any]:
    """Ask the page what the site can see (never raises)."""
    try:
        values = page.evaluate(PAGE_FINGERPRINT_JS)
    except Exception as error:
        return {"error": str(error)}
    return values if isinstance(values, dict) else {"error": "unexpected probe result"}


def verify_identity_on_page(page, identity: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compare the live browser with the account's identity BEFORE logging in.

    Returns ``{ok, mismatches, observed, checked_at}``.  ``ok`` is True only when
    every value the site can read agrees with the stored device profile.
    """
    result: Dict[str, Any] = {"ok": False, "mismatches": [], "observed": {},
                              "checked_at": _now_iso()}
    if not isinstance(identity, dict):
        result["mismatches"].append("no saved device profile")
        return result
    observed = read_page_fingerprint(page)
    result["observed"] = observed
    if observed.get("error"):
        result["mismatches"].append(f"probe failed: {observed['error']}")
        return result

    def mismatch(label, expected, actual):
        if expected is None or expected == "":
            return
        if str(expected) != str(actual):
            result["mismatches"].append(f"{label}: expected {expected}, got {actual}")

    mismatch("user agent", identity.get("user_agent"), observed.get("user_agent"))
    mismatch("platform", identity.get("platform"), observed.get("platform"))
    mismatch("hardware concurrency", identity.get("hardware_concurrency"),
             observed.get("hardware_concurrency"))
    mismatch("device memory", identity.get("device_memory"), observed.get("device_memory"))
    mismatch("locale", identity.get("locale"), observed.get("language"))
    mismatch("timezone", identity.get("timezone_id"), observed.get("timezone"))
    screen = identity.get("screen") or {}
    viewport = identity.get("viewport") or {}
    observed_screen = observed.get("screen") or {}
    observed_viewport = observed.get("viewport") or {}
    mismatch("screen width", screen.get("width"), observed_screen.get("width"))
    mismatch("screen height", screen.get("height"), observed_screen.get("height"))
    mismatch("viewport width", viewport.get("width"), observed_viewport.get("width"))
    mismatch("viewport height", viewport.get("height"), observed_viewport.get("height"))
    dpr = identity.get("device_scale_factor")
    if dpr:
        got = observed.get("device_scale_factor")
        try:
            if abs(float(dpr) - float(got)) > 0.01:
                result["mismatches"].append(f"device pixel ratio: expected {dpr}, got {got}")
        except (TypeError, ValueError):
            result["mismatches"].append(f"device pixel ratio: expected {dpr}, got {got}")
    if observed.get("webdriver"):
        result["mismatches"].append("navigator.webdriver is visible (automation tell)")
    result["ok"] = not result["mismatches"]
    return result


def mark_profile_saved(account: Any, identity: Optional[Dict[str, Any]],
                       *, probe: Optional[Dict[str, Any]] = None,
                       profile_dir: Optional[Path] = None,
                       log_fn=None) -> bool:
    """Remember that the profile now holds a logged-in account (post-login save)."""
    if not isinstance(identity, dict):
        return False
    folder = Path(profile_dir) if profile_dir else profile_dir_for(account)
    identity["profile_saved_at"] = _now_iso()
    identity["profile_dir"] = str(folder) if folder else identity.get("profile_dir")
    if isinstance(probe, dict):
        identity["verified_observed"] = {
            key: probe.get(key) for key in
            ("user_agent", "platform", "screen", "viewport", "device_scale_factor",
             "timezone", "webgl_vendor", "webgl_renderer", "canvas_hash")
        }
        identity["verified_at"] = _now_iso()
    save_identity(account, identity)
    if folder:
        owner = dict(profile_owner(folder) or {})
        owner.update({
            "account_key": account_key_for(account),
            "email": str(account.get("email") or "") if isinstance(account, dict) else "",
            "profile_dir": str(folder),
            "logged_in_at": identity["profile_saved_at"],
            "browser": "chromium",
        })
        owner.setdefault("created_at", _now_iso())
        try:
            _atomic_write_json(folder / PROFILE_OWNER_FILE, owner)
        except Exception:
            pass
        try:
            _atomic_write_json(folder / "profile_state.json",
                               {"saved_at": identity["profile_saved_at"],
                                "observed": identity.get("verified_observed")})
        except Exception:
            pass
    if log_fn:
        log_fn(f"[Profile] saved the logged-in browser profile for "
               f"{account_key_for(account)} → {folder}")
    return True


def pool_options(account: Any, *, cfg: Optional[Dict[str, Any]] = None,
                 log_fn=None) -> Dict[str, Any]:
    """Options for ``ContextPool.create_context()`` (identity, unique per account)."""
    identity = identity_for(account, cfg=cfg, log_fn=log_fn)
    if not identity:
        return {}
    options = context_kwargs(identity)
    return {
        "identity": identity,
        "extra_context_options": options,
        "stealth_fingerprint": stealth_fingerprint(identity),
    }


# --------------------------------------------------------------------------- #
#  audit (used by tools/session_doctor.py)
# --------------------------------------------------------------------------- #

def audit(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or config()
    entries = registry_snapshot()
    by_signature: Dict[str, List[str]] = {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        by_signature.setdefault(str(entry.get("signature") or ""), []).append(key)
    duplicates = [{"signature": sig, "accounts": keys}
                  for sig, keys in by_signature.items() if sig and len(keys) > 1]
    return {
        "enabled": enabled(cfg),
        "profile_mode": profile_mode(cfg),
        "registry": str(registry_path()),
        "total": len(entries),
        "duplicates": duplicates,
        "unique": not duplicates,
        "identities": entries,
    }


def profile_status(account: Any, *, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Everything the tools need to show about one account's Chrome profile."""
    cfg = cfg or config()
    folder = profile_dir_for(account, cfg)
    owner = profile_owner(folder)
    key = account_key_for(account)
    return {
        "account_key": key,
        "profile_mode": profile_mode(cfg),
        "profile_dir": str(folder) if folder else None,
        "exists": bool(folder and Path(folder).is_dir()),
        "used": profile_is_used(folder),
        "owner": owner,
        "owned_by_this_account": bool(owner) and str(owner.get("account_key")) == key,
        "claimed_by_other": bool(owner) and str(owner.get("account_key")) not in ("", key),
        "saved_at": (owner or {}).get("logged_in_at"),
    }


def format_identity_table(entries: Optional[Dict[str, Any]] = None) -> str:
    entries = entries if entries is not None else registry_snapshot()
    if not entries:
        return "no device profiles yet (they are created on the first launch)"
    lines = []
    for key, entry in sorted(entries.items()):
        if not isinstance(entry, dict):
            continue
        ua = str(entry.get("user_agent") or "")
        chrome = _chrome_major(ua)
        viewport = entry.get("viewport") or {}
        lines.append(
            f"  {key[:34]:<34} {str(entry.get('platform') or '?'):<12} "
            f"{('Chrome/' + str(chrome)) if chrome else '?':<10} "
            f"{str(viewport.get('width')) + 'x' + str(viewport.get('height')):<10} "
            f"{str(entry.get('timezone_id') or '?'):<20} "
            f"{'rot ' + str(entry.get('rotations')) if entry.get('rotations') else ''}"
        )
    return "\n".join(lines)


__all__ = [
    "IDENTITY_FILE", "PAGE_FINGERPRINT_JS", "PROFILE_MODES", "PROFILE_OWNER_FILE",
    "PROFILES_DIRNAME", "REGISTRY_FILE", "claim_profile", "mark_profile_saved",
    "profile_owner", "profile_status", "read_page_fingerprint",
    "verify_identity_on_page",
    "account_key_for", "attach_session_dir", "audit", "config", "context_kwargs",
    "derive_identity", "describe", "enabled", "format_identity_table",
    "identity_for", "load_identity", "mark_warmup_done", "pool_options",
    "primary_root", "profile_dir_for", "profile_is_used", "profile_mode",
    "profiles_root", "registry_path", "registry_snapshot", "rotate_identity",
    "save_identity", "session_dir_for", "signature_of", "stealth_fingerprint",
    "warmup_pending",
]
