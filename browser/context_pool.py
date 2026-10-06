"""Context Pool — single browser, many isolated contexts.

This is the **core** of the 2026 upgrade.  Instead of launching one Camoufox
browser per thread (which is RAM-prohibitive at scale), we launch **one**
Camoufox browser and create N isolated ``BrowserContext`` objects inside it.
Each context has its own cookies, storage, and fingerprint — identical isolation
to a separate browser, but at ~1/100th the memory cost.

Architecture
------------
::

    ┌──────────────────────────────────────────┐
    │           ONE Camoufox Browser            │
    │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐      │
    │  │Ctx #1│ │Ctx #2│ │Ctx #3│ │Ctx #N│      │
    │  │acc 1 │ │acc 2 │ │acc 3 │ │acc N │      │
    │  │page  │ │page  │ │page  │ │page  │      │
    │  └──────┘ └──────┐ └──────┘ └──────┘      │
    └──────────────────────────────────────────┘

Each context is created from an ``AccountEntry`` (session folder / token /
cookie) and gets its own storage_state and proxy.  When a chat finishes the
context is closed and its slot is recycled for the next account.

Key guarantees
--------------
* **Smart cleanup** — every context is tracked; ``close_all()`` guarantees no
  leak even if a worker crashes.
* **Bounded pool** — ``max_contexts`` caps concurrent contexts (default 50).
  Workers block on a semaphore until a slot frees up.
* **Thread-safe** — all operations guarded by a lock.
* **Backward compatible** — if the pool fails to start, callers can fall back
  to the old per-thread browser model.
"""

from __future__ import annotations

import threading
import time
import warnings
from typing import Any, Dict, List, Optional, Tuple

# Camoufox emits greenlet/deprecation noise that floods logs.
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*greenlet.*")
warnings.filterwarnings("ignore", message=".*cannot switch to a different thread.*")


# --------------------------------------------------------------------------- #
# Context slot
# --------------------------------------------------------------------------- #

class ContextSlot:
    """Tracks one live BrowserContext and its associated account."""

    __slots__ = (
        "slot_id", "account_key", "context", "page", "created_at",
        "closed", "proxy",
    )

    def __init__(self, slot_id: int, account_key: str, context, page, proxy=None):
        self.slot_id = slot_id
        self.account_key = account_key
        self.context = context
        self.page = page
        self.created_at = time.time()
        self.closed = False
        self.proxy = proxy

    @property
    def age(self) -> float:
        return time.time() - self.created_at


# --------------------------------------------------------------------------- #
# Pool
# --------------------------------------------------------------------------- #

