"""Camoufox browser automation for Chitchat Bot."""
import os
import sys
import random
import time
import re
import asyncio
import threading
import warnings
import uuid
import winsound
from PyQt6.QtCore import QThread, pyqtSignal
from browser.account_session_store import (
    load_saved_fingerprint,
    save_account_session,
    delete_account_session,
    mark_session_banned,
    refresh_storage_state,
    record_session_health,
    session_health_for,
    session_stock_summary,
)

# Phase 13 — session auto-load / blind-session detection + repair.
# Best-effort import: without it the bot keeps its old behaviour unchanged.
try:
    from browser import session_health as session_guard
    SESSION_GUARD_AVAILABLE = True
except Exception as _session_guard_err:  # pragma: no cover
    session_guard = None
    SESSION_GUARD_AVAILABLE = False
    _SESSION_GUARD_IMPORT_ERROR = str(_session_guard_err)
import os
from collections import deque
from core.config_loader import normalize_chat_timing
from entry.paths import project_root as _project_root

# v2.1 — robust stranger-SMS detection (multi-selector DOM reader + tracker).
# Imported best-effort: if it is missing the old extractor is used unchanged.
try:
    from browser import chat_reader
    CHAT_READER_AVAILABLE = True
except Exception as _chat_reader_err:  # pragma: no cover
    chat_reader = None
    CHAT_READER_AVAILABLE = False
    _CHAT_READER_IMPORT_ERROR = str(_chat_reader_err)

# Import ChatRuleBot for intelligent replies
try:
    from chat import ChatRuleBot
    CHAT_RULE_BOT_AVAILABLE = True
except Exception as _chat_rule_import_err:
    CHAT_RULE_BOT_AVAILABLE = False
    _CHAT_RULE_IMPORT_ERROR = str(_chat_rule_import_err)

# Camoufox exposes a Playwright-compatible API and uses greenlets internally.
warnings.filterwarnings('ignore', category=DeprecationWarning)


# ---------------------------------------------------------------------------
# NOTIFICATION SYSTEM - Windows toast + sound alert
# ---------------------------------------------------------------------------

def notify_captcha(title: str, message: str, play_sound: bool = True):
    """Send Windows toast notification + play alert sound.
    
    Args:
        title: Notification title (e.g. "CAPTCHA ALERT")
        message: Notification message (e.g. "Solve captcha to continue")
        play_sound: Whether to play alert sound (default True)
    """
    try:
        from plyer import notification
        notification.notify(
            title=title,
            message=message,
            app_name="Chitchat Bot",
            timeout=10,
        )
    except ImportError:
        # Fallback: use PowerShell toast notification
        try:
            ps_cmd = f'''
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
            [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
            $template = @"
            <toast>
                <visual>
                    <binding template="ToastGeneric">
                        <text>{title}</text>
                        <text>{message}</text>
                    </binding>
                </visual>
            </toast>
"@
            $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
            $xml.LoadXml($template)
            $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Chitchat Bot").Show($toast)
            '''
            import subprocess
            subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, timeout=5)
        except Exception:
            pass
    
    if play_sound:
        try:
            # Play Windows alert sound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass


def play_captcha_alert():
    """Repeated alert beeps so an on-page captcha gets manual attention."""
    try:
        import time as _time
        for _ in range(3):
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            _time.sleep(0.35)
    except Exception:
        pass


def play_beep_sequence(repeats: int = 1):
    """Short generic beep(s) for minor events (connect/snap/ban)."""
    try:
        import time as _time
        for _ in range(max(1, int(repeats))):
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            _time.sleep(0.25)
    except Exception:
        pass
warnings.filterwarnings('ignore', message='.*greenlet.*')
warnings.filterwarnings('ignore', message='.*cannot switch to a different thread.*')

_CAMOUFOX_INSTALL_LOCK = threading.Lock()
# Class-level lock shared by ALL workers so that browser launches are
# serialized across threads.  Without this, every worker creates its own
# instance-level lock (which is useless for cross-worker coordination) and
# multiple Camoufox windows pop up simultaneously.
_GLOBAL_BROWSER_LAUNCH_LOCK = threading.Lock()
_MESSAGE_VARIANT_PATTERN = re.compile(r"\{([^{}]*\|[^{}]*)\}")


def expand_message_template(message):
    """Expand random alternatives in a message template.

    Alternatives use the form ``{first option | second option}``.  Every
    placeholder is selected independently, so a line can contain multiple
    placeholders and produce a different combination each time it is sent.
    Expressions without at least two non-empty alternatives are left as-is
    rather than sending an accidentally empty or malformed message.
    """
    if not isinstance(message, str):
        return message

    def choose_variant(match):
        options = [option.strip() for option in match.group(1).split("|")]
        options = [option for option in options if option]
        if len(options) < 2:
            return match.group(0)
        return random.choice(options)

    return _MESSAGE_VARIANT_PATTERN.sub(choose_variant, message)


def _camoufox_proxy(proxy_config):
    """Convert the app's proxy format to Camoufox's proxy mapping."""
    if not proxy_config:
        return None

    server = proxy_config['server']
    if not server.startswith(('http://', 'https://', 'socks5://')):
        server = f'http://{server}'

    proxy = {'server': server}
    if proxy_config.get('username') is not None:
        proxy['username'] = proxy_config['username']
    if proxy_config.get('password') is not None:
        proxy['password'] = proxy_config['password']
    return proxy


def _kill_orphan_camoufox(log_fn=None, engine=None):
    """Kill orphaned browser processes left by a failed launch.

    Delegates to :func:`browser_engine.kill_orphan_browsers`, which handles
    both Chromium (chrome/chromium) and Camoufox (camoufox/firefox) processes.
    Only kills orphans whose parent is gone, so a healthy browser another
    worker just launched is safe.

    Args:
        engine: Browser engine type ("chromium" or "camoufox"). If None,
                kills orphans for both engines.
    """
    try:
        from browser.browser_engine import kill_orphan_browsers
        if engine:
            return kill_orphan_browsers(log_fn=log_fn, engine=engine)
        # Kill orphans for both engines when engine type is unknown
        killed = 0
        for eng in ("chromium", "camoufox"):
            killed += kill_orphan_browsers(log_fn=log_fn, engine=eng)
        return killed
    except Exception:
        pass
    # Fallback: the original inline implementation (kept for safety).
    try:
        import psutil
    except Exception:
        return 0
    killed = 0
    try:
        for proc in psutil.process_iter(["pid", "name", "ppid"]):
            try:
                name = (proc.info.get("name") or "").lower()
            except Exception:
                continue
            if not name:
                continue
            # Match both engines so a mid-run switch doesn't leak old procs.
            if any(n in name for n in ("camoufox", "firefox", "chromium", "chrome")):
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


class _LegacyCountTracker:
    """Emergency tracker used only if browser/chat_reader.py is missing.

    Keeps the old behaviour (report everything, never de-duplicate) so the
    bot still works, but the caller is told in the log that the robust
    tracker is unavailable.
    """

    def __init__(self):
        self._seen = 0

    def reset(self):
        self._seen = 0

    def snapshot(self, messages):
        self._seen = len(list(messages or []))
        return []

    def sync(self, messages):
        messages = list(messages or [])
        new = messages[self._seen:] if len(messages) > self._seen else []
        self._seen = len(messages)
        return new


