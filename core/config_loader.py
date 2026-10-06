"""Load configuration shared by source and frozen Chitchat Bot builds.

This module provides a single, fully-documented configuration system so the
entire bot can be customised A-to-Z by editing ``config.json`` — chat timing,
human behaviour, background tabs, performance tuning, ban detection, session
management, reply styling and periodic website visits.

Every section has safe defaults, so a missing or partial ``config.json`` never
breaks the bot; unknown keys are ignored and invalid values fall back to the
defaults.
"""

import json
import math
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
#  DEFAULT CONFIGURATION (mirrors config.json — edit either one)
# --------------------------------------------------------------------------- #

DEFAULT_CHAT_TIMING = {
    "new_chat_delay_min_seconds": 3.0,
    "new_chat_delay_max_seconds": 5.0,
    "rest_interval_min_minutes": 5.0,
    "rest_interval_max_minutes": 10.0,
    "rest_duration_min_minutes": 1.0,
    "rest_duration_max_minutes": 2.0,
}

DEFAULT_HUMAN_BEHAVIOR = {
    # Make the bot look human: random pauses, typing speed, reading time.
    "enabled": True,
    # Pause (seconds) before clicking a button, after hovering.
    "reaction_pause_min_seconds": 0.25,
    "reaction_pause_max_seconds": 0.85,
    # Simulated typing speed (characters per second) for message input.
    "typing_speed_min_cps": 8.0,
    "typing_speed_max_cps": 16.0,
    # Reading pause after a stranger replies (seconds).
    "read_reply_min_seconds": 0.8,
    "read_reply_max_seconds": 2.5,
    # Occasional micro-idle (seconds) — random scroll/tap while waiting.
    "micro_idle_chance": 0.35,
    "micro_idle_min_seconds": 0.4,
    "micro_idle_max_seconds": 1.8,
    # Typing simulator: extra pre-send pause scaled by reply length.
    "typing_ms_per_char": 20.0,
    "typing_max_seconds": 2.5,
    # Inactivity watcher: nudge the stranger after no reply for N seconds,
    # at most this many nudges per chat, then end the chat after the grace.
    "inactivity_timeout_seconds": 600.0,
    "inactivity_nudges_max": 1,
    "inactivity_grace_seconds": 300.0,
}

DEFAULT_BACKGROUND_TABS = {
    # Single-site visits: between chats the bot opens ONE random site from
    # data/unique_sites.txt in a fresh tab, reads/scrolls it a few seconds,
    # closes the tab and returns to chitchat.gg.  (Old persistent YouTube/
    # Reddit/Wikipedia background tabs were removed.)
    "enabled": True,
    # URLs kept for backward compatibility; no longer opened as tabs.
    "urls": [],
    # Periodically open one site from unique_sites.txt and return to chat.
    "periodic_visit_enabled": True,
    "periodic_visit_interval_min_minutes": 3.0,
    "periodic_visit_interval_max_minutes": 7.0,
    # Read/scroll duration on the visited site (3-6s per the user spec).
    "periodic_visit_dwell_min_seconds": 3.0,
    "periodic_visit_dwell_max_seconds": 6.0,
    "periodic_visit_scroll": True,
    # Kept for backward compatibility (unused now).
    "tab_open_delay_min_seconds": 3.0,
    "tab_open_delay_max_seconds": 4.0,
}

DEFAULT_PERFORMANCE = {
    # Smooth, low-CPU operation.  These reduce the load on weaker PCs.
    "poll_interval_seconds": 0.5,        # how often to check for new messages
    "connect_poll_interval_seconds": 1.0,  # how often to check chat connect
    "min_poll_interval_seconds": 0.2,    # floor (never poll faster than this)
    "evaluate_throttle_seconds": 0.0,    # min gap between page.evaluate() calls
    # Block heavy/unnecessary network resources to save CPU + RAM + bandwidth.
    "block_resource_types": [],          # e.g. ["image","media","font"]
    # Slow-motion the whole bot (seconds added to every wait) for debugging.
    "debug_slowdown_seconds": 0.0,
    # Browser engine: "chromium" (default, recommended — stable, renders every
    # site, low memory) or "camoufox" (legacy anti-detect Firefox — higher
    # memory, can blank on heavy sites).  All higher-level code (tabs, human
    # behavior, chat) works identically with either engine.
    "browser_engine": "chromium",
}

