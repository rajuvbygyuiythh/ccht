"""
Thread Manager for Multi-threaded Browser Automation
Manages multiple browser workers running concurrently
"""
import os
import random
import threading
from typing import Any, Dict, Optional
from PyQt6.QtCore import QObject, pyqtSignal
from browser.browser_automation import ChitchatWorker
from core.config_loader import normalize_chat_timing, load_runtime_config


class NonRepeatingRandomSelector:
    """Thread-safe random selector that exhausts its items before repeating."""

    def __init__(self, items=None):
        self._items = tuple(items or ())
        self._remaining = []
        self._last_item = None
        self._lock = threading.Lock()

    def get_next(self):
        """Return a random unused item, refilling the pool when exhausted."""
        with self._lock:
            if not self._items:
                return None

            if not self._remaining:
                self._remaining = list(self._items)
                random.shuffle(self._remaining)

                # Avoid repeating the previous item at a cycle boundary while
                # keeping every item in the new cycle available.
                if len(self._remaining) > 1 and self._last_item is not None:
                    last_index = len(self._remaining) - 1
                    if self._remaining[last_index] == self._last_item:
                        replacement_index = random.randrange(last_index)
                        self._remaining[last_index], self._remaining[replacement_index] = (
                            self._remaining[replacement_index],
                            self._remaining[last_index],
                        )

            selected = self._remaining.pop()
            self._last_item = selected
            return selected

    def return_item(self, item):
        """Put an unsuccessful selection back into the current unused pool."""
        with self._lock:
            if item is None or not any(candidate is item for candidate in self._items):
                return False

            # Identity checks keep separate files distinct even when their
            # message contents happen to be identical.
            if any(candidate is item for candidate in self._remaining):
                return False

            self._remaining.append(item)
            random.shuffle(self._remaining)
            return True