class ChitchatAutomation:
    """
    Browser automation class for Chitchat.gg random chat platform.
    Handles the complete flow: login, captcha, and continuous chat.
    """

    PUBLIC_HOMEPAGE_URL = 'https://www.chitchat.gg/'
    LOGIN_ENTRY_URL = 'https://app.chitchat.gg/login'
    NEW_SESSION_ENTRY_URL = 'https://app.chitchat.gg/start/new'
    PRE_HOMEPAGE_FALLBACK_URLS = (
        'https://www.google.com',
        'https://www.duckduckgo.com',
        'https://www.youtube.com',
    )
    PRE_HOMEPAGE_TAB_TIMEOUT_MS = 15000
    
    def __init__(self, account=None, fixed_messages=None, headless=True, thread_id=1, chat_timeout=8, reply_delay=1.0, proxy_config=None, proxy_fallback_configs=None, hide_after_login=False, fixed_message_templates=None, account_mode="login", shuffle_chat=False, snapchat_id_selector=None, message_template_selector=None, message_template_returner=None, first_message_mode="false", reply_delay_min=None, reply_delay_max=None, first_message_delay_min=None, first_message_delay_max=None, chat_timing=None, context_pool_slot=None, engine_bridge=None, chat_time_minutes=None, pic_folder=None, pic_send_enabled=False, pic_after_messages=4, pics_per_chat=1):
        """
        Initialize Chitchat automation with configuration.

        Args:
            account: Account dict with 'email' and 'password' for login
            fixed_messages: List of fixed messages to send sequentially (legacy)
            fixed_message_templates: List of message template records containing
                                     a filename and list of messages. Legacy
                                     lists of messages are also accepted.
            headless: Run browser in headless mode (default: True)
            thread_id: Thread identifier for logging (default: 1)
            chat_timeout: Max bot replies before moving to next user (default: 8)
            reply_delay: Legacy fixed delay fallback (default: 1.0)
            reply_delay_min: Minimum random delay before sending a reply
            reply_delay_max: Maximum random delay before sending a reply
            proxy_config: Proxy configuration dict with 'server', 'username', 'password' (optional)
            proxy_fallback_configs: Proxy configurations available when a saved
                                   restore proxy cannot be used
            hide_after_login: Move browser window off-screen after login (default: False)
            shuffle_chat: Send each selected template's messages in random order (default: False)
            snapchat_id_selector: Callable that returns the Snapchat username for a new chat
            message_template_selector: Callable that returns the message template for a new chat
            message_template_returner: Callable that returns an unused template after a failed chat
            first_message_mode: Send the first message after its configured delay,
                                wait for a reply, or choose randomly
                                ("true", "false", or "random")
            first_message_delay_min: Minimum random delay before sending the first message
            first_message_delay_max: Maximum random delay before sending the first message
            chat_timing: Validated delays and rest intervals for the chat loop
        """
        self.account_mode = account_mode
        if account is None and account_mode == "create":
            # Create mode does not have an email/password account.  Give the
            # anonymous authenticated browser session a local identity so its
            # cookies and storage can still be restored on a later run.
            self.account = {
                "email": f"created-session-{uuid.uuid4().hex[:16]}",
                "account_type": "created_session",
            }
        else:
            self.account = account  # {'email': '...', 'password': '...'}
        self.fixed_messages = fixed_messages or []
        self.fixed_message_templates = fixed_message_templates or []
        if not self.fixed_message_templates and self.fixed_messages:
            self.fixed_message_templates = [self.fixed_messages]
        self.headless = headless
        self.thread_id = thread_id
        self.chat_timeout = chat_timeout
        self.reply_delay = reply_delay
        self.chat_timing = normalize_chat_timing(chat_timing)
        # --- A-to-Z config sections (all have safe defaults) ---
        # Load every section from config.json so the whole bot is customisable
        # without changing any caller.  Each loader falls back to safe defaults
        # when config.json is missing or partial, so this never breaks the bot.
        from core.config_loader import (
            load_human_behavior,
            load_background_tabs,
            load_performance,
            load_ban_detection,
            load_session_management,
            load_replies,
            load_website_visits,
        )
        self.human_behavior = load_human_behavior()
        self.background_tabs_cfg = load_background_tabs()
        self.performance_cfg = load_performance()
        self.ban_detection_cfg = load_ban_detection()
        self.session_management_cfg = load_session_management()
        self.replies_cfg = load_replies()
        self.website_visits_cfg = load_website_visits()
        self._next_rest_at = None
        self._stop_event = threading.Event()
        if reply_delay_min is None:
            reply_delay_min = reply_delay
        if reply_delay_max is None:
            reply_delay_max = reply_delay
        try:
            self.reply_delay_min = max(0.0, float(reply_delay_min))
            self.reply_delay_max = max(0.0, float(reply_delay_max))
        except (TypeError, ValueError):
            self.reply_delay_min = 1.0
            self.reply_delay_max = 1.0
        if self.reply_delay_min > self.reply_delay_max:
            self.reply_delay_min, self.reply_delay_max = (
                self.reply_delay_max,
                self.reply_delay_min,
            )

        if first_message_delay_min is None and first_message_delay_max is None:
            first_message_delay_min = 0.0
            first_message_delay_max = 0.0
        elif first_message_delay_min is None:
            first_message_delay_min = first_message_delay_max
        elif first_message_delay_max is None:
            first_message_delay_max = first_message_delay_min
        try:
            self.first_message_delay_min = max(0.0, float(first_message_delay_min))
            self.first_message_delay_max = max(0.0, float(first_message_delay_max))
        except (TypeError, ValueError):
            self.first_message_delay_min = 0.0
            self.first_message_delay_max = 0.0
        if self.first_message_delay_min > self.first_message_delay_max:
            self.first_message_delay_min, self.first_message_delay_max = (
                self.first_message_delay_max,
                self.first_message_delay_min,
            )
        self.proxy_config = proxy_config
        self.proxy_fallback_configs = [
            config for config in (proxy_fallback_configs or []) if config
        ]
        self.hide_after_login = hide_after_login
        self.shuffle_chat = shuffle_chat
        # Max minutes per conversation with one stranger. None (default)
        # means: chat until the snap is shared (no time limit).
        self.chat_time_minutes = chat_time_minutes
        # Live dashboard hooks (additive): chat feed + session status
        self.chat_callback = None
        self.session_callback = None
        # ---- PICTURE SENDER (additive, browser-layer only) ----
        # The chat engine is NEVER told about pictures: this only uploads
        # an image file at the configured "middle chat point" and the
        # engine keeps replying exactly as before (pic + SMS both go).
        self.pic_folder = pic_folder
        self.pic_send_enabled = bool(pic_send_enabled) and bool(pic_folder)
        self.pic_after_messages = max(1, int(pic_after_messages or 4))
        self.pics_per_chat = max(1, int(pics_per_chat or 1))
        self._pic_pool = []
        self._pic_pool_pos = 0
        self._setup_pic_pool()
        first_message_mode = str(first_message_mode).lower()
        if first_message_mode not in {"true", "false", "random"}:
            first_message_mode = "false"
        self.first_message_mode = first_message_mode
        self.snapchat_id_selector = snapchat_id_selector
        self.message_template_selector = message_template_selector
        self.message_template_returner = message_template_returner
        self.is_running = False
        self.browser = None
        self.context = None
        self.camoufox_manager = None
        # Use the global class-level lock so browser launches are serialized
        # across ALL workers, not just within a single worker.
        self.browser_launch_lock = _GLOBAL_BROWSER_LAUNCH_LOCK
        # --- Context-pool mode (2026 upgrade) ---
        # When set, the worker uses a pre-created (browser, context) from the
        # shared ContextPool instead of launching its own Camoufox browser.
        self._context_pool_slot = context_pool_slot
        # --- Go engine bridge (2026 upgrade) ---
        # When set, the worker asks the Go engine for permission before
        # launching a browser (launch gate) so many concurrent launches
        # don't freeze the PC.  May be None when the engine is unavailable.
        self._engine_bridge = engine_bridge
        self.generated_fingerprint = None
        self.restore_fingerprint = (
            load_saved_fingerprint(self.account)
            if self.account_mode == "restore" and self.account
            else None
        )
        self._saved_proxy_config = (
            self.account.get("saved_proxy")
            if self.account_mode == "restore" and isinstance(self.account, dict)
            else None
        )
        self._proxy_fallback_attempted = False
        self._navigation_history = []
        self._tracked_page_ids = set()
        # --- Ban detection / background-tab / stock state ---
        self._ban_detected = False
        self._ban_handled = False
        self._background_tab_pages = []
        self._background_tab_next_visit_at = None
        self._bg_last_visited_url = None  # for random site rotation
        
        # Callback functions for logging (must be set before ChatRuleBot init)
        self.log_callback = print
        self.status_callback = None
        self.proxy_warning_callback = None
        self.captcha_callback = None
        self._latest_captcha_check = 0.0
        self._captcha_active = False
        
        # --- ChatRuleBot for intelligent replies ---
        self.chat_rule_bot = None
        self._chat_page_ref = None

        # --- v2.1 SMS detection state ---
        # Texts we just sent: used to label our own bubbles when the site
        # renders them without a username (so we never answer ourselves).
        self._recent_sent_texts = deque(maxlen=12)
        # My username, cached once discovered (survives DOM changes mid-chat).
        self._my_username = None
        # Last extraction diagnostics + "saw nothing" counter for warnings.
        self._last_detect_diag = {}
        self._empty_extract_polls = 0
        self._detect_warned = False
        self._heal_logged = False

        # --- Phase 13: session auto-load / blind-session repair state ---
        # "blind" = the saved cookies cannot log this account in any more.
        self._session_blind = False
        self._session_repair_attempted = False
        self._session_repair_reason = ""
        self._last_session_refresh = 0.0
        self._session_probe_reason = ""

    def _launch_camoufox(self):
        """Launch the configured browser engine (Chromium or Camoufox).

        The engine is chosen via ``config.json`` → ``browser_engine``:
        ``"chromium"`` (default, recommended — stable, renders every site) or
        ``"camoufox"`` (legacy anti-detect Firefox).  Either way a standard
        Playwright ``Browser`` is returned, so all higher-level code (tabs,
        human browsing, chat) works unchanged.
        """
        self._ensure_frozen_camoufox()

        # Resolve which engine to use.
        try:
            from core.config_loader import load_performance
            engine = str(load_performance().get("browser_engine", "chromium")).lower().strip()
        except Exception:
            engine = "chromium"
        if engine not in ("chromium", "camoufox"):
            engine = "chromium"

        proxy = _camoufox_proxy(self.proxy_config)
        if proxy:
            auth_label = "authenticated " if proxy.get('username') is not None else ""
            self.log(f"[Browser] Using {auth_label}proxy: {self.proxy_config['server']}")

        # Camoufox-only Firefox prefs (harmless to fetch; ignored for Chromium).
        firefox_prefs = None
        try:
            from core.resource_governor import firefox_prefs_for_camoufox
            firefox_prefs = firefox_prefs_for_camoufox()
        except Exception:
            pass

        from browser.browser_engine import launch_browser, close_browser, kill_orphan_browsers

        handle = None
        browser = None
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                browser, handle = launch_browser(
                    engine=engine,
                    headless=self.headless,
                    proxy=proxy,
                    log_fn=self.log,
                    fingerprint=self.restore_fingerprint,
                    firefox_prefs=firefox_prefs,
                )
                break
            except Exception as launch_err:
                self.log(f"[Browser] Launch attempt {attempt}/{max_attempts} failed: {launch_err}")
                try:
                    if handle is not None:
                        close_browser(handle, self.log)
                except Exception:
                    pass
                kill_orphan_browsers(self.log, engine=engine)
                if attempt >= max_attempts:
                    raise
                # Exponential backoff: 1.5s, 3s, ...
                import time as _t
                backoff = 1.5 * (2 ** (attempt - 1))
                self.log(f"[Browser] Retrying in {backoff:.1f}s ...")
                _t.sleep(backoff)
        self.camoufox_manager = handle  # opaque cleanup handle (engine-agnostic)
        return browser


    def _ensure_frozen_camoufox(self):
        """Ensure the browser engine is installed for frozen (PyInstaller) builds.

        For the Chromium engine, this installs Playwright's bundled Chromium
        browser if missing.  For the Camoufox engine, it installs Camoufox's
        native browser.  Source runs keep the normal behavior (no-op here).
        """
        if not getattr(sys, 'frozen', False):
            return

        # Resolve the engine so we install the right browser.
        try:
            from core.config_loader import load_performance
            _engine = str(load_performance().get("browser_engine", "chromium")).lower().strip()
        except Exception:
            _engine = "chromium"

        if _engine == "chromium":
            # Playwright downloads its Chromium into its cache directory.
            # If missing, install it so the frozen app can launch Chromium.
            try:
                from playwright.sync_api import sync_playwright
                # Triggering the driver start probes the browser; if the
                # browser isn't present Playwright raises a clear error that
                # the user can fix by running `playwright install chromium`.
                # We attempt a lightweight probe first.
                import subprocess
                try:
                    subprocess.run(
                        [sys.executable, "-m", "playwright", "install", "chromium"],
                        check=False,
                        timeout=300,
                    )
                except Exception:
                    pass
            except Exception:
                pass
            return

        # --- Legacy Camoufox install path ---
        from pathlib import Path
        from camoufox.pkgman import CamoufoxFetcher, camoufox_path

        # Several browser threads can reach this method during the first run.
        # Serialize the cache probe/download so they cannot delete or partially
        # replace one another's Camoufox installation.
        with _CAMOUFOX_INSTALL_LOCK:
            install_dir = None
            try:
                install_dir = Path(camoufox_path(download_if_missing=False))
            except Exception:
                # A missing or unsupported cache is repaired below by the
                # package manager.  Keep the original exception out of the
                # user log because it is only the discovery probe.
                pass

            if install_dir is not None and self._camoufox_executable(install_dir).is_file():
                return

            self.log('[Browser] Camoufox browser is not installed; downloading it now...')
            self.set_status('Installing Camoufox browser (first run)...')
            try:
                CamoufoxFetcher().install()
                install_dir = Path(camoufox_path(download_if_missing=False))
            except Exception as exc:
                raise RuntimeError(
                    'Camoufox browser installation failed. Check your internet '
                    'connection and run the application again.'
                ) from exc

            if not self._camoufox_executable(install_dir).is_file():
                raise RuntimeError(
                    f'Camoufox installation completed but its executable was not '
                    f'found in {install_dir}.'
                )

            self.log(f'[Browser] Camoufox browser installed at {install_dir}')

    @staticmethod
    def _camoufox_executable(install_dir):
        """Return the native Camoufox executable for the current platform."""
        from pathlib import Path

        install_dir = Path(install_dir)
        if sys.platform == 'win32':
            return install_dir / 'camoufox.exe'
        if sys.platform == 'darwin':
            return install_dir / 'Camoufox.app' / 'Contents' / 'MacOS' / 'camoufox'
        return install_dir / 'camoufox-bin'

    def _close_camoufox(self):
        """Close the browser through the engine handle that owns its runtime.

        Works for both Chromium (Playwright) and Camoufox — ``camoufox_manager``
        is an opaque cleanup handle set by :meth:`_launch_camoufox`.
        """
        handle = self.camoufox_manager
        self.camoufox_manager = None
        self.context = None
        self.browser = None
        if handle:
            try:
                from browser.browser_engine import close_browser
                close_browser(handle, self.log)
            except Exception:
                # Fall back to the legacy Camoufox exit protocol if the
                # browser_engine module isn't importable for some reason.
                try:
                    handle.__exit__(None, None, None)
                except Exception:
                    pass
    
    def set_log_callback(self, callback):
        """Set custom logging callback function"""
        self.log_callback = callback

    def set_chat_callback(self, callback):
        """Live chat feed hook: callback(who, text) with who='user'|'bot'."""
        self.chat_callback = callback

    def set_session_callback(self, callback):
        """Live session hook: callback(status) e.g. 'ACTIVE', 'BANNED'."""
        self.session_callback = callback

    def _emit_chat(self, who, text):
        """Push a chat line to the live dashboard (never raises)."""
        if self.chat_callback:
            try:
                self.chat_callback(who, str(text))
            except Exception:
                pass

    def _emit_session(self, status):
        """Push a session status change to the live dashboard."""
        if self.session_callback:
            try:
                self.session_callback(str(status))
            except Exception:
                pass
    
    # ------------------------------------------------------------------
    # PICTURE SENDER (additive): random picture at the middle chat point
    # ------------------------------------------------------------------
    _PIC_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

    def _setup_pic_pool(self):
        """Scan the pictures folder and build a shuffled pool of pic files.

        Files are expected to be pre-renamed by the dashboard
        (eva_pic_001.jpg, eva_pic_002.png, ...). Any image file found in
        the folder is usable, renamed or not.
        """
        self._pic_pool = []
        self._pic_pool_pos = 0
        if not self.pic_folder or not os.path.isdir(self.pic_folder):
            return
        try:
            files = sorted(
                f for f in os.listdir(self.pic_folder)
                if os.path.isfile(os.path.join(self.pic_folder, f))
                and os.path.splitext(f)[1].lower() in self._PIC_EXTENSIONS)
            self._pic_pool = [os.path.join(self.pic_folder, f) for f in files]
            random.shuffle(self._pic_pool)
            if self._pic_pool:
                self.log(f"[Pics] {len(self._pic_pool)} picture(s) loaded from {self.pic_folder}")
            else:
                self.log(f"[Pics] No pictures found in {self.pic_folder}")
        except Exception as e:
            self.log(f"[Pics] Could not read picture folder: {e}")

    def _next_pic_file(self):
        """Next random picture (no repeats until the whole pool is used)."""
        if not self._pic_pool:
            return None
        if self._pic_pool_pos >= len(self._pic_pool):
            random.shuffle(self._pic_pool)
            self._pic_pool_pos = 0
        path = self._pic_pool[self._pic_pool_pos]
        self._pic_pool_pos += 1
        return path

    def _send_picture(self, page, file_path, chat_number=None):
        """Send one picture file to the current stranger.

        Tries, in order:
          1. <input type="file"> (direct or after clicking an attach button)
          2. paste event on the chat textarea (JS File from local bytes)
          3. drag & drop event on the page (JS DataTransfer)

        NEVER raises into the chat flow: on failure it just logs and
        returns False, and the SMS reply continues exactly as before.
        """
        try:
            if not file_path or not os.path.isfile(file_path):
                self.log(f"[Pics] Picture file missing: {file_path}")
                return False

            # --- Method 1: real file input (most reliable) ---
            for selector in ('input[type="file"]',):
                try:
                    file_input = page.locator(selector).first
                    file_input.set_input_files(file_path, timeout=3000)
                    self._humanize_pause(0.8, 1.6)
                    self._maybe_click_send_after_upload(page)
                    return True
                except Exception:
                    continue

            # --- attach button might reveal a hidden file input ---
            try:
                clicked = page.evaluate("""
                    () => {
                        const hints = ['image', 'photo', 'pic', 'attach',
                                       'file', 'upload', 'media', 'send picture'];
                        const els = [...document.querySelectorAll(
                            'button, [role="button"], label, a, div[title], svg')];
                        for (const el of els) {
                            const t = ((el.getAttribute('title') || '') + ' ' +
                                       (el.getAttribute('aria-label') || '') + ' ' +
                                       (el.textContent || '')).toLowerCase();
                            if (hints.some(h => t.includes(h)) && t.length < 40) {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                if clicked:
                    self._humanize_pause(0.5, 1.0)
                    try:
                        file_input = page.locator('input[type="file"]').first
                        file_input.set_input_files(file_path, timeout=3000)
                        self._humanize_pause(0.8, 1.6)
                        self._maybe_click_send_after_upload(page)
                        return True
                    except Exception:
                        pass
            except Exception:
                pass

            # --- Method 2: paste event with a JS File (base64 payload) ---
            try:
                import base64
                with open(file_path, "rb") as fh:
                    b64 = base64.b64encode(fh.read()).decode("ascii")
                ext = os.path.splitext(file_path)[1].lower().lstrip(".")
                mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "png": "image/png", "webp": "image/webp",
                        "gif": "image/gif", "bmp": "image/bmp"}.get(ext, "image/jpeg")
                name = os.path.basename(file_path)
                ok = page.evaluate("""
                    async ([b64, name, mime]) => {
                        const res = await fetch(`data:${mime};base64,${b64}`);
                        const blob = await res.blob();
                        const file = new File([blob], name, {type: mime});
                        const dt = new DataTransfer();
                        dt.items.add(file);
                        const target = document.querySelector(
                            'textarea, input[type="text"]') || document.body;
                        const paste = new ClipboardEvent('paste', {
                            bubbles: true, cancelable: true});
                        Object.defineProperty(paste, 'clipboardData', {value: dt});
                        target.dispatchEvent(paste);
                        return true;
                    }
                """, [b64, name, mime])
                if ok:
                    self._humanize_pause(0.8, 1.6)
                    self._maybe_click_send_after_upload(page)
                    return True
            except Exception:
                pass

            # --- Method 3: drag & drop with a JS File ---
            try:
                import base64
                with open(file_path, "rb") as fh:
                    b64 = base64.b64encode(fh.read()).decode("ascii")
                ext = os.path.splitext(file_path)[1].lower().lstrip(".")
                mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "png": "image/png", "webp": "image/webp",
                        "gif": "image/gif", "bmp": "image/bmp"}.get(ext, "image/jpeg")
                name = os.path.basename(file_path)
                page.evaluate("""
                    async ([b64, name, mime]) => {
                        const res = await fetch(`data:${mime};base64,${b64}`);
                        const blob = await res.blob();
                        const file = new File([blob], name, {type: mime});
                        const dt = new DataTransfer();
                        dt.items.add(file);
                        const target = document.querySelector(
                            'textarea, form, [class*="chat"], [class*="message"]')
                            || document.body;
                        for (const type of ['dragenter', 'dragover', 'drop']) {
                            target.dispatchEvent(new DragEvent(type, {
                                bubbles: true, cancelable: true, dataTransfer: dt}));
                        }
                        return true;
                    }
                """, [b64, name, mime])
                self._humanize_pause(0.8, 1.6)
                self._maybe_click_send_after_upload(page)
                return True
            except Exception:
                pass

            self.log("[Pics] This site did not accept a picture upload "
                     "(SMS replies continue normally)")
            return False
        except Exception as e:
            self.log(f"[Pics] Picture send failed (chat continues): {e}")
            return False

    def _maybe_click_send_after_upload(self, page):
        """After an upload/paste, click a Send button if one appeared."""
        try:
            for text in ("Send", "Submit", "Upload"):
                btn = self._find_visible_button(page, text, exact=False)
                if btn is not None:
                    self._humanize_click(btn, timeout=2000)
                    return
        except Exception:
            pass

    def set_status_callback(self, callback):
        """Set custom status callback function"""
        self.status_callback = callback

    def set_proxy_warning_callback(self, callback):
        """Set a callback for warnings that should be prominent in the GUI."""
        self.proxy_warning_callback = callback

    def set_captcha_callback(self, callback):
        """Set a callback fired once per captcha appearance (no extra args)."""
        self.captcha_callback = callback

    def _emit_captcha(self):
        if self.captcha_callback:
            try:
                self.captcha_callback()
            except Exception:
                pass

    def _check_captcha_alert(self, page):
        """Poll for an on-page captcha/challenge during chat, alert once per appearance.

        Throttled to ~once per 5 seconds.  One toast + callback per captcha
        appearance; the flag resets when the captcha disappears, so a captcha
        that re-appears on a later chat triggers a fresh alert.
        """
        import time as _time
        now = _time.time()
        if now - self._latest_captcha_check < 5.0:
            return False
        self._latest_captcha_check = now
        try:
            present = self._check_captcha_present(page)
            detected = bool((present or {}).get("present"))
        except Exception:
            return False
        if detected:
            if not self._captcha_active:
                self._captcha_active = True
                self.log("[CAPTCHA] Captcha/challenge on page — solve it in the browser to continue")
                notify_captcha(
                    "CAPTCHA ALERT",
                    "Solve the captcha on the Chitchat.gg page to continue",
                    play_sound=False,
                )
                self._emit_captcha()
            return True
        self._captcha_active = False
        return False

    def _emit_proxy_warning(self, message):
        if self.proxy_warning_callback:
            self.proxy_warning_callback(message)

    def _record_navigation(self, page, url=None):
        """Record the main-frame URL as a lightweight browser-history snapshot."""
        if not url:
            try:
                url = page.url
            except Exception:
                return
        if not url or str(url).startswith("about:blank"):
            return

        import datetime

        entry = {
            "url": str(url),
            "visited_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if not self._navigation_history or self._navigation_history[-1]["url"] != entry["url"]:
            self._navigation_history.append(entry)

    def _track_page_history(self, page):
        """Attach navigation tracking once to a Playwright page."""
        page_id = id(page)
        if page_id in self._tracked_page_ids:
            return
        self._tracked_page_ids.add(page_id)
        self._record_navigation(page)

        def on_frame_navigated(frame):
            try:
                if frame == page.main_frame:
                    self._record_navigation(page, frame.url)
            except Exception:
                pass

        try:
            page.on("framenavigated", on_frame_navigated)
        except Exception:
            pass

    def _use_proxy_fallback(self, reason):
        """Switch once from a saved restore proxy to a random/direct connection."""
        if self._proxy_fallback_attempted or not self._saved_proxy_config:
            return False

        self._proxy_fallback_attempted = True
        account_label = (
            self.account.get("email", "restored account")
            if isinstance(self.account, dict)
            else "restored account"
        )
        if self.proxy_fallback_configs:
            alternatives = [
                config
                for config in self.proxy_fallback_configs
                if config != self._saved_proxy_config
            ]
            self.proxy_config = random.choice(
                alternatives or self.proxy_fallback_configs
            )
            fallback_text = (
                "continuing with a random proxy from the selected proxy file"
            )
        else:
            self.proxy_config = None
            fallback_text = "continuing without a proxy"

        message = (
            f"Saved proxy failed for {account_label} ({reason}); "
            f"{fallback_text}."
        )
        self.log(f"[Proxy Warning] {message}")
        self.set_status(f"WARNING: {fallback_text}")
        self._emit_proxy_warning(message)
        return True

    def _save_successful_account_session(self, page):
        """Save state only after the authenticated chat page was reached."""
        if not isinstance(self.account, dict) or not self.account.get("email"):
            return
        if SESSION_GUARD_AVAILABLE and isinstance(self.account, dict) \
                and not self.account.get("session_dir"):
            # Recovered/legacy account without a folder: remember the folder we
            # are about to write so later refreshes hit the same place.
            pass

        try:
            session_dir = save_account_session(
                self.account,
                self.context,
                page,
                generated_fingerprint=(getattr(self, "generated_fingerprint", None)
                                       or getattr(self, "restore_fingerprint", None)),
                proxy_config=getattr(self, "proxy_config", None),
                history=getattr(self, "_navigation_history", None) or [],
            )
            self.log(f"[Account] Saved browser session for {self.account['email']} to {session_dir}")
            # Phase 13: remember when this session was verified + its health,
            # so the GUI/tools/session doctor can see it without a launch.
            try:
                if SESSION_GUARD_AVAILABLE:
                    health = session_guard.session_health(self.account)
                    session_guard.record_health(
                        self.account, health,
                        last_verified_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        verified_by="browser_automation",
                    )
                    if health.get("blind"):
                        self._session_blind_report(health.get("reason") or "saved state is blind")
                    else:
                        self._session_blind = False
            except Exception:
                pass
            self._emit_session("ACTIVE")
            return str(session_dir)
        except Exception as error:
            self.log(f"[Account] Warning: could not save browser session: {error}")
            return None

    # ------------------------------------------------------------------ #
    #  BAN DETECTION + AUTO-CLOSE + AUTO-DELETE  (new, additive)
    # ------------------------------------------------------------------ #
    def _detect_ban(self, page):
        """Return True if the page shows a ban / suspension / block indicator.

        Checks the visible page text against configurable ban indicators and,
        when enabled, treats an unexpected redirect to the login page (while we
        are supposed to be in an authenticated chat session) as a ban.
        """
        if not self.ban_detection_cfg.get("enabled", True):
            return False
        try:
            indicators = self.ban_detection_cfg.get("ban_indicators") or []
            login_redirect_is_ban = self.ban_detection_cfg.get(
                "login_redirect_is_ban", True
            )
            current_url = ""
            page_text = ""
            try:
                current_url = str(page.url or "")
            except Exception:
                current_url = ""
            try:
                page_text = page.evaluate(
                    "() => (document.body && document.body.innerText) || ''"
                )
            except Exception:
                page_text = ""
            lowered = (page_text or "").lower()
            for indicator in indicators:
                if not indicator:
                    continue
                if str(indicator).lower() in lowered:
                    self.log(
                        f"[Ban] Ban indicator matched: '{indicator}' "
                        f"(url: {current_url})"
                    )
                    return True
            if (
                login_redirect_is_ban
                and current_url
                and "/login" in current_url
                and "app.chitchat.gg" in current_url
            ):
                self.log(
                    f"[Ban] Unexpected login redirect treated as ban "
                    f"(url: {current_url})"
                )
                return True
        except Exception as error:
            self.log(f"[Ban] Detection check error (continuing): {error}")
        return False

    def _handle_ban(self, page):
        """Auto-close the browser and delete/ban the saved session on a ban.

        Returns True when the ban was handled (caller should stop the loop).
        Idempotent: a second call within the same session is a no-op.
        """
        if self._ban_handled:
            return True
        self._ban_handled = True
        self._ban_detected = True
        email = ""
        if isinstance(self.account, dict):
            email = str(self.account.get("email") or "")
        self.log("=" * 60)
        self.log(f"[Ban] ✗ Account banned: {email}")
        self.set_status(f"BANNED: {email}")
        self._emit_session("BANNED")
        cfg = self.ban_detection_cfg
        auto_delete = cfg.get("auto_delete_banned_session", True)
        auto_close = cfg.get("auto_close_on_ban", True)
        try:
            if auto_delete and email:
                if delete_account_session(self.account):
                    self.log(f"[Ban] ✓ Deleted banned session for {email}")
                else:
                    mark_session_banned(self.account)
                    self.log(
                        f"[Ban] Could not fully delete; flagged session as banned"
                    )
            elif email:
                mark_session_banned(self.account)
                self.log(f"[Ban] Flagged session as banned (auto-delete disabled)")
        except Exception as error:
            self.log(f"[Ban] Warning during session cleanup: {error}")
        # Live stock update after the ban is processed.
        self._log_account_stock(prefix="[Ban] ")
        if auto_close:
            self.log("[Ban] Auto-closing browser because account is banned")
            try:
                self.is_running = False
                self._stop_event.set()
            except Exception:
                pass
        return True

    # ------------------------------------------------------------------ #
    #  ACCOUNT STOCK / LIVE COUNT  (new, additive)
    # ------------------------------------------------------------------ #
    def _log_session_health(self, prefix="[Session] "):
        """Log the saved-session health summary (alive / blind / repairable)."""
        if not SESSION_GUARD_AVAILABLE:
            return
        if not self._session_cfg("log_health_on_start", True):
            return
        try:
            summary = session_guard.summarize()
            self.log(f"{prefix}{session_guard.format_summary(summary)}")
            if summary.get("blind"):
                self.log(
                    f"{prefix}{summary['blind']} session(s) are BLIND "
                    f"(expired/empty/corrupt). "
                    + (f"{summary['repairable']} can be repaired automatically with "
                       f"the credentials in accounts.txt." if summary.get("repairable")
                       else "add 'email:password' to accounts.txt so the bot can "
                            "log them in again.")
                )
            if self._session_cfg("prune_dead_sessions", False) and summary.get("blind"):
                result = session_guard.prune_dead_sessions(dry_run=False)
                if result.get("count"):
                    self.log(f"{prefix}moved {result['count']} dead session(s) to "
                             f"account_sessions/_dead/ (files kept)")
        except Exception as exc:
            self.log(f"{prefix}health summary failed: {exc}")

    def _log_account_stock(self, prefix="[Stock] "):
        """Log how many saved accounts are alive / banned / total."""
        try:
            summary = session_stock_summary()
            line = (
                f"{prefix}Accounts alive: {summary['alive']}  |  "
                f"banned: {summary['banned']}  |  total saved: {summary['total']}"
            )
            if summary.get("blind"):
                line += f"  |  blind: {summary['blind']}"
            self.log(line)
        except Exception as error:
            self.log(f"{prefix}Could not compute account stock: {error}")

    # ------------------------------------------------------------------ #
    #  UNIQUE-SITE VISIT  (replaces old persistent background tabs)
    # ------------------------------------------------------------------ #
    # The old "always-open YouTube/Reddit/Wikipedia tabs" behaviour is
    # removed.  Now the bot periodically opens ONE random site listed in
    # data/unique_sites.txt, reads/scolls it for a few seconds, closes the
    # tab, and returns to chitchat.gg.
    def _unique_sites_file(self):
        """Resolve unique_sites.txt (data dir, or cwd fallback)."""
        candidates = [
            os.path.join(_project_root(), 'data', 'unique_sites.txt'),
            os.path.join(os.getcwd(), 'unique_sites.txt'),
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        return candidates[0]

    def _load_unique_sites(self):
        """Read site URLs from unique_sites.txt (hot-reloaded every visit)."""
        path = self._unique_sites_file()
        sites = []
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if not re.match(r'^https?://', line, re.IGNORECASE):
                        line = f'https://{line}'
                    sites.append(line)
        except Exception as error:
            if self.background_tabs_cfg.get("enabled", True):
                self.log(f"[SiteVisit] Could not read unique_sites.txt: {error}")
            return []
        return sites

    def _open_background_tabs(self, context):
        """Old persistent background-tab behaviour is removed.

        Previously the bot kept 2-3 tabs (YouTube/Reddit/etc.) open for the
        whole session.  Now it only schedules periodic single-site visits
        driven by data/unique_sites.txt — open one random site, read/scroll
        3-6s, close it, and return to chitchat.gg.
        """
        self._close_background_tabs()
        self._schedule_background_tab_visit()

    def _schedule_background_tab_visit(self):
        """Schedule the next periodic visit to a background tab."""
        if not self.background_tabs_cfg.get("periodic_visit_enabled", True):
            self._background_tab_next_visit_at = None
            return
        minimum = self.background_tabs_cfg.get(
            "periodic_visit_interval_min_minutes", 3.0
        )
        maximum = self.background_tabs_cfg.get(
            "periodic_visit_interval_max_minutes", 7.0
        )
        if maximum <= 0:
            self._background_tab_next_visit_at = None
            return
        interval = random.uniform(minimum, maximum) * 60
        self._background_tab_next_visit_at = time.monotonic() + interval

    def _maybe_visit_unique_site(self, chat_page):
        """Periodically open ONE random site from unique_sites.txt.

        The site is opened in a fresh tab, read/scroll for a short dwelling
        (default 3-6s), then the tab is closed and focus returns to the
        chitchat tab.  No persistent background tabs are kept open.

        Called between chats.  Returns True if a visit happened, else False.
        """
        if (
            not self.background_tabs_cfg.get("enabled", True)
            or not self.background_tabs_cfg.get("periodic_visit_enabled", True)
        ):
            return False
        sites = self._load_unique_sites()
        if not sites:
            self._schedule_background_tab_visit()
            return False
        if self._background_tab_next_visit_at is None:
            return False
        if time.monotonic() < self._background_tab_next_visit_at:
            return False
        if not self.is_running:
            return False

        # Random site rotation: avoid visiting the same site twice in a row.
        last_visited = getattr(self, "_bg_last_visited_url", None)
        candidates = [s for s in sites if s != last_visited] or list(sites)
        url = random.choice(candidates)
        self._bg_last_visited_url = url

        dwell = random.uniform(
            max(0.0, float(self.background_tabs_cfg.get("periodic_visit_dwell_min_seconds", 3.0))),
            max(0.0, float(self.background_tabs_cfg.get("periodic_visit_dwell_max_seconds", 6.0))),
        )
        if dwell <= 0:
            dwell = 3.0
        self.log(
            f"[SiteVisit] Opening random site from unique_sites.txt: {url} "
            f"(read/scroll ~{dwell:.0f}s), then back to chitchat"
        )

        site_page = None
        try:
            site_page = chat_page.context.new_page()
            if not self.is_running:
                return False
            site_page.goto(url, timeout=20000, wait_until="domcontentloaded")
            if not self.is_running:
                return False
            from browser.human_behavior import human_browse_tab
            visited_ok = human_browse_tab(
                site_page,
                dwell_seconds=dwell,
                scroll=self.background_tabs_cfg.get("periodic_visit_scroll", True),
                hover_links=True,
                click_links=False,  # stay on the one chosen site, then return
                is_running_check=lambda: self.is_running,
            )
            if visited_ok:
                self.log(f"[SiteVisit] Read/scroll finished on {url}; returning to chat")
            else:
                self.log("[SiteVisit] Visit interrupted; returning to chat")
        except Exception as browse_error:
            self.log(f"[SiteVisit] Error visiting {url}: {browse_error}")
        finally:
            if site_page is not None:
                try:
                    site_page.close()
                except Exception:
                    pass
        try:
            chat_page.bring_to_front()
        except Exception:
            pass
        self._schedule_background_tab_visit()
        return True

    def _close_background_tabs(self):
        """Clear unique-site visit state (no persistent tabs to close)."""
        self._background_tab_pages = []
        self._bg_last_visited_url = None
        self._background_tab_next_visit_at = None

    # ------------------------------------------------------------------ #
    #  PERFORMANCE: resource blocking  (new, additive; opt-in via config)
    # ------------------------------------------------------------------ #
    def _apply_resource_blocking(self, context):
        """Block configured resource types to reduce CPU/memory on weak PCs.

        Only activates when ``performance.block_resource_types`` is non-empty
        (empty by default, so existing behaviour is unchanged).  Image/media/
        font blocking is safe for a chat site and dramatically lowers load.
        """
        blocked = self.performance_cfg.get("block_resource_types") or []
        if not blocked:
            return
        try:
            blocked_set = {str(b).strip().lower() for b in blocked if str(b).strip()}
            if not blocked_set:
                return

            def _route_handler(route):
                try:
                    request = route.request
                    resource_type = str(
                        getattr(request, "resource_type", "") or ""
                    ).lower()
                    if resource_type in blocked_set:
                        route.abort()
                        return
                except Exception:
                    pass
                try:
                    route.continue_()
                except Exception:
                    pass

            context.route("**/*", _route_handler)
            self.log(
                f"[Perf] Blocking resource types: {sorted(blocked_set)} "
                f"(reduces CPU/memory load)"
            )
        except Exception as error:
            self.log(f"[Perf] Could not apply resource blocking: {error}; continuing")

    # ------------------------------------------------------------------ #
    #  HUMAN BEHAVIOUR HELPERS  (new, additive; used by chat loop)
    # ------------------------------------------------------------------ #
    def _humanize_reaction_pause(self):
        """Reaction pause before a click/hover, configurable via human_behavior."""
        if not self.human_behavior.get("enabled", True):
            return
        minimum = self.human_behavior.get("reaction_pause_min_seconds", 0.25)
        maximum = self.human_behavior.get("reaction_pause_max_seconds", 0.85)
        time.sleep(random.uniform(minimum, maximum))

    def _humanize_read_reply(self):
        """Simulate reading a stranger's reply before responding."""
        if not self.human_behavior.get("enabled", True):
            return
        minimum = self.human_behavior.get("read_reply_min_seconds", 0.8)
        maximum = self.human_behavior.get("read_reply_max_seconds", 2.5)
        time.sleep(random.uniform(minimum, maximum))

    def _maybe_micro_idle(self):
        """Occasionally perform a tiny idle to look human while waiting."""
        if not self.human_behavior.get("enabled", True):
            return
        if random.random() < float(self.human_behavior.get("micro_idle_chance", 0.35)):
            minimum = self.human_behavior.get("micro_idle_min_seconds", 0.4)
            maximum = self.human_behavior.get("micro_idle_max_seconds", 1.8)
            time.sleep(random.uniform(minimum, maximum))

    def _get_chat_messages(self):
        """Select a template and return its filename and messages for one chat.
        
        Tracks used first messages to avoid repetition.
        """
        if self.message_template_selector:
            selected_template = self.message_template_selector()
        elif self.fixed_message_templates:
            selected_template = random.choice(self.fixed_message_templates)
        else:
            selected_template = self.fixed_messages

        if isinstance(selected_template, dict):
            template_filename = selected_template.get('filename')
            chat_messages = list(selected_template.get('messages') or [])
        else:
            # Backward compatibility for callers that still pass a bare list
            # of messages rather than a filename-bearing template record.
            template_filename = None
            chat_messages = list(selected_template or [])
        
        # Remove first message from list (we use it separately)
        first_message = None
        if chat_messages:
            first_message = chat_messages[0]
            chat_messages = chat_messages[1:]

        if self.shuffle_chat:
            random.shuffle(chat_messages)

        return selected_template, template_filename, chat_messages, first_message

    def _return_unused_message_template(self, selected_template, template_filename, chat_number):
        """Return a selected template when its chat sent none of its messages."""
        if selected_template is None or not self.message_template_returner:
            return False

        returned = self.message_template_returner(selected_template)
        if returned:
            file_label = template_filename or "legacy message template"
            self.log(
                f"[Chat #{chat_number}] No messages sent; returned message file "
                f"to unused pool: {file_label}"
            )
        return returned

    def _should_send_first_message(self):
        """Choose whether this conversation should start with our message."""
        if self.first_message_mode == "random":
            return random.choice((True, False))
        return self.first_message_mode == "true"

    def _get_reply_delay(self):
        """Return a fresh random delay for the next reply."""
        return random.uniform(self.reply_delay_min, self.reply_delay_max)

    def _typing_delay_for(self, text):
        """Typing simulator — extra pre-send pause scaled by reply length.

        Short replies send fast, long replies take proportionally longer,
        capped so a lengthy line never stalls the conversation. Config knobs
        live in ``human_behavior`` (typing_ms_per_char / typing_max_seconds).
        """
        try:
            per_char = float(self.human_behavior.get("typing_ms_per_char", 20.0))
            ceiling = float(self.human_behavior.get("typing_max_seconds", 2.5))
        except (TypeError, ValueError):
            per_char, ceiling = 20.0, 2.5
        if per_char <= 0:
            return 0.0
        return min(max(len(text) * per_char / 1000.0, 0.0), max(ceiling, 0.0))

    def _get_first_message_delay(self):
        """Return a fresh random delay before sending the first message."""
        return random.uniform(
            self.first_message_delay_min,
            self.first_message_delay_max,
        )

    def _get_chat_snapchat_id(self):
        """Select the Snapchat username for one chat."""
        if self.snapchat_id_selector:
            return self.snapchat_id_selector()
        return None

    @staticmethod
    def _process_message(message, chat_snapchat_id=None):
        """Render a message template and substitute the chat username."""
        processed_message = expand_message_template(message)
        return processed_message.replace(
            '%username%',
            chat_snapchat_id if chat_snapchat_id else 'username',
        )
    
    def log(self, message):
        """Log a message using the callback with timestamp"""
        if self.log_callback:
            import datetime
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            timestamped_message = f"[{timestamp}] {message}"
            self.log_callback(timestamped_message)
    
    def set_status(self, status):
        """Update status using the callback"""
        if self.status_callback:
            self.status_callback(status)
    
    def get_ip_geolocation(self):
        """
        Detect IP geolocation, timezone, and language settings.
        """
        import requests
        
        proxy_info = ""
        if self.proxy_config:
            proxy_info = " (via proxy)"
        
        self.log(f"[IP Detection] Fetching IP geolocation{proxy_info}...")
        timezone_id = 'UTC'
        lang_tag = 'en-US,en'
        geolocation = None
        country = None
        country_code = None
        
        # Prepare proxy for requests library if configured
        proxies = None
        if self.proxy_config:
            proxy_url = self.proxy_config['server']
            # Add scheme only if not already present
            if not proxy_url.startswith(('http://', 'https://', 'socks5://')):
                proxy_url = f"http://{proxy_url}"
            if 'username' in self.proxy_config and 'password' in self.proxy_config:
                proxy_auth = f"{self.proxy_config['username']}:{self.proxy_config['password']}"
                proxy_url = f"http://{proxy_auth}@{proxy_url.split('://', 1)[-1]}"
            
            proxies = {
                'http': proxy_url,
                'https': proxy_url
            }
            self.log(f"[IP Detection] Using proxy: {self.proxy_config['server']}")
        
        try:
            self.log("[IP Detection] Trying API (ip-api.com)...")
            r = requests.get('http://ip-api.com/json', timeout=10, proxies=proxies)
            if r.ok:
                j = r.json()
                country = j.get('country')
                country_code = (j.get('countryCode') or '').upper()
                timezone_id = j.get('timezone') or timezone_id
                lat = j.get('lat')
                lon = j.get('lon')
                detected_ip = j.get('query', 'Unknown')
                if lat is not None and lon is not None:
                    geolocation = {'latitude': float(lat), 'longitude': float(lon)}
                self.log(f"[IP Detection] SUCCESS - IP: {detected_ip}")
                self.log(f"[IP Detection] Location: {country or 'Unknown'} ({country_code or '??'})")
        except Exception as e:
            self.log(f"[IP Detection] API error: {e}")
        
        self.log(f"[Timezone] Using: {timezone_id}")
        
        return {
            'timezone_id': timezone_id,
            'lang_tag': lang_tag,
            'geolocation': geolocation,
            'country': country,
            'country_code': country_code
        }
    
    def setup_asyncio_error_handler(self):
        """Suppress expected compatibility-layer errors during browser shutdown."""
        def handle_asyncio_exception(loop, context):
            exception = context.get('exception')
            if exception:
                exc_str = str(exception).lower()
                exc_type = type(exception).__name__.lower()
                if any(x in exc_str for x in ['greenlet', 'target', 'closed', 'connection', 'cannot switch']):
                    return
                if 'greenlet' in exc_type:
                    return
            message = context.get('message', '').lower()
            if any(x in message for x in ['greenlet', 'target', 'closed', 'connection']):
                return
            if 'message' in context and context['message']:
                self.log(f"[Asyncio] {context['message']}")
        
        try:
            loop = asyncio.get_event_loop()
            loop.set_exception_handler(handle_asyncio_exception)
        except:
            pass

    def _humanize_pause(self, minimum=0.8, maximum=1.8):
        """Pause for a variable reaction time before a browser interaction."""
        time.sleep(random.uniform(minimum, maximum))

    def _wait_while_running(self, seconds):
        """Wait for a duration while allowing stop() to wake the worker."""
        seconds = max(0.0, float(seconds))
        if seconds <= 0:
            return self.is_running

        if self._stop_event.wait(seconds):
            return False
        return self.is_running

    def _wait_for_chat_end_confirmation(self, page, timeout=5.0):
        """Wait until the page reports that the skipped chat has ended."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self.is_running:
            if self.check_if_disconnected(page):
                self.log("[Chat] ✓ Skip confirmed")
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self._stop_event.wait(min(0.25, remaining)):
                return False

        self.log("[Chat] ⚠ Skip confirmation was not detected; continuing")
        return False

    def _wait_before_new_chat(self, reason="chat ended"):
        """Pause after a finished chat before looking for START."""
        delay = random.uniform(
            self.chat_timing["new_chat_delay_min_seconds"],
            self.chat_timing["new_chat_delay_max_seconds"],
        )
        self.log(
            f"[Chat] Waiting {delay:.2f}s after {reason} before starting next chat"
        )
        return self._wait_while_running(delay)

    def _schedule_next_rest(self):
        """Schedule the next rest from the end of the current rest period."""
        minimum = self.chat_timing["rest_interval_min_minutes"]
        maximum = self.chat_timing["rest_interval_max_minutes"]
        if maximum <= 0:
            self._next_rest_at = None
            return

        interval_minutes = random.uniform(minimum, maximum)
        self._next_rest_at = time.monotonic() + (interval_minutes * 60)

    def _maybe_take_rest(self, chat_number=None):
        """Rest after the configured active interval, without interrupting a chat."""
        if not self.is_running:
            return False
        if self._next_rest_at is None:
            return True
        if time.monotonic() < self._next_rest_at:
            return True

        rest_minutes = random.uniform(
            self.chat_timing["rest_duration_min_minutes"],
            self.chat_timing["rest_duration_max_minutes"],
        )
        chat_label = f" after chat #{chat_number}" if chat_number is not None else ""
        self.log(
            f"[Chat Bot] Taking a {rest_minutes:.2f}-minute rest{chat_label}"
        )
        if not self._wait_while_running(rest_minutes * 60):
            return False

        self.log("[Chat Bot] Rest complete; resuming chat")
        self._schedule_next_rest()
        return self.is_running

    def _humanize_click(self, locator, *, timeout=None, no_wait_after=None):
        """Click a Playwright locator after a human-like reaction pause.

        Uses Bezier curve mouse movement for more human-like behavior.
        """
        hover_options = {}
        if timeout is not None:
            hover_options['timeout'] = timeout
        
        # Get element position for Bezier mouse movement
        try:
            from browser.human_behavior import human_mouse_move
            box = locator.bounding_box(timeout=3000)
            if box:
                # Move to element center with Bezier curve
                target_x = int(box['x'] + box['width'] / 2)
                target_y = int(box['y'] + box['height'] / 2)
                human_mouse_move(locator.page, target_x, target_y, steps=20)
        except Exception:
            pass
        
        locator.hover(**hover_options)
        self._humanize_pause()
        click_options = {}
        if timeout is not None:
            click_options['timeout'] = timeout
        if no_wait_after is not None:
            click_options['no_wait_after'] = no_wait_after
        return locator.click(**click_options)

    def _find_visible_button(self, scope, label, *, exact=False):
        """Find an enabled, visible button by its rendered text."""
        buttons = scope.locator('button')
        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                if not button.is_visible(timeout=500) or not button.is_enabled():
                    continue
                button_label = button.inner_text(timeout=500).strip()
            except Exception:
                continue

            if exact:
                matches = button_label.casefold() == label.casefold()
            else:
                matches = label.casefold() in button_label.casefold()
            if matches:
                return button
        return None

    def _load_pre_homepage_urls(self):
        """Load URLs to visit in disposable tabs before opening the homepage."""
        candidate_paths = [os.path.join(os.getcwd(), 'urls.txt')]

        if getattr(sys, 'frozen', False):
            candidate_paths.append(
                os.path.join(
                    os.path.dirname(os.path.abspath(sys.executable)),
                    'urls.txt',
                )
            )
            meipass = getattr(sys, '_MEIPASS', None)
            if meipass:
                candidate_paths.append(os.path.join(meipass, 'urls.txt'))

        candidate_paths.append(
            os.path.join(
                _project_root(),
                'data',
                'urls.txt',
            )
        )

        urls_file = next(
            (path for path in candidate_paths if os.path.isfile(path)),
            None,
        )
        if urls_file is None:
            self.log(
                '[Pre-homepage] urls.txt not found; using the fallback website list'
            )
            return list(self.PRE_HOMEPAGE_FALLBACK_URLS)

        try:
            with open(urls_file, 'r', encoding='utf-8') as handle:
                urls = []
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if not re.match(r'^https?://', line, re.IGNORECASE):
                        line = f'https://{line}'
                    urls.append(line)

            if urls:
                self.log(f'[Pre-homepage] Loaded {len(urls)} URL(s) from {urls_file}')
                return urls

            self.log(
                f'[Pre-homepage] No usable URLs found in {urls_file}; '
                'using the fallback website list'
            )
        except Exception as error:
            self.log(
                f'[Pre-homepage] Could not read {urls_file}: {error}; '
                'using the fallback website list'
            )

        return list(self.PRE_HOMEPAGE_FALLBACK_URLS)

    def _visit_pre_homepage_tabs(self, context):
        """Optionally visit disposable URLs before the main session.

        IMPORTANT: this feature is disabled by default.  The previous build
        loaded ``website_visits.pre_homepage_enabled`` but did not actually
        check it here, so even ``false`` in config.json still opened the
        fallback URLs.  With Camoufox/Firefox those pages can appear as
        separate top-level windows, which looked like multiple browsers even
        when Threads=1.
        """
        if not self.website_visits_cfg.get("pre_homepage_enabled", False):
            self.log("[Pre-homepage] Disabled — no extra pages/windows will be opened.")
            return

        urls = self._load_pre_homepage_urls()
        tab_count = int(self.website_visits_cfg.get("pre_homepage_tab_count", 0) or 0)
        if tab_count > 0:
            urls = urls[:tab_count]
        if not urls:
            self.log("[Pre-homepage] Enabled but no URLs configured; skipping.")
            return
        random.shuffle(urls)
        self.log(f'[Pre-homepage] Visit order: {urls}')

        tabs = []
        for index, url in enumerate(urls, start=1):
            try:
                tab = context.new_page()
                tabs.append((tab, url))
                self.log(f'[Pre-homepage] Opened tab {index}: {url}')
            except Exception as error:
                self.log(
                    f'[Pre-homepage] Could not open tab {index} for {url}: '
                    f'{error}; continuing'
                )

        for index, (tab, url) in enumerate(tabs, start=1):
            try:
                self.log(f'[Pre-homepage] Visiting tab {index}: {url}')
                tab.goto(
                    url,
                    timeout=self.PRE_HOMEPAGE_TAB_TIMEOUT_MS,
                    wait_until='domcontentloaded',
                )
                self.log(f'[Pre-homepage] Tab {index} loaded: {tab.url}')
            except Exception as error:
                self.log(
                    f'[Pre-homepage] Tab {index} failed for {url}: {error}; '
                    'continuing'
                )

        self.log(f'[Pre-homepage] Closing {len(tabs)} disposable tab(s)')
        for index, (tab, url) in enumerate(tabs, start=1):
            try:
                tab.close()
                self.log(f'[Pre-homepage] Closed tab {index}: {url}')
            except Exception as error:
                self.log(
                    f'[Pre-homepage] Could not close tab {index}: {error}; '
                    'continuing'
                )

    def _open_flow_from_homepage(self, page, flow_name, destination_url):
        """Open a Chitchat flow through the public homepage link."""
        self.log(f"[{flow_name}] Visiting the public Chitchat homepage...")

        try:
            # First try direct navigation to login page
            try:
                self.log(f"[{flow_name}] Trying direct navigation to {destination_url}...")
                page.goto(
                    destination_url,
                    timeout=30000,
                    wait_until='domcontentloaded',
                )
                self.log(f"[{flow_name}] Direct navigation successful")
                time.sleep(2)
                return True
            except Exception as direct_error:
                self.log(f"[{flow_name}] Direct navigation failed: {direct_error}")

            # Fallback: Try homepage link
            try:
                page.goto(
                    self.PUBLIC_HOMEPAGE_URL,
                    timeout=30000,
                    wait_until='domcontentloaded',
                )
                self.log(f"[{flow_name}] Homepage DOM content loaded")
            except Exception as nav_error:
                self.log(f"[{flow_name}] Homepage navigation with domcontentloaded failed: {nav_error}")
                try:
                    page.goto(
                        self.PUBLIC_HOMEPAGE_URL,
                        timeout=30000,
                        wait_until='load',
                    )
                except Exception:
                    page.goto(
                        self.PUBLIC_HOMEPAGE_URL,
                        timeout=30000,
                        wait_until='commit',
                    )

            links = page.locator(f'a[href="{destination_url}"]')
            link = None
            deadline = time.time() + 10000 / 1000  # Reduced from 30s to 10s
            while time.time() < deadline:
                for index in range(links.count()):
                    candidate = links.nth(index)
                    if candidate.is_visible():
                        link = candidate
                        break
                if link is not None:
                    break
                time.sleep(0.25)

            if link is None:
                # Try clicking any Login/Sign in button
                try:
                    login_btn = page.locator('button:has-text("Login"), button:has-text("Sign in"), a:has-text("Login"), a:has-text("Sign in")').first
                    if login_btn.is_visible(timeout=5000):
                        self._humanize_click(login_btn, timeout=10000, no_wait_after=True)
                        time.sleep(3)
                        if page.url != self.PUBLIC_HOMEPAGE_URL:
                            return True
                except:
                    pass
                
                # Last resort: direct navigation
                self.log(f"[{flow_name}] No link found, navigating directly to {destination_url}")
                page.goto(destination_url, timeout=30000, wait_until='domcontentloaded')
                time.sleep(2)
                return True

            self.log(f"[{flow_name}] Clicking homepage link: {destination_url}")
            self._humanize_click(link, timeout=10000, no_wait_after=True)

            try:
                page.wait_for_url(
                    f'{destination_url}**',
                    timeout=30000,
                    wait_until='domcontentloaded',
                )
            except Exception:
                try:
                    page.wait_for_load_state('domcontentloaded', timeout=5000)
                except Exception:
                    pass

            if not page.url.startswith(destination_url):
                # Direct navigation as fallback
                self.log(f"[{flow_name}] Link didn't work, navigating directly")
                page.goto(destination_url, timeout=30000, wait_until='domcontentloaded')
                time.sleep(2)

            self.log(f"[{flow_name}] Designated page URL: {page.url}")
            return True
        except Exception as error:
            self.log(f"[{flow_name}] ✗ Could not open the designated page: {error}")
            # Try direct navigation as last resort
            try:
                self.log(f"[{flow_name}] Last resort: direct navigation to {destination_url}")
                page.goto(destination_url, timeout=30000, wait_until='domcontentloaded')
                time.sleep(2)
                return True
            except:
                self._log_login_page_diagnostics(page)
                return False

    def _type_login_value(self, field, value, field_name):
        """Enter a login value using keyboard events so the app sees real typing."""
        self.log(f"[Login] Typing {field_name}...")
        self._humanize_click(field)
        field.fill('')
        # Use human-behavior typing with Gaussian per-character delays (50-200ms)
        # for a more natural keystroke rhythm than a fixed 35ms delay.
        try:
            from browser.human_behavior import type_human
            type_human(field, value, min_delay_ms=50, max_delay_ms=200)
        except Exception:
            field.type(value, delay=35)

    def _open_email_password_login(self, page):
        """Select the email/password login method on the login landing page."""
        self.log("[Login Step 2/6] Clicking 'Email and Password'...")
        try:
            button = page.locator(
                'button[type="button"]:has-text("Email and Password")'
            ).first
            button.wait_for(state='visible', timeout=30000)
            self._humanize_click(button, timeout=10000)
            self.log("[Login] ✓ Email and Password selected")
            return True
        except Exception as error:
            self.log(f"[Login] ✗ Could not select Email and Password: {error}")
            self._log_login_page_diagnostics(page)
            return False

    def login_with_account(self, page):
        """
        Login to Chitchat.gg using email/password.
        
        Args:
            page: Playwright page object
            
        Returns:
            Boolean indicating success
        """
        if not self.account:
            self.log("[Login] ✗ No account credentials provided")
            return False
        
        email = self.account.get('email', '')
        password = self.account.get('password', '')
        
        if not email or not password:
            self.log("[Login] ✗ Email or password is empty")
            return False
        
        try:
            # Step 1: Visit the public homepage and use its Login link.
            self.log("[Login Step 1/6] Visiting homepage and clicking Login...")
            if not self._open_flow_from_homepage(
                page,
                'Login',
                self.LOGIN_ENTRY_URL,
            ):
                return False

            if not self._open_email_password_login(page):
                return False
            
            # Step 3: Type email
            self.log(f"[Login Step 3/6] Entering email: {email[:3]}***{email[-10:] if len(email) > 13 else '***'}")
            email_input = self._wait_for_login_field(
                page,
                'input[name="email"]:visible, input[type="email"]:visible, '
                'input[autocomplete~="email"]:visible, input[placeholder*="email" i]:visible',
                'email',
            )
            if not email_input:
                return False
            self._type_login_value(email_input, email, 'email')
            self.log("[Login] ✓ Email entered")
            
            # Step 4: Type password
            self.log("[Login Step 4/6] Entering password...")
            password_input = self._wait_for_login_field(
                page,
                'input[name="password"]:visible, input[type="password"]:visible, '
                'input[autocomplete="password"]:visible, input[placeholder*="password" i]:visible',
                'password',
            )
            if not password_input:
                return False
            self._type_login_value(password_input, password, 'password')
            self.log("[Login] ✓ Password entered")

            # The login page mounts hCaptcha only after the credentials make the
            # form dirty.  Do not submit while that asynchronous widget is still
            # loading: the server requires its token.
            self.log("[Login Step 5/6] Waiting for captcha to load...")
            if not self._wait_for_captcha_widget(page, max_wait=30000):
                self.log("[Login] ✗ Captcha widget did not load; login was not submitted")
                self._log_login_page_diagnostics(page)
                return False

            self.log("[Login] ⚠ Captcha detected! Waiting for manual verification...")
            self.set_status("Waiting for captcha...")
            
            # NOTIFY USER: Sound + Windows toast
            notify_captcha(
                "CAPTCHA ALERT - LOGIN",
                "Solve captcha to continue login!",
                play_sound=True,
            )

            # Wait for captcha to be solved.
            captcha_solved = self._wait_for_captcha_solved(page, max_wait=300)

            if not captcha_solved:
                self.log("[Login] ✗ Captcha not solved in time")
                return False

            self.log("[Login] ✓ Captcha solved!")
            
            # Click login button
            self.log("[Login] Clicking login button...")
            
            login_clicked = False
            try:
                # Find the Login button (submit button with text "Login")
                login_button = page.locator('button[type="submit"]:has-text("Login")').first
                if login_button.is_visible(timeout=3000):
                    self._humanize_click(login_button)
                    login_clicked = True
                    self.log("[Login] ✓ Login button clicked")
            except Exception as e:
                self.log(f"[Login] Could not click login button: {e}")
            
            if not login_clicked:
                try:
                    # Fallback: Any submit button in form
                    login_button = page.locator('form button[type="submit"]').first
                    if login_button.is_visible(timeout=2000):
                        self._humanize_click(login_button)
                        login_clicked = True
                        self.log("[Login] ✓ Submit button clicked")
                except Exception:
                    pass
            
            if not login_clicked:
                # Try pressing Enter
                self._humanize_pause()
                page.keyboard.press('Enter')
                self.log("[Login] Pressed Enter to submit")
            
            # Wait for login to process
            time.sleep(3)
            
            # Check if login was successful (should redirect away from login page)
            current_url = page.url
            self.log(f"[Login] Post-login URL: {current_url}")
            
            if 'login' in current_url.lower():
                # Still on login page - check for error messages
                try:
                    error_text = page.evaluate('''
                        () => {
                            const errorElements = document.querySelectorAll('.text-destructive, .error, [role="alert"]');
                            for (let el of errorElements) {
                                const text = el.innerText.trim();
                                if (text) return text;
                            }
                            return null;
                        }
                    ''')
                    if error_text:
                        self.log(f"[Login] ✗ Login error: {error_text}")
                except Exception:
                    pass
                
                self.log("[Login] ✗ Login may have failed - still on login page")
                # Give it more time and check again
                time.sleep(3)
                current_url = page.url
                if 'login' in current_url.lower():
                    return False
            
            self.log("[Login] ✓ Login successful!")
            return True
            
        except Exception as e:
            self.log(f"[Login] Error during login: {e}")
            import traceback
            self.log(f"[Login] Traceback: {traceback.format_exc()}")
            return False

    def _wait_for_login_field(self, page, selector, field_name, timeout=45000):
        """Wait for a dynamically rendered login field and return its locator."""
        self.log(f"[Login] Waiting for {field_name} field (up to {timeout // 1000}s)...")
        field = page.locator(selector).first
        try:
            field.wait_for(state='visible', timeout=timeout)
            return field
        except Exception as error:
            self.log(f"[Login] ✗ {field_name.capitalize()} field did not render: {error}")
            self._log_login_page_diagnostics(page)
            return None

    def _log_login_page_diagnostics(self, page):
        """Log enough page state to distinguish a slow app from a blocked/broken page."""
        try:
            diagnostics = page.evaluate('''() => ({
                readyState: document.readyState,
                title: document.title,
                inputs: Array.from(document.querySelectorAll('input')).map(input => ({
                    type: input.type,
                    name: input.name,
                    placeholder: input.placeholder,
                    visible: !!(input.offsetWidth || input.offsetHeight || input.getClientRects().length),
                })),
                bodyText: (document.body?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 300),
            })''')
            self.log(f"[Login] Render diagnostics: {diagnostics}")
        except Exception as error:
            self.log(f"[Login] Could not collect render diagnostics: {error}")
    
    def _check_captcha_present(self, page):
        """Check if captcha/Cloudflare challenge is present on the page"""
        try:
            captcha_status = page.evaluate('''
                () => {
                    // Check for hCaptcha iframe
                    const hcaptchaIframe = document.querySelector('iframe[title*="captcha" i], iframe[title*="hCaptcha" i], iframe[src*="hcaptcha"]');
                    if (hcaptchaIframe) {
                        const rect = hcaptchaIframe.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            return { present: true, type: "hcaptcha" };
                        }
                    }
                    
                    // Check for Cloudflare challenge
                    const cloudflareFrame = document.querySelector('iframe[src*="challenges.cloudflare"], iframe[title*="challenge" i]');
                    if (cloudflareFrame) {
                        const rect = cloudflareFrame.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            return { present: true, type: "cloudflare" };
                        }
                    }
                    
                    // Check for hCaptcha container div
                    const hcaptchaDiv = document.querySelector('[data-hcaptcha-widget-id], .h-captcha');
                    if (hcaptchaDiv) {
                        return { present: true, type: "hcaptcha" };
                    }
                    
                    // Check for Cloudflare turnstile
                    const turnstile = document.querySelector('[class*="cf-turnstile"], [data-sitekey]');
                    if (turnstile) {
                        return { present: true, type: "cloudflare_turnstile" };
                    }
                    
                    return { present: false, type: null };
                }
            ''')
            return captcha_status
        except Exception:
            return {"present": False, "type": None}

    def _wait_for_captcha_widget(self, page, max_wait=30000):
        """Wait until the asynchronously mounted CAPTCHA/Cloudflare challenge is visible."""
        self.log(f"[Login] Waiting for captcha/cloudflare widget (up to {max_wait // 1000}s)...")
        
        # Check immediately if already present
        captcha_check = self._check_captcha_present(page)
        if captcha_check.get("present"):
            captcha_type = captcha_check.get("type", "captcha")
            if "cloudflare" in str(captcha_type).lower():
                notify_captcha(
                    "CLOUDFLARE CHALLENGE",
                    "Solve Cloudflare to continue!",
                    play_sound=True,
                )
            else:
                notify_captcha(
                    "CAPTCHA DETECTED",
                    "Solve captcha to continue!",
                    play_sound=True,
                )
        
        try:
            page.wait_for_function('''() => {
                // Check for captcha iframes
                const iframes = document.querySelectorAll(
                    'iframe[title*="captcha" i], iframe[src*="hcaptcha" i], '
                    + 'iframe[src*="recaptcha" i], iframe[src*="challenges.cloudflare" i], '
                    + 'iframe[src*="funcaptcha" i], iframe[title*="challenge" i]'
                );
                const iframeVisible = Array.from(iframes).some(widget => {
                    const style = window.getComputedStyle(widget);
                    const rect = widget.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none'
                        && rect.width > 0 && rect.height > 0;
                });
                
                // Also check for captcha divs/widgets
                const captchaDivs = document.querySelectorAll(
                    '[class*="captcha" i], [id*="captcha" i], '
                    + '[class*="challenge" i], [data-callback]'
                );
                const divVisible = Array.from(captchaDivs).some(div => {
                    const style = window.getComputedStyle(div);
                    return style.visibility !== 'hidden' && style.display !== 'none'
                        && div.offsetHeight > 0;
                });
                
                return iframeVisible || divVisible;
            }''', timeout=max_wait)
            return True
        except Exception as error:
            self.log(f"[Login] Captcha widget was not ready: {error}")
            return False
    
    def _wait_for_captcha_solved(self, page, max_wait=300):
        """
        Wait for captcha to be solved.
        
        Args:
            page: Playwright page object
            max_wait: Maximum wait time in seconds
            
        Returns:
            Boolean indicating if captcha was solved
        """
        start_time = time.time()
        last_log_time = 0
        
        while self.is_running:
            elapsed = int(time.time() - start_time)
            
            # Log progress every 10 seconds
            if elapsed - last_log_time >= 10:
                self.log(f"[Captcha] Waiting for verification... ({elapsed}s)")
                last_log_time = elapsed
            
            # Check timeout
            if elapsed > max_wait:
                return False
            
            # Check if captcha is solved
            try:
                captcha_status = page.evaluate('''
                    () => {
                        // Method 1: Check if hCaptcha response textarea has value
                        const hcaptchaResponse = document.querySelector('textarea[name="h-captcha-response"]');
                        if (hcaptchaResponse && hcaptchaResponse.value && hcaptchaResponse.value.length > 0) {
                            return { solved: true, method: "hcaptcha-response" };
                        }
                        
                        // Method 2: Check g-recaptcha-response
                        const gRecaptchaResponse = document.querySelector('textarea[name="g-recaptcha-response"]');
                        if (gRecaptchaResponse && gRecaptchaResponse.value && gRecaptchaResponse.value.length > 0) {
                            return { solved: true, method: "grecaptcha-response" };
                        }
                        
                        // Method 3: Check if captcha iframe shows checkmark (success state)
                        const hcaptchaIframe = document.querySelector('iframe[title*="hCaptcha" i]');
                        if (hcaptchaIframe) {
                            // Check parent container for success state
                            const container = hcaptchaIframe.closest('div');
                            if (container && container.getAttribute('data-hcaptcha-response')) {
                                return { solved: true, method: "container-response" };
                            }
                        }
                        
                        return { solved: false };
                    }
                ''')
                
                if captcha_status.get('solved'):
                    return True
                    
            except Exception:
                pass
            
            time.sleep(1)
        
        return False
    
    def create_new_account_session(self, page):
        """Run the unauthenticated new-session flow retained from the main branch."""
        try:
            self.log("[Create Step 1/5] Visiting homepage and clicking Text Chat...")
            if not self._open_flow_from_homepage(
                page,
                'Create',
                self.NEW_SESSION_ENTRY_URL,
            ):
                return False

            self.log("[Create Step 2/5] Waiting for gender selection to render...")
            dialog = page.locator('div[role="dialog"]').first
            try:
                dialog.wait_for(state='visible', timeout=45000)
            except Exception as error:
                self.log(f"[Create] ✗ Gender dialog did not render: {error}")
                self._log_login_page_diagnostics(page)
                return False

            self.log("[Create Step 2/5] Selecting Female gender...")
            selected = False
            selectors = (
                'label:has-text("Female")',
                'button[role="radio"][value="F"]',
                'input[type="radio"][value="F"]',
            )
            for selector in selectors:
                try:
                    option = dialog.locator(selector).last
                    option.wait_for(state='visible', timeout=10000)
                    self._humanize_click(option, timeout=5000)
                    selected = True
                    break
                except Exception:
                    continue
            if not selected:
                self.log("[Create] ✗ Could not select Female gender after the dialog loaded")
                self._log_login_page_diagnostics(page)
                return False

            self.log("[Create Step 3/5] Accepting the start dialog...")
            try:
                agree_button = page.locator('button:has-text("I AGREE, LET\'S GO!")').first
                self._humanize_click(agree_button, timeout=10000)
            except Exception:
                submit_button = page.locator('div[role="dialog"] button[type="submit"]').first
                self._humanize_click(submit_button, timeout=10000)

            self.log("[Create Step 4/5] Waiting for manual captcha completion...")
            self.set_status("Waiting for captcha...")
            
            # NOTIFY USER: Sound + Windows toast
            notify_captcha(
                "CAPTCHA ALERT - ACCOUNT",
                "Solve captcha to create account!",
                play_sound=True,
            )
            
            deadline = time.time() + 300
            while self.is_running and time.time() < deadline:
                solved = page.evaluate('''() => {
                    const response = document.querySelector('textarea[name="h-captcha-response"]');
                    if (response && response.value) return true;
                    return !location.pathname.includes('/start/new');
                }''')
                if solved:
                    break
                time.sleep(1)
            else:
                self.log("[Create] ✗ Captcha was not completed in time")
                return False

            self.log("[Create Step 5/5] Starting text chat...")
            for _ in range(15):
                try:
                    button = page.locator('button:has-text("Start Text Chat")').first
                    if button.is_visible(timeout=1000):
                        self._humanize_click(button, timeout=5000)
                        self.log("[Create] ✓ New Chitchat session started")
                        return True
                except Exception:
                    pass
                try:
                    self._humanize_pause()
                    page.keyboard.press('Escape')
                except Exception:
                    pass
                time.sleep(1)
            self.log("[Create] ✗ Start Text Chat button was not available")
            return False
        except Exception as e:
            self.log(f"[Create] Error starting new session: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  Phase 13 — session auto-load, verification, blind repair, keep-alive
    # ------------------------------------------------------------------ #
    def _session_cfg(self, key, default):
        """Read a ``session_management`` option with a safe fallback."""
        try:
            return self.session_management_cfg.get(key, default)
        except Exception:
            return default

    def _session_verified(self, page):
        """Run the auth probe: is this page really an authenticated app view?"""
        if not SESSION_GUARD_AVAILABLE:
            # Old behaviour: only the URL is inspected.
            try:
                url = str(page.url or "")
            except Exception:
                url = ""
            if "login" in url.lower():
                self._session_probe_reason = "redirected to the login page"
                return False
            return True
        try:
            status, reason, _probe = session_guard.verify_page(page, wait_ms=1200)
        except Exception as exc:
            status, reason = None, f"probe error: {type(exc).__name__}: {exc}"
        self._session_probe_reason = reason
        if status is None:
            # Cannot tell yet (still loading / SPA): retry once after a pause
            try:
                status, reason, _probe = session_guard.verify_page(page, wait_ms=2500)
                self._session_probe_reason = reason
            except Exception:
                status = None
        if status is None:
            self.log(f"[Session] probe inconclusive ({reason}) — continuing carefully")
            return True
        return bool(status)

    def _session_blind_report(self, reason=""):
        """Record + log a blind session (grep-able 'BLIND SESSION')."""
        self._session_blind = True
        self._session_repair_reason = reason or self._session_probe_reason or "unverified"
        health = {}
        if SESSION_GUARD_AVAILABLE:
            try:
                health = session_guard.session_health(self.account)
            except Exception:
                health = {}
        self.log(
            f"[Session] BLIND SESSION detected for "
            f"{self._account_label()}: {self._session_repair_reason}"
        )
        if health:
            self.log(f"[Session] saved state: state={health.get('state')} "
                     f"cookies={health.get('cookies', 0)} "
                     f"live={health.get('live_auth_cookies', 0)} "
                     f"days_left={health.get('days_left')}")
        # Persist the flag so the GUI/tools can see it without a launch.
        try:
            record_session_health(self.account, health or None, blind=True,
                                  blind_reason=self._session_repair_reason)
            if isinstance(self.account, dict):
                self.account["blind"] = True
                self.account["health"] = health or {}
        except Exception:
            pass

    def _account_label(self):
        if isinstance(self.account, dict):
            return str(self.account.get("email") or self.account.get("session_key") or "account")
        return "account"

    def _session_credentials_available(self):
        """Attach matching accounts.txt credentials (in memory) if we can."""
        if not SESSION_GUARD_AVAILABLE or not isinstance(self.account, dict):
            return False
        try:
            session_guard.attach_credentials(self.account)
        except Exception:
            pass
        return bool(self.account.get("password"))

    def _repair_blind_session(self, page):
        """Blind session + stored credentials → log in again and re-save.

        This is the "acc auto browser e load hoye chat korte pare" path: instead
        of giving up on a dead session, the account is logged in with the
        password from ``accounts.txt`` and the fresh cookies are written back.
        """
        if self._session_repair_attempted:
            return False
        self._session_repair_attempted = True

        if not self._session_cfg("auto_repair_blind_sessions", True) or \
                not self._session_cfg("blind_fallback_to_login", True):
            self.log("[Session] auto repair is disabled in config.json — skipping")
            return False
        if not self._session_credentials_available():
            self.log(
                f"[Session] ✗ BLIND SESSION and no credentials for "
                f"{self._account_label()} in accounts.txt — cannot repair."
            )
            self.log("[Session] add 'email:password' for this account to accounts.txt "
                     "to let the bot re-login automatically")
            return False

        self.log(f"[Session] repairing {self._account_label()} "
                 f"by logging in with stored credentials...")
        try:
            if not self.login_with_account(page):
                self.log("[Session] ✗ repair login failed — account needs manual attention")
                return False
        except Exception as exc:
            self.log(f"[Session] ✗ repair login error: {type(exc).__name__}: {exc}")
            return False

        self._session_blind = False
        self.log("[Session] ✓ BLIND SESSION repaired — fresh authenticated session")
        try:
            # Save the fresh cookies FIRST, then mark the session repaired, so
            # the recorded health describes the new state (not the dead one).
            self._save_successful_account_session(page)
            if SESSION_GUARD_AVAILABLE:
                session_guard.record_repaired(self.account,
                                              repaired_reason=self._session_repair_reason)
        except Exception as exc:
            self.log(f"[Session] could not persist repaired session: {exc}")
        return True

    def _maybe_refresh_session(self, page, reason="keep-alive"):
        """Write the live cookies back to disk (throttled session keep-alive)."""
        if not isinstance(self.account, dict) or not self.account.get("email"):
            return False
        if self.account_mode not in ("restore", "login", "create"):
            return False
        if not self._session_cfg("session_refresh_each_chat", True):
            return False
        minutes = float(self._session_cfg("session_refresh_minutes", 10) or 0)
        try:
            if SESSION_GUARD_AVAILABLE and not session_guard.should_refresh(self.account, minutes):
                return False
        except Exception:
            pass
        try:
            path = refresh_storage_state(self.account, self.context, page=page, reason=reason)
            if path:
                self._last_session_refresh = time.time()
                if SESSION_GUARD_AVAILABLE:
                    health = session_guard.session_health(self.account)
                    if health.get("blind"):
                        self._session_blind_report(health.get("reason") or "cookies expired")
                return True
        except Exception as exc:
            self.log(f"[Session] refresh failed: {exc}")
        return False

    def _preflight_session_plan(self):
        """Decide *before* launching whether this account is worth a browser.

        Returns ``(ok, plan)``.  A blind session without credentials is skipped
        so the thread moves to the next account instead of wasting a launch.
        """
        if self.account_mode != "restore" or not SESSION_GUARD_AVAILABLE:
            return True, {}
        if not isinstance(self.account, dict):
            return True, {}
        try:
            plan = session_guard.plan_for_account(self.account)
        except Exception:
            return True, {}
        state = (plan.get("health") or {}).get("state", "unknown")
        if plan.get("action") == "skip" and plan.get("blind"):
            self.log(f"[Session] skipping {self._account_label()}: {plan.get('reason')}")
            return False, plan
        if plan.get("blind") and plan.get("action") == "repair":
            self.log(f"[Session] {self._account_label()} is blind "
                     f"({state}) — will repair by logging in")
        elif state == "expiring":
            self.log(f"[Session] warning: {self._account_label()} cookies expire in "
                     f"{(plan.get('health') or {}).get('days_left')} day(s)")
        return True, plan

    def restore_saved_account_session(self, page):
        """Open the authenticated app using the restored cookies/local storage.

        Phase 13: after opening the page the *page itself* is verified (login
        form / chat UI / username / URL rules) instead of trusting the URL
        alone.  An unauthenticated result is reported as a blind session so the
        caller can repair it with a credential login.
        """
        try:
            if not self.account or not self.account.get("storage_state_path"):
                self.log("[Restore] ✗ Saved browser state is missing")
                self._session_blind_report("no saved storage state")
                return False

            restore_url = self.account.get("restore_url") or self.NEW_SESSION_ENTRY_URL
            self.log(f"[Restore] Loading saved session for {self._account_label()} "
                     f"at {restore_url} ...")
            page.goto(
                restore_url,
                timeout=30000,
                wait_until="domcontentloaded",
            )
            self.log(f"[Restore] Current URL: {page.url}")

            if not self._session_cfg("verify_after_restore", True):
                # Legacy behaviour: URL-only decision.
                if "login" in str(page.url).lower():
                    self.log("[Restore] ✗ Saved session is no longer authenticated")
                    self._session_blind_report("redirected to login")
                    return False
                return True

            if not self._session_verified(page):
                self.log(f"[Restore] ✗ saved session no longer authenticated "
                         f"({self._session_probe_reason})")
                self._session_blind_report(self._session_probe_reason
                                           or "not authenticated after restore")
                return False
            self.log("[Restore] ✓ saved session verified as authenticated")
            return True
        except Exception as error:
            self.log(f"[Restore] ✗ Could not open saved account session: {error}")
            return False

    def _human_warmup(self, page):
        """Run a human-like warmup phase before login: browse homepage, scroll, hover, jitter."""
        cfg = self.website_visits_cfg
        if not cfg.get("human_warmup_enabled", False):
            self.log("[Warmup] Disabled — skipping warmup phase.")
            return

        self.log("[Warmup] Starting human warmup phase...")

        try:
            page.goto(
                self.PUBLIC_HOMEPAGE_URL,
                timeout=30000,
                wait_until="domcontentloaded",
            )
            self.log(f"[Warmup] Homepage loaded: {page.url}")
        except Exception as e:
            self.log(f"[Warmup] Homepage load failed: {e}; continuing anyway")

        time.sleep(random.uniform(1.5, 3.0))

        # Mouse idle jitter while "looking at" the page
        try:
            from browser.human_behavior import mouse_idle_jitter
            mouse_idle_jitter(page, duration=random.uniform(2.0, 4.0), moves=random.randint(2, 5))
        except Exception:
            pass

        # Scroll the page
        if cfg.get("warmup_scroll", False):
            try:
                from browser.human_behavior import random_scroll
                for _ in range(random.randint(1, 3)):
                    random_scroll(page, min_pixels=100, max_pixels=400)
                    time.sleep(random.uniform(0.5, 1.5))
            except Exception:
                pass

        # Hover over visible links
        if cfg.get("warmup_hover_links", False):
            try:
                hover_count = int(cfg.get("warmup_hover_link_count", 3) or 3)
                links = page.locator("a[href]:visible").all()
                if links:
                    import random as _rand
                    sample = _rand.sample(links, min(hover_count, len(links)))
                    for link in sample:
                        try:
                            link.hover(timeout=2000)
                            time.sleep(random.uniform(0.3, 0.8))
                        except Exception:
                            pass
            except Exception:
                pass

        # Simulate tab switching
        try:
            from browser.human_behavior import simulate_tab_switch
            if random.random() < 0.6:
                simulate_tab_switch(page, "switch")
                time.sleep(random.uniform(0.5, 1.0))
                simulate_tab_switch(page, "prev")
        except Exception:
            pass

        # Viewport resize jitter
        try:
            from browser.human_behavior import viewport_resize_jitter
            viewport_resize_jitter(page)
        except Exception:
            pass

        self.log("[Warmup] ✓ Warmup phase complete")

    def start_chitchat_session(self, page):
        """
        Start a new Chitchat.gg session: login with account, handle captcha, and enter chat.
        
        Args:
            page: Playwright page object
            
        Returns:
            Boolean indicating success
        """
        try:
            # Step 1: Login with credentials or restore the saved authenticated
            # browser state.  The remaining popup/chat-start flow is shared.
            if self.account_mode == "restore":
                self.log("[Step 1/3] Restoring saved account session...")
                session_ready = self.restore_saved_account_session(page)
                if not session_ready:
                    # Phase 13: a blind session is not the end — if accounts.txt
                    # holds the password we log in again and re-save the state.
                    session_ready = self._repair_blind_session(page)
                if not session_ready:
                    self.log("[Step 1/3] ✗ Account restore failed")
                    return False
                self.log("[Step 1/3] ✓ Saved account session restored!")
            else:
                self.log("[Step 1/3] Logging in with account...")

                if not self.login_with_account(page):
                    self.log("[Step 1/3] ✗ Login failed")
                    return False

                self.log("[Step 1/3] ✓ Login successful!")
            
            # Hide browser window after successful login if requested
            if self.hide_after_login:
                try:
                    # CDP is Chromium-only. Camoufox is Firefox-based, so use
                    # the browser's native window APIs instead.
                    page.evaluate("""() => {
                        window.moveTo(-32000, -32000);
                        window.resizeTo(1, 1);
                    }""")
                    self.log("[Browser] Window moved off-screen")
                except Exception as e:
                    self.log(f"[Browser] Could not hide window: {e}")
            
            # Give time for redirect after login
            time.sleep(2)

            current_url = page.url
            self.log(f"[Session] Post-login URL: {current_url}")

            # If already on the chat page after login, skip the "Start Text Chat"
            # button search — it only exists on the homepage before login.
            if "/chat/" in current_url.lower():
                self.log("[Session] ✓ Already on chat page — skipping Start Text Chat button")
                return True

            # Step 2: Handle any popup dialogs (like "Video Chat is now live!")
            self.log("[Step 2/3] Checking for popup dialogs...")
            
            # Helper function to dismiss any visible popup/dialog
            def try_dismiss_popup():
                """Try to dismiss any popup dialog. Returns True if a popup was found and dismissed."""
                try:
                    # Radix dialogs expose the action as a normal descendant of
                    # the open dialog. Prefer that exact dialog-scoped locator
                    # so this popup cannot fall through to ESC.
                    open_dialogs = page.locator(
                        'div[role="dialog"][data-state="open"], '
                        'div[role="alertdialog"][data-state="open"]'
                    )
                    for index in range(open_dialogs.count()):
                        dialog = open_dialogs.nth(index)
                        if not dialog.is_visible(timeout=500):
                            continue
                        not_now_button = self._find_visible_button(
                            dialog,
                            'Not now',
                            exact=True,
                        )
                        if not_now_button is not None:
                            self._humanize_click(not_now_button, timeout=5000)
                            self.log(
                                "[Step 2/3] ✓ Dismissed popup via "
                                "humanized Not now click"
                            )
                            return True

                    # Locate popup controls with Playwright so their clicks can
                    # use Camoufox's humanized input.
                    result = page.evaluate('''
                        () => {
                            // Look for any visible dialog/popup
                            const dialogs = document.querySelectorAll('div[role="dialog"], div[role="alertdialog"], [data-state="open"]');
                            
                            for (let dialog of dialogs) {
                                const rect = dialog.getBoundingClientRect();
                                const style = window.getComputedStyle(dialog);
                                
                                // Check if dialog is visible
                                if (rect.width > 0 && rect.height > 0 && 
                                    style.display !== 'none' && 
                                    style.visibility !== 'hidden' &&
                                    style.opacity !== '0') {
                                    
                                    const dialogText = dialog.innerText || '';
                                    
                                    // Look for "Not now" button first (for video chat popup)
                                    const buttons = dialog.querySelectorAll('button');
                                    
                                    for (let btn of buttons) {
                                        const btnText = (btn.textContent || '').trim().toLowerCase();
                                        if (btnText.includes('not now') || btnText === 'not now') {
                                            return { dismissed: true, method: 'not-now-button', button_text: btnText, text: dialogText.substring(0, 50) };
                                        }
                                    }
                                    
                                    // Look for close button (X)
                                    const closeBtn = dialog.querySelector('button[aria-label="Close"], button[aria-label="close"], button svg.lucide-x');
                                    if (closeBtn) {
                                        return { dismissed: true, method: 'close-button', text: dialogText.substring(0, 50) };
                                    }
                                    
                                    // Look for any button that might dismiss (Skip, Cancel, Later, etc.)
                                    for (let btn of buttons) {
                                        const btnText = (btn.textContent || '').trim().toLowerCase();
                                        if (btnText.includes('skip') || btnText.includes('cancel') || 
                                            btnText.includes('later') || btnText.includes('dismiss') ||
                                            btnText.includes('close') || btnText === 'x') {
                                            return { dismissed: true, method: 'fallback-button', button_text: btnText, text: dialogText.substring(0, 50) };
                                        }
                                    }
                                    
                                    return { dismissed: false, found: true, text: dialogText.substring(0, 100) };
                                }
                            }
                            return { dismissed: false, found: false };
                        }
                    ''')
                    
                    if result.get('dismissed'):
                        if result.get('method') == 'close-button':
                            dismiss_button = page.locator(
                                'button[aria-label="Close"], button[aria-label="close"], '
                                'button:has(svg.lucide-x)'
                            ).first
                            if not dismiss_button.is_visible(timeout=1000):
                                return False
                        else:
                            dismiss_button = self._find_visible_button(
                                page,
                                result.get('button_text', ''),
                            )
                            if dismiss_button is None:
                                return False
                        self._humanize_click(dismiss_button, timeout=5000)
                        self.log(f"[Step 2/3] ✓ Dismissed popup via {result.get('method')}: {result.get('text', '')[:40]}...")
                        return True
                    elif result.get('found'):
                        self.log(f"[DEBUG] Found popup but couldn't dismiss: {result.get('text', '')[:50]}...")
                        # Try pressing Escape as fallback
                        self._humanize_pause(0.3, 0.7)
                        page.keyboard.press('Escape')
                        return True
                    return False
                except Exception as e:
                    self.log(f"[DEBUG] Error in try_dismiss_popup: {e}")
                    return False
            
            # Wait a moment for any popup to appear
            time.sleep(1)
            
            # Try to dismiss popups multiple times
            max_dialog_attempts = 5
            for attempt in range(max_dialog_attempts):
                if not try_dismiss_popup():
                    self.log("[Step 2/3] No popup dialog found")
                    break
                time.sleep(0.5)
            
            # Step 3: Click "Start Text Chat" button with parallel popup dismissal
            self.log("[Step 3/3] Looking for 'Start Text Chat' button...")
            try:
                # Wait up to 15 seconds for button to be visible, checking for popups in parallel
                max_wait_attempts = 15
                button_clicked = False
                
                for wait_attempt in range(max_wait_attempts):
                    if not self.is_running:
                        return False
                    
                    # First, always check and dismiss any popup that might be blocking
                    popup_dismissed = try_dismiss_popup()
                    if popup_dismissed:
                        time.sleep(0.5)  # Give time for popup to close
                        continue  # Re-check after dismissing popup
                    
                    # Now try to find and click the Start Text Chat button
                    try:
                        # Use JavaScript for more reliable button detection
                        click_result = page.evaluate('''
                            () => {
                                // First check if there's any blocking dialog
                                const dialogs = document.querySelectorAll('div[role="dialog"], div[role="alertdialog"]');
                                for (let dialog of dialogs) {
                                    const rect = dialog.getBoundingClientRect();
                                    const style = window.getComputedStyle(dialog);
                                    if (rect.width > 0 && rect.height > 0 && 
                                        style.display !== 'none' && 
                                        style.visibility !== 'hidden') {
                                        // There's a dialog blocking - try to find Not now button
                                        const buttons = dialog.querySelectorAll('button');
                                        for (let btn of buttons) {
                                            const btnText = (btn.textContent || '').trim().toLowerCase();
                                            if (btnText.includes('not now')) {
                                                return { clicked: false, popup_dismissed: true, reason: 'dismissed-popup' };
                                            }
                                        }
                                        return { clicked: false, popup_dismissed: false, reason: 'dialog-blocking' };
                                    }
                                }
                                
                                // No blocking dialog, find Start Text Chat button
                                const buttons = document.querySelectorAll('button');
                                for (let btn of buttons) {
                                    const text = (btn.textContent || '').trim();
                                    if (text.includes('Start Text Chat')) {
                                        const rect = btn.getBoundingClientRect();
                                        const style = window.getComputedStyle(btn);
                                        if (rect.width > 0 && rect.height > 0 && 
                                            style.display !== 'none' && 
                                            style.visibility !== 'hidden' &&
                                            !btn.disabled) {
                                            return { clicked: true, reason: 'button-clicked' };
                                        }
                                    }
                                }
                                return { clicked: false, reason: 'button-not-found' };
                            }
                        ''')
                        
                        if click_result.get('clicked'):
                            start_button = self._find_visible_button(page, 'Start Text Chat')
                            if start_button is None:
                                time.sleep(0.2)
                                continue
                            self._humanize_click(start_button, timeout=5000)
                            self.log("[Step 3/3] ✓ 'Start Text Chat' button clicked")
                            button_clicked = True
                            break
                        elif click_result.get('popup_dismissed'):
                            not_now_button = self._find_visible_button(page, 'Not now')
                            if not_now_button is not None:
                                self._humanize_click(not_now_button, timeout=5000)
                            self.log("[Step 3/3] Dismissed blocking popup, retrying...")
                            time.sleep(0.5)
                            continue
                        elif click_result.get('reason') == 'dialog-blocking':
                            self.log("[DEBUG] Dialog blocking, pressing Escape...")
                            self._humanize_pause()
                            page.keyboard.press('Escape')
                            time.sleep(0.5)
                            continue
                            
                    except Exception as e:
                        self.log(f"[DEBUG] Error in button click attempt: {e}")
                    
                    time.sleep(1)
                
                if button_clicked:
                    time.sleep(random.uniform(1.0, 2.0))
                else:
                    self.log("[Step 3/3] ✗ 'Start Text Chat' button not found after all attempts")
                    # Take screenshot for debugging
                    try:
                        screenshot_path = f"debug_no_start_chat_{int(time.time())}.png"
                        page.screenshot(path=screenshot_path)
                        self.log(f"[DEBUG] Screenshot saved: {screenshot_path}")
                    except:
                        pass
                    return False
                    
            except Exception as e:
                self.log(f"[Step 3/3] Error clicking Start Text Chat: {e}")
                return False
            
            self.log("[Setup] ✓ Chitchat session started successfully!")
            return True
            
        except Exception as e:
            self.log(f"[Setup] Error starting Chitchat session: {e}")
            import traceback
            self.log(f"[Setup] Traceback: {traceback.format_exc()}")
            return False

    # ------------------------------------------------------------------
    # Stranger-SMS detection (v2.1)
    # ------------------------------------------------------------------
    def _new_message_tracker(self):
        """Fresh incremental tracker for one conversation."""
        if CHAT_READER_AVAILABLE:
            return chat_reader.MessageTracker()
        return _LegacyCountTracker()

    def _record_sent_text(self, text):
        """Remember what we sent (used to label our own bubbles)."""
        try:
            if CHAT_READER_AVAILABLE:
                clean = chat_reader.norm_text(text)
            else:
                clean = " ".join(str(text or "").split())
            if clean:
                self._recent_sent_texts.append(clean)
        except Exception:
            pass

    def _log_detect_health(self, page, chat_number=None):
        """Warn loudly (once) when the page cannot be parsed at all.

        A silent "0 messages extracted" is exactly how the old bot failed:
        the stranger kept typing and the bot looked blind.  Instead of
        failing silently we log the selectors that were tried, and — with
        EVA_DUMP_CHAT_DOM=1 — save the page HTML so the markup can be fixed.
        """
        try:
            if self._empty_extract_polls < getattr(self, "DETECT_WARN_AFTER", 5):
                return
            if self._detect_warned:
                return
            self._detect_warned = True
            where = f" (chat #{chat_number})" if chat_number else ""
            self.log(
                f"[Detect] WARNING{where}: 0 messages parsed for "
                f"{self._empty_extract_polls} polls — the chat markup may have changed."
            )
            if CHAT_READER_AVAILABLE and self._last_detect_diag:
                self.log(f"[Detect] {chat_reader.describe_diag(self._last_detect_diag)}")
                sample = (self._last_detect_diag or {}).get("sampleClasses") or []
                if sample:
                    self.log(f"[Detect] sample rows: {sample[:3]}")
                suggestion = (self._last_detect_diag or {}).get("suggestionFile")
                if suggestion:
                    self.log(f"[Detect] selector suggestion written: {suggestion}")
                self.log("[Detect] fix it with:  python tools/chat_detect_debug.py "
                         "--html <saved chat page.html> --suggest   (--save applies it)")
            if str(os.environ.get("EVA_DUMP_CHAT_DOM", "")).strip() not in ("", "0", "false", "False"):
                if CHAT_READER_AVAILABLE:
                    path = chat_reader.dump_dom(page)
                    if path:
                        self.log(f"[Detect] page HTML saved for debugging: {path}")
        except Exception as exc:
            self.log(f"[Detect] health check error: {exc}")

    def extract_chat_from_page(self, page):
        """Read the chat list (multi-selector, with diagnostics).

        Returns a list of {'speaker': 'You'|'Stranger', 'message': str}.
        Falls back to the original single-selector extractor whenever the
        new reader fails, so detection can never be worse than before.
        """
        if CHAT_READER_AVAILABLE:
            try:
                messages, diag = chat_reader.extract_with_diag(
                    page,
                    recent_sent=list(self._recent_sent_texts),
                    my_username=self._my_username,
                )
                self._last_detect_diag = diag or {}
                found_username = (diag or {}).get("myUsername")
                if found_username:
                    self._my_username = str(found_username)
                if messages:
                    self._empty_extract_polls = 0
                    if (diag or {}).get("healed") and not self._heal_logged:
                        self._heal_logged = True
                        learned = (diag or {}).get("healedSelectors") or {}
                        self.log("[Detect] chat markup CHANGED — the bot healed itself "
                                 "and learned the new selectors:")
                        self.log(f"[Detect] {chat_reader.describe_diag(diag)}")
                        self.log("[Detect] keep them with:  python tools/chat_detect_debug.py "
                                 "--html <saved chat page.html> --save")
                        if not learned:
                            self.log("[Detect] (no selector detail in diag)")
                    return messages
                self._empty_extract_polls += 1
                # nothing parsed: give the legacy selector set a chance too
                legacy = self._extract_chat_from_page_legacy(page)
                if legacy:
                    self._empty_extract_polls = 0
                    self.log("[Detect] new reader found nothing — legacy selector set used")
                    return legacy
                return messages
            except Exception as exc:
                self.log(f"[Detect] reader failed ({type(exc).__name__}: {exc}) — using legacy extractor")
        return self._extract_chat_from_page_legacy(page)

    def _extract_chat_from_page_legacy(self, page):
        """
        ORIGINAL single-selector extraction, kept as a guaranteed fallback.

        Extract chat messages from the Chitchat.gg chat page.
        
        Args:
            page: Playwright page object
            
        Returns:
            List of message dictionaries with 'speaker' and 'message' keys
        """
        try:
            # Extract chat messages using Chitchat.gg's HTML structure
            chat_content = page.evaluate('''
                () => {
                    const messages = [];
                    
                    // First, find our own username from the sidebar user info
                    let myUsername = null;
                    
                    // Try to find our username from the sidebar user button
                    const userButton = document.querySelector('button .truncate.text-sm.font-bold');
                    if (userButton) {
                        myUsername = userButton.textContent.trim();
                    }
                    
                    // Alternative: look in the bottom left user section
                    if (!myUsername) {
                        const userSection = document.querySelector('.bg-panel button span.truncate');
                        if (userSection) {
                            myUsername = userSection.textContent.trim();
                        }
                    }
                    
                    // Fallback: look for any user profile section
                    if (!myUsername) {
                        const profileSpan = document.querySelector('span[alt][username]');
                        if (profileSpan && profileSpan.closest('.bg-panel')) {
                            myUsername = profileSpan.getAttribute('username');
                        }
                    }
                    
                    console.log('[Extract] My username: ' + myUsername);
                    
                    // Find the chat container - it's the <ol> element in main
                    const chatContainer = document.querySelector('main ol');
                    if (!chatContainer) {
                        console.log('[Extract] Chat container (main ol) not found');
                        return messages;
                    }
                    
                    // Get all message <li> elements with class 'select-text'
                    const messageItems = chatContainer.querySelectorAll('li.select-text');
                    console.log('[Extract] Found ' + messageItems.length + ' message items');
                    
                    for (let li of messageItems) {
                        // Find the username span - it has class 'font-bold' and role='button'
                        const usernameSpan = li.querySelector('span.font-bold[role="button"]');
                        if (!usernameSpan) {
                            console.log('[Extract] No username span found in li');
                            continue;
                        }
                        
                        const username = usernameSpan.textContent.trim();
                        
                        // Find the message content - it's in span.emoji-content
                        const messageSpan = li.querySelector('span.emoji-content');
                        if (!messageSpan) {
                            console.log('[Extract] No message span found for user: ' + username);
                            continue;
                        }
                        
                        const messageText = messageSpan.textContent.trim();
                        
                        // Determine if this is "You" or "Stranger"
                        let speaker = 'Stranger';
                        if (myUsername && username === myUsername) {
                            speaker = 'You';
                        }
                        
                        if (messageText.length > 0) {
                            console.log('[Extract] ' + speaker + ' (' + username + '): ' + messageText);
                            messages.push({
                                speaker: speaker,
                                message: messageText
                            });
                        }
                    }
                    
                    console.log('[Extract] Total messages extracted: ' + messages.length);
                    return messages;
                }
            ''')
            return chat_content
        except Exception as e:
            self.log(f"[Chat Extract] Error: {e}")
            return []
    
    def check_if_disconnected(self, page):
        """
        Check if the stranger has disconnected.
        
        Args:
            page: Playwright page object
            
        Returns:
            Boolean indicating if disconnected
        """
        try:
            # Use JavaScript to check for disconnect indicators in Chitchat.gg
            is_disconnected = page.evaluate('''
                () => {
                    // Method 1: Check for "has skipped this chat" message
                    const pageText = document.body.innerText || '';
                    if (pageText.includes('has skipped this chat') ||
                        pageText.includes('partner has left') ||
                        pageText.includes('chat has ended') ||
                        pageText.includes('disconnected')) {
                        return { disconnected: true, method: 'skip-message' };
                    }
                    
                    // Method 2: Check if START button is visible (instead of SKIP)
                    // When disconnected, button shows "START" with bg-primary class
                    // When connected, button shows "SKIP" with bg-warning class
                    const buttons = document.querySelectorAll('button');
                    for (let btn of buttons) {
                        const text = btn.textContent.trim().toUpperCase();
                        if (text === 'START') {
                            // Check if this button has the primary style (disconnected state)
                            const classes = btn.className || '';
                            if (classes.includes('bg-primary') && !classes.includes('bg-warning')) {
                                return { disconnected: true, method: 'start-button' };
                            }
                        }
                    }
                    
                    // Method 3: Check if message textarea is disabled
                    const textarea = document.querySelector('textarea[name="message"]');
                    if (textarea && textarea.disabled) {
                        return { disconnected: true, method: 'textarea-disabled' };
                    }
                    
                    return { disconnected: false };
                }
            ''')
            
            return is_disconnected.get('disconnected', False)
        except Exception as e:
            return False
    
    def send_chat_message(self, page, message, chat_number=None):
        """
        Send a message in the Chitchat chat.
        
        Args:
            page: Playwright page object
            message: String message to send
            chat_number: Current chat number for logging (optional)
            
        Returns:
            Boolean indicating success
        """
        try:
            # Find the message input - try multiple selectors
            input_selectors = [
                'textarea[placeholder*="message" i]',
                'input[placeholder*="message" i]',
                'textarea[class*="input" i]',
                'input[type="text"][class*="chat" i]',
                'textarea',
                'input[type="text"]'
            ]
            
            message_input = None
            for selector in input_selectors:
                try:
                    element = page.locator(selector).first
                    if element.is_visible(timeout=1000):
                        message_input = element
                        break
                except Exception:
                    continue
            
            if not message_input:
                self.log("[Chat] Could not find message input field")
                return False
            
            # Click to focus
            self._humanize_click(message_input, timeout=2000)
            
            # Type the message like a human (per-character with Gaussian delays)
            # instead of fill() which pastes instantly — a major bot tell.
            try:
                from browser.human_behavior import type_human
                type_human(message_input, message, min_delay_ms=50, max_delay_ms=200)
            except Exception:
                # Fallback to fill if the human-typing module is unavailable.
                message_input.fill(message)
            
            # Send with Enter key
            self._humanize_pause(0.3, 0.8)
            page.keyboard.press('Enter')
            time.sleep(0.1)
            # v2.1: remember our own text so detection never answers it back
            self._record_sent_text(message)
            
            # Log to GUI
            if chat_number is not None:
                self.log(f"[Chat #{chat_number}] You: {message}")
            
            print(f"[T{self.thread_id}] [Chat] Sent: {message}")
            return True
            
        except Exception as e:
            self.log(f"[Chat] Error sending message: {e}")
            return False
    
    def start_new_chat(self, page):
        """
        Start a new chat after disconnect or end.
        
        Args:
            page: Playwright page object
            
        Returns:
            Boolean indicating success
        """
        try:
            max_attempts = 3
            
            for attempt in range(max_attempts):
                # Method 1: Click the START button (primary method for Chitchat.gg)
                try:
                    # Find the exact START button so a broader text match does
                    # not accidentally select "Start Text Chat".
                    start_button = self._find_visible_button(page, 'START', exact=True)
                    if start_button is not None:
                        self._humanize_click(start_button)
                        self.log("[Chat] ✓ Clicked START button")
                        time.sleep(random.uniform(1.0, 1.5))
                        return True
                except Exception:
                    pass
                
                # Method 2: Use JavaScript to find and click START button
                try:
                    clicked = page.evaluate('''
                        () => {
                            const buttons = document.querySelectorAll('button');
                            for (let btn of buttons) {
                                const text = btn.textContent.trim().toUpperCase();
                                if (text === 'START') {
                                    return true;
                                }
                            }
                            return false;
                        }
                    ''')
                    if clicked:
                        start_button = self._find_visible_button(page, 'START', exact=True)
                        if start_button is not None:
                            self._humanize_click(start_button)
                            self.log("[Chat] ✓ Clicked START button via Playwright fallback")
                            time.sleep(random.uniform(1.0, 1.5))
                            return True
                except Exception:
                    pass
                
                # Method 3: Fallback selectors for other possible button texts
                fallback_selectors = [
                    'button:has-text("New Chat")',
                    'button:has-text("Start New")',
                    'button:has-text("Find")',
                    'button:has-text("Next")',
                    'button:has-text("Start Text Chat")'
                ]
                
                for selector in fallback_selectors:
                    try:
                        button = page.locator(selector).first
                        if button.is_visible(timeout=1000):
                            self._humanize_click(button)
                            self.log(f"[Chat] Clicked new chat button (fallback)")
                            time.sleep(random.uniform(1.0, 1.5))
                            return True
                    except Exception:
                        continue
                
                # Method 4: Press ESC key to trigger new chat (keyboard shortcut)
                try:
                    self._humanize_pause(0.3, 0.7)
                    page.keyboard.press('Escape')
                    time.sleep(0.5)
                except Exception:
                    pass
                
                time.sleep(1.0)
            
            self.log("[Chat] Could not find new chat button")
            return False
            
        except Exception as e:
            self.log(f"[Chat] Error starting new chat: {e}")
            return False
    
    def end_chat(self, page):
        """
        End the current chat by clicking SKIP button or pressing ESC.
        
        Args:
            page: Playwright page object
            
        Returns:
            Boolean indicating success
        """
        try:
            # Method 1: Click SKIP button (primary method for Chitchat.gg)
            try:
                skip_button = self._find_visible_button(page, 'SKIP', exact=True)
                if skip_button is not None:
                    self._humanize_click(skip_button)
                    self.log("[Chat] ✓ Clicked SKIP to end chat")
                    time.sleep(0.5)
                    self._wait_for_chat_end_confirmation(page)
                    return True
            except Exception:
                pass
            
            # Method 2: Use JavaScript to click SKIP button
            try:
                clicked = page.evaluate('''
                    () => {
                        const buttons = document.querySelectorAll('button');
                        for (let btn of buttons) {
                            const text = btn.textContent.trim().toUpperCase();
                            if (text === 'SKIP') {
                                return true;
                            }
                        }
                        return false;
                    }
                ''')
                if clicked:
                    skip_button = self._find_visible_button(page, 'SKIP', exact=True)
                    if skip_button is not None:
                        self._humanize_click(skip_button)
                        self.log("[Chat] ✓ Clicked SKIP via Playwright fallback")
                        time.sleep(0.5)
                        self._wait_for_chat_end_confirmation(page)
                        return True
            except Exception:
                pass
            
            # Method 3: Try ESC key (keyboard shortcut)
            try:
                self._humanize_pause(0.3, 0.7)
                page.keyboard.press('Escape')
                self.log("[Chat] Pressed ESC to end chat")
                time.sleep(0.5)
                self._wait_for_chat_end_confirmation(page)
            except Exception:
                pass
            
            # Method 4: Fallback to other end buttons
            end_selectors = [
                'button:has-text("End")',
                'button:has-text("Disconnect")',
                'button:has-text("Stop")',
                'button:has-text("Leave")'
            ]
            
            for selector in end_selectors:
                try:
                    button = page.locator(selector).first
                    if button.is_visible(timeout=1000):
                        self._humanize_click(button)
                        self.log("[Chat] Ended chat")
                        time.sleep(0.5)
                        self._wait_for_chat_end_confirmation(page)
                        return True
                except Exception:
                    continue
            
            return True  # ESC might have worked
            
        except Exception as e:
            self.log(f"[Chat] Error ending chat: {e}")
            return False
    
    def continuous_chat_loop(self, page):
        """
        Main chat loop that continuously sends fixed messages to strangers.
        
        Args:
            page: Playwright page object
        Returns:
            dict with statistics
        """
        stats = {
            'total_chats': 0,
            'messages_sent': 0,
            'messages_received': 0,
            'disconnects': 0,
            'snaps_shared': 0,
            'pics_sent': 0
        }
        
        self.log("="*60)
        self.log(f"[Chat Bot] Starting continuous chat loop")
        self.log(f"[Chat Bot] Loaded {len(self.fixed_message_templates)} message template file(s)")
        self.log("="*60)
        if self.session_management_cfg.get("log_alive_count_on_start", True):
            self._log_account_stock(prefix="[Stock] ")
        self._schedule_next_rest()
        
        while self.is_running:
            if not self.is_running:
                break

            # --- Browser-health heartbeat (anti-hang) ---
            # If the page crashed or the browser disconnected, break out so
            # the session restarts (or the context is recycled by the pool)
            # instead of hanging forever on a dead page.  This is the #2 fix
            # for "browser run hoyna" — a crashed page silently hangs.
            try:
                if page is None or page.is_closed():
                    self.log("[Health] Page closed/crashed — restarting session")
                    break
                if self.browser is not None:
                    # Per-worker mode: check browser connectivity.
                    if not self.browser.is_connected():
                        self.log("[Health] Browser disconnected — restarting session")
                        break
            except Exception as _hb:
                self.log(f"[Health] Heartbeat check error: {_hb}")
                break

            # --- Ban detection: if the account is banned, auto-close + delete ---
            if self._detect_ban(page):
                self._handle_ban(page)
                break

            stats['total_chats'] += 1
            self.log(f"\n[Chat #{stats['total_chats']}] Waiting for stranger...")
            
            # Wait for chat to connect
            connected = False
            for wait_attempt in range(30):
                if not self.is_running:
                    break
                
                # Check for various connection indicators using JavaScript
                try:
                    connection_status = page.evaluate('''
                        () => {
                            // Method 1: Look for "You are now chatting with" or "Say hi!" text
                            const connectedText = document.querySelector('#connected-text, span[id="connected-text"]');
                            if (connectedText) {
                                const text = connectedText.innerText || '';
                                if (text.includes('chatting with') || text.includes('Say hi')) {
                                    return { connected: true, method: 'connected-text', text: text.substring(0, 50) };
                                }
                            }
                            
                            // Method 2: Check page text for connection indicators
                            const pageText = document.body.innerText || '';
                            if ((pageText.includes('now chatting with') || pageText.includes('Say hi!')) &&
                                !pageText.includes('has skipped this chat') &&
                                !pageText.includes('partner has left')) {
                                return { connected: true, method: 'page-text' };
                            }
                            
                            // Method 3: Check if SKIP button is visible (connected state)
                            // When connected, button shows "SKIP" with bg-warning class
                            const buttons = document.querySelectorAll('button');
                            for (let btn of buttons) {
                                const text = btn.textContent.trim().toUpperCase();
                                if (text === 'SKIP') {
                                    const classes = btn.className || '';
                                    if (classes.includes('bg-warning')) {
                                        return { connected: true, method: 'skip-button' };
                                    }
                                }
                            }
                            
                            // Method 4: Check if textarea is enabled (not disabled)
                            const textarea = document.querySelector('textarea[name="message"]');
                            if (textarea && !textarea.disabled) {
                                // Additional check: make sure we're not on setup page
                                const agreeBtn = document.querySelector('button:has-text("I AGREE")');
                                if (!agreeBtn) {
                                    return { connected: true, method: 'textarea-enabled' };
                                }
                            }
                            
                            return { connected: false };
                        }
                    ''')
                    
                    if connection_status.get('connected'):
                        connected = True
                        method = connection_status.get('method', 'unknown')
                        self.log(f"[Chat #{stats['total_chats']}] ✓ Connected to stranger! (via: {method})")
                        break
                        
                except Exception as e:
                    pass
                
                # Captcha may block chat connection; alert user to solve it.
                try:
                    self._check_captcha_alert(page)
                except Exception:
                    pass

                time.sleep(self.performance_cfg.get("connect_poll_interval_seconds", 1.0))
            
            if not connected:
                self.log(f"[Chat #{stats['total_chats']}] Could not connect, trying new chat")
                if not self.is_running:
                    break
                if not self._maybe_take_rest(stats['total_chats']):
                    break
                self.start_new_chat(page)
                continue
            
            # Pick one random message template for this chat.
            chat_snapchat_id = self._get_chat_snapchat_id()
            selected_template, template_filename, chat_messages, first_message = self._get_chat_messages()
            total_chat_messages = len(chat_messages)

            # Send fixed messages
            if template_filename:
                self.log(f"[Chat #{stats['total_chats']}] Chosen message file: {template_filename}")
            self.log(f"[Chat #{stats['total_chats']}] Starting conversation...")
            
            current_fixed_message_index = 0
            conversation_complete = False
            chat_started_at = time.monotonic()   # chat-time limit clock
            pics_sent_this_chat = 0              # picture sender counter
            send_first_message = self._should_send_first_message()
            last_stranger_heard = time.monotonic()
            inactivity_nudges_sent = 0

            # --- v2.1 SMS detection: fingerprint tracker ------------------
            # The old code compared len(dom) against a counter that it also
            # incremented for our own sends; if the site did not render our
            # message (or extra pending replies were sent) the counter ran
            # ahead and the stranger's SMS were never detected again.
            tracker = self._new_message_tracker()
            if send_first_message:
                # We open the chat: whatever is already on screen is history.
                try:
                    tracker.snapshot(self.extract_chat_from_page(page))
                except Exception as _base_err:
                    self.log(f"[Detect] baseline snapshot failed: {_base_err}")
            # When the stranger opens the chat the first visible SMS must be
            # treated as NEW, so no baseline is taken in that mode.
            
            # Initialize ChatRuleBot state for this chat
            chat_rule_state = None
            self.log(f"[Chat #{stats['total_chats']}] CHAT_RULE_BOT_AVAILABLE={CHAT_RULE_BOT_AVAILABLE}")
            if not CHAT_RULE_BOT_AVAILABLE:
                self.log(f"[Chat #{stats['total_chats']}] ChatRuleBot import failed: {globals().get('_CHAT_RULE_IMPORT_ERROR', 'unknown')}")
            if CHAT_RULE_BOT_AVAILABLE:
                if self.chat_rule_bot is None:
                    try:
                        self.chat_rule_bot = ChatRuleBot()
                        self.log(f"[ChatRuleBot] Initialized OK — pipe_db={self.chat_rule_bot.pipe_db_size}, evoflow={self.chat_rule_bot.evoflow_rule_count}")
                    except Exception as e:
                        self.chat_rule_bot = None
                        self.log(f"[ChatRuleBot] FAILED to init: {type(e).__name__}: {e}")
                if self.chat_rule_bot:
                    chat_rule_state = self.chat_rule_bot.new_conversation()
                    self.log(f"[Chat #{stats['total_chats']}] ChatRuleBot active — state keys={list(chat_rule_state.keys())}")
                else:
                    self.log(f"[Chat #{stats['total_chats']}] ChatRuleBot is None — falling back to fixed messages")

            if send_first_message:
                self.log(f"[Chat #{stats['total_chats']}] First-message sending enabled")
            else:
                self.log(f"[Chat #{stats['total_chats']}] Waiting for stranger's first message")
            
            # Send first message
            if send_first_message and first_message:
                first_message_delay = self._get_first_message_delay()
                if first_message_delay > 0:
                    self.log(
                        f"[Chat #{stats['total_chats']}] Waiting "
                        f"{first_message_delay:.2f}s before sending first message"
                    )
                    time.sleep(first_message_delay)
                else:
                    self.log(f"[Chat #{stats['total_chats']}] Sending first message immediately")

                # A stranger may reply during the first-message delay: the
                # tracker reports it on the next poll and the bot answers it
                # there (nothing is swallowed any more).
                processed_message = self._process_message(first_message, chat_snapchat_id)
                self.log(f"[Chat #{stats['total_chats']}] First message: {processed_message}")
                
                if self.send_chat_message(page, processed_message, stats['total_chats']):
                    stats['messages_sent'] += 1
                    self._emit_chat("bot", processed_message)
                else:
                    self.log(f"[Chat #{stats['total_chats']}] Failed to send first message")
                    conversation_complete = True
            
            # Wait for replies — end after N bot replies or when snap is shared
            bot_reply_count = 1 if not conversation_complete else 0  # count first message as reply 1
            MAX_BOT_REPLIES = self.chat_timeout  # repurposed as max replies (default 8)
            
            while not conversation_complete and self.is_running:
                # Chat-time limit (optional): move to the next stranger
                # when the configured minutes elapse. Default (None) means
                # chat until the snap is shared.
                if self.chat_time_minutes:
                    elapsed_min = (time.monotonic() - chat_started_at) / 60.0
                    if elapsed_min >= self.chat_time_minutes:
                        self.log(
                            f"[Chat #{stats['total_chats']}] Chat time limit "
                            f"({self.chat_time_minutes} min) reached — moving to next user"
                        )
                        conversation_complete = True
                        break
                # Check if disconnected
                if self.check_if_disconnected(page):
                    stats['disconnects'] += 1
                    self.log(f"[Chat #{stats['total_chats']}] Stranger disconnected")
                    break

                # Captcha may appear mid-chat; alert user to solve it.
                try:
                    self._check_captcha_alert(page)
                except Exception:
                    pass
                
                # Check for new messages
                try:
                    current_messages = self.extract_chat_from_page(page)
                    self._log_detect_health(page, stats['total_chats'])
                    new_messages = tracker.sync(current_messages)

                    if new_messages:
                        stranger_replied = False

                        for msg in new_messages:
                            if msg['speaker'] == 'Stranger':
                                self.log(f"[Chat #{stats['total_chats']}] Stranger: {msg['message']}")
                                self._emit_chat("user", msg['message'])
                                stats['messages_received'] += 1
                                stranger_replied = True
                        
                        if stranger_replied:
                            last_stranger_heard = time.monotonic()
                            
                            # Simulate reading the stranger's reply first.
                            self._humanize_read_reply()
                            # Apply reply delay before sending reply
                            reply_delay = self._get_reply_delay()
                            if reply_delay > 0:
                                time.sleep(reply_delay)
                            
                            # Use ChatRuleBot if available, otherwise use fixed messages
                            if self.chat_rule_bot and chat_rule_state:
                                # Get the last stranger message
                                last_stranger_msg = ""
                                for msg in reversed(new_messages):
                                    if msg['speaker'] == 'Stranger':
                                        last_stranger_msg = msg['message']
                                        break
                                
                                # Generate intelligent reply using ChatRuleBot
                                try:
                                    processed_message = self.chat_rule_bot.reply(last_stranger_msg, chat_rule_state)
                                    self.log(f"[Chat #{stats['total_chats']}] ChatRuleBot reply: {processed_message} (step={chat_rule_state.get('sequence_step', '?')})")
                                    typing_feel = self._typing_delay_for(processed_message)
                                    if typing_feel > 0:
                                        time.sleep(typing_feel)
                                        self.log(f"[Chat #{stats['total_chats']}] Typing feel: +{typing_feel:.2f}s "
                                                 f"({len(processed_message)} ch)")
                                except Exception as crb_err:
                                    self.log(f"[Chat #{stats['total_chats']}] ChatRuleBot.reply() FAILED: {type(crb_err).__name__}: {crb_err}")
                                    # Fallback to fixed messages on exception
                                    if current_fixed_message_index < total_chat_messages:
                                        message = chat_messages[current_fixed_message_index]
                                        processed_message = self._process_message(message, chat_snapchat_id)
                                        current_fixed_message_index += 1
                                        self.log(f"[Chat #{stats['total_chats']}] Fallback to fixed message: {processed_message}")
                                    else:
                                        conversation_complete = True
                                        break
                            elif current_fixed_message_index < total_chat_messages:
                                # Fallback to fixed messages
                                message = chat_messages[current_fixed_message_index]
                                processed_message = self._process_message(message, chat_snapchat_id)
                                current_fixed_message_index += 1
                                self.log(f"[Chat #{stats['total_chats']}] FIXED msg [{current_fixed_message_index}/{total_chat_messages}]: {processed_message}")
                            else:
                                # No more fixed messages
                                self.log(f"[Chat #{stats['total_chats']}] ✓ All messages sent!")
                                conversation_complete = True
                                break
                            
                            # ---- PICTURE SENDER (middle chat point) ----
                            # Fires once per stranger when the reply count
                            # reaches the configured point. The picture is
                            # uploaded FIRST, then the engine's SMS reply is
                            # sent right after it (pic + SMS together).
                            # The chat engine is never involved: its reply,
                            # memory and flow are 100% unaffected.
                            if (self.pic_send_enabled
                                    and pics_sent_this_chat < self.pics_per_chat
                                    and bot_reply_count >= self.pic_after_messages):
                                pic_path = self._next_pic_file()
                                if pic_path:
                                    if self._send_picture(page, pic_path, stats['total_chats']):
                                        pics_sent_this_chat += 1
                                        stats['pics_sent'] += 1
                                        pic_name = os.path.basename(pic_path)
                                        self.log(f"[Chat #{stats['total_chats']}] 📷 Picture sent to stranger: {pic_name} (total {stats['pics_sent']})")
                                        self._emit_chat("pic", pic_name)
                                    else:
                                        self.log(f"[Chat #{stats['total_chats']}] [Pics] Picture not delivered — SMS reply continues normally")

                            if self.send_chat_message(page, processed_message, stats['total_chats']):
                                stats['messages_sent'] += 1
                                bot_reply_count += 1
                                self._emit_chat("bot", processed_message)
                                # Track snap shares
                                snap_keywords = ['snap', 'eva_', 'my sc', 'my snp', 'sn-p', 's,n.ap', 'sn..p', 'add me']
                                if any(kw in processed_message.lower() for kw in snap_keywords):
                                    stats['snaps_shared'] += 1
                                    self.log(f"[Stats] Snap shared! Total: {stats['snaps_shared']}")
                                # Send any pending multi-message replies (e.g. hint emojis + text)
                                if self.chat_rule_bot and chat_rule_state:
                                    pending = self.chat_rule_bot.get_pending_replies(chat_rule_state)
                                    for pending_msg in pending:
                                        time.sleep(1.5)  # Small delay between messages
                                        if self.send_chat_message(page, pending_msg, stats['total_chats']):
                                            stats['messages_sent'] += 1
                                            self._emit_chat("bot", pending_msg)
                                        else:
                                            break
                                # End chat if snap was shared or max replies reached
                                snap_shared = chat_rule_state and chat_rule_state.get("snap_pivoted", False)
                                if snap_shared:
                                    self.log(f"[Chat #{stats['total_chats']}] Snap shared — ending chat")
                                    conversation_complete = True
                                    break
                                if bot_reply_count >= MAX_BOT_REPLIES:
                                    self.log(f"[Chat #{stats['total_chats']}] Max {MAX_BOT_REPLIES} replies reached — moving to next user")
                                    conversation_complete = True
                                    break
                            else:
                                conversation_complete = True
                                break
                                
                except Exception as e:
                    self.log(f"[Chat #{stats['total_chats']}] Error: {e}")

                # ---- INACTIVITY WATCHER (background support system) --------
                # If the stranger has not replied for the configured window,
                # send at most N soft nudges ("u there?"); once nudges are
                # used and the grace window passes, leave the chat so the bot
                # moves on instead of hanging on a ghosted conversation.
                inactivity_timeout = float(
                    self.human_behavior.get("inactivity_timeout_seconds", 600.0))
                max_nudges = int(
                    self.human_behavior.get("inactivity_nudges_max", 1))
                inactivity_grace = float(
                    self.human_behavior.get("inactivity_grace_seconds", 300.0))
                idle_for = time.monotonic() - last_stranger_heard
                if idle_for > inactivity_timeout:
                    if inactivity_nudges_sent < max_nudges:
                        nudge_text = random.choice(
                            ["u there? 😏", "still alive? 👀", "u there?"])
                        typing_feel = self._typing_delay_for(nudge_text)
                        if typing_feel > 0:
                            time.sleep(typing_feel)
                        if self.send_chat_message(page, nudge_text, stats['total_chats']):
                            stats['messages_sent'] += 1
                            self._emit_chat("bot", nudge_text)
                            inactivity_nudges_sent += 1
                            last_stranger_heard = time.monotonic()
                            self.log(
                                f"[Chat #{stats['total_chats']}] Inactivity "
                                f"nudge #{inactivity_nudges_sent}: {nudge_text}")
                        else:
                            conversation_complete = True
                            break
                    elif idle_for > inactivity_timeout + inactivity_grace:
                        self.log(
                            f"[Chat #{stats['total_chats']}] Stranger idle for "
                            f"{idle_for:.0f}s after nudges — moving to next user")
                        conversation_complete = True
                        break

                time.sleep(self.performance_cfg.get("poll_interval_seconds", 0.5))
            
            if current_fixed_message_index == 0:
                self._return_unused_message_template(
                    selected_template, template_filename, stats['total_chats']
                )

            # End chat, wait for the end state, and prepare the next one.
            chat_was_disconnected = self.check_if_disconnected(page)
            transition_reason = "chat ended"
            if not chat_was_disconnected:
                self.log(f"[Chat #{stats['total_chats']}] Disconnecting...")
                self.end_chat(page)
                transition_reason = "skip confirmation"

            if not self.is_running:
                break
            if not self._maybe_take_rest(stats['total_chats']):
                break
            if not self._wait_before_new_chat(transition_reason):
                break

            # --- Periodic unique-site visit: open a random site from
            #     unique_sites.txt, read/scroll, close it, return to chat ---
            self._maybe_visit_unique_site(page)

            # --- Periodic viewport jitter + tab switch (human idle behavior) ---
            if random.random() < 0.4:
                try:
                    from browser.human_behavior import viewport_resize_jitter, simulate_tab_switch, mouse_idle_jitter
                    viewport_resize_jitter(page)
                    if random.random() < 0.3:
                        simulate_tab_switch(page, "switch")
                        time.sleep(random.uniform(0.3, 0.7))
                        simulate_tab_switch(page, "prev")
                    mouse_idle_jitter(page, duration=random.uniform(1.0, 2.5), moves=random.randint(1, 3))
                except Exception:
                    pass

            # --- Session keep-alive: refresh the saved cookies (throttled) ---
            # Without this, long runs lose their session on the next start
            # (cookies expire while the bot is chatting).
            self._maybe_refresh_session(page, reason=f"after chat #{stats['total_chats']}")

            # --- Optional live stock update after each chat ---
            if self.session_management_cfg.get("log_alive_count_after_each_chat", False):
                self._log_account_stock(prefix=f"[Stock] after chat #{stats['total_chats']} ")

            self.start_new_chat(page)
            
            if self.is_running:
                time.sleep(0.5)
        
        # Final keep-alive write so the newest cookies are on disk even when
        # the run is stopped between chats.
        try:
            self._maybe_refresh_session(page, reason="chat loop finished")
        except Exception:
            pass

        self.log("="*60)
        self.log(f"[Chat Bot] Session completed")
        self.log(f"  Total Chats: {stats['total_chats']}")
        self.log(f"  Messages Sent: {stats['messages_sent']}")
        self.log(f"  Messages Received: {stats['messages_received']}")
        self.log(f"  Disconnects: {stats['disconnects']}")
        self.log("="*60)
        
        return stats
    
    def _run_session_attempt(self):
        """Launch one browser/context and run the account flow once."""
        self._navigation_history = []
        self._tracked_page_ids = set()
        self._session_started_in_attempt = False

        try:
            # --- Context-pool mode (2026 upgrade) ---
            # When a context-pool slot is provided, use the pre-created
            # (browser, context) from the shared pool instead of launching
            # a new Camoufox browser per worker.  This is the "Golden Rule":
            # one browser, many contexts — minimal memory, fast startup.
            if self._context_pool_slot is not None:
                self.set_status("Using pooled context...")
                slot = self._context_pool_slot
                self.browser = slot.get("browser")
                self.context = slot.get("context")
                self.camoufox_manager = None  # pool owns the browser lifecycle
                self.log("[ContextPool] Using shared-browser isolated context")
            else:
                self.set_status("Launching Camoufox...")
                # --- Browser launch gate (Go engine) ---
                # Ask the engine for permission to launch a browser.  The
                # engine caps how many browsers may launch at once and blocks
                # launches under high CPU/memory pressure, preventing the PC
                # from freezing when many threads start simultaneously.
                # If the engine is unavailable we proceed immediately (fail-open).
                launch_slot_held = False
                if self._engine_bridge is not None:
                    for _attempt in range(40):  # up to ~40 retries (~80 s max)
                        if not self.is_running:
                            return False
                        ok, wait_ms = self._engine_bridge.launch_acquire()
                        if ok:
                            launch_slot_held = True
                            self.log(
                                f"[LaunchGate] Browser launch approved "
                                f"(thread {self.thread_id})"
                            )
                            break
                        # Engine says wait — sleep and retry.
                        sleep_s = max(0.25, min(float(wait_ms) / 1000.0, 5.0))
                        self.log(
                            f"[LaunchGate] Launch deferred {sleep_s:.1f}s "
                            f"(thread {self.thread_id}, wait_ms={wait_ms:.0f})"
                        )
                        time.sleep(sleep_s)
                    if not launch_slot_held:
                        self.log(
                            "[LaunchGate] Could not get a launch slot after "
                            "retries; proceeding anyway (fail-open)"
                        )
                        launch_slot_held = False  # don't release what we didn't acquire
                with self.browser_launch_lock:
                    self.browser = self._launch_camoufox()

                if not self.is_running:
                    # Clean up the launch slot if we acquired one.
                    if launch_slot_held and self._engine_bridge is not None:
                        self._engine_bridge.launch_release()
                    return False

                context_options = {}
                if self.account_mode == "restore":
                    storage_state_path = self.account.get("storage_state_path")
                    if not storage_state_path or not os.path.isfile(storage_state_path):
                        self.log("[Restore] ✗ Saved storage state file is missing")
                        if launch_slot_held and self._engine_bridge is not None:
                            self._engine_bridge.launch_release()
                        return False
                    context_options["storage_state"] = storage_state_path

                # For the Chromium engine, add stealth kwargs (random UA,
                # viewport, locale, timezone) so the browser looks like a real
                # user.  Camoufox forges its own fingerprint, so we skip these
                # in that mode to avoid conflicts.
                try:
                    from core.config_loader import load_performance
                    _engine = str(load_performance().get("browser_engine", "chromium")).lower().strip()
                except Exception:
                    _engine = "chromium"
                if _engine == "chromium":
                    try:
                        from browser.browser_engine import chromium_context_kwargs
                        # Build ONE coherent fingerprint (UA + viewport +
                        # platform + hw + memory) and reuse it for both the
                        # context kwargs AND the init-script stealth, so
                        # navigator.userAgent always agrees with the context UA
                        # (no mismatch leak).  Merge: stealth kwargs first, then
                        # caller overrides (e.g. storage_state) so the account
                        # session wins.
                        _stealth_kwargs, _stealth_fp = chromium_context_kwargs()
                        _stealth_kwargs.update(context_options)
                        context_options = _stealth_kwargs
                    except Exception:
                        _stealth_fp = None

                self.context = self.browser.new_context(**context_options)

                # Apply the init-script anti-detect layer for Chromium, using
                # the SAME fingerprint so UA/platform/hw/memory are consistent
                # between the context and the injected script.
                if _engine == "chromium":
                    try:
                        from browser.browser_engine import apply_chromium_stealth
                        apply_chromium_stealth(self.context, self.log, fingerprint=_stealth_fp)
                    except Exception:
                        pass
                # The browser is now fully up and the context is ready —
                # release the launch-gate slot so the next waiting worker
                # can start its own browser.
                if launch_slot_held and self._engine_bridge is not None:
                    self._engine_bridge.launch_release()
                    launch_slot_held = False
                try:
                    self.context.on("page", self._track_page_history)
                except Exception:
                    pass
                # --- Performance: optionally block heavy resource types ---
                self._apply_resource_blocking(self.context)

            page = self.context.new_page()
            self._track_page_history(page)
            self._chat_page_ref = page

            # Visit disposable URLs before any session flow navigates the retained
            # page to the public homepage.
            self._visit_pre_homepage_tabs(self.context)

            # Open persistent background tabs so the browser looks like a real
            # multi-tab user, and log how many saved accounts are currently alive.
            self._open_background_tabs(self.context)
            if self.session_management_cfg.get("log_alive_count_on_start", True):
                self._log_account_stock(prefix="[Stock] ")

            # Human warmup: browse homepage, scroll, hover, idle jitter
            self._human_warmup(page)

            session_started = (
                self.create_new_account_session(page)
                if self.account_mode == "create"
                else self.start_chitchat_session(page)
            )
            if not session_started:
                self.log("[Chitchat Bot] Failed to start session")
                return False

            # --- Post-login ban check: a restored account may already be banned ---
            if self._detect_ban(page):
                self._save_successful_account_session(page)  # save even on ban
                self._handle_ban(page)
                return False

            self._session_started_in_attempt = True
            self._save_successful_account_session(page)
            self.log("[Chitchat Bot] Session started; keeping browser session for chat...")
            self.set_status("Chatting with browser session...")
            self.continuous_chat_loop(page)
            self.log("[Chitchat Bot] Chat session completed!")
            return True
        finally:
            try:
                self._close_background_tabs()
            except Exception:
                pass
            if self._context_pool_slot is not None:
                # Context-pool mode: release the context back to the pool.
                # The pool owns the browser; we only close our context.
                try:
                    if self.context is not None:
                        self.context.close()
                except Exception:
                    pass
                self.context = None
                self.browser = None
                self._context_pool_slot = None
                self.log("[ContextPool] Context released back to pool")
            else:
                try:
                    self._close_camoufox()
                except Exception as error:
                    if "greenlet" not in str(error).lower():
                        self.log(f"[Cleanup] Warning: {error}")

    def run(self):
        """Run one Chitchat session in a native Camoufox browser."""
        self.is_running = not self._stop_event.is_set()
        self.setup_asyncio_error_handler()

        try:
            self.set_status("Starting Chitchat.gg automation...")
            self.log("[Chitchat Bot] Starting automated session...")
            # Phase 13: show what the saved sessions look like before launching
            # (alive / blind / repairable) so a dead session is visible early.
            self._log_session_health()
            if not self.is_running:
                self.log("[Stop] Stopping...")
                return

            while self.is_running:
                # Phase 13: never launch a browser for a blind session that
                # cannot be repaired — the thread moves to the next account.
                try:
                    worth_it, _plan = self._preflight_session_plan()
                except Exception:
                    worth_it = True
                if not worth_it:
                    self.log("[Session] account skipped before launch — no browser started")
                    self.set_status("Skipped: blind session")
                    return

                try:
                    session_started = self._run_session_attempt()
                except Exception as error:
                    if (
                        not getattr(self, "_session_started_in_attempt", False)
                        and self._use_proxy_fallback("browser/session error")
                    ):
                        self.log("[Proxy Warning] Retrying the account with the fallback connection...")
                        continue
                    self.log(f"Error: {error}")
                    import traceback
                    self.log(f"Traceback: {traceback.format_exc()}")
                    break

                if session_started or not self.is_running:
                    break

                if self._use_proxy_fallback("session could not start"):
                    self.log("[Proxy Warning] Retrying the account with the fallback connection...")
                    continue
                break
        finally:
            if self._ban_detected:
                self.log("[Cleanup] Session ended due to account ban.")
            elif self.is_running:
                self.log("[Cleanup] Natural shutdown...")
            else:
                self.log("[Cleanup] Stop signal detected...")

            try:
                self._close_background_tabs()
            except Exception:
                pass
            try:
                self._close_camoufox()
            except Exception as error:
                if "greenlet" not in str(error).lower():
                    self.log(f"[Cleanup] Warning: {error}")
            self.is_running = False
            self.set_status("Browser closed.")
    
    def stop(self):
        """Ask the worker thread to close its thread-affine Camoufox objects."""
        self.log("[Stop] Stopping browser...")
        self.is_running = False
        self._stop_event.set()


class ChitchatWorker(QThread):
    """
    Worker thread wrapper for ChitchatAutomation class.
    """
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)
    proxy_warning_signal = pyqtSignal(str)
    captcha_signal = pyqtSignal(int)
    finished_signal = pyqtSignal()
    # Live dashboard additions: (thread_id, who, text) / (thread_id, status)
    chat_signal = pyqtSignal(int, str, str)
    session_signal = pyqtSignal(int, str)
    
    def __init__(self, account=None, fixed_messages=None, headless=True, thread_id=1, chat_timeout=8, reply_delay=1.0, proxy_config=None, proxy_fallback_configs=None, hide_after_login=False, fixed_message_templates=None, account_mode="login", shuffle_chat=False, snapchat_id_selector=None, message_template_selector=None, message_template_returner=None, first_message_mode="false", reply_delay_min=None, reply_delay_max=None, first_message_delay_min=None, first_message_delay_max=None, chat_timing=None, context_pool_slot=None, engine_bridge=None, chat_time_minutes=None, pic_folder=None, pic_send_enabled=False, pic_after_messages=4, pics_per_chat=1):
        super().__init__()
        self.thread_id = thread_id
        self.automation = ChitchatAutomation(
            account=account,
            account_mode=account_mode,
            fixed_messages=fixed_messages,
            headless=headless,
            thread_id=thread_id,
            chat_timeout=chat_timeout,
            reply_delay=reply_delay,
            proxy_config=proxy_config,
            proxy_fallback_configs=proxy_fallback_configs,
            hide_after_login=hide_after_login,
            fixed_message_templates=fixed_message_templates,
            shuffle_chat=shuffle_chat,
            snapchat_id_selector=snapchat_id_selector,
            message_template_selector=message_template_selector,
            message_template_returner=message_template_returner,
            first_message_mode=first_message_mode,
            reply_delay_min=reply_delay_min,
            reply_delay_max=reply_delay_max,
            first_message_delay_min=first_message_delay_min,
            first_message_delay_max=first_message_delay_max,
            chat_timing=chat_timing,
            context_pool_slot=context_pool_slot,
            engine_bridge=engine_bridge,
            chat_time_minutes=chat_time_minutes,
            pic_folder=pic_folder,
            pic_send_enabled=pic_send_enabled,
            pic_after_messages=pic_after_messages,
            pics_per_chat=pics_per_chat,
        )
        
        # Set up callbacks
        self.automation.set_log_callback(lambda msg: self.log_signal.emit(msg))
        self.automation.set_chat_callback(
            lambda who, text: self.chat_signal.emit(self.thread_id, who, text))
        self.automation.set_session_callback(
            lambda status: self.session_signal.emit(self.thread_id, status))
        self.automation.set_status_callback(lambda status: self.status_signal.emit(status))
        self.automation.set_proxy_warning_callback(
            lambda message: self.proxy_warning_signal.emit(message)
        )
        self.automation.set_captcha_callback(
            lambda: self.captcha_signal.emit(self.thread_id)
        )
    
    def run(self):
        """Run the automation in a thread"""
        try:
            self.automation.run()
        except Exception as e:
            error_str = str(e).lower()
            if 'greenlet' not in error_str and 'cannot switch' not in error_str:
                self.log_signal.emit(f"[Error] Unexpected error: {e}")
        finally:
            self.finished_signal.emit()
    
    def stop(self):
        """Stop the automation"""
        self.automation.stop()

    def force_cleanup(self):
        """Force-close the browser/context owned by this worker.

        Called by ThreadManager when a worker thread is about to be
        hard-terminated (QThread.terminate()).  terminate() kills the
        OS thread, so the automation's ``run()`` finally-block (which
        normally closes Camoufox/Playwright) would NEVER run — leaving
        a zombie Firefox process + open Playwright driver pipes that
        break the NEXT browser launch (the "greenlet cannot switch to a
        different thread" / driver lockup crash).

        This method closes the browser/context from the manager thread
        BEFORE terminate() is called.  Camoufox's sync API is built on
        greenlets bound to the owning thread, so closing from another
        thread can itself raise a greenlet error — we swallow it,
        because the goal here is just to release the OS resources
        (subprocess pipes) so the next run can launch a fresh browser.
        """
        try:
            self.automation._close_background_tabs()
        except Exception:
            pass
        # Context-pool mode: only the context is ours; the pool owns the
        # browser.  Close the context so its pages are released.
        if self.automation._context_pool_slot is not None:
            try:
                if self.automation.context is not None:
                    self.automation.context.close()
            except Exception:
                pass
            self.automation.context = None
            self.automation.browser = None
            self.automation._context_pool_slot = None
        else:
            # Per-worker browser: close it through the Camoufox manager
            # so the Firefox subprocess and Playwright driver are torn down.
            try:
                self.automation._close_camoufox()
            except Exception as error:
                if "greenlet" not in str(error).lower() and \
                   "cannot switch" not in str(error).lower():
                    try:
                        self.log_signal.emit(f"[Cleanup] force_cleanup warning: {error}")
                    except Exception:
                        pass
            # Belt-and-suspenders: if the manager is still set, kill the
            # underlying process directly so no zombie lingers.
            mgr = getattr(self.automation, "camoufox_manager", None)
            if mgr is not None:
                try:
                    # Camoufox manager wraps a Playwright Browser; try to
                    # reach the process and terminate it.
                    browser = getattr(self.automation, "browser", None)
                    proc = getattr(browser, "_process", None) if browser else None
                    if proc is not None and hasattr(proc, "kill"):
                        proc.kill()
                except Exception:
                    pass

    def set_proxy_warning_callback(self, callback):
        """Set a callback for a proxy fallback warning."""
        self.automation.set_proxy_warning_callback(callback)
    
    @property
    def is_running(self):
        """Check if automation is running"""
        return self.automation.is_running
