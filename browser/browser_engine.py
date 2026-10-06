"""Browser engine abstraction — launch Camoufox OR Chromium via Playwright.

This module lets the bot switch between two browser engines WITHOUT touching
any of the higher-level automation code (tabs, human behavior, chat flow).
All code above this layer works with standard Playwright ``Browser`` /
``BrowserContext`` / ``Page`` objects, which are identical for both engines.

Why this exists
---------------
Camoufox (anti-detect Firefox) caused recurring problems: blank/white pages
on heavy sites, high memory, occasional crashes.  Chromium via Playwright is
far more stable, renders every site correctly, and is the same engine real
users use.  This module lets the user pick the engine via ``config.json``:

    "browser_engine": "chromium"   # or "camoufox" (legacy)

Usage
-----
    from browser_engine import launch_browser, close_browser

    browser, handle = launch_browser(
        engine="chromium",
        headless=False,
        proxy=proxy_dict_or_none,
        log_fn=my_logger,
    )
    context = browser.new_context(...)
    page = context.new_page()
    ...
    close_browser(handle, log_fn=my_logger)

The ``handle`` is an opaque object that knows how to clean up its engine
(a Playwright ``sync_playwright()`` context manager for Chromium, or the
Camoufox manager for Camoufox).  Callers should NEVER inspect it directly.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional, Tuple, Callable

LogFn = Optional[Callable[[str], None]]


# ---------------------------------------------------------------------------
# Stealth helpers for Chromium (replace Camoufox's built-in fingerprinting)
# ---------------------------------------------------------------------------

# A spread of realistic, modern Chromium User-Agent strings.  Each browser
# launch picks ONE UA at random (and the SAME one is reused for both the
# context user_agent and the init-script consistency check) so the browser
# doesn't all use the same identical UA — a cheap anti-detect measure.
# We keep the UA, platform, and Chrome major version in sync so fingerprinting
# sites can't spot a UA that claims "Chrome 131" while navigator says otherwise.
_CHROMIUM_UA_POOL = (
    # Windows 10/11 Chrome 131
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    # macOS Chrome 131
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
)

# Common real-world viewport sizes (so not every browser reports 1920x1080).
_VIEWPORT_POOL = (
    {"width": 1280, "height": 800},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1600, "height": 900},
    {"width": 1920, "height": 1080},
)

# Realistic hardwareConcurrency values (headless Chromium reports 2, which is a
# tell; real desktops report 4/8/12/16).  We pick one at random per launch.
_HARDWARE_CONCURRENCY_POOL = (4, 8, 8, 12, 16)

# Realistic deviceMemory values in GB (headless Chromium exposes None, which is
# a strong headless tell; real Chrome reports 4 or 8).
_DEVICE_MEMORY_POOL = (4, 8, 8)

# The stealth init script is built per-launch so it can embed the EXACT UA,
# platform, hardwareConcurrency and deviceMemory chosen for this context.
# This guarantees navigator.userAgent / platform / etc. all agree with each
# other and with the context-level user_agent (no mismatch leaks).
#
# Key fixes vs. the old version:
#   * navigator.plugins  — use a real PluginArray-like object (old version's
#     plain array was detected because it lacked .item()/.namedItem()/.refresh).
#   * navigator.languages — properly override (old getter was non-configurable).
#   * window.chrome      — define a full app/runtime object (old: undefined).
#   * navigator.webdriver — delete + redefine so it stays undefined.
#   * navigator.deviceMemory / hardwareConcurrency — add if missing.
#   * WebRTC IP leak      — patch RTCPeerConnection so STUN candidates never
#     reveal the real local/public IP (the #1 device-leak vector).
#   * navigator.platform  — align with the UA (Win32 for Windows, MacIntel for
#     macOS) so they don't contradict each other.
def _build_stealth_init_script(ua: str, platform: str, hw_conc: int, dev_mem: int) -> str:
    import random as _r
    screen_w = _r.choice([1366, 1440, 1536, 1600, 1920])
    screen_h = _r.choice([768, 900, 864, 900, 1080])
    avail_h = screen_h - _r.choice([40, 60, 80])
    dpr = _r.choice([1.0, 1.0, 1.25, 1.5])
    outer_w = screen_w + _r.randint(-50, 0)
    outer_h = avail_h + _r.randint(-20, 0)
    inner_w = outer_w - _r.randint(16, 40)
    inner_h = outer_h - _r.randint(85, 140)
    return """