class ThreadManager(QObject):
    """
    Manages multiple browser automation threads.
    Each thread runs independently with random keyword selection.
    """
    
    # Signals for GUI updates
    thread_started = pyqtSignal(int, str)  # thread_id, message
    thread_log = pyqtSignal(int, str)      # thread_id, message
    chat_message = pyqtSignal(int, str, str)   # thread_id, who, text
    session_status = pyqtSignal(int, str)      # thread_id, status
    thread_status = pyqtSignal(int, str)   # thread_id, status
    proxy_warning = pyqtSignal(int, str)   # thread_id, warning
    captcha_alert = pyqtSignal(int)        # thread_id requiring manual captcha
    account_exhausted = pyqtSignal(str)    # user-facing exhaustion message
    thread_finished = pyqtSignal(int)      # thread_id
    all_threads_finished = pyqtSignal()
    
    def __init__(self, device_type=None, snapchat_ids=None, accounts=None, fixed_messages=None, mobile_ua_file=None, desktop_ua_file=None, keywords_file=None, domain_url=None, headless=True, chat_timeout=8, reply_delay=1.0, proxy_config=None, proxy_configs=None, hide_after_login=False, fixed_message_templates=None, account_mode="login", shuffle_chat=False, first_message_mode="false", reply_delay_min=None, reply_delay_max=None, first_message_delay_min=None, first_message_delay_max=None, chat_timing=None, chat_time_minutes=None, pic_folder=None, pic_send_enabled=False, pic_after_messages=4, pics_per_chat=1):
        """
        Initialize thread manager.
        
        Args:
            device_type: Device type ('mobile' or 'desktop')
            snapchat_ids: List of Snapchat usernames for multi-threading
            accounts: List of account dicts with 'email' and 'password' for login
            fixed_messages: List of fixed messages to send sequentially (legacy)
            fixed_message_templates: List of message template records containing
                                     a filename and list of messages
            mobile_ua_file: Path to mobile user agents file (optional)
            desktop_ua_file: Path to desktop user agents file (optional)
            keywords_file: Path to keywords file (optional)
            domain_url: Target domain URL (optional)
            headless: Run browser in headless mode (default: True)
            chat_timeout: Max bot replies before moving to next user (default: 8)
            reply_delay: Legacy fixed delay fallback (default: 1.0)
            reply_delay_min: Minimum random delay before sending a reply
            reply_delay_max: Maximum random delay before sending a reply
            proxy_config: Single proxy configuration dict (deprecated, use proxy_configs)
            proxy_configs: List of proxy configuration dicts with 'server', 'username', 'password' (optional)
            hide_after_login: Move browser window off-screen after login (default: False)
            shuffle_chat: Send each selected template's messages in random order (default: False)
            first_message_mode: Send the first message after its configured delay,
                                wait for a reply, or choose randomly
                                ("true", "false", or "random")
            first_message_delay_min: Minimum random delay before sending the first message
            first_message_delay_max: Maximum random delay before sending the first message
            chat_timing: Validated delays and rest intervals for the chat loop
        """
        super().__init__()
        
        self.device_type = device_type
        self.mobile_ua_file = mobile_ua_file
        self.desktop_ua_file = desktop_ua_file
        self.keywords_file = keywords_file
        self.domain_url = domain_url
        self.headless = headless  # Store headless flag
        self.chat_timeout = chat_timeout  # Store max bot replies
        self.reply_delay = reply_delay  # Store reply delay
        self.chat_timing = normalize_chat_timing(chat_timing)
        # Max minutes per conversation (None = until snap share, default)
        self.chat_time_minutes = chat_time_minutes
        # Picture sender (additive): folder + middle-chat-point settings
        self.pic_folder = pic_folder
        self.pic_send_enabled = bool(pic_send_enabled) and bool(pic_folder)
        self.pic_after_messages = max(1, int(pic_after_messages or 4))
        self.pics_per_chat = max(1, int(pics_per_chat or 1))
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
        first_message_mode = str(first_message_mode).lower()
        if first_message_mode not in {"true", "false", "random"}:
            first_message_mode = "false"
        self.first_message_mode = first_message_mode
        self.fixed_messages = fixed_messages or []  # Legacy single-template support
        self.fixed_message_templates = fixed_message_templates or []
        if not self.fixed_message_templates and self.fixed_messages:
            self.fixed_message_templates = [self.fixed_messages]
        self.message_template_selector = NonRepeatingRandomSelector(self.fixed_message_templates)
        self.accounts = accounts or []  # Store accounts for login
        self.account_mode = account_mode
        self.hide_after_login = hide_after_login  # Store hide_after_login flag
        self.shuffle_chat = shuffle_chat
        
        # Support both single proxy (legacy) and multiple proxies
        if proxy_configs:
            self.proxy_configs = proxy_configs if isinstance(proxy_configs, list) else [proxy_configs]
        elif proxy_config:
            self.proxy_configs = [proxy_config]
        else:
            self.proxy_configs = []
        
        # Handle snapchat IDs (support both single and multiple)
        if isinstance(snapchat_ids, list):
            self.snapchat_ids = snapchat_ids
        elif snapchat_ids:
            self.snapchat_ids = [snapchat_ids]
        else:
            self.snapchat_ids = []
        # Duplicate usernames cannot be selected without repeating the same value.
        self.snapchat_ids = list(dict.fromkeys(self.snapchat_ids))
        self.snapchat_id_selector = NonRepeatingRandomSelector(self.snapchat_ids)
        
        self.workers = {}  # thread_id -> BrowserWorker
        self.is_running = False
        self.thread_count = 0
        
        # Load keywords and user agents once (if provided)
        self.keywords = self._load_file_lines(keywords_file) if keywords_file else []
        self.mobile_uas = self._load_file_lines(mobile_ua_file) if mobile_ua_file else []
        self.desktop_uas = self._load_file_lines(desktop_ua_file) if desktop_ua_file else []
        self.all_uas = self.mobile_uas + self.desktop_uas
        
        # Thread-safe lock for keyword selection
        self.keyword_lock = threading.Lock()
        self.keyword_usage_count = {kw: 0 for kw in self.keywords}
        
        # Account tracking to prevent reusing failed accounts
        self.account_lock = threading.Lock()
        self.used_accounts = set()  # Set of account emails that have been used
        self.failed_accounts = set()  # Set of account emails that failed to login
        self.thread_accounts = {}  # thread_id -> account (current account assignment)
        self.accounts_exhausted = False
        self._account_exhaustion_announced = False
        
        # ---- Runtime orchestration ----
        # Single-browser mode is intentionally Python-only.  Go is lazy-loaded
        # only for multi-browser runs; the legacy Node/JS timing worker is no
        # longer started because its IPC adds overhead without a real benefit.
        self._runtime_cfg = load_runtime_config()
        self._engine = None
        self._js_worker = None
        self._engine_enabled = bool(self._runtime_cfg.get("go_engine_enabled", True))
        self._js_enabled = False
        self._stagger_interval = 1.5
        self._last_thread_start_time = 0.0
        self._initial_thread_count = 0
        # EngineBridge is intentionally lazy: importing/constructing the bridge
        # is unnecessary work for a single-browser run.

        # ---- Context-pool integration (2026 upgrade) ----
        # When account_mode == "restore" and the AccountManager has session
        # folders, we use the shared-browser context-pool architecture instead
        # of launching a separate browser per worker.  This is the "Golden
        # Rule": one browser, many isolated contexts — minimal memory, fast
        # startup, smooth parallel automation.
        self._context_pool = None
        self._account_manager = None
        self._use_context_pool = False
        self._context_pool_max = 50  # default; overridden by config
        try:
            from core.config_loader import load_context_pool_config
            cp_cfg = load_context_pool_config()
            self._context_pool_max = cp_cfg.get("max_contexts", 50)
            self._context_pool_headless = cp_cfg.get("headless", headless)
            self._context_pool_block_resources = cp_cfg.get("block_resource_types", [])
            self._context_pool_geoip = cp_cfg.get("geoip", True)
            self._context_pool_humanize = cp_cfg.get("humanize", True)
        except Exception:
            self._context_pool_headless = headless
            self._context_pool_block_resources = []
            self._context_pool_geoip = True
            self._context_pool_humanize = True

        # ---- Resource Governor (2026 upgrade — 1k-browser smooth) ----
        # A psutil-based tiered watchdog (throttle / recycle / redline) that
        # keeps the PC responsive under heavy load.  Driven by tick() so it
        # adds zero threads.  Degrades to a no-op if psutil is missing.
        self._governor = None
        try:
            from core.resource_governor import ResourceGovernor
            from core.config_loader import load_resource_governor_config
            gov_cfg = load_resource_governor_config()
            self._governor = ResourceGovernor(
                config=gov_cfg,
                log_fn=lambda msg: self.thread_log.emit(0, msg),
            )
        except Exception:
            self._governor = None
    
    @staticmethod
    def _load_file_lines(file_path):
        """Load lines from a file"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip()]
        except Exception as e:
            print(f"Error loading file {file_path}: {e}")
            return []
    
    def _get_random_keyword(self):
        """
        Get a random keyword with balanced distribution.
        Tries to use less-used keywords first.
        """
        with self.keyword_lock:
            if not self.keywords:
                return None
            
            # Sort keywords by usage count
            sorted_keywords = sorted(self.keyword_usage_count.items(), key=lambda x: x[1])
            
            # Pick from the least used 50% of keywords
            pool_size = max(1, len(sorted_keywords) // 2)
            keyword_pool = [kw for kw, _ in sorted_keywords[:pool_size]]
            
            # Randomly select from pool
            keyword = random.choice(keyword_pool)
            self.keyword_usage_count[keyword] += 1
            
            return keyword

    def _get_next_message_template(self):
        """Select the next message file for a chat across all workers."""
        return self.message_template_selector.get_next()

    def _return_message_template(self, template):
        """Return a template when its chat did not send any messages."""
        return self.message_template_selector.return_item(template)

    def _get_next_snapchat_id(self):
        """Select the next Snapchat username for a chat across all workers."""
        return self.snapchat_id_selector.get_next()
    
    def _get_random_ua_file(self):
        """Get a random user agent file (mobile or desktop)"""
        available_files = []
        if self.mobile_ua_file and self.mobile_uas:
            available_files.append(('mobile', self.mobile_ua_file))
        if self.desktop_ua_file and self.desktop_uas:
            available_files.append(('desktop', self.desktop_ua_file))
        
        if not available_files:
            return None, None
        
        ua_type, ua_file = random.choice(available_files)
        return ua_type, ua_file

    @staticmethod
    def _account_key(account):
        """Return the identity used to prevent account sharing/repetition."""
        if not isinstance(account, dict):
            return str(account)
        return str(
            account.get("session_key")
            or account.get("email")
            or account.get("account_key")
            or ""
        ).strip().lower()

    def _announce_account_exhausted(self, thread_id=None):
        """Announce exhaustion once while preserving active workers."""
        self.accounts_exhausted = True
        if not self._account_exhaustion_announced:
            self._account_exhaustion_announced = True
            message = (
                "All restored accounts exhausted."
                if self.account_mode == "restore"
                else "All accounts exhausted."
            )
            self.thread_log.emit(thread_id or 0, f"⚠ {message}")
            self.account_exhausted.emit(message)
    
    def _create_worker_with_random_config(self, thread_id, force_new_account=False):
        """
        Create a browser worker for Chitchat.gg automation.
        
        Args:
            thread_id: Unique identifier for this thread
            force_new_account: Force getting a new unused account (for restart after failure)
            
        Returns:
            ChitchatWorker configured for Chitchat automation, or None if no accounts available
        """
        # Assign account for this thread
        account = None
        if self.accounts:
            with self.account_lock:
                if thread_id in self.thread_accounts and not force_new_account:
                    # Keep an existing assignment if a caller recreates the
                    # same thread without explicitly requesting a replacement.
                    account = self.thread_accounts[thread_id]
                else:
                    # Claim the account while holding the lock.  Random choice
                    # plus the used set gives every thread a unique account and
                    # prevents repeats until this run is exhausted.
                    available_accounts = [
                        candidate
                        for candidate in self.accounts
                        if self._account_key(candidate) not in self.used_accounts
                        and self._account_key(candidate) not in self.failed_accounts
                    ]

                    if not available_accounts:
                        self._announce_account_exhausted(thread_id)
                        self.thread_log.emit(
                            thread_id,
                            f"✗ No more unused accounts available for thread {thread_id}",
                        )
                        return None

                    account = random.choice(available_accounts)
                    account_key = self._account_key(account)
                    self.used_accounts.add(account_key)
                    self.thread_accounts[thread_id] = account
                    if force_new_account:
                        self.thread_log.emit(
                            thread_id,
                            f"Assigned replacement account: {account.get('email', account_key)}",
                        )
        
        # Assign proxy for this thread
        # If we have multiple proxies, assign different ones to different threads
        # If we have fewer proxies than threads, cycle through them
        proxy_config = None
        if self.proxy_configs:
            if self.account_mode == "restore":
                # A restored account first uses the proxy saved with that
                # account. If it has none, the selected proxy file can provide
                # an initial random proxy.
                proxy_config = account.get("saved_proxy") if account else None
                if proxy_config is None:
                    proxy_config = random.choice(self.proxy_configs)
            else:
                # Keep the existing per-thread rotation for login/create mode.
                idx = (thread_id - 1) % len(self.proxy_configs)
                proxy_config = self.proxy_configs[idx]
        elif self.account_mode == "restore" and account:
            proxy_config = account.get("saved_proxy")
        
        # Create worker for Chitchat automation
        worker = ChitchatWorker(
            account=account,  # Pass account credentials for login
            account_mode=self.account_mode,
            fixed_messages=self.fixed_messages,  # Legacy fixed messages
            fixed_message_templates=self.fixed_message_templates,  # Filename + messages records
            headless=self.headless,  # Pass headless flag
            shuffle_chat=self.shuffle_chat,
            thread_id=thread_id,  # Pass thread ID for logging
            chat_timeout=self.chat_timeout,  # Pass max bot replies
            reply_delay=self.reply_delay,  # Pass reply delay
            first_message_mode=self.first_message_mode,  # Pass first-message behavior
            reply_delay_min=self.reply_delay_min,  # Pass minimum random reply delay
            reply_delay_max=self.reply_delay_max,  # Pass maximum random reply delay
            first_message_delay_min=self.first_message_delay_min,
            first_message_delay_max=self.first_message_delay_max,
            chat_timing=self.chat_timing,
            proxy_config=proxy_config,  # Pass proxy configuration for this thread
            proxy_fallback_configs=(
                self.proxy_configs if self.account_mode == "restore" else []
            ),
            hide_after_login=self.hide_after_login,  # Pass hide_after_login flag
            snapchat_id_selector=self._get_next_snapchat_id if self.snapchat_ids else None,
            message_template_selector=self._get_next_message_template if self.fixed_message_templates else None,
            message_template_returner=self._return_message_template if self.fixed_message_templates else None,
            context_pool_slot=self._acquire_pool_slot(account) if self._use_context_pool else None,
            engine_bridge=self._engine if self._engine is not None else None,
            chat_time_minutes=self.chat_time_minutes,
            pic_folder=self.pic_folder,
            pic_send_enabled=self.pic_send_enabled,
            pic_after_messages=self.pic_after_messages,
            pics_per_chat=self.pics_per_chat
        )
        
        # Store metadata for logging
        worker.assigned_keyword = 'per-chat' if self.snapchat_ids else 'N/A'
        worker.ua_type = 'desktop'
        worker.temp_keywords_file = None  # No temp files needed
        
        return worker
    
    def start_threads(self, thread_count):
        """
        Start multiple browser automation threads.
        
        Args:
            thread_count: Number of threads to start
        """
        if self.is_running:
            self.thread_log.emit(0, "Threads already running!")
            return
        
        self.is_running = True
        self.thread_count = thread_count
        self._initial_thread_count = thread_count

        # Resolve runtime mode once per run.
        mode = str(self._runtime_cfg.get("mode", "auto")).lower()
        multi_min = max(2, int(self._runtime_cfg.get("multi_browser_min", 2)))
        self._multi_runtime = thread_count >= multi_min and mode != "safe"
        self._engine_enabled = (
            self._multi_runtime
            and bool(self._runtime_cfg.get("go_engine_enabled", True))
            and thread_count >= int(self._runtime_cfg.get("go_engine_min_browsers", multi_min))
        )
        self._js_enabled = False
        self._use_context_pool = False

        # Hard invariant: one requested thread means one browser worker.
        # No Go supervisor, context pool, or JS worker is allowed in this mode.
        if thread_count <= 1:
            self._multi_runtime = False
            self._engine = None
            self._use_context_pool = False
            self.thread_log.emit(
                0,
                "[Runtime] SINGLE-BROWSER mode: exactly 1 worker/browser; Go/Node/ContextPool OFF"
            )

        # Wire the Resource Governor's callbacks to the engine + pool now that
        # both are about to start.  The governor uses these to throttle
        # launches, recycle bloated contexts, run Firefox GC, and kill the
        # worst browser process under redline pressure.
        self._wire_governor()
        
        # Start the Go concurrency engine (optional accelerator).  The engine
        # monitors CPU/mem and recommends an adaptive worker count; it also
        # provides global rate limiting and a worker heartbeat watchdog.
        self._maybe_start_engine(thread_count)
        
        # Start the shared-browser context pool (2026 Golden Rule).  When
        # account_mode == "restore", this launches ONE Camoufox browser and
        # creates isolated contexts inside it for each worker — far more
        # memory-efficient and smoother than launching a browser per worker.
        # Even in "login" mode, if saved sessions exist we try the pool
        # because per-worker browsers at >1 thread are the #1 cause of OOM
        # and PC hangs (each Camoufox = 200-300MB, Issue #87).
        # NOTE: the pool only helps when accounts have storage_state_path
        # (i.e. saved sessions).  In pure "login" mode the accounts are
        # email/password only, so the pool can't create restore contexts —
        # in that case we skip forcing pool mode and just warn.
        use_pool = (self._multi_runtime and self.account_mode == "restore" and bool(self._runtime_cfg.get("context_pool_enabled", True)))
        if self._multi_runtime and not use_pool and thread_count > 1:
            try:
                import os as _os
                _project_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                _sd = _os.path.join(_project_dir, "data", "account_sessions")
                if _os.path.isdir(_sd):
                    _has_sessions = any(
                        f.endswith(".json")
                        for f in _os.listdir(_sd)
                    )
                    if _has_sessions and self.account_mode == "restore":
                        use_pool = True
                        self.thread_log.emit(
                            0,
                            "[ContextPool] Multi-thread detected with saved "
                            "sessions — forcing shared-browser pool mode to "
                            "prevent OOM (per-worker = 200MB×N)."
                        )
            except Exception:
                pass
        # Phase 14: with one real Chrome profile per account the shared-browser
        # context pool is the wrong tool (a profile == a browser).  Skip it and
        # let every worker open its own profile instead.
        separate_profiles = False
        try:
            from browser import browser_identity as _bi
            separate_profiles = _bi.profile_mode(_bi.config()) == "persistent"
        except Exception:
            separate_profiles = False
        if separate_profiles and use_pool:
            self.thread_log.emit(
                0,
                "[Profile] one separate Chrome profile per account — context pool "
                "disabled (each worker opens its own browser profile)",
            )
            use_pool = False

        if use_pool:
            self._start_context_pool(thread_count)
        elif thread_count > 1:
            self.thread_log.emit(
                0,
                "[Warning] Running multiple threads in per-worker browser mode. "
                "Each browser uses ~200-300MB RAM. For smooth large-scale "
                "operation: save sessions first, then use 'restore' mode + the "
                "context pool (auto-activated when sessions exist)."
            )
        
        # Staggered thread starts: spacing out browser launches avoids a CPU
        # and memory spike when many workers start simultaneously.  The interval
        # is adaptive: more threads = slightly longer spacing, but we cap it so
        # startup doesn't take too long.  For 1 thread there is no stagger at all.
        stagger = self._stagger_interval
        if thread_count <= 1:
            stagger = 0.0
        elif thread_count <= 3:
            stagger = max(0.5, stagger * 0.5)   # small runs: 0.75 s
        elif thread_count <= 10:
            stagger = stagger * 0.75             # medium runs: ~1.1 s
        # else: keep the full 1.5 s for large runs

        # --- Resource-aware launch gate (Go engine) ---
        # Tell the engine the maximum number of browsers that may launch
        # simultaneously.  With many threads, capping concurrent launches to
        # 2-3 keeps the PC responsive; the launch gate (in browser_automation)
        # blocks individual workers until a slot frees up.
        if self._engine and self._engine.is_running():
            try:
                if thread_count <= 3:
                    launch_cap = 2
                elif thread_count <= 8:
                    launch_cap = 3
                else:
                    launch_cap = 4
                # BUG C fix: clear any stale in-flight launch counter left
                # by an interrupted previous run BEFORE setting the new cap,
                # so this run's browser launches are never blocked by a
                # phantom "gate full" state from the last run.
                try:
                    self._engine.reset_launch_gate()
                except Exception:
                    pass
                self._engine.set_launch_max(launch_cap)
                self.thread_log.emit(
                    0,
                    f"[engine] Browser launch gate: max {launch_cap} concurrent "
                    f"launches for {thread_count} threads",
                )
            except Exception:
                pass

        for i in range(thread_count):
            thread_id = i + 1
            self._start_single_thread(thread_id)
            # Stagger all but the last start.
            if i < thread_count - 1 and stagger > 0:
                # Resource-aware pause: if the engine reports high CPU or
                # memory pressure, extend the stagger so the next browser
                # doesn't push the machine over the edge.
                effective_stagger = stagger
                if self._engine and self._engine.is_running():
                    try:
                        snap = self._engine.resource_snapshot()
                        cpu_avg = snap.get("cpu_avg", 0.0)
                        mem_avg = snap.get("mem_avg", 0.0)
                        if cpu_avg > 0.75 or mem_avg > 0.75:
                            effective_stagger = stagger * 2.0
                            self.thread_log.emit(
                                0,
                                f"[engine] High resource pressure "
                                f"(cpu={cpu_avg:.0%} mem={mem_avg:.0%}); "
                                f"extending stagger to {effective_stagger:.1f}s",
                            )
                    except Exception:
                        pass
                # Use event-loop-friendly wait instead of blocking sleep
                import time as _time
                deadline = _time.monotonic() + effective_stagger
                while _time.monotonic() < deadline:
                    from PyQt6.QtCore import QCoreApplication
                    QCoreApplication.processEvents()
                    _time.sleep(0.05)
                # Forward any engine logs/events that arrived during the wait.
                self._drain_engine_messages()
    
    def _start_single_thread(self, thread_id, force_new_account=False):
        """
        Start a single browser automation thread with continuous loop.
        
        Args:
            thread_id: Unique identifier for this thread
            force_new_account: Force getting a new unused account (for restart after failure)
        """
        worker = self._create_worker_with_random_config(thread_id, force_new_account)
        if not worker:
            self.thread_log.emit(thread_id, f"Cannot start thread {thread_id}: No available accounts")
            
            # Emit finished signal since we can't start
            self.thread_finished.emit(thread_id)
            
            # Remove from workers dict if present
            if thread_id in self.workers:
                del self.workers[thread_id]
            
            # Check if all threads finished
            if len(self.workers) == 0:
                self.all_threads_finished.emit()
            
            return
        
        # Connect signals with thread_id
        worker.log_signal.connect(lambda msg: self.thread_log.emit(thread_id, msg))
        worker.status_signal.connect(lambda status: self.thread_status.emit(thread_id, status))
        worker.chat_signal.connect(
            lambda who, text: self.chat_message.emit(thread_id, who, text))
        worker.session_signal.connect(
            lambda status: self.session_status.emit(thread_id, status))
        worker.proxy_warning_signal.connect(
            lambda message: self.proxy_warning.emit(thread_id, message)
        )
        worker.captcha_signal.connect(
            lambda tid: self.captcha_alert.emit(tid)
        )
        worker.finished_signal.connect(lambda: self._on_thread_finished(thread_id))
        
        # Store worker
        self.workers[thread_id] = worker
        
        # Emit start signal
        self.thread_started.emit(
            thread_id, 
            f"Thread {thread_id} starting: Keyword='{worker.assigned_keyword}', UA={worker.ua_type}"
        )
        
        # Start the worker thread
        worker.start()
        
        # Register this worker with the Go engine so it is tracked for
        # heartbeat/watchdog and counted in the adaptive scaler.
        self._register_worker_with_engine(thread_id)
    
    # ------------------------------------------------------------------
    # Context-pool integration (2026 upgrade — Golden Rule: one browser,
    # many contexts)
    # ------------------------------------------------------------------
    
    def _start_context_pool(self, thread_count):
        """Initialize the shared-browser context pool and account manager.
        
        Called from start_threads() when account_mode == 'restore'.  This
        launches a single Camoufox browser and prepares the AccountManager
        so each worker can acquire an isolated context from the pool.
        """
        if self._context_pool is not None:
            return  # already started
        
        try:
            from core.account_manager import AccountManager
            from browser.context_pool import ContextPool, reset_pool
            
            # Discover accounts from EVERY session root (account_sessions/,
            # data/account_sessions/ and EVA_SESSIONS_DIR).  Phase 13: this is
            # what makes "acc auto browser e load" actually find the sessions
            # the bot saved earlier.
            self._account_manager = AccountManager()
            self._account_manager.load()   # no argument = all roots
            summary = self._account_manager.summary()
            total = summary.get("total", 0)
            idle = summary.get("idle", 0)
            blind = summary.get("blind", 0)
            repairable = summary.get("repairable", 0)

            if total == 0:
                self.thread_log.emit(0, "[ContextPool] No accounts found in any "
                                        "account_sessions/ root; using per-worker browsers")
                return

            self.thread_log.emit(0, f"[ContextPool] Loaded {total} accounts ({idle} idle, "
                                    f"{blind} blind{f', {repairable} repairable' if repairable else ''}) "
                                    f"— activating shared-browser pool")
            self.thread_log.emit(0, self._account_manager.format_summary())
            
            # Cap the pool size to the number of *usable* accounts and the
            # configured max_contexts.  Blind sessions get no context.
            usable = max(0, total - blind) + repairable
            max_ctx = min(self._context_pool_max, max(usable, idle), thread_count)
            if max_ctx < 1:
                max_ctx = 1
            
            # Reset any stale pool from a previous run.
            reset_pool()
            
            self._context_pool = ContextPool(
                max_contexts=max_ctx,
                headless=self._context_pool_headless,
                geoip=self._context_pool_geoip,
                humanize=self._context_pool_humanize,
                block_resource_types=self._context_pool_block_resources,
                viewport_jitter=True,
                verbose=False,
            )
            
            # Start the shared browser (one launch for all workers).
            started = self._context_pool.start(fingerprint=None, proxy=None)
            if not started:
                self.thread_log.emit(0, "[ContextPool] ✗ Failed to start shared browser; falling back to per-worker browsers")
                self._context_pool = None
                return
            
            self._use_context_pool = True
            self.thread_log.emit(0, f"[ContextPool] ✓ Shared browser started — pool size: {max_ctx} contexts max")
            
            # Tell the Go engine about the context pool limit.
            if self._engine is not None:
                try:
                    self._engine.set_ctx_max(max_ctx)
                except Exception:
                    pass
        
        except Exception as error:
            self.thread_log.emit(0, f"[ContextPool] Error starting pool: {error} — using per-worker browsers")
            self._context_pool = None
            self._use_context_pool = False
    
    def _acquire_pool_slot(self, account):
        """Acquire an isolated context from the pool for the given account.
        
        Returns a dict with 'browser', 'context', 'slot_id' keys, or None
        if the pool is unavailable or the account has no storage_state.
        """
        if not self._use_context_pool or self._context_pool is None:
            return None
        
        try:
            storage_state_path = account.get("storage_state_path")
            if not storage_state_path:
                return None

            # Phase 13: never build a context from a blind session — it would
            # just open an unauthenticated page.  A repairable session is
            # allowed through: the worker logs in again inside the context.
            try:
                from browser import session_health as _sh
                allowed, reason, plan = _sh.context_pool_allowed(account)
                if not allowed:
                    self.thread_log.emit(
                        0,
                        f"[ContextPool] BLIND SESSION {account.get('email', '?')} — "
                        f"skipping ({reason})",
                    )
                    return None
                if plan.get("blind"):
                    self.thread_log.emit(
                        0,
                        f"[ContextPool] {account.get('email', '?')} session is blind — "
                        f"the worker will repair it with a login",
                    )
            except Exception:
                pass

            fingerprint = account.get("fingerprint_path")
            proxy = account.get("saved_proxy") or account.get("proxy")

            # Phase 14: every pooled context gets the account's permanent device
            # profile, so N parallel accounts look like N different browsers.
            identity_options: Dict[str, Any] = {}
            stealth_fingerprint = None
            try:
                from browser import browser_identity as _bi
                _key = _bi.account_key_for(account)
                options = _bi.pool_options(
                    account,
                    log_fn=lambda line: self.thread_log.emit(0, line),
                )
                identity_options = options.get("extra_context_options") or {}
                stealth_fingerprint = options.get("stealth_fingerprint")
                if identity_options:
                    self.thread_log.emit(
                        0,
                        f"[Identity] {_key} → {_bi.describe(options.get('identity'))}",
                    )
            except Exception as _identity_err:
                self.thread_log.emit(0, f"[Identity] pool identity unavailable: {_identity_err}")

            slot = self._context_pool.create_context(
                account_key=account.get("email", "unknown"),
                storage_state_path=storage_state_path,
                fingerprint=fingerprint,
                proxy=proxy,
                extra_context_options=identity_options or None,
                stealth_fingerprint=stealth_fingerprint,
            )
            
            if slot is None:
                self.thread_log.emit(0, "[ContextPool] Pool full or error — worker will use its own browser")
                return None
            
            return {
                "slot_id": slot.slot_id,
                "browser": self._context_pool._browser,
                "context": slot.context,
            }
        except Exception as error:
            self.thread_log.emit(0, f"[ContextPool] Error acquiring context: {error}")
            return None
    
    def _release_pool_slot(self, slot_id):
        """Release a context back to the pool after a worker finishes."""
        if self._context_pool is None or slot_id is None:
            return
        try:
            self._context_pool.close_context(slot_id)
        except Exception:
            pass
    
    def _stop_context_pool(self):
        """Shut down the context pool and the shared browser."""
        if self._context_pool is None:
            return
        try:
            self._context_pool.close_all()
            self.thread_log.emit(0, "[ContextPool] ✓ Shared browser and all contexts closed")
        except Exception as error:
            self.thread_log.emit(0, f"[ContextPool] Warning during shutdown: {error}")
        finally:
            self._context_pool = None
            self._use_context_pool = False
            try:
                from browser.context_pool import reset_pool
                reset_pool()
            except Exception:
                pass
    
    # ------------------------------------------------------------------
    # Go engine + JS worker integration helpers
    # ------------------------------------------------------------------
    
    def _wire_governor(self):
        """Connect the Resource Governor's callbacks to the engine + pool.

        Called once at the start of ``start_threads()``.  Safe to call when
        the engine or pool is off — the governor's callbacks are simply left
        as None and those tiers become no-ops.
        """
        if self._governor is None:
            return
        g = self._governor

        # Throttle: deny new launches via the Go engine's launch gate.
        def _deny(reason: str):
            if self._engine and self._engine.is_running():
                try:
                    # Setting max to 0 effectively denies all launches.
                    self._engine.set_launch_max(0)
                except Exception:
                    pass

        g.deny_launches_cb = _deny

        # Un-throttle: restore a sane launch cap + tell engine to adapt.
        def _restore_max(cap: int):
            if self._engine and self._engine.is_running():
                try:
                    self._engine.set_launch_max(cap)
                    # Also let the engine adaptively restore (handles the
                    # case where shrinkUnderPressure had lowered it further).
                    self._engine.launch_adapt(cap)
                except Exception:
                    pass

        g.set_launch_max_cb = _restore_max

        # Recycle: close the n oldest pooled contexts + signal engine.
        def _recycle(n: int) -> int:
            if self._engine and self._engine.is_running():
                try:
                    self._engine.signal_recycle()
                except Exception:
                    pass
            if self._context_pool is not None:
                try:
                    return self._context_pool.recycle_oldest(n)
                except Exception:
                    return 0
            return 0

        g.recycle_contexts_cb = _recycle

        # Firefox GC: minimize memory on all live pages.
        def _firefox_gc():
            if self._context_pool is not None:
                try:
                    self._context_pool.trigger_firefox_gc()
                except Exception:
                    pass

        g.firefox_gc_cb = _firefox_gc

        # Never kill an arbitrary Firefox/Camoufox process.  Redline pressure
        # is reported to the UI/log and new launches can be throttled, but
        # unrelated browser processes are left untouched.
        g.kill_worst_browser_cb = None

    def _maybe_start_engine(self, thread_count):
        """Start the optional Go supervisor only for multi-browser runs."""
        if not self._engine_enabled or thread_count < 2:
            self._engine = None
            return
        try:
            from core.engine_bridge import EngineBridge
            self._engine = EngineBridge()
            if self._engine.start():
                self._engine.set_config(
                    max_workers=max(1, thread_count),
                    min_workers=1,
                )
                self.thread_log.emit(0, "[Runtime] Multi-browser mode: Go supervisor enabled")
            else:
                err = self._engine.start_error() or "unknown reason"
                self._engine = None
                self.thread_log.emit(0, f"[Runtime] Go supervisor unavailable ({err}); using Python scheduler")
        except Exception as exc:
            self._engine = None
            self.thread_log.emit(0, f"[Runtime] Go supervisor unavailable ({exc}); using Python scheduler")
        # JS worker intentionally stays disabled.  Python handles the tiny
        # timing calculations locally, avoiding a Node subprocess + IPC.
    
    def _register_worker_with_engine(self, thread_id):
        """Tell the Go engine about a new worker."""
        if self._engine and self._engine.is_running():
            try:
                self._engine.register_worker(thread_id)
                self._engine.heartbeat(thread_id)
            except Exception:
                pass
    
    def _unregister_worker_with_engine(self, thread_id):
        """Tell the Go engine a worker is gone."""
        if self._engine and self._engine.is_running():
            try:
                self._engine.unregister_worker(thread_id)
            except Exception:
                pass
    
    def _drain_engine_messages(self):
        """Forward engine log/event messages to the GUI log window.

        Also sends periodic heartbeats for all running workers so the Go
        engine's watchdog does not flag them as hung (heartbeat timeout is
        ~120 s by default).
        """
        if not self._engine or not self._engine.is_running():
            return
        try:
            # Send a heartbeat for every worker that is still alive.  This
            # keeps the engine's watchdog happy without each worker needing
            # its own heartbeat thread.
            for thread_id, worker in self.workers.items():
                if worker.isRunning():
                    self._engine.heartbeat(thread_id)
            for log_msg in self._engine.drain_logs():
                level = log_msg.get("level", "info")
                text = log_msg.get("msg", "")
                prefix = {"warn": "⚠ [engine] ", "error": "✗ [engine] "}.get(level, "[engine] ")
                self.thread_log.emit(0, prefix + str(text))
            for evt in self._engine.drain_events():
                etype = evt.get("event", "")
                if etype == "worker_hung":
                    wid = evt.get("worker_id")
                    self.thread_log.emit(wid or 0, f"⚠ [engine] worker {wid} flagged hung (no heartbeat)")
                elif etype == "worker_recovered":
                    wid = evt.get("worker_id")
                    self.thread_log.emit(wid or 0, f"[engine] worker {wid} recovered")
                elif etype == "scale_change":
                    frm = evt.get("from")
                    to = evt.get("to")
                    reason = evt.get("reason", "")
                    self.thread_log.emit(0, f"[engine] scale {frm}→{to} ({reason})")
                elif etype == "mem_pressure":
                    self.thread_log.emit(0, "⚠ [engine] mem_pressure: launch cap shrunk (governor redline)")
                elif etype == "recycle":
                    self.thread_log.emit(0, "⚠ [engine] recycle: context creation paused (governor recycle)")
        except Exception:
            pass
    
    def tick(self):
        """Periodic maintenance call (hook this into a GUI timer, ~1 s).

        Currently forwards engine logs/events and sends worker heartbeats so
        the Go engine watchdog does not flag live workers as hung.
        """
        if self._engine and self._engine.is_running():
            self._drain_engine_messages()
        # Resource Governor: adaptive RAM/CPU watchdog. Runs every tick but
        # internally throttles its own sampling to sample_interval (~2s) so it
        # adds negligible overhead. This is the key anti-hang mechanism.
        try:
            if self._governor is not None:
                self._governor.tick()
        except Exception as _ge:
            try:
                self.thread_log.emit(0, f"[governor] tick error: {_ge}")
            except Exception:
                pass

    def _engine_recommended_workers(self):
        """Return the Go engine's recommended worker count, or -1 if N/A."""
        if self._engine and self._engine.is_running():
            try:
                return self._engine.recommended_workers()
            except Exception:
                return -1
        return -1
    
    def _engine_acquire(self, thread_id):
        """Ask the Go rate limiter for permission to act. Returns (ok, wait_ms)."""
        if self._engine and self._engine.is_running():
            try:
                return self._engine.acquire(thread_id)
            except Exception:
                return True, 0.0
        return True, 0.0
    
    def _engine_pop_hung(self):
        """Return worker IDs the engine flagged as hung."""
        if self._engine and self._engine.is_running():
            try:
                return self._engine.pop_hung_workers()
            except Exception:
                return []
        return []
    
    def _js_reaction_pause(self):
        if self._js_worker and self._js_worker.is_running():
            try:
                return self._js_worker.reaction_pause()
            except Exception:
                pass
        import random as _r
        return _r.uniform(0.25, 0.85)
    
    def _js_read_reply(self):
        if self._js_worker and self._js_worker.is_running():
            try:
                return self._js_worker.read_reply()
            except Exception:
                pass
        import random as _r
        return _r.uniform(0.8, 2.5)
    
    def _js_new_chat_delay(self):
        if self._js_worker and self._js_worker.is_running():
            try:
                return self._js_worker.new_chat_delay()
            except Exception:
                pass
        import random as _r
        return _r.uniform(3.0, 5.0)
    
    def set_engine_enabled(self, enabled):
        """Toggle the Go/JS engines at runtime."""
        self._engine_enabled = bool(enabled)
        self._js_enabled = bool(enabled)
    
    def get_engine_status(self):
        """Return a summary of engine/worker state for the GUI."""
        status = {
            "engine_running": bool(self._engine and self._engine.is_running()),
            "js_running": bool(self._js_worker and self._js_worker.is_running()),
            "recommended_workers": self._engine_recommended_workers(),
        }
        if self._engine and self._engine.is_running():
            status.update(self._engine.resource_snapshot())
        return status

    def _on_thread_finished(self, thread_id):
        """
        Handle thread completion and restart if still running.

        Args:
            thread_id: ID of finished thread
        """
        # Unregister from Go engine so it stops watching this worker
        self._unregister_worker_with_engine(thread_id)

        worker = self.workers.get(thread_id)

        # RELEASE the old worker's context-pool slot BEFORE restarting.
        # BUG B fix: the previous code restarted the thread without releasing
        # the finished worker's pool slot, so the pool's semaphore leaked one
        # permit per restart.  After enough restarts the semaphore hit zero and
        # every new worker blocked forever on create_context() — the bot froze
        # and no new browser/context could be acquired.
        if worker is not None:
            self._cleanup_worker_temp_files(worker)
            self._release_worker_pool_slot(worker)

        # If still running, check if we should restart this thread
        if self.is_running and not self.accounts_exhausted:
            # Only mark the account as failed if the worker reported an error.
            # Normal chat completion should NOT burn the account.
            with self.account_lock:
                current_account = self.thread_accounts.get(thread_id)
                if current_account:
                    account_key = self._account_key(current_account)
                    # Check if the worker actually failed (not just normal completion)
                    worker_obj = self.workers.get(thread_id)
                    had_error = False
                    if worker_obj is not None:
                        last_status = getattr(worker_obj, '_last_status', '') or ''
                        had_error = any(kw in last_status.lower() for kw in
                                        ('ban', 'captcha', 'crash', 'error', 'fail', 'timeout'))
                    if had_error:
                        self.failed_accounts.add(account_key)
                        self.thread_log.emit(
                            thread_id,
                            f"Marked account as failed: {current_account.get('email', account_key)}",
                        )
                    else:
                        self.thread_log.emit(
                            thread_id,
                            f"Account completed normally: {current_account.get('email', account_key)}",
                        )

            # Try to restart with a new unused account
            self.thread_log.emit(thread_id, f"Thread {thread_id} stopped. Attempting restart with new account...")

            # Schedule restart without blocking the Qt GUI thread.
            from PyQt6.QtCore import QTimer
            restart_delay_ms = int(random.uniform(1000.0, 3000.0))

            # Drop the finished worker from the registry so the new worker
            # for this thread_id is the only one tracked.
            if thread_id in self.workers:
                del self.workers[thread_id]

            # Restart thread with new account (acquires a FRESH pool slot).
            # QTimer keeps this work on the Qt event loop instead of sleeping
            # inside the GUI thread.
            QTimer.singleShot(
                restart_delay_ms,
                lambda tid=thread_id: self._start_single_thread(tid, force_new_account=True)
                if self.is_running else None,
            )
        else:
            # Emit finished signal
            self.thread_finished.emit(thread_id)

            # Remove from workers dict
            if thread_id in self.workers:
                del self.workers[thread_id]

            # Check if all threads finished
            if len(self.workers) == 0:
                self.all_threads_finished.emit()

    def _release_worker_pool_slot(self, worker):
        """Release a finished worker's context-pool slot back to the pool.

        The worker's automation may have already closed its context in its
        own ``run()`` finally-block, but the pool's bookkeeping (the slot
        id + semaphore permit) is only released via ``close_context``.
        Without this call, restarts leak pool permits (BUG B).
        Safe to call when the pool is off or the worker had no slot.
        """
        if worker is None:
            return
        try:
            slot = getattr(worker.automation, "_context_pool_slot", None)
        except Exception:
            slot = None
        if slot is None:
            return
        slot_id = slot.get("slot_id") if isinstance(slot, dict) else None
        if slot_id is not None:
            self._release_pool_slot(slot_id)
        # Mark it consumed so we never double-release.
        try:
            worker.automation._context_pool_slot = None
        except Exception:
            pass
    
    def stop_all_threads(self):
        """Request a clean asynchronous shutdown; never terminate a QThread.

        Browser automation owns thread-affine Playwright/Camoufox objects.
        Hard-killing that thread is unsafe and was a major source of frozen
        GUIs, orphaned Firefox processes, and broken next-run launches.
        This method is intended to run outside the GUI thread; it waits for
        natural worker completion and leaves late workers to finish through
        their normal ``finished`` signal.  If a straggler exceeds the graceful
        window, its browser/context is force-closed from this thread so no
        browser stays open after a stop (see the force-cleanup block below).
        """
        self.thread_log.emit(0, "Stopping all threads (clean shutdown)...")
        self.is_running = False

        for thread_id, worker in list(self.workers.items()):
            try:
                self.thread_log.emit(thread_id, f"Stop requested for thread {thread_id}")
                worker.stop()
            except Exception as exc:
                self.thread_log.emit(thread_id, f"Stop request error: {exc}")

        import time
        deadline = time.monotonic() + 10.0  # Reduced from 30s to 10s
        while self.workers and time.monotonic() < deadline:
            alive = False
            for worker in list(self.workers.values()):
                try:
                    if worker.isRunning():
                        alive = True
                        worker.wait(100)
                except Exception:
                    pass
            if not alive:
                break
            time.sleep(0.05)

        # Do not call terminate(), force_cleanup(), or global taskkill here.
        # If a browser takes longer than the graceful window, its own finally
        # block still owns cleanup and the Qt finished signal will remove it.
        remaining = [tid for tid, w in self.workers.items() if w.isRunning()]
        if remaining:
            self.thread_log.emit(
                0,
                f"[Shutdown] {len(remaining)} browser worker(s) still closing; "
                "Force closing their browsers...",
            )
            # Force-close each stuck worker's browser/context from THIS thread
            # so no browser window/process is left behind when the user stops
            # the bot.  ``force_cleanup`` was built exactly for this: it closes
            # the thread-affine Camoufox/Playwright objects and swallows
            # greenlet errors.  The worker thread then exits naturally when its
            # pending browser call errors out; run()'s own finally-block is
            # idempotent, so closing again is harmless.  We still NEVER call
            # terminate() / taskkill — clean thread exit paths stay intact.
            for tid, worker in list(self.workers.items()):
                try:
                    worker.force_cleanup()
                except Exception:
                    pass
                try:
                    worker.stop()
                    worker.wait(2000)  # Wait 2 seconds max
                except Exception:
                    pass
            # Clean up context pool, Go engine, and flags even on force path
            self._stop_context_pool()
            if self._engine is not None:
                try:
                    self._engine.shutdown()
                    self.thread_log.emit(0, "[Runtime] Go supervisor stopped")
                except Exception:
                    pass
            self._engine = None
            self._js_worker = None
            self._account_exhaustion_announced = False
            self.accounts_exhausted = False
            self.thread_log.emit(0, "Force shutdown complete")
            return

        # All workers are naturally finished.  Auxiliary services can now
        # shut down without racing a browser operation.
        self._stop_context_pool()
        if self._engine is not None:
            try:
                self._engine.shutdown()
                self.thread_log.emit(0, "[Runtime] Go supervisor stopped")
            except Exception:
                pass
        self._engine = None
        self._js_worker = None
        self._account_exhaustion_announced = False
        self.accounts_exhausted = False
        self.thread_log.emit(0, "Clean shutdown complete")

    @staticmethod
    def _cleanup_worker_temp_files(worker):
        """Remove a worker's temp keyword file if present. Never raises."""
        if hasattr(worker, 'temp_keywords_file'):
            try:
                import os
                if worker.temp_keywords_file and os.path.exists(worker.temp_keywords_file):
                    os.remove(worker.temp_keywords_file)
            except Exception:
                pass
    
    def get_thread_status(self):
        """
        Get status of all threads.
        
        Returns:
            dict: thread_id -> is_running
        """
        return {
            thread_id: worker.isRunning() 
            for thread_id, worker in self.workers.items()
        }
    
    def get_active_thread_count(self):
        """Get number of currently active threads"""
        return len([w for w in self.workers.values() if w.isRunning()])
    
    def get_keyword_statistics(self):
        """
        Get keyword usage statistics.
        
        Returns:
            dict: keyword -> usage_count
        """
        return self.keyword_usage_count.copy()
