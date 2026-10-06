"""Persistence helpers for account browser sessions.

The files written by this module contain authentication cookies, local storage,
and (when supplied) proxy credentials.  Callers should keep the session root
private; the project gitignore excludes the default directory.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ACCOUNT_SESSIONS_DIR_NAME = "account_sessions"
SESSION_SCHEMA_VERSION = 1
RESTORE_ENTRY_URL = "https://app.chitchat.gg/start/new"


def get_account_sessions_dir(base_dir=None) -> Path:
    """Return the *primary* account-session directory (where saves go)."""
    if base_dir is not None:
        root = Path(base_dir)
    else:
        # Use the project root (parent of the root-level paths.py), not the
        # package folder this module now lives in and not Path.cwd().
        root = Path(__file__).resolve().parent.parent
    return root / ACCOUNT_SESSIONS_DIR_NAME


def iter_session_roots(base_dir=None):
    """Every directory that may hold saved sessions.

    The bot has written sessions to both ``account_sessions/`` (this module)
    and ``data/account_sessions/`` (context pool), and ``EVA_SESSIONS_DIR``
    can add a third location.  Loading must look at all of them, otherwise
    "restore my saved accounts" silently finds nothing.
    """
    module = _health_module()
    if module is not None:
        try:
            roots = module.default_session_roots(base_dir)
            if roots:
                return list(roots)
        except Exception:
            pass
    return [get_account_sessions_dir(base_dir)]


def account_session_key(email: str) -> str:
    """Return a stable, filesystem-safe identifier without exposing the email."""
    normalized = str(email or "").strip().lower()
    if not normalized:
        raise ValueError("An account email is required to save a browser session")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def sanitize_proxy_config(proxy_config):
    """Copy the proxy fields used by Camoufox, preserving proxy auth when present."""
    if not isinstance(proxy_config, dict):
        return None

    server = str(proxy_config.get("server") or "").strip()
    if not server:
        return None

    sanitized = {"server": server}
    for key in ("username", "password"):
        value = proxy_config.get(key)
        if value is not None:
            sanitized[key] = str(value)
    return sanitized


def _atomic_write_json(path: Path, value) -> None:
    """Write JSON beside its destination and replace the destination atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _serialize_generated_fingerprint(fingerprint):
    if fingerprint is None:
        return None
    if hasattr(fingerprint, "dumps"):
        return json.loads(fingerprint.dumps())
    if dataclasses.is_dataclass(fingerprint):
        return dataclasses.asdict(fingerprint)
    if isinstance(fingerprint, dict):
        return fingerprint
    raise TypeError(f"Unsupported fingerprint type: {type(fingerprint)!r}")


def _capture_observed_fingerprint(page):
    """Capture stable, observable browser properties from the active page."""
    return page.evaluate(
        """
        () => {
            const safe = (callback, fallback = null) => {
                try { return callback(); } catch (_) { return fallback; }
            };
            const webgl = safe(() => {
                const canvas = document.createElement('canvas');
                const context = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
                if (!context) return null;
                const extension = context.getExtension('WEBGL_debug_renderer_info');
                return {
                    vendor: extension ? context.getParameter(extension.UNMASKED_VENDOR_WEBGL) : null,
                    renderer: extension ? context.getParameter(extension.UNMASKED_RENDERER_WEBGL) : null,
                };
            });
            const canvasHash = safe(() => {
                const canvas = document.createElement('canvas');
                canvas.width = 240;
                canvas.height = 60;
                const context = canvas.getContext('2d');
                context.textBaseline = 'top';
                context.font = '16px Arial';
                context.fillStyle = '#f60';
                context.fillRect(8, 8, 120, 30);
                context.fillStyle = '#069';
                context.fillText('account-session', 12, 12);
                const value = canvas.toDataURL();
                let hash = 0;
                for (let index = 0; index < value.length; index += 1) {
                    hash = ((hash << 5) - hash) + value.charCodeAt(index);
                    hash |= 0;
                }
                return String(hash >>> 0);
            });
            return {
                user_agent: navigator.userAgent,
                app_version: navigator.appVersion,
                platform: navigator.platform,
                vendor: navigator.vendor,
                language: navigator.language,
                languages: Array.from(navigator.languages || []),
                hardware_concurrency: navigator.hardwareConcurrency || null,
                device_memory: navigator.deviceMemory || null,
                max_touch_points: navigator.maxTouchPoints || 0,
                webdriver: !!navigator.webdriver,
                timezone: safe(() => Intl.DateTimeFormat().resolvedOptions().timeZone),
                screen: {
                    width: screen.width,
                    height: screen.height,
                    avail_width: screen.availWidth,
                    avail_height: screen.availHeight,
                    color_depth: screen.colorDepth,
                    pixel_depth: screen.pixelDepth,
                },
                viewport: {
                    width: window.innerWidth,
                    height: window.innerHeight,
                    device_pixel_ratio: window.devicePixelRatio,
                },
                webgl: webgl,
                canvas_hash: canvasHash,
            };
        }
        """
    )