DEFAULT_BAN_DETECTION = {
    # Detect banned/suspended accounts and auto-close the browser + delete the
    # saved session so the slot is never reused.
    "enabled": True,
    # Substrings (lowercase) that indicate a ban when found on the page or URL.
    "ban_indicators": [
        "you have been banned",
        "account suspended",
        "account has been suspended",
        "your account is banned",
        "access denied",
        "you are banned",
        "permanently banned",
        "temporarily banned",
        "your ip has been",
    ],
    # Treat a redirect to /login mid-session as a dead (banned/expired) session.
    "login_redirect_is_ban": True,
    # Delete the saved session folder when a ban is detected.
    "auto_delete_banned_session": True,
    # Auto-close the browser as soon as a ban is detected.
    "auto_close_on_ban": True,
}

DEFAULT_SESSION_MANAGEMENT = {
    # Stock / count of alive accounts shown in the live logs.
    "log_alive_count_on_start": True,
    "log_alive_count_after_each_chat": False,
    # Directory name (relative to cwd) for saved account sessions.
    "sessions_dir_name": "account_sessions",
}

DEFAULT_REPLIES = {
    # Casual openers/replies the bot can use for warmup short chats.  The main
    # chat still uses the message-template files; this is only for warmup.
    "warmup_openers": [
        "hey :)",
        "how are you",
        "hi there",
        "what's up",
        "hello!",
        "hey, how's it going",
    ],
    # Warmup short-chat count range (only used when human warmup runs).
    "warmup_chat_count_min": 2,
    "warmup_chat_count_max": 3,
    "warmup_messages_per_chat_min": 1,
    "warmup_messages_per_chat_max": 2,
}

DEFAULT_WEBSITE_VISITS = {
    # Disposable pre-homepage tabs (already loaded from urls.txt by default).
    "pre_homepage_enabled": True,
    # Number of disposable tabs to open before the homepage (0 = all in urls.txt).
    "pre_homepage_tab_count": 0,
    # Human warmup after account creation: browse + short chats + auto-close.
    "human_warmup_enabled": True,
    "warmup_browse_homepage": True,
    "warmup_scroll": True,
    "warmup_hover_links": True,
    "warmup_hover_link_count": 3,
}

DEFAULT_HUMAN_WARMUP = DEFAULT_WEBSITE_VISITS  # alias kept for clarity


# --------------------------------------------------------------------------- #
#  PATH HELPERS
# --------------------------------------------------------------------------- #