class ContextPool:
    """Manages a single shared Camoufox browser and a pool of contexts."""

    def __init__(
        self,
        max_contexts: int = 50,
        headless: bool = True,
        geoip: bool = True,
        humanize: bool = True,
        block_resource_types: Optional[List[str]] = None,
        viewport_jitter: bool = True,
        verbose: bool = False,
    ):
        self.max_contexts = max_contexts
        self.headless = headless
        self.geoip = geoip
        self.humanize = humanize
        self.block_resource_types = block_resource_types or []
        self.viewport_jitter = viewport_jitter
        self.verbose = verbose

        self._lock = threading.RLock()
        self._semaphore = threading.Semaphore(max_contexts)
        self._browser = None
        self._camoufox_manager = None
        self._slots: Dict[int, ContextSlot] = {}
        self._next_slot_id = 1
        self._started = False
        self._shutting_down = False

    # ---- lifecycle ----

    def start(self, fingerprint=None, proxy=None) -> bool:
        """Launch the shared browser (Chromium or Camoufox).  Returns True on success.

        The engine is chosen via ``config.json`` → ``browser_engine``:
        ``"chromium"`` (default) or ``"camoufox"`` (legacy).  Either way a
        standard Playwright ``Browser`` is stored, so context creation and
        all higher-level code work unchanged.
        """
        with self._lock:
            if self._started:
                return True
            if self._shutting_down:
                return False
        # Resolve which engine to use.
        try:
            from core.config_loader import load_performance
            engine = str(load_performance().get("browser_engine", "chromium")).lower().strip()
        except Exception:
            engine = "chromium"
        if engine not in ("chromium", "camoufox"):
            engine = "chromium"

        if engine == "camoufox":
            try:
                from camoufox.sync_api import Camoufox
            except Exception as e:
                if self.verbose:
                    print(f"[ContextPool] Camoufox import failed: {e}")
                return False
        # Chromium needs no special import here (browser_engine handles it).

        launch_options: Dict[str, Any] = {
            "headless": self.headless,
            "geoip": self.geoip,
            "humanize": self.humanize,
        }
        if fingerprint is not None:
            launch_options["fingerprint"] = fingerprint
        if proxy is not None:
            launch_options["proxy"] = proxy

        # Camoufox-only Firefox memory prefs (ignored for Chromium).
        firefox_prefs = None
        try:
            from core.resource_governor import firefox_prefs_for_camoufox
            firefox_prefs = firefox_prefs_for_camoufox()
        except Exception:
            pass

        browser = None
        manager = None
        try:
            from browser.browser_engine import launch_browser
            browser, manager = launch_browser(
                engine=engine,
                headless=self.headless,
                proxy=proxy,
                log_fn=print if self.verbose else None,
                fingerprint=fingerprint,
                firefox_prefs=firefox_prefs,
            )
        except Exception as e:
            if self.verbose:
                print(f"[ContextPool] Browser launch failed: {e}")
            return False

        with self._lock:
            self._browser = browser
            self._camoufox_manager = manager  # opaque cleanup handle
            self._started = True
        if self.verbose:
            print(f"[ContextPool] Shared {engine} browser started (max_contexts={self.max_contexts})")
        return True

    def is_started(self) -> bool:
        with self._lock:
            return self._started

    # ---- context creation / recycling ----

    def _random_viewport(self) -> Optional[Dict[str, int]]:
        """Randomize viewport ±10-15px from a base size (human-like)."""
        import random
        base_w = random.choice([1280, 1366, 1440, 1536, 1600, 1920])
        base_h = random.choice([720, 768, 800, 900, 1024, 1080])
        w = base_w + random.randint(-15, 15)
        h = base_h + random.randint(-10, 10)
        return {"width": max(320, w), "height": max(240, h)}

    def create_context(
        self,
        account_key: str,
        storage_state_path: Optional[str] = None,
        fingerprint=None,
        proxy: Optional[Dict[str, Any]] = None,
        extra_context_options: Optional[Dict[str, Any]] = None,
        stealth_fingerprint: Optional[Dict[str, Any]] = None,
    ) -> Optional[ContextSlot]:
        """Create a new isolated context for *account_key*.

        Blocks until a pool slot is available (semaphore-bounded).
        Returns a ``ContextSlot`` or ``None`` on failure.

        ``extra_context_options`` carries the account's permanent device
        profile (UA, viewport, screen, locale, timezone — see
        ``browser.browser_identity``) so two accounts never look like the same
        browser; ``stealth_fingerprint`` adds the matching init-script layer.
        """
        if not self.is_started():
            return None
        # Block until a slot is free (bounded concurrency).
        self._semaphore.acquire()
        try:
            return self._do_create(
                account_key, storage_state_path, fingerprint, proxy,
                extra_context_options, stealth_fingerprint,
            )
        except Exception:
            self._semaphore.release()
            return None

    def _do_create(
        self,
        account_key: str,
        storage_state_path: Optional[str],
        fingerprint,
        proxy,
        extra_context_options,
        stealth_fingerprint=None,
    ) -> Optional[ContextSlot]:
        import os
        with self._lock:
            browser = self._browser
            if browser is None or self._shutting_down:
                return None
            slot_id = self._next_slot_id
            self._next_slot_id += 1

        context_options: Dict[str, Any] = {}
        if storage_state_path and os.path.isfile(storage_state_path):
            context_options["storage_state"] = storage_state_path
        if self.viewport_jitter:
            vp = self._random_viewport()
            if vp:
                context_options["viewport"] = vp
        if proxy:
            context_options["proxy"] = proxy
        if extra_context_options:
            context_options.update(extra_context_options)

        try:
            context = browser.new_context(**context_options)
        except Exception as e:
            if self.verbose:
                print(f"[ContextPool] new_context failed: {e}")
            return None

        # Anti-detect init script for Chromium, using the account's permanent
        # device profile (must run BEFORE the first page is created).
        if stealth_fingerprint:
            try:
                from browser.browser_engine import apply_chromium_stealth
                apply_chromium_stealth(context, print if self.verbose else None,
                                       fingerprint=stealth_fingerprint)
            except Exception as e:
                if self.verbose:
                    print(f"[ContextPool] stealth apply skipped: {e}")

        # Resource blocking.
        self._apply_resource_blocking(context)

        try:
            page = context.new_page()
        except Exception as e:
            if self.verbose:
                print(f"[ContextPool] new_page failed: {e}")
            try:
                context.close()
            except Exception:
                pass
            return None

        slot = ContextSlot(slot_id, account_key, context, page, proxy)
        with self._lock:
            self._slots[slot_id] = slot
        if self.verbose:
            print(f"[ContextPool] Created context #{slot_id} for {account_key}")
        return slot

    def _apply_resource_blocking(self, context) -> None:
        """Optionally block heavy resource types to save CPU/RAM."""
        if not self.block_resource_types:
            return
        try:
            context.route("**/*", lambda route: self._route_handler(route))
        except Exception:
            pass

    def _route_handler(self, route) -> None:
        """Abort requests for blocked resource types."""
        try:
            rtype = route.request.resource_type
            if rtype in self.block_resource_types:
                route.abort()
            else:
                route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    # ---- context close / return ----

    def close_context(self, slot_id: int) -> bool:
        """Close a context and free its pool slot."""
        with self._lock:
            slot = self._slots.pop(slot_id, None)
        if slot is None:
            return False
        self._safe_close_slot(slot)
        self._semaphore.release()
        if self.verbose:
            print(f"[ContextPool] Closed context #{slot_id}")
        return True

    def _safe_close_slot(self, slot: ContextSlot) -> None:
        """Close page + context, swallowing all errors (cleanup guarantee)."""
        slot.closed = True
        try:
            if slot.page:
                slot.page.close()
        except Exception:
            pass
        try:
            if slot.context:
                slot.context.close()
        except Exception:
            pass

    # ---- query ----

    def active_count(self) -> int:
        with self._lock:
            return len(self._slots)

    def slot_info(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "slot_id": s.slot_id,
                    "account_key": s.account_key,
                    "age_s": round(s.age, 1),
                    "closed": s.closed,
                }
                for s in self._slots.values()
            ]

    # ---- Resource Governor integration (2026 upgrade) ----

    def get_live_pages(self) -> List[Any]:
        """Return all live Playwright pages across active contexts.

        Used by the Resource Governor to run Firefox memory-minimize JS on
        every page (mitigates Camoufox Issue #245 memory growth).
        """
        pages: List[Any] = []
        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            try:
                if slot.page and not slot.page.is_closed():
                    pages.append(slot.page)
                # Also grab any extra pages the context opened.
                if slot.context:
                    for p in slot.context.pages:
                        if p and not p.is_closed() and p not in pages:
                            pages.append(p)
            except Exception:
                pass
        return pages

    def recycle_oldest(self, n: int) -> int:
        """Close the *n* oldest active contexts to free leaked RAM.

        Called by the Resource Governor's orange/red tier.  The semaphore
        permit is released for each closed slot so new workers can acquire
        fresh (clean) contexts.  Returns the number actually closed.
        Never raises.
        """
        if n <= 0:
            return 0
        closed = 0
        with self._lock:
            # Sort by age descending (oldest first).
            sorted_slots = sorted(
                self._slots.values(), key=lambda s: s.created_at
            )
            to_close = sorted_slots[:n]
            close_ids = [s.slot_id for s in to_close]
        for sid in close_ids:
            if self.close_context(sid):
                closed += 1
        return closed

    def trigger_firefox_gc(self) -> None:
        """Best-effort Firefox memory-minimize across all live pages.

        Runs a small JS snippet on each page that calls window.gc() (if
        available), clears caches, and drops bfcache entries.  Mitigates
        the per-navigation memory growth of Camoufox Issue #245 without
        restarting the context.  Never raises.
        """
        try:
            from core.resource_governor import firefox_memory_minimize_pages
            pages = self.get_live_pages()
            if pages:
                firefox_memory_minimize_pages(pages)
        except Exception:
            pass

    # ---- shutdown ----

    def close_all(self) -> None:
        """Close every context and shut down the shared browser."""
        with self._lock:
            self._shutting_down = True
            slots = list(self._slots.values())
            self._slots.clear()
        for slot in slots:
            self._safe_close_slot(slot)
            self._semaphore.release()
        # Close the browser.
        with self._lock:
            manager = self._camoufox_manager
            self._browser = None
            self._camoufox_manager = None
            self._started = False
        if manager is not None:
            try:
                from browser.browser_engine import close_browser
                close_browser(manager, print if self.verbose else None)
            except Exception:
                # Fall back to the legacy Camoufox exit protocol.
                try:
                    manager.__exit__(None, None, None)
                except Exception as e:
                    if "greenlet" not in str(e).lower() and self.verbose:
                        print(f"[ContextPool] Browser exit warning: {e}")
        if self.verbose:
            print("[ContextPool] All contexts closed, browser shut down")


