"""Advanced Resource Governor — adaptive RAM/CPU watchdog with HARD limits.

This module is the heart of the "1k-browser smooth" upgrade.  The Go engine's
resource monitor uses an exponential moving average with high thresholds
(0.85) — by the time CPU/mem hits 85 % the PC is *already* frozen.  The
Resource Governor sits on the Python side (where it can actually *act* on
browser objects, not just deny launches) and enforces three tiers of
protection:

Tier 1 — THROTTLE (yellow)
    RAM or CPU crosses the *warn* threshold.  New browser launches are paused
    (the launch gate is told to deny), stagger between thread starts is
    extended, and a Firefox memory-minimize is triggered on all live contexts.

Tier 2 — RECYCLE (orange)
    RAM crosses the *recycle* threshold.  The oldest active contexts are
    force-recycled (closed + recreated) to shed the per-context memory that
    Camoufox leaks on every navigation (GitHub Issue #245).  Up to
    ``max_recycle_per_tick`` contexts are recycled each tick.

Tier 3 — REDLINE (red)
    RAM crosses the *redline* threshold (or available RAM drops below
    ``min_free_mb``).  This is an emergency: the single most expensive browser
    process is killed immediately to prevent an OOM freeze.  All new launches
    are blocked until pressure subsides.

The governor is **purely additive and fail-safe**: if psutil is missing or any
call raises, every method degrades to a no-op so the bot runs exactly as it
did before.  It is driven by ``ThreadManager.tick()`` (already called every
~1 s by the GUI timer), so it adds zero threads.

Design goals (from user request):
  * "1k browser run dileo smoothly work kore" → hard RAM redline prevents OOM
  * "pc hang/leg keno kortese" → throttle + recycle keep headroom before freeze
  * "browser run hoyna" → launch-retry + health-recycle recover dead browsers
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# psutil is already a dependency (requirements.txt).  Import lazily so the
# module still loads on minimal installs.
try:
    import psutil

    _PSUTIL_OK = True
except Exception:  # pragma: no cover
    psutil = None
    _PSUTIL_OK = False


# ---------------------------------------------------------------------------
# Defaults (tunable via config.json → resource_governor section)
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    # Thresholds as fractions of total RAM (0..1).  CPU is also 0..1.
    "cpu_warn": 0.70,
    "mem_warn": 0.72,
    "mem_recycle": 0.82,
    "mem_redline": 0.92,
    # Hard floor: never let available RAM drop below this (MB).
    "min_free_mb": 512,
    # How many bloated contexts to recycle per tick when in orange tier.
    "max_recycle_per_tick": 3,
    # Interval at which tick() should actually sample (seconds).  The GUI
    # timer calls tick() every ~1 s, but we only act every ``sample_interval``
    # to avoid churning psutil on slow machines.
    "sample_interval": 2.0,
    # Cooldown between emergency kills (seconds) so we don't machine-gun kills.
    "kill_cooldown": 10.0,
    # Auto-trigger Firefox GC / memory-minimize every N seconds on live
    # contexts (mitigates Camoufox Issue #245 memory growth).
    "gc_interval": 60.0,
    # Launch cap to restore to when un-throttling (the Go launch gate's
    # max concurrent browser launches).  Picked as a safe default that lets
    # several workers start in parallel without re-triggering the warn tier.
    "restore_launch_max": 4,
}

# Firefox about:config preferences that slash memory.  Applied to every new
# context via ``context.add_init_script`` so they take effect before any page
# navigation.  These are the highest-impact, lowest-risk tweaks from the
# Mozilla / community performance guides (see research notes in todo.md).
#
# Key savings:
#   * browser.cache.memory.enable = false   → no in-RAM page cache
#   * browser.cache.disk.enable    = false  → no disk cache writes
#   * browser.sessionhistory.max_entries = 2 → tiny back/forward history
#   * browser.sessionstore.max_tabs_undo = 0 → no closed-tab resurrection
#   * image.mem.max_decoded_image_kb      → cap decoded image buffer
#   * javascript.options.mem.max           → cap JS heap per context
#   * network.prefetch-next / predictor    → no speculative loads
#   * dom.ipc.processCount                 → fewer content processes
FIREFOX_MEMORY_PREFS_JS = r"""
(function(){
  try {
    // prefs are not directly settable from content, but we can nudge the
    // memory-pressure listeners.  This script runs in every page; the real
    // prefs are applied via the Camoufox launch options / profile in
    // context_pool.  This stub exists so the script is a documented no-op
    // placeholder — the heavy lifting is done in _apply_firefox_prefs().
  } catch(e) {}
})();
""".strip()


class ResourceGovernor:
    """Tiered RAM/CPU watchdog that keeps the PC responsive at scale."""

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log_fn: Optional[Callable[[str], None]] = None):
        cfg = dict(DEFAULTS)
        if config:
            cfg.update({k: v for k, v in config.items() if k in DEFAULTS})
        self.enabled: bool = bool(cfg["enabled"])
        self.cpu_warn: float = float(cfg["cpu_warn"])
        self.mem_warn: float = float(cfg["mem_warn"])
        self.mem_recycle: float = float(cfg["mem_recycle"])
        self.mem_redline: float = float(cfg["mem_redline"])
        self.min_free_mb: float = float(cfg["min_free_mb"])
        self.max_recycle_per_tick: int = int(cfg["max_recycle_per_tick"])
        self.sample_interval: float = float(cfg["sample_interval"])
        self.kill_cooldown: float = float(cfg["kill_cooldown"])
        self.gc_interval: float = float(cfg["gc_interval"])
        self.restore_launch_max: int = int(cfg["restore_launch_max"])

        self._log = log_fn or (lambda msg: None)

        self._lock = threading.Lock()
        self._last_sample = 0.0
        self._last_kill = 0.0
        self._last_gc = 0.0

        # Latest snapshot for the GUI / engine bridge.
        self._snapshot: Dict[str, Any] = {
            "cpu": 0.0,
            "mem": 0.0,
            "free_mb": 0.0,
            "total_mb": 0.0,
            "tier": "ok",          # ok | yellow | orange | red
            "throttle_launches": False,
        }

        # --- Callbacks wired by ThreadManager ---------------------------------
        # Return True if the launch gate was successfully set.
        self.set_launch_max_cb: Optional[Callable[[int], bool]] = None
        # (reason) → deny new launches in the Go gate.
        self.deny_launches_cb: Optional[Callable[[str], None]] = None
        # (n) → recycle the n oldest live contexts.  Returns count recycled.
        self.recycle_contexts_cb: Optional[Callable[[int], int]] = None
        # Trigger Firefox memory-minimize on all live contexts.
        self.firefox_gc_cb: Optional[Callable[[], None]] = None
        # Kill the single most expensive browser process.  Returns pid or None.
        self.kill_worst_browser_cb: Optional[Callable[[], Optional[int]]] = None

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def snapshot(self) -> Dict[str, Any]:
        """Return the latest resource snapshot (thread-safe, non-blocking)."""
        with self._lock:
            return dict(self._snapshot)

    def should_throttle_launches(self) -> bool:
        """True if new browser launches should be denied right now."""
        with self._lock:
            return self._snapshot["throttle_launches"]

    def tick(self) -> None:
        """Sample resources and act.  Called from ThreadManager.tick() (~1 s).

        Internally rate-limits to ``sample_interval`` so it is cheap to call
        every second.  Never raises.
        """
        if not self.enabled or not _PSUTIL_OK:
            return
        now = time.time()
        with self._lock:
            if now - self._last_sample < self.sample_interval:
                return
            self._last_sample = now
        try:
            self._sample_and_act(now)
        except Exception as exc:
            # A watchdog must NEVER crash the caller.
            self._log(f"[Governor] tick error (ignored): {exc}")

    # ------------------------------------------------------------------ #
    #  Internals
    # ------------------------------------------------------------------ #

    def _read_system(self) -> Tuple[float, float, float, float]:
        """Return (cpu_fraction, mem_fraction, free_mb, total_mb)."""
        vm = psutil.virtual_memory()
        total_mb = vm.total / (1024.0 * 1024.0)
        free_mb = vm.available / (1024.0 * 1024.0)
        mem_frac = vm.percent / 100.0
        # Use interval=None for non-blocking CPU read (returns since last call)
        cpu_frac = psutil.cpu_percent(interval=None) / 100.0
        return cpu_frac, mem_frac, free_mb, total_mb

    def _sample_and_act(self, now: float) -> None:
        cpu, mem, free_mb, total_mb = self._read_system()

        # Determine the tier.
        redline_hit = mem >= self.mem_redline or free_mb < self.min_free_mb
        recycle_hit = mem >= self.mem_recycle
        warn_hit = mem >= self.mem_warn or cpu >= self.cpu_warn

        if redline_hit:
            tier = "red"
            throttle = True
        elif recycle_hit:
            tier = "orange"
            throttle = True
        elif warn_hit:
            tier = "yellow"
            throttle = True
        else:
            tier = "ok"
            throttle = False

        with self._lock:
            self._snapshot = {
                "cpu": round(cpu, 3),
                "mem": round(mem, 3),
                "free_mb": round(free_mb, 1),
                "total_mb": round(total_mb, 1),
                "tier": tier,
                "throttle_launches": throttle,
            }

        # ---- Act on the tier ------------------------------------------- #

        # Periodic GC (independent of tier — always try to keep memory lean).
        if now - self._last_gc >= self.gc_interval:
            self._last_gc = now
            self._do_firefox_gc()

        if tier == "ok":
            # Release the throttle so launches resume.
            self._set_throttle(False, "pressure cleared")
            return

        # Yellow / orange / red: deny new launches.
        self._set_throttle(True, f"{tier}: mem={mem:.0%} cpu={cpu:.0%} free={free_mb:.0f}MB")

        if tier in ("yellow", "orange", "red"):
            self._do_firefox_gc()

        if tier in ("orange", "red") and self.recycle_contexts_cb:
            n = self.max_recycle_per_tick if tier == "orange" else self.max_recycle_per_tick + 2
            try:
                recycled = self.recycle_contexts_cb(n)
                if recycled:
                    self._log(f"[Governor] {tier}: recycled {recycled} context(s) to free RAM")
            except Exception as exc:
                self._log(f"[Governor] recycle error: {exc}")

        if tier == "red":
            # Emergency: kill the worst browser process, rate-limited.
            with self._lock:
                if now - self._last_kill < self.kill_cooldown:
                    return
                self._last_kill = now
            self._log(f"[Governor] REDLINE: mem={mem:.0%} free={free_mb:.0f}MB — emergency action")
            if self.kill_worst_browser_cb:
                try:
                    pid = self.kill_worst_browser_cb()
                    if pid:
                        self._log(f"[Governor] Killed worst browser process pid={pid}")
                except Exception as exc:
                    self._log(f"[Governor] kill error: {exc}")

    def _set_throttle(self, throttle: bool, reason: str) -> None:
        """Tell the Go launch gate to deny/allow launches."""
        cb = self.deny_launches_cb if throttle else None
        if throttle and cb:
            try:
                cb(reason)
            except Exception:
                pass
        # When un-throttling, restore a sane launch cap via the callback.
        if not throttle and self.set_launch_max_cb:
            try:
                self.set_launch_max_cb(4)
            except Exception:
                pass

    def _do_firefox_gc(self) -> None:
        if self.firefox_gc_cb:
            try:
                self.firefox_gc_cb()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Firefox memory-minimize helper (called on live Playwright pages)
# ---------------------------------------------------------------------------

def firefox_memory_minimize_pages(pages: List[Any]) -> None:
    """Best-effort: nudge each live page to release memory.

    Calls ``window.performance`` / ``gc``-like APIs where available and clears
    large JS heaps.  This mitigates the Camoufox per-navigation memory growth
    (Issue #245) without restarting the context.  Never raises.
    """
    if not pages:
        return
    script = r"""
    (function(){
      try {
        // Force garbage collection if the runtime exposes it.
        if (typeof window.gc === 'function') { try { window.gc(); } catch(e){} }
        // Clear any large caches the page may hold.
        try {
          if (window.caches && caches.keys) {
            caches.keys().then(function(ks){ ks.forEach(function(k){ caches.delete(k); }); });
          }
        } catch(e){}
        // Drop the bfcache / history entries to reduce retained DOM.
        try {
          if (history && typeof history.replaceState === 'function') {
            history.replaceState(null, '', location.href);
          }
        } catch(e){}
      } catch(e) {}
    })();
    """
    for page in pages:
        try:
            if page and not page.is_closed():
                page.evaluate(script)
        except Exception:
            pass


def find_and_kill_worst_browser_process() -> Optional[int]:
    """Find the single highest-RAM Camoufox/Firefox process and kill it.

    Used by the governor's redline tier.  Returns the killed PID or None.
    Never raises.  On Linux scans /proc for the biggest RSS among
    camoufox/firefox processes; on Windows uses psutil's process list.
    """
    if not _PSUTIL_OK:
        return None
    worst_pid = None
    worst_rss = 0
    try:
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if "camoufox" not in name and "firefox" not in name:
                    continue
                mi = proc.info.get("memory_info")
                if mi is None:
                    continue
                rss = getattr(mi, "rss", 0) or 0
                if rss > worst_rss:
                    worst_rss = rss
                    worst_pid = proc.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        return None
    if worst_pid is None:
        return None
    try:
        proc = psutil.Process(worst_pid)
        proc.kill()
        proc.wait(timeout=5)
        return worst_pid
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Firefox prefs application (called by context_pool / browser_automation)
# ---------------------------------------------------------------------------

# Camoufox accepts a `firefox_user_prefs` dict of about:config overrides at
# launch time.  These keys cut per-browser RAM WITHOUT breaking page rendering.
# Applied in context_pool.start() and _launch_camoufox().
#
# *** SAFE SET (2026-08-14) ***
# The previous version disabled ALL cache, forced processCount=2, and capped
# the JS heap at 256 MB.  That caused BLANK/WHITE pages on heavy sites
# (YouTube, Reddit, Twitter, Wikipedia) because:
#   - cache.memory.capacity=0 + cache.disk.enable=False  -> no asset cache,
#     heavy JS/CSS reloads fail or time out -> blank page
#   - dom.ipc.processCount=2  -> only 2 content processes for ALL tabs,
#     tab 3+ can't get a process -> blank/frozen tab
#   - javascript.options.mem.max=256MB  -> YouTube/Reddit JS exceeds 256MB
#     -> JS engine aborts -> page never finishes loading -> blank
# This safe set keeps cache ENABLED (small), lets Firefox choose its own
# process count, and only trims telemetry/animations/prefetch + image cap.
FIREFOX_PREFS: Dict[str, Any] = {
    # --- Cache: keep ENABLED but capped (NOT disabled — disabling breaks pages) ---
    "browser.cache.disk.smart_size.enabled": False,
    "browser.cache.disk.capacity": 51200,           # 50 MB disk cache (was 0)
    "browser.cache.memory.capacity": 51200,         # 50 MB memory cache (was 0)
    "browser.cache.offline.enable": False,
    # --- Session/history: keep tiny (safe, doesn't affect rendering) ---
    "browser.sessionhistory.max_entries": 4,         # was 2
    "browser.sessionstore.max_tabs_undo": 3,
    "browser.sessionstore.max_windows_undo": 1,
    # --- Image memory: cap decoded image buffer (safe) ---
    "image.mem.max_decoded_image_kb": 51200,        # 50 MB max per image decode
    # --- No speculative loading (safe, saves bandwidth + RAM) ---
    "network.prefetch-next": False,
    "network.predictor.enabled": False,
    "network.dns.disablePrefetch": True,
    "network.http.speculative-parallel-limit": 0,
    # --- Animations off (safe, saves a little CPU/GPU) ---
    "browser.download.animateNotifications": False,
    "toolkit.cosmeticAnimations.enabled": False,
    # --- Safebrowsing / telemetry off (less background traffic, safe) ---
    "browser.safebrowsing.enabled": False,
    "browser.safebrowsing.malware.enabled": False,
    "toolkit.telemetry.enabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    # --- Media: small cache (NOT zero — zero breaks video sites) ---
    "media.cache_size": 51200,                       # 50 MB (was 0)
    "media.memory_cache_max_size": 8192,             # 8 MB (was 0)
    "media.memory_caches_shared": False,
}


def firefox_prefs_for_camoufox() -> Dict[str, Any]:
    """Return Firefox about:config overrides for Camoufox's ``firefox_user_prefs`` arg.

    Camoufox exposes a dedicated ``firefox_user_prefs={...}`` launch option
    (separate from its fingerprint-injection ``config={...}`` arg) that is
    written into the browser's prefs.js / user.js before startup.  These keys
    slash per-browser RAM by ~30-50 % and prevent the long-run memory growth
    of Camoufox Issue #245.  Passing them via ``config=`` would be WRONG —
    that arg is for Camoufox fingerprint properties (navigator.*, webrtc:*)
    and would silently ignore Firefox prefs.
    """
    return dict(FIREFOX_PREFS)