def _write_storage_state(context, destination: Path) -> None:
    """Save cookies/local storage, using IndexedDB when this Playwright supports it."""
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        try:
            context.storage_state(path=str(temporary), indexed_db=True)
        except TypeError:
            # Older Playwright versions do not expose the indexed_db argument.
            context.storage_state(path=str(temporary))
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def save_account_session(
    account,
    context,
    page,
    *,
    generated_fingerprint=None,
    proxy_config=None,
    history=None,
    base_dir=None,
):
    """Persist the successful account session and return its directory."""
    if not isinstance(account, dict):
        raise ValueError("An account mapping is required to save a browser session")

    email = str(account.get("email") or "").strip()
    session_key = account.get("session_key") or account_session_key(email)
    # Phase 13: refresh the session *in place*.  A restored account knows its
    # own folder (it may live in data/account_sessions/ or a custom root), so
    # re-saving must not create a duplicate under the primary root.
    explicit_dir = account.get("session_dir")
    if explicit_dir:
        session_dir = Path(explicit_dir)
    else:
        session_dir = get_account_sessions_dir(base_dir) / f"account_{session_key}"
    session_dir.mkdir(parents=True, exist_ok=True)

    storage_path = session_dir / "storage_state.json"
    fingerprint_path = session_dir / "fingerprint.json"
    history_path = session_dir / "history.json"
    metadata_path = session_dir / "metadata.json"

    _write_storage_state(context, storage_path)

    observed_fingerprint = _capture_observed_fingerprint(page)
    _atomic_write_json(
        fingerprint_path,
        {
            "generated": _serialize_generated_fingerprint(generated_fingerprint),
            "observed": observed_fingerprint,
        },
    )

    history_entries = list(history or [])
    _atomic_write_json(history_path, history_entries)

    metadata = {
        "schema_version": SESSION_SCHEMA_VERSION,
        "account_key": session_key,
        "email": email,
        "account_type": account.get("account_type") or "login_account",
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "restore_url": RESTORE_ENTRY_URL,
        "storage_state": storage_path.name,
        "fingerprint": fingerprint_path.name,
        "history": history_path.name,
        "proxy": sanitize_proxy_config(proxy_config),
        "last_url": getattr(page, "url", "") or "",
    }
    _atomic_write_json(metadata_path, metadata)
    return session_dir