(function () {
    const UA = %r;
    const PLAT = %r;
    const HW = %d;
    const MEM = %d;

    // ---- navigator.webdriver : the #1 headless tell ----
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined, configurable: true
        });
    } catch (e) {}

    // ---- navigator.userAgent / appVersion : keep in sync with context UA ----
    try {
        Object.defineProperty(navigator, 'userAgent', {get: () => UA, configurable: true});
    } catch (e) {}
    try {
        Object.defineProperty(navigator, 'appVersion', {
            get: () => UA.replace(/^Mozilla\\//, ''), configurable: true
        });
    } catch (e) {}
    try {
        Object.defineProperty(navigator, 'platform', {get: () => PLAT, configurable: true});
    } catch (e) {}

    // ---- navigator.plugins : real PluginArray-like object ----
    try {
        const makePlugin = (name) => {
            const p = {name: name, filename: 'internal-pdf-viewer',
                       description: 'Portable Document Format', length: 1};
            p[0] = {type: 'application/pdf', suffixes: 'pdf', description: ''};
            return p;
        };
        const plugins = [
            makePlugin('PDF Viewer'),
            makePlugin('Chrome PDF Viewer'),
            makePlugin('Chromium PDF Viewer'),
            makePlugin('Microsoft Edge PDF Viewer'),
            makePlugin('WebKit built-in PDF'),
        ];
        plugins.item = (i) => plugins[i] || null;
        plugins.namedItem = (n) => plugins.find(p => p.name === n) || null;
        plugins.refresh = () => {};
        Object.defineProperty(navigator, 'plugins', {
            get: () => plugins, configurable: true
        });
        const mimes = [{type: 'application/pdf', suffixes: 'pdf', description: ''}];
        mimes.item = (i) => mimes[i] || null;
        mimes.namedItem = (n) => mimes.find(m => m.type === n) || null;
        Object.defineProperty(navigator, 'mimeTypes', {
            get: () => mimes, configurable: true
        });
    } catch (e) {}

    // ---- navigator.languages : ['en-US', 'en'] (real user) ----
    try {
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'], configurable: true
        });
        Object.defineProperty(navigator, 'language', {
            get: () => 'en-US', configurable: true
        });
    } catch (e) {}

    // ---- navigator.hardwareConcurrency / deviceMemory ----
    try {
        Object.defineProperty(navigator, 'hardwareConcurrency', {
            get: () => HW, configurable: true
        });
    } catch (e) {}
    try {
        Object.defineProperty(navigator, 'deviceMemory', {
            get: () => MEM, configurable: true
        });
    } catch (e) {}

    // ---- screen dimensions : prevent headless screen/viewport mismatch ----
    try {
        Object.defineProperty(screen, 'width', {get: () => %d, configurable: true});
        Object.defineProperty(screen, 'height', {get: () => %d, configurable: true});
        Object.defineProperty(screen, 'availWidth', {get: () => %d, configurable: true});
        Object.defineProperty(screen, 'availHeight', {get: () => %d, configurable: true});
        Object.defineProperty(screen, 'colorDepth', {get: () => 24, configurable: true});
        Object.defineProperty(screen, 'pixelDepth', {get: () => 24, configurable: true});
    } catch (e) {}

    // ---- devicePixelRatio : consistent with viewport ----
    try {
        Object.defineProperty(window, 'devicePixelRatio', {
            get: () => %s, configurable: true
        });
    } catch (e) {}

    // ---- outer vs inner window size : real browsers have outer > inner ----
    try {
        Object.defineProperty(window, 'outerWidth', {
            get: () => %d, configurable: true
        });
        Object.defineProperty(window, 'outerHeight', {
            get: () => %d, configurable: true
        });
        Object.defineProperty(window, 'innerWidth', {
            get: () => %d, configurable: true
        });
        Object.defineProperty(window, 'innerHeight', {
            get: () => %d, configurable: true
        });
    } catch (e) {}

    // ---- screenX / screenY : window position ----
    try {
        Object.defineProperty(window, 'screenX', {get: () => %d, configurable: true});
        Object.defineProperty(window, 'screenY', {get: () => %d, configurable: true});
    } catch (e) {}

    // ---- window.chrome : full app object (headless lacks it) ----
    try {
        if (!window.chrome || typeof window.chrome !== 'object') {
            window.chrome = {};
        }
        window.chrome.runtime = window.chrome.runtime || {
            OnInstalledReason: {CHROME_UPDATE: 'chrome_update', INSTALL: 'install', UPDATE: 'update'},
            PlatformOs: {MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd'},
            connect: () => {}, sendMessage: () => {}
        };
        window.chrome.app = window.chrome.app || {
            isInstalled: false, InstallState: {DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'},
            RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'}
        };
        window.chrome.csi = window.chrome.csi || (() => ({}));
        window.chrome.loadTimes = window.chrome.loadTimes || (() => ({}));
    } catch (e) {}

    // ---- permissions.query : don't throw for notifications in headless ----
    try {
        const origQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (origQuery) {
            window.navigator.permissions.query = (p) => (
                p && p.name === 'notifications'
                    ? Promise.resolve({state: Notification.permission})
                    : origQuery.call(window.navigator.permissions, p)
            );
        }
    } catch (e) {}

    // ---- WebRTC IP leak : prevent STUN/ICE candidates from revealing the
    //      real local/public IP.  This is the #1 device-identity leak vector
    //      — without it, Chitchat can see our real IP even through a proxy.
    try {
        const OrigRTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
        if (OrigRTC) {
            const PatchedRTC = function (config, constraints) {
                const pc = new OrigRTC(config, constraints);
                const origAdd = pc.addEventListener.bind(pc);
                pc.addEventListener = function (type, listener, opts) {
                    if (type === 'icecandidate') {
                        return;
                    }
                    return origAdd(type, listener, opts);
                };
                try {
                    Object.defineProperty(pc, 'onicecandidate', {
                        get: () => null,
                        set: () => {},
                        configurable: true
                    });
                } catch (e2) {}
                return pc;
            };
            PatchedRTC.prototype = OrigRTC.prototype;
            window.RTCPeerConnection = PatchedRTC;
            if (window.webkitRTCPeerConnection) {
                window.webkitRTCPeerConnection = PatchedRTC;
            }
        }
    } catch (e) {}

    // ---- iframe consistency : apply the same overrides inside iframes ----
    try {
        const origContentWindow = Object.getOwnPropertyDescriptor(
            HTMLIFrameElement.prototype, 'contentWindow');
        if (origContentWindow && origContentWindow.get) {
            Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
                get: function () {
                    const cw = origContentWindow.get.call(this);
                    try {
                        if (cw) {
                            Object.defineProperty(cw.navigator, 'webdriver', {get: () => undefined, configurable: true});
                        }
                    } catch (e) {}
                    return cw;
                },
                configurable: true
            });
        }
    } catch (e) {}
})();
""" % (ua, platform, hw_conc, dev_mem,
       screen_w, screen_h, screen_w, avail_h,
       dpr,
       outer_w, outer_h, inner_w, inner_h,
       _r.randint(0, 100), _r.randint(0, 80))


def _random_ua() -> str:
    import random
    return random.choice(_CHROMIUM_UA_POOL)


def _random_viewport() -> Dict[str, int]:
    import random
    return random.choice(_VIEWPORT_POOL)


def _platform_for_ua(ua: str) -> str:
    """Return the navigator.platform that matches the chosen UA string."""
    if "Windows" in ua:
        return "Win32"
    if "Macintosh" in ua:
        return "MacIntel"
    if "Linux" in ua:
        return "Linux x86_64"
    return "Win32"


def _random_hardware_concurrency() -> int:
    import random
    return random.choice(_HARDWARE_CONCURRENCY_POOL)


def _random_device_memory() -> int:
    import random
    return random.choice(_DEVICE_MEMORY_POOL)


def _stealth_fingerprint() -> Dict[str, Any]:
    """Build ONE coherent fingerprint for a single browser context.

    Returns a dict with the UA, viewport, platform, hardwareConcurrency and
    deviceMemory that ``chromium_context_kwargs()`` and
    ``apply_chromium_stealth()`` MUST share, so there is never a mismatch
    between the context-level user_agent and what navigator.userAgent reports
    inside the page.
    """
    import random
    ua = random.choice(_CHROMIUM_UA_POOL)
    return {
        "user_agent": ua,
        "viewport": random.choice(_VIEWPORT_POOL),
        "platform": _platform_for_ua(ua),
        "hardware_concurrency": random.choice(_HARDWARE_CONCURRENCY_POOL),
        "device_memory": random.choice(_DEVICE_MEMORY_POOL),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def launch_browser(
    engine: str = "chromium",
    headless: bool = True,
    proxy: Optional[Dict[str, Any]] = None,
    log_fn: LogFn = None,
    fingerprint: Optional[Any] = None,
    firefox_prefs: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, Any]:
    """Launch a browser and return ``(browser, handle)``.

    Parameters
    ----------
    engine : str
        "chromium" (default, recommended) or "camoufox" (legacy).
    headless : bool
        Whether to run without a visible window.
    proxy : dict | None
        Playwright proxy dict ``{"server": ..., "username": ..., "password": ...}``
        or None for a direct connection.
    log_fn : callable | None
        Optional logger ``fn(str)``.
    fingerprint : any
        Camoufox-only: a saved fingerprint to restore.  Ignored for Chromium.
    firefox_prefs : dict | None
        Camoufox-only: ``firefox_user_prefs`` overrides.  Ignored for Chromium.

    Returns
    -------
    (browser, handle)
        ``browser`` is a Playwright ``Browser``.  ``handle`` is an opaque
        cleanup token — pass it to :func:`close_browser`.
    """
    engine = (engine or "chromium").lower().strip()

    if engine == "camoufox":
        return _launch_camoufox(headless, proxy, log_fn, fingerprint, firefox_prefs)
    # Default and recommended: Chromium
    return _launch_chromium(headless, proxy, log_fn)


def close_browser(handle: Any, log_fn: LogFn = None) -> None:
    """Close a browser launched by :func:`launch_browser`.

    Safely no-ops if ``handle`` is None.
    """
    if handle is None:
        return
    try:
        handle.close()
    except Exception as e:
        if log_fn:
            log_fn(f"[Browser] close error (ignored): {e}")


# ---------------------------------------------------------------------------
# Chromium (Playwright) — the recommended engine
# ---------------------------------------------------------------------------

def _launch_chromium(headless, proxy, log_fn):
    import random
    from playwright.sync_api import sync_playwright

    if log_fn:
        log_fn("[Browser] Launching Chromium (Playwright) — stable, renders all sites")

    pw = sync_playwright().start()
    try:
        launch_args = [
            "--disable-blink-features=AutomationControlled",  # hide navigator.webdriver flag
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-features=TranslateUI,IsolateOrigins,site-per-process",
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--disable-dev-shm-usage",          # avoid /dev/shm issues in containers
            "--disable-popup-blocking",          # let chat popups through
            "--lang=en-US,en",
        ]
        # A realistic persistent profile dir per launch keeps cookies/storage
        # isolated but lets the browser behave like a real session (no
        # "fresh install" tells).  We use a temp dir so it's cleaned on exit.
        launch_kwargs = {
            "headless": headless,
            "args": launch_args,
        }
        if proxy:
            launch_kwargs["proxy"] = proxy

        browser = pw.chromium.launch(**launch_kwargs)
    except Exception:
        # Make sure the Playwright runtime doesn't leak if the launch failed.
        try:
            pw.stop()
        except Exception:
            pass
        raise

    # Build a handle that owns the Playwright runtime so close_browser() can
    # tear everything down cleanly.
    handle = _ChromiumHandle(pw, browser)
    if log_fn:
        log_fn("[Browser] Chromium started successfully")
    return browser, handle


class _ChromiumHandle:
    """Cleanup handle for a Chromium/Playwright browser."""

    def __init__(self, pw, browser):
        self._pw = pw
        self._browser = browser

    def close(self):
        # Close the browser first, then stop the Playwright runtime.
        try:
            if self._browser and self._browser.is_connected():
                self._browser.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Camoufox (legacy) — kept for users who want to fall back
# ---------------------------------------------------------------------------

def _launch_camoufox(headless, proxy, log_fn, fingerprint, firefox_prefs):
    from camoufox.sync_api import Camoufox

    if log_fn:
        log_fn(
            "[Stealth] Camoufox BrowserForge fingerprint and UA, GeoIP location, "
            "WebRTC IP matching, and humanized cursor enabled."
        )
    launch_options = {
        "headless": headless,
        "geoip": True,
        "humanize": True,
    }
    if proxy is not None:
        launch_options["proxy"] = proxy
    if fingerprint is not None:
        launch_options["fingerprint"] = fingerprint
        if log_fn:
            log_fn("[Restore] Reusing the saved browser fingerprint")
    if firefox_prefs:
        launch_options["firefox_user_prefs"] = firefox_prefs

    manager = Camoufox(**launch_options)
    browser = manager.__enter__()
    return browser, manager  # the manager IS the cleanup handle


# ---------------------------------------------------------------------------
# Orphan-process cleanup
# ---------------------------------------------------------------------------

def kill_orphan_browsers(log_fn: LogFn = None, engine: str = "chromium") -> int:
    """Kill orphaned browser processes left by a failed launch.

    Works for both engines: matches chromium/chrome (Chromium) and
    camoufox/firefox (Camoufox).  Only kills processes whose parent is gone
    (orphans), so a healthy browser another worker just launched is safe.
    """
    engine = (engine or "chromium").lower().strip()
    try:
        import psutil
    except Exception:
        return 0
    # Names to match per engine.
    names = ["chromium", "chrome", "chromium-browser", "google-chrome"]
    if engine == "camoufox":
        names = ["camoufox", "firefox"]
    else:
        # When using Chromium, also clean up any leftover camoufox/firefox
        # from a previous run that switched engines.
        names = names + ["camoufox", "firefox"]

    killed = 0
    try:
        for proc in psutil.process_iter(["pid", "name", "ppid"]):
            try:
                name = (proc.info.get("name") or "").lower()
            except Exception:
                continue
            if not name:
                continue
            if not any(n in name for n in names):
                continue
            ppid = proc.info.get("ppid", 0)
            try:
                parent_alive = ppid and psutil.pid_exists(ppid)
            except Exception:
                parent_alive = True
            if not parent_alive:
                try:
                    proc.kill()
                    killed += 1
                    if log_fn:
                        log_fn(f"[Browser] Killed orphan browser PID {proc.info['pid']}")
                except Exception:
                    pass
    except Exception:
        pass
    return killed


# ---------------------------------------------------------------------------
# Chromium stealth: apply to a BrowserContext (anti-detect basics)
# ---------------------------------------------------------------------------

def apply_chromium_stealth(context, log_fn: LogFn = None, fingerprint: Optional[Dict[str, Any]] = None) -> None:
    """Apply anti-detect settings to a Chromium BrowserContext.

    Injects an init script (built from ``fingerprint``) that masks the common
    headless/automated tells AND blocks the WebRTC IP leak.  The ``fingerprint``
    dict MUST be the same one passed to ``new_context(user_agent=...)`` so that
    navigator.userAgent inside the page agrees with the context-level UA.

    Call this right after ``browser.new_context(...)`` and BEFORE any page is
    created, so the init script runs before site JavaScript.
    """
    try:
        fp = fingerprint or _stealth_fingerprint()
        ua = fp.get("user_agent") or _random_ua()
        platform = fp.get("platform") or _platform_for_ua(ua)
        hw = int(fp.get("hardware_concurrency") or 8)
        mem = int(fp.get("device_memory") or 8)
        context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        # Add the init script BEFORE any page is created so it runs first.
        context.add_init_script(_build_stealth_init_script(ua, platform, hw, mem))
        if log_fn:
            vp = fp.get("viewport") or {}
            log_fn(
                f"[Stealth] Chromium anti-detect applied: UA={ua[:45]}... "
                f"platform={platform} hw={hw} mem={mem}GB "
                f"viewport={vp.get('width')}x{vp.get('height')} "
                f"WebRTC-leak=blocked"
            )
    except Exception as e:
        if log_fn:
            log_fn(f"[Stealth] anti-detect apply skipped: {e}")


def chromium_context_kwargs(fingerprint: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return recommended new_context() kwargs for a stealthy Chromium context.

    Pass these into ``browser.new_context(**chromium_context_kwargs())``.
    Returns the SAME ``fingerprint`` dict (if given) so the caller can pass it
    on to :func:`apply_chromium_stealth` and guarantee UA/platform consistency.
    """
    fp = fingerprint or _stealth_fingerprint()
    kwargs = {
        "user_agent": fp.get("user_agent") or _random_ua(),
        "viewport": fp.get("viewport") or _random_viewport(),
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "java_script_enabled": True,
        "ignore_https_errors": False,
    }
    # Stash the fingerprint on the dict so the caller can extract it and pass
    # it to apply_chromium_stealth().  (new_context ignores unknown keys?  No —
    # it would error, so we keep this OUT of kwargs and return it separately.)
    return kwargs, fp