def get_config_path() -> Path:
    """Return the external config path for the current execution mode.

    PyInstaller's ``__file__`` points inside ``_internal`` for an onedir
    build, while user-editable configuration belongs beside the executable.
    Source runs keep using the project directory beside this module.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.json"
    return Path(__file__).resolve().parent.parent / "data" / "config.json"


def load_config(path=None) -> dict:
    """Load a JSON configuration object, returning an empty object on error."""
    config_path = Path(path) if path is not None else get_config_path()
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, TypeError, json.JSONDecodeError):
        return {}

    return config if isinstance(config, dict) else {}


# --------------------------------------------------------------------------- #
#  VALIDATION HELPERS
# --------------------------------------------------------------------------- #

def _non_negative_number(value, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return max(0.0, number)


def _bool(value, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    return fallback


def _str_list(value, fallback):
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return list(fallback)


def _ordered_pair(values, minimum_key, maximum_key):
    """Ensure a min/max pair is ordered (min <= max)."""
    if values[minimum_key] > values[maximum_key]:
        values[minimum_key], values[maximum_key] = (
            values[maximum_key],
            values[minimum_key],
        )


def _normalize_section(raw_section, defaults, *, numeric_keys=(), bool_keys=(),
                       list_keys=(), pair_keys=()):
    """Build a validated section dict from defaults + a raw JSON section."""
    values = dict(defaults)
    if isinstance(raw_section, dict):
        for key in numeric_keys:
            if key in raw_section:
                values[key] = _non_negative_number(raw_section[key], values[key])
        for key in bool_keys:
            if key in raw_section:
                values[key] = _bool(raw_section[key], values[key])
        for key in list_keys:
            if key in raw_section:
                values[key] = _str_list(raw_section[key], values[key])
        # Copy any string keys that are neither numeric/bool/list as-is.
        for key, default in defaults.items():
            if key in raw_section and key not in numeric_keys and key not in bool_keys and key not in list_keys:
                candidate = raw_section[key]
                if isinstance(candidate, type(default)) or default is None:
                    values[key] = candidate
    for minimum_key, maximum_key in pair_keys:
        _ordered_pair(values, minimum_key, maximum_key)
    return values


# --------------------------------------------------------------------------- #
#  SECTION LOADERS (each returns a validated dict)
# --------------------------------------------------------------------------- #

_CHAT_TIMING_NUMERIC = tuple(DEFAULT_CHAT_TIMING.keys())
_CHAT_TIMING_PAIRS = (
    ("new_chat_delay_min_seconds", "new_chat_delay_max_seconds"),
    ("rest_interval_min_minutes", "rest_interval_max_minutes"),
    ("rest_duration_min_minutes", "rest_duration_max_minutes"),
)


def normalize_chat_timing(chat_timing=None) -> dict:
    """Return validated timing values with ordered min/max pairs."""
    return _normalize_section(
        chat_timing,
        DEFAULT_CHAT_TIMING,
        numeric_keys=_CHAT_TIMING_NUMERIC,
        pair_keys=_CHAT_TIMING_PAIRS,
    )


def load_chat_timing(path=None) -> dict:
    """Load chat timing from the ``chat_timing`` section of config.json.

    Flat keys are accepted as a compatibility convenience, while the JSON
    template uses the clearer nested section.
    """
    config = load_config(path)
    section = config.get("chat_timing")
    section = section if isinstance(section, dict) else {}

    raw_timing = {
        key: section.get(key, config.get(key, default))
        for key, default in DEFAULT_CHAT_TIMING.items()
    }
    return normalize_chat_timing(raw_timing)


_HUMAN_BEHAVIOR_NUMERIC = (
    "reaction_pause_min_seconds", "reaction_pause_max_seconds",
    "typing_speed_min_cps", "typing_speed_max_cps",
    "read_reply_min_seconds", "read_reply_max_seconds",
    "micro_idle_chance", "micro_idle_min_seconds", "micro_idle_max_seconds",
    "typing_ms_per_char", "typing_max_seconds",
    "inactivity_timeout_seconds", "inactivity_nudges_max",
    "inactivity_grace_seconds",
)
_HUMAN_BEHAVIOR_BOOL = ("enabled",)
_HUMAN_BEHAVIOR_PAIRS = (
    ("reaction_pause_min_seconds", "reaction_pause_max_seconds"),
    ("typing_speed_min_cps", "typing_speed_max_cps"),
    ("read_reply_min_seconds", "read_reply_max_seconds"),
    ("micro_idle_min_seconds", "micro_idle_max_seconds"),
)


def normalize_human_behavior(section=None) -> dict:
    return _normalize_section(
        section,
        DEFAULT_HUMAN_BEHAVIOR,
        numeric_keys=_HUMAN_BEHAVIOR_NUMERIC,
        bool_keys=_HUMAN_BEHAVIOR_BOOL,
        pair_keys=_HUMAN_BEHAVIOR_PAIRS,
    )


def load_human_behavior(path=None) -> dict:
    config = load_config(path)
    return normalize_human_behavior(config.get("human_behavior"))


_BG_TABS_NUMERIC = (
    "periodic_visit_interval_min_minutes", "periodic_visit_interval_max_minutes",
    "periodic_visit_dwell_min_seconds", "periodic_visit_dwell_max_seconds",
    "tab_open_delay_min_seconds", "tab_open_delay_max_seconds",
)
_BG_TABS_BOOL = ("enabled", "periodic_visit_enabled", "periodic_visit_scroll")
_BG_TABS_LIST = ("urls",)
_BG_TABS_PAIRS = (
    ("periodic_visit_interval_min_minutes", "periodic_visit_interval_max_minutes"),
    ("periodic_visit_dwell_min_seconds", "periodic_visit_dwell_max_seconds"),
    ("tab_open_delay_min_seconds", "tab_open_delay_max_seconds"),
)


def normalize_background_tabs(section=None) -> dict:
    return _normalize_section(
        section,
        DEFAULT_BACKGROUND_TABS,
        numeric_keys=_BG_TABS_NUMERIC,
        bool_keys=_BG_TABS_BOOL,
        list_keys=_BG_TABS_LIST,
        pair_keys=_BG_TABS_PAIRS,
    )


def load_background_tabs(path=None) -> dict:
    config = load_config(path)
    return normalize_background_tabs(config.get("background_tabs"))


_PERF_NUMERIC = (
    "poll_interval_seconds", "connect_poll_interval_seconds",
    "min_poll_interval_seconds", "evaluate_throttle_seconds",
    "debug_slowdown_seconds",
)
_PERF_BOOL = ()
_PERF_LIST = ("block_resource_types",)


def normalize_performance(section=None) -> dict:
    return _normalize_section(
        section,
        DEFAULT_PERFORMANCE,
        numeric_keys=_PERF_NUMERIC,
        bool_keys=_PERF_BOOL,
        list_keys=_PERF_LIST,
    )


def load_performance(path=None) -> dict:
    config = load_config(path)
    return normalize_performance(config.get("performance"))


_BAN_BOOL = ("enabled", "login_redirect_is_ban", "auto_delete_banned_session", "auto_close_on_ban")
_BAN_LIST = ("ban_indicators",)


def normalize_ban_detection(section=None) -> dict:
    return _normalize_section(
        section,
        DEFAULT_BAN_DETECTION,
        bool_keys=_BAN_BOOL,
        list_keys=_BAN_LIST,
    )


def load_ban_detection(path=None) -> dict:
    config = load_config(path)
    return normalize_ban_detection(config.get("ban_detection"))


_SESSION_BOOL = ("log_alive_count_on_start", "log_alive_count_after_each_chat")


def normalize_session_management(section=None) -> dict:
    return _normalize_section(
        section,
        DEFAULT_SESSION_MANAGEMENT,
        bool_keys=_SESSION_BOOL,
    )


def load_session_management(path=None) -> dict:
    config = load_config(path)
    return normalize_session_management(config.get("session_management"))


_REPLIES_NUMERIC = (
    "warmup_chat_count_min", "warmup_chat_count_max",
    "warmup_messages_per_chat_min", "warmup_messages_per_chat_max",
)
_REPLIES_LIST = ("warmup_openers",)
_REPLIES_PAIRS = (
    ("warmup_chat_count_min", "warmup_chat_count_max"),
    ("warmup_messages_per_chat_min", "warmup_messages_per_chat_max"),
)


def normalize_replies(section=None) -> dict:
    return _normalize_section(
        section,
        DEFAULT_REPLIES,
        numeric_keys=_REPLIES_NUMERIC,
        list_keys=_REPLIES_LIST,
        pair_keys=_REPLIES_PAIRS,
    )


def load_replies(path=None) -> dict:
    config = load_config(path)
    return normalize_replies(config.get("replies"))


_VISIT_BOOL = (
    "pre_homepage_enabled", "human_warmup_enabled", "warmup_browse_homepage",
    "warmup_scroll", "warmup_hover_links",
)
_VISIT_NUMERIC = ("pre_homepage_tab_count", "warmup_hover_link_count")


def normalize_website_visits(section=None) -> dict:
    return _normalize_section(
        section,
        DEFAULT_WEBSITE_VISITS,
        numeric_keys=_VISIT_NUMERIC,
        bool_keys=_VISIT_BOOL,
    )


def load_website_visits(path=None) -> dict:
    config = load_config(path)
    return normalize_website_visits(config.get("website_visits"))



# --------------------------------------------------------------------------- #
#  Runtime mode configuration
# --------------------------------------------------------------------------- #

_RUNTIME_DEFAULTS = {
    "mode": "auto",
    "single_browser_max": 1,
    "multi_browser_min": 2,
    "go_engine_enabled": True,
    "go_engine_min_browsers": 2,
    "js_worker_enabled": False,
    "context_pool_enabled": True,
    "context_pool_min_browsers": 2,
    "governor_enabled": True,
    "governor_kill_processes": False,
}


def load_runtime_config(path=None) -> dict:
    """Load runtime orchestration settings with conservative safe defaults."""
    config = load_config(path)
    raw = config.get("runtime", {})
    if not isinstance(raw, dict):
        raw = {}
    result = dict(_RUNTIME_DEFAULTS)
    result.update(raw)
    return result

# --------------------------------------------------------------------------- #
#  Context-pool configuration (2026 upgrade)
# --------------------------------------------------------------------------- #

_CONTEXT_POOL_DEFAULTS = {
    "max_contexts": 50,
    "headless": True,
    "geoip": True,
    "humanize": True,
    "viewport_jitter": True,
    "block_resource_types": ["image", "font", "media"],
    "stagger_interval_seconds": 1.5,
}


def load_context_pool_config(path=None) -> dict:
    """Load the context_pool section from config.json with safe defaults."""
    config = load_config(path)
    raw = config.get("context_pool", {})
    if not isinstance(raw, dict):
        raw = {}
    result = dict(_CONTEXT_POOL_DEFAULTS)
    result.update(raw)
    return result


# --------------------------------------------------------------------------- #
#  Engine configuration (Go engine tuning)
# --------------------------------------------------------------------------- #

_ENGINE_CONFIG_DEFAULTS = {
    "enabled": True,
    "max_workers": 100,
    "min_workers": 1,
    # NOTE: thresholds are 0-1 fractions, NOT 0-100 percentages, because the
    # Go engine reads them as fractions (config.go default = 0.80).  Using
    # 85.0 here would make the scaler/ctx-pause never trigger because the
    # monitor reports CPU/mem as fractions in [0, 1].
    "cpu_threshold": 0.85,
    "mem_threshold": 0.85,
    "rate_limit_per_minute": 0,
    "heartbeat_timeout_seconds": 120,
}


def load_engine_config(path=None) -> dict:
    """Load the engine_config section from config.json with safe defaults."""
    config = load_config(path)
    raw = config.get("engine_config", {})
    if not isinstance(raw, dict):
        raw = {}
    result = dict(_ENGINE_CONFIG_DEFAULTS)
    result.update(raw)
    return result


# --------------------------------------------------------------------------- #
#  Resource Governor configuration (adaptive RAM/CPU watchdog)
# --------------------------------------------------------------------------- #

_RESOURCE_GOVERNOR_DEFAULTS = {
    "enabled": True,
    "cpu_warn": 0.70,
    "mem_warn": 0.72,
    "mem_recycle": 0.82,
    "mem_redline": 0.92,
    "min_free_mb": 512,
    "max_recycle_per_tick": 3,
    "sample_interval": 2.0,
    "kill_cooldown": 10.0,
    "gc_interval": 60.0,
    "restore_launch_max": 4,
    "_comment": "Resource Governor: psutil-based tiered watchdog. Tier1 THROTTLE (yellow) freezes new launches when mem>=mem_warn. Tier2 RECYCLE (orange) closes oldest contexts when mem>=mem_recycle. Tier3 REDLINE (red) kills worst browser process when mem>=mem_redline. Lower thresholds for weaker PCs."
}


def load_resource_governor_config(path=None) -> dict:
    """Load the resource_governor section from config.json with safe defaults."""
    config = load_config(path)
    raw = config.get("resource_governor", {})
    if not isinstance(raw, dict):
        raw = {}
    result = dict(_RESOURCE_GOVERNOR_DEFAULTS)
    result.update(raw)
    return result


def save_config_sections(updates: dict, path=None) -> bool:
    """Merge section dicts into config.json and write it back.

    ``updates`` maps a top-level section name to a dict of key/value pairs,
    e.g. ``{"chat_timing": {"new_chat_delay_min_seconds": 5.0}}``.  Every
    other section and key in the existing file is preserved.  Returns True on
    success, False on any IO/JSON error (never raises).
    """
    try:
        config = load_config(path)
        config_path = Path(path) if path is not None else get_config_path()
        for section, values in updates.items():
            if not isinstance(values, dict):
                continue
            current = config.get(section)
            if not isinstance(current, dict):
                current = {}
                config[section] = current
            for key, value in values.items():
                try:
                    current[key] = type(current.get(key, value))(float(value))
                except (TypeError, ValueError):
                    current[key] = value
        with config_path.open("w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  ALL-IN-ONE LOADER
# --------------------------------------------------------------------------- #

def load_all_config(path=None) -> dict:
    """Load and validate every config section at once.

    Returns a dict with keys: runtime, chat_timing, human_behavior,
    background_tabs, performance, ban_detection, session_management, replies,
    website_visits, context_pool, engine_config, resource_governor.
    """
    return {
        "runtime": load_runtime_config(path),
        "chat_timing": load_chat_timing(path),
        "human_behavior": load_human_behavior(path),
        "background_tabs": load_background_tabs(path),
        "performance": load_performance(path),
        "ban_detection": load_ban_detection(path),
        "session_management": load_session_management(path),
        "replies": load_replies(path),
        "website_visits": load_website_visits(path),
        "context_pool": load_context_pool_config(path),
        "engine_config": load_engine_config(path),
        "resource_governor": load_resource_governor_config(path),
    }