# --------------------------------------------------------------------------- #
# Singleton accessor
# ---------------------------------------------------------------------------

_pool_singleton: Optional[ContextPool] = None
_pool_lock = threading.Lock()


def get_pool(
    max_contexts: int = 50,
    headless: bool = True,
    **kwargs,
) -> ContextPool:
    """Get or create the global ContextPool singleton."""
    global _pool_singleton
    with _pool_lock:
        if _pool_singleton is None:
            _pool_singleton = ContextPool(
                max_contexts=max_contexts, headless=headless, **kwargs,
            )
        return _pool_singleton


def reset_pool() -> None:
    """Tear down the singleton (used by tests and full restart)."""
    global _pool_singleton
    with _pool_lock:
        if _pool_singleton is not None:
            try:
                _pool_singleton.close_all()
            except Exception:
                pass
        _pool_singleton = None


# --------------------------------------------------------------------------- #
# OS-level browser process cleanup (crash-recovery safety net)
# --------------------------------------------------------------------------- #

def force_kill_camoufox_processes(timeout: float = 5.0) -> int:
    """Force-kill any lingering Camoufox/Firefox browser subprocesses.

    When a worker thread is hard-terminated (QThread.terminate()) before its
    ``run()`` finally-block can close Camoufox, the Firefox process is
    orphaned and its Playwright driver pipes stay half-open.  The *next* run
    then fails to launch a browser ("greenlet cannot switch to a different
    thread" or a frozen driver).  This helper finds and kills those orphans
    by OS process name so the next launch starts clean.

    Safe to call from any thread — it only touches OS process tables, not
    any greenlet/thread-bound Playwright object.  Returns the number of
    processes killed.  Best-effort: never raises.
    """
    import sys
    killed = 0

    def _pkill_windows(names):
        nonlocal killed
        try:
            import subprocess
            # taskkill /F /IM <name> for each candidate image name.
            for name in names:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/IM", name, "/T"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=timeout,
                    )
                    killed += 1
                except Exception:
                    pass
        except Exception:
            pass

    def _pkill_posix(names):
        nonlocal killed
        try:
            import subprocess
            for name in names:
                try:
                    subprocess.run(
                        ["pkill", "-f", name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=timeout,
                    )
                    killed += 1
                except Exception:
                    pass
        except Exception:
            # Fallback: scan /proc for camoufox cmdline entries.
            try:
                import os as _os
                for pid in _os.listdir("/proc"):
                    if not pid.isdigit():
                        continue
                    try:
                        with open(f"/proc/{pid}/cmdline", "rb") as fh:
                            cmd = fh.read().decode("utf-8", "ignore").lower()
                        if "camoufox" in cmd or ("firefox" in cmd and "camoufox" in cmd):
                            _os.kill(int(pid), 9)
                            killed += 1
                    except Exception:
                        pass
            except Exception:
                pass

    # Camoufox ships a patched Firefox.  Image names vary by platform.
    if sys.platform == "win32":
        _pkill_windows(["camoufox.exe", "camoufox-bin.exe", "firefox.exe"])
    else:
        _pkill_posix(["camoufox", "camoufox-bin"])
    return killed