def _safe_session_file(session_dir: Path, filename) -> Path | None:
    if not filename:
        return None
    root = session_dir.resolve()
    candidate = (session_dir / str(filename)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def load_saved_account_sessions(base_dir=None):
    """Discover saved sessions for the GUI restore mode (all roots).

    Phase 13: each account carries its session ``health`` (``state``,
    ``blind``, ``days_left``), whether matching credentials exist
    (``has_credentials`` — the password is attached in memory only) and the
    recommended ``action`` (``restore`` / ``repair`` / ``skip``), so callers
    can load, repair or skip an account without a browser launch.
    """
    accounts = []
    seen_keys = set()
    metadata_paths = []
    for root in iter_session_roots(base_dir):
        try:
            if not Path(root).is_dir():
                continue
        except OSError:
            continue
        metadata_paths.extend(sorted(Path(root).glob("account_*/metadata.json")))
    for metadata_path in metadata_paths:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            email = str(metadata.get("email") or "").strip()
            session_key = str(
                metadata.get("account_key") or account_session_key(email)
            )
            storage_path = _safe_session_file(
                metadata_path.parent, metadata.get("storage_state")
            )
            fingerprint_path = _safe_session_file(
                metadata_path.parent, metadata.get("fingerprint")
            )
            history_path = _safe_session_file(
                metadata_path.parent, metadata.get("history")
            )
            if (
                not email
                or session_key in seen_keys
                or storage_path is None
                or not storage_path.is_file()
            ):
                continue

            seen_keys.add(session_key)
            account = {
                "email": email,
                "account_type": metadata.get("account_type") or "login_account",
                "session_key": session_key,
                "session_dir": str(metadata_path.parent),
                "storage_state_path": str(storage_path),
                "fingerprint_path": str(fingerprint_path) if fingerprint_path else None,
                "history_path": str(history_path) if history_path else None,
                "restore_url": metadata.get("restore_url") or RESTORE_ENTRY_URL,
                "saved_proxy": sanitize_proxy_config(metadata.get("proxy")),
                "last_url": metadata.get("last_url") or "",
                "restore": True,
                "banned": (metadata_path.parent / BANNED_FLAG_FILENAME).exists(),
            }
            _attach_health_and_plan(account)
            accounts.append(account)
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            # One damaged session should not prevent the remaining sessions
            # from being offered for restore.
            continue

    return accounts


# --------------------------------------------------------------------------- #
#  BAN + ALIVE-COUNT HELPERS (additive — do not modify existing functions)
# --------------------------------------------------------------------------- #

BANNED_FLAG_FILENAME = ".banned"


def _session_dir_from_account(account, base_dir=None):
    """Resolve the on-disk session directory for an account mapping."""
    if not isinstance(account, dict):
        return None
    session_dir = account.get("session_dir")
    if session_dir:
        path = Path(session_dir)
        return path if path.is_dir() else None
    email = str(account.get("email") or "").strip()
    if not email:
        return None
    key = account.get("session_key") or account_session_key(email)
    candidate = get_account_sessions_dir(base_dir) / f"account_{key}"
    return candidate if candidate.is_dir() else None


def delete_account_session(account, base_dir=None):
    """Delete a saved account session directory and return True on success.

    Used after a ban is detected so the dead session is not offered for restore.
    """
    session_dir = _session_dir_from_account(account, base_dir=base_dir)
    if session_dir is None or not session_dir.exists():
        return False
    try:
        import shutil

        shutil.rmtree(session_dir, ignore_errors=True)
    except Exception:
        return False
    return not session_dir.exists()


def mark_session_banned(account, base_dir=None):
    """Flag a session directory as banned without deleting it.

    Returns the path of the flag file on success, ``None`` otherwise.  When the
    flag already exists the existing path is returned so callers stay idempotent.
    """
    session_dir = _session_dir_from_account(account, base_dir=base_dir)
    if session_dir is None:
        return None
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        flag = session_dir / BANNED_FLAG_FILENAME
        if not flag.exists():
            _atomic_write_json(
                flag.with_suffix(".json"),
                {
                    "banned_at": datetime.now(timezone.utc).isoformat(),
                    "email": str(account.get("email") or "") if isinstance(account, dict) else "",
                },
            )
            flag.write_text("banned", encoding="utf-8")
        return flag
    except Exception:
        return None


def _iter_session_dirs(base_dir=None):
    """All ``account_*`` folders across every session root (deduped by name)."""
    directories = []
    seen = set()
    for root in iter_session_roots(base_dir):
        try:
            is_dir = Path(root).is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            continue
        for entry in sorted(Path(root).glob("account_*")):
            if not entry.is_dir() or entry.name in seen:
                continue
            seen.add(entry.name)
            directories.append(entry)
    return directories


def _is_session_banned(session_dir: Path) -> bool:
    return (session_dir / BANNED_FLAG_FILENAME).exists()


def _session_has_storage(session_dir: Path) -> bool:
    meta = session_dir / "metadata.json"
    if not meta.is_file():
        return False
    try:
        metadata = json.loads(meta.read_text(encoding="utf-8"))
        storage_name = metadata.get("storage_state")
        if not storage_name:
            return False
        return (session_dir / str(storage_name)).is_file()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _health_module():
    """Lazy import of browser.session_health (keeps this module standalone)."""
    try:
        from browser import session_health
        return session_health
    except Exception:
        return None


def session_health_for(account_or_dir, warn_days=None):
    """Health dict for a session (delegates to browser.session_health)."""
    module = _health_module()
    if module is None:
        return {"state": "unknown", "blind": False, "reason": "health module unavailable"}
    try:
        if warn_days is None:
            warn_days = default_warn_days()
        return module.session_health(account_or_dir, warn_days=warn_days)
    except Exception as exc:
        return {"state": "unknown", "blind": False,
                "reason": f"health check failed: {type(exc).__name__}: {exc}"}


def default_warn_days() -> float:
    try:
        from core.config_loader import load_session_management
        return float(load_session_management().get("session_expiry_warn_days", 3))
    except Exception:
        return 3.0


def _attach_health_and_plan(account):
    """Add health + repair plan (+ in-memory credentials) to an account dict."""
    module = _health_module()
    if module is None:
        return account
    try:
        creds = module.load_credentials()
        plan = module.plan_for_account(account, credentials=creds,
                                       warn_days=default_warn_days())
        account["health"] = plan.get("health") or {}
        account["blind"] = bool(account["health"].get("blind"))
        account["plan"] = plan
        account["action"] = plan.get("action")
        account["has_credentials"] = bool(plan.get("has_credentials"))
        if plan.get("action") == "repair":
            module.attach_credentials(account, creds)
    except Exception:
        account.setdefault("health", {})
        account.setdefault("blind", False)
    return account


def count_alive_sessions(base_dir=None):
    """Return the number of *loadable* (not banned, live cookies) sessions.

    Phase 13: an empty/corrupt/expired session no longer counts as alive — it
    is reported by :func:`count_blind_sessions` instead, so the stock numbers
    are honest.
    """
    module = _health_module()
    if module is None:
        alive = 0
        for session_dir in _iter_session_dirs(base_dir=base_dir):
            if not _is_session_banned(session_dir) and _session_has_storage(session_dir):
                alive += 1
        return alive
    warn_days = default_warn_days()
    alive = 0
    for session_dir in _iter_session_dirs(base_dir=base_dir):
        if _is_session_banned(session_dir):
            continue
        health = session_health_for(session_dir, warn_days=warn_days)
        if not health.get("blind"):
            alive += 1
    return alive


def count_blind_sessions(base_dir=None):
    """Return the number of saved sessions whose cookies are gone/broken."""
    module = _health_module()
    if module is None:
        return 0
    warn_days = default_warn_days()
    blind = 0
    for session_dir in _iter_session_dirs(base_dir=base_dir):
        if _is_session_banned(session_dir):
            continue
        if session_health_for(session_dir, warn_days=warn_days).get("blind"):
            blind += 1
    return blind


def count_banned_sessions(base_dir=None):
    """Return the number of sessions flagged as banned."""
    return sum(
        1 for session_dir in _iter_session_dirs(base_dir=base_dir)
        if _is_session_banned(session_dir)
    )


def count_total_sessions(base_dir=None):
    """Return the total number of saved session directories."""
    return len(_iter_session_dirs(base_dir=base_dir))


def list_session_statuses(base_dir=None):
    """Return a list of dicts describing each saved session's status."""
    statuses = []
    for session_dir in _iter_session_dirs(base_dir=base_dir):
        email = ""
        saved_at = ""
        try:
            metadata = json.loads(
                (session_dir / "metadata.json").read_text(encoding="utf-8")
            )
            email = str(metadata.get("email") or "")
            saved_at = str(metadata.get("saved_at") or "")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        statuses.append(
            {
                "session_dir": str(session_dir),
                "email": email,
                "saved_at": saved_at,
                "banned": _is_session_banned(session_dir),
                "has_storage": _session_has_storage(session_dir),
                "alive": (not _is_session_banned(session_dir))
                and _session_has_storage(session_dir),
            }
        )
    return statuses


def session_stock_summary(base_dir=None):
    """Return alive / blind / banned / total counts for live logging."""
    total = count_total_sessions(base_dir=base_dir)
    banned = count_banned_sessions(base_dir=base_dir)
    alive = count_alive_sessions(base_dir=base_dir)
    blind = count_blind_sessions(base_dir=base_dir)
    return {"alive": alive, "blind": blind, "banned": banned, "total": total}


def refresh_storage_state(account, context, page=None, *, reason: str = "",
                          extra_metadata=None, base_dir=None):
    """Write the *current* cookies/local storage again (session keep-alive).

    Called after a successful chat (throttled) so long runs keep fresh cookies
    on disk for the next start, and blind sessions that were repaired by a
    credential login become loadable again.  Never raises.
    """
    if not isinstance(account, dict):
        return None
    try:
        session_dir = _session_dir_from_account(account, base_dir=base_dir)
        if session_dir is None:
            email = str(account.get("email") or "").strip()
            key = account.get("session_key") or account_session_key(email)
            session_dir = get_account_sessions_dir(base_dir) / f"account_{key}"
        session_dir.mkdir(parents=True, exist_ok=True)
        destination = session_dir / "storage_state.json"
        _write_storage_state(context, destination)
        now = datetime.now(timezone.utc).isoformat()
        module = _health_module()
        health = {}
        if module is not None:
            try:
                health = module.session_health(session_dir, warn_days=default_warn_days())
            except Exception:
                health = {}
        metadata = {
            "last_refresh_at": now,
            "last_verified_at": now,
            "refresh_reason": str(reason or "keep-alive"),
            "health": {k: v for k, v in health.items()
                       if k not in ("info", "path", "session_dir")} or None,
            "health_state": health.get("state"),
            "blind": bool(health.get("blind")),
        }
        if page is not None:
            try:
                metadata["last_url"] = str(getattr(page, "url", "") or "")
            except Exception:
                pass
        if extra_metadata:
            metadata.update(extra_metadata)
        _atomic_write_json(session_dir / "metadata.json",
                           {**(_read_json(session_dir / "metadata.json") or {}), **metadata})
        return session_dir
    except Exception:
        return None


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def record_session_health(account, health=None, *, warn_days=None, **extra):
    """Persist the health block for a session (delegates when available)."""
    module = _health_module()
    if module is None:
        return False
    try:
        if health is None:
            health = session_health_for(account, warn_days=warn_days)
        return module.record_health(account, health, **extra)
    except Exception:
        return False


def load_saved_fingerprint(account):
    """Load the BrowserForge fingerprint object saved for an account, if valid."""
    path = account.get("fingerprint_path") if isinstance(account, dict) else None
    if not path:
        return None

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        data = payload.get("generated") if isinstance(payload, dict) else payload
        if not isinstance(data, dict):
            return None

        from browserforge.fingerprints.generator import (
            Fingerprint,
            NavigatorFingerprint,
            ScreenFingerprint,
            VideoCard,
        )

        def build(cls, values):
            if not isinstance(values, dict):
                return None
            allowed = {field.name for field in dataclasses.fields(cls)}
            return cls(**{key: value for key, value in values.items() if key in allowed})

        screen = build(ScreenFingerprint, data.get("screen"))
        navigator = build(NavigatorFingerprint, data.get("navigator"))
        if screen is None or navigator is None:
            return None

        video_card = data.get("videoCard")
        return Fingerprint(
            screen=screen,
            navigator=navigator,
            headers=dict(data.get("headers") or {}),
            videoCodecs=dict(data.get("videoCodecs") or {}),
            audioCodecs=dict(data.get("audioCodecs") or {}),
            pluginsData=dict(data.get("pluginsData") or {}),
            battery=data.get("battery"),
            videoCard=build(VideoCard, video_card) if video_card else None,
            multimediaDevices=list(data.get("multimediaDevices") or []),
            fonts=list(data.get("fonts") or []),
            mockWebRTC=data.get("mockWebRTC"),
            slim=data.get("slim"),
        )
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        ImportError,
    ):
        return None
