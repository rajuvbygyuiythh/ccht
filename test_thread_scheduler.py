#!/usr/bin/env python3
"""test_thread_scheduler.py — adaptive thread management + session→chat runner.

Two things are verified here (all offline, no browser, no psutil needed — the
load sampler is injected):

A. ``core/thread_scheduler.py``
   capacity planning · scale up/down with hysteresis + cooldown · freeze
   protection (pause gate) · resume · status output · disabled/no-psutil
   degradation.
B. ``entry/thread_manager.py`` integration
   clamp of the requested thread count, scale-up starts a thread, scale-down
   winds one down without restarting it, manual override.
C. ``tools/thread_planner.py`` CLI.
D. ``tools/session_chat.py`` — the cookie/session → chat pipeline runner:
   account selection, plan output, dry-run wiring, watchdog, summary.

Run:  python test_thread_scheduler.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from core.thread_scheduler import (DEFAULTS, PauseGate, ThreadScheduler,  # noqa: E402
                                   capacity_plan, format_plan, pause_gate,
                                   reset_pause_gate)
from test_session_health import cookie, make_session  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK ] {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  [FAIL] {name} {detail}")


def sample(cpu=0.2, mem=0.4, free_mb=6000, cores=8, total_mb=16000):
    return {"cpu": cpu, "mem": mem, "free_mb": free_mb, "cores": cores,
            "total_mb": total_mb, "ok": True}


class Loads:
    """A settable fake load sampler."""

    def __init__(self, **kwargs):
        self.value = sample(**kwargs)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return dict(self.value)

    def set(self, **kwargs):
        self.value = sample(**kwargs)


# --------------------------------------------------------------------------- #
# A. scheduler
# --------------------------------------------------------------------------- #

def test_capacity_plan() -> None:
    print("\n— A. capacity plan: how many browsers does this PC carry? —")
    plan = capacity_plan({"ram_per_browser_mb": 500, "reserve_mb": 2000, "max_threads": 8},
                         sample=sample(free_mb=7000, cores=8))
    check("RAM decides the count", plan["ram_threads"] == 10, str(plan["ram_threads"]))
    check("CPU cores cap the count", plan["cpu_threads"] == 6, str(plan["cpu_threads"]))
    check("recommendation respects both + the ceiling",
          plan["recommended_threads"] == 6, str(plan["recommended_threads"]))
    check("start never exceeds the recommendation",
          plan["start_threads"] <= plan["recommended_threads"])

    tight = capacity_plan({"ram_per_browser_mb": 500, "reserve_mb": 2000},
                          sample=sample(free_mb=1800, cores=4))
    check("little free RAM → at least one thread", tight["recommended_threads"] == 1,
          str(tight["recommended_threads"]))
    huge = capacity_plan({"max_threads": 3}, sample=sample(free_mb=64000, cores=32))
    check("ceiling still wins on a big machine", huge["recommended_threads"] == 3)
    check("plan text is a readable line", "recommended" in format_plan(plan), format_plan(plan))
    check("unknown sampler does not crash",
          capacity_plan({}, sample={"ok": False})["recommended_threads"] >= 1)


def test_scaling() -> None:
    print("\n— A2. scale up / scale down with hysteresis —")
    reset_pause_gate()
    loads = Loads(cpu=0.2, mem=0.3, free_mb=8000)
    calls = []
    logs = []
    scheduler = ThreadScheduler(
        {"start_threads": 2, "max_threads": 5, "ramp_seconds": 0,
         "scale_cooldown_seconds": 10, "sample_interval": 0, "log_interval_seconds": 0},
        sampler=loads, log_fn=logs.append,
        set_threads_cb=lambda target, reason: calls.append(target))
    check("scheduler enabled with a sampler", scheduler.enabled is True)
    check("starts at the configured start_threads", scheduler.target_threads == 2)

    now = 1000.0
    check("low load → scale up", scheduler.tick(now)["action"] == "scaled-up")
    check("callback applied the new target", calls == [3], str(calls))
    check("cooldown blocks a second change immediately",
          scheduler.tick(now + 1)["action"] == "want-up")
    now += 20
    check("after the cooldown it scales up again",
          scheduler.tick(now)["action"] == "scaled-up")
    check("ceiling respected", scheduler.target_threads <= 5)

    # cooldown elapsed, no further change because the ceiling is reached
    for step in range(1, 6):
        now += 20
        scheduler.tick(now)
    check("never grows past max_threads", scheduler.target_threads == 5,
          str(scheduler.target_threads))

    loads.set(cpu=0.88, mem=0.7, free_mb=6000)     # high, but not redline
    now += 100
    check("high CPU → scale down",
          scheduler.tick(now)["action"] == "scaled-down")
    check("scaled down by one step", scheduler.target_threads == 4, str(scheduler.target_threads))

    loads.set(cpu=0.97, mem=0.95, free_mb=200)     # redline
    now += 100
    scheduler.tick(now)
    check("redline pauses AND sheds a thread",
          pause_gate().is_paused() and scheduler.target_threads == 3,
          f"paused={pause_gate().is_paused()} target={scheduler.target_threads}")
    reset_pause_gate()
    loads.set(cpu=0.20, mem=0.5, free_mb=800)
    now += 100
    result = scheduler.tick(now)
    check("growth blocked when RAM is short",
          result["action"] in ("want-up", "steady"), str(result))
    now += 100
    before = scheduler.target_threads
    scheduler.tick(now)
    check("a RAM shortage never adds a thread",
          scheduler.target_threads == before, f"{before} → {scheduler.target_threads}")

    # low RAM must not scale up even when CPU is idle
    scheduler.target_threads = 2
    loads.set(cpu=0.1, mem=0.4, free_mb=2600)
    now += 100
    scheduler.tick(now)
    check("RAM budget check keeps the thread count", scheduler.target_threads == 2,
          str(scheduler.target_threads))


def test_freeze_protection() -> None:
    print("\n— A3. freeze protection: pause instead of hang —")
    reset_pause_gate()
    loads = Loads(cpu=0.2, mem=0.4, free_mb=6000)
    logs = []
    paused = []
    resumed = []
    scheduler = ThreadScheduler(
        {"start_threads": 3, "sample_interval": 0, "ramp_seconds": 0,
         "scale_cooldown_seconds": 0, "log_interval_seconds": 0},
        sampler=loads, log_fn=logs.append,
        pause_cb=paused.append, resume_cb=lambda: resumed.append(True))
    now = 2000.0

    loads.set(cpu=0.97, mem=0.93, free_mb=300)
    result = scheduler.tick(now)
    check("red load → paused", result["action"] == "paused")
    check("the pause gate is closed", pause_gate().is_paused() is True)
    check("the reason is stored", "heavy load" in pause_gate().reason(), pause_gate().reason())
    check("the pause callback fired", paused == ["PC under heavy load"], str(paused))
    check("the pause is logged",
          any("paused all workers" in line for line in logs), str(logs))

    # still red: no work, no repeated log spam
    check("stays paused while the load is red",
          scheduler.tick(now + 10)["action"] == "paused-wait")
    check("the gate is still closed", pause_gate().is_paused() is True)

    loads.set(cpu=0.35, mem=0.45, free_mb=6000)
    result = scheduler.tick(now + 20)
    check("cool load → resumed", result["action"] == "resumed")
    check("the gate reopened", pause_gate().is_paused() is False)
    check("the resume callback fired", resumed == [True], str(resumed))

    # a worker waiting on the gate is released
    reset_pause_gate()
    gate = pause_gate()
    gate.pause("unit test")
    stop_event = threading.Event()
    released = {"ok": None}

    def waiter():
        released["ok"] = gate.wait_if_paused(stop_event=stop_event, poll=0.05)
    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    time.sleep(0.15)
    check("a pause really blocks a worker", released["ok"] is None and thread.is_alive())
    gate.resume()
    thread.join(timeout=2.0)
    check("resume releases the worker", released["ok"] is True)

    # stopping the bot while paused must return immediately
    gate.pause("unit test 2")
    stop_event.set()
    started = time.time()
    value = gate.wait_if_paused(stop_event=stop_event, poll=0.05)
    check("Stop wins over a pause", value is False and time.time() - started < 1.0,
          f"value={value}")
    reset_pause_gate()

    # the gate is honest about time spent paused
    gate.pause("timing")
    time.sleep(0.1)
    check("paused_for reports elapsed seconds", gate.paused_for() >= 0.05,
          str(gate.paused_for()))
    gate.resume()
    check("paused_for resets after resume", gate.paused_for() == 0.0)


def test_degradation() -> None:
    print("\n— A4. safe degradation —")
    scheduler = ThreadScheduler({"enabled": False}, sampler=Loads(),
                                set_threads_cb=lambda t, r: None)
    check("disabled scheduler reports 'disabled'",
          scheduler.tick()["action"] == "disabled")
    scheduler = ThreadScheduler({"enabled": True, "adaptive": False},
                                sampler=Loads(), log_fn=lambda m: None)
    check("non-adaptive mode never changes threads",
          scheduler.tick(1000.0)["action"] == "steady")
    broken = ThreadScheduler({"sample_interval": 0, "ramp_seconds": 0},
                             sampler=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                             set_threads_cb=lambda t, r: None)
    check("a broken sampler never raises", broken.tick(1000.0)["action"] in
          ("steady", "want-up", "scaled-up"))
    no_psutil = ThreadScheduler({}, sampler=None)
    check("no psutil + no sampler → scheduler disables itself",
          no_psutil.enabled is False)
    check("status line is always printable",
          "threads=" in ThreadScheduler({"enabled": False}).status_line())


# --------------------------------------------------------------------------- #
# B. thread manager integration
# --------------------------------------------------------------------------- #

def _install_qt_stub():
    """PyQt6 is not installed in the test sandbox — provide a minimal stand-in."""
    if "PyQt6" in sys.modules:
        return
    import types as _types

    class _Signal:
        def connect(self, *args, **kwargs):
            pass

        def emit(self, *args, **kwargs):
            pass

    class _Base:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: None

    class QObject(_Base):
        pass

    class QThread(_Base):
        def isRunning(self):
            return False

        def wait(self, *args):
            return True

        def start(self):
            pass

        def terminate(self):
            pass

    class QTimer(_Base):
        @staticmethod
        def singleShot(*args, **kwargs):
            pass

    core = _types.ModuleType("PyQt6.QtCore")
    core.QObject, core.QThread, core.QTimer = QObject, QThread, QTimer
    core.pyqtSignal = lambda *a, **k: _Signal()
    package = _types.ModuleType("PyQt6")
    package.QtCore = core
    sys.modules["PyQt6"] = package
    sys.modules["PyQt6.QtCore"] = core


def _fake_manager():
    """A ThreadManager with everything heavy stubbed out."""
    _install_qt_stub()
    from test_session_health import _load_automation_module
    _load_automation_module()          # stubs winsound / other Windows-only bits
    from entry.thread_manager import ThreadManager
    manager = ThreadManager.__new__(ThreadManager)
    manager.workers = {}
    manager.thread_count = 3
    manager.is_running = True
    manager.accounts_exhausted = False
    manager._scale_down_ids = set()
    manager._requested_thread_count = 3
    manager.logs = []
    manager.thread_log = types.SimpleNamespace(emit=lambda tid, msg: manager.logs.append(msg))
    manager.thread_finished = types.SimpleNamespace(emit=lambda tid: None)
    manager.all_threads_finished = types.SimpleNamespace(emit=lambda: None)
    manager.started = []
    manager.stopped = []

    def _start(thread_id, force_new_account=False):
        manager.started.append(thread_id)
        manager.workers[thread_id] = types.SimpleNamespace(
            stop=lambda: manager.stopped.append(thread_id))

    manager._start_single_thread = _start
    manager._engine = None
    manager._unregister_worker_with_engine = lambda tid: None
    manager._register_worker_with_engine = lambda tid: None
    manager._release_worker_pool_slot = lambda worker: None
    manager.account_lock = threading.Lock()
    manager.thread_accounts = {}
    manager.failed_accounts = set()
    manager.account_lock = threading.Lock()
    from core.thread_scheduler import ThreadScheduler
    manager._scheduler = ThreadScheduler(
        {"start_threads": 1, "max_threads": 4, "ramp_seconds": 0, "sample_interval": 0},
        sampler=Loads(free_mb=20000),
    )
    return manager


def test_manager_integration() -> None:
    print("\n— B. thread manager: grow / shrink live workers —")
    manager = _fake_manager()
    check("plan text available", "recommended" in manager.describe_capacity())

    manager.workers = {1: types.SimpleNamespace(stop=lambda: None)}
    manager._apply_thread_target(3, "unit test up")
    check("scale up starts new threads", sorted(manager.started) == [2, 3],
          str(manager.started))
    check("workers registry updated", len(manager.workers) == 3)
    check("scale up is logged",
          any("starting thread" in line for line in manager.logs), str(manager.logs))

    manager._apply_thread_target(1, "unit test down")
    check("scale down asks the newest workers to stop",
          sorted(manager.stopped) == [2, 3], str(manager.stopped))
    check("those ids are marked so they are not restarted",
          manager._scale_down_ids == {2, 3}, str(manager._scale_down_ids))

    # the finished handler must NOT restart a wound-down thread
    manager.workers = {2: types.SimpleNamespace(stop=lambda: None)}
    manager.workers[2].automation = types.SimpleNamespace(_context_pool_slot=None)
    manager._on_thread_finished(2)
    check("a wound-down thread is not restarted",
          manager.started == [2, 3] and 2 not in manager.workers,
          f"started={manager.started}")

    # a normal (not wound-down) thread still restarts, as before
    manager.workers = {5: types.SimpleNamespace(stop=lambda: None,
                                                 automation=types.SimpleNamespace(
                                                     _context_pool_slot=None))}
    manager.accounts_exhausted = False
    manager.thread_accounts = {}
    manager.failed_accounts = set()
    manager._account_key = staticmethod(lambda account: "key")
    manager.is_running = False          # shutdown path → no restart, no crash
    manager._on_thread_finished(5)
    check("shutdown path still finishes cleanly", 5 not in manager.workers)

    # manual override
    manager2 = _fake_manager()
    manager2.workers = {}
    manager2.set_thread_count(2)
    check("manual override starts threads", sorted(manager2.started) == [1, 2],
          str(manager2.started))
    check("manual override respects the ceiling",
          manager2._scheduler.target_threads <= manager2._scheduler.cfg["max_threads"])

    # requested count is clamped to what the PC can carry
    manager3 = _fake_manager()
    clamped = manager3.recommended_thread_count(40)
    check("a wild thread request is clamped", clamped <= 40 and clamped >= 1,
          str(clamped))
    manager3._scheduler = None
    check("without a scheduler the request is unchanged",
          manager3.recommended_thread_count(7) == 7)


# --------------------------------------------------------------------------- #
# C. planner CLI
# --------------------------------------------------------------------------- #

def test_planner_cli() -> None:
    print("\n— C. tools/thread_planner.py —")
    tool = str(ROOT / "tools" / "thread_planner.py")
    result = subprocess.run([sys.executable, tool], capture_output=True, text=True,
                            timeout=120)
    check("planner runs", result.returncode in (0, 2) and "THREAD PLANNER" in result.stdout,
          result.stdout[-200:] + result.stderr[-200:])
    check("planner explains the RAM budget", "RAM budget per browser" in result.stdout)
    check("planner explains pause behaviour", "PAUSE every worker" in result.stdout)

    result = subprocess.run([sys.executable, tool, "--json"], capture_output=True,
                            text=True, timeout=120)
    payload = json.loads(result.stdout)
    check("--json is machine readable", "plan" in payload and "config" in payload,
          str(payload)[:200])
    check("--json carries the recommendation",
          isinstance(payload["plan"].get("recommended_threads"), int))

    result = subprocess.run([sys.executable, tool, "--simulate"], capture_output=True,
                            text=True, timeout=120)
    check("--simulate prints the four load levels",
          result.stdout.count("scaled-") + result.stdout.count("paused") >= 3,
          result.stdout[-400:])
    check("--simulate shows the freeze case", "about to freeze" in result.stdout)
    check("--simulate leaves the gate open", pause_gate().is_paused() is False)

    result = subprocess.run([sys.executable, tool, "--threads", "1"], capture_output=True,
                            text=True, timeout=120)
    check("--threads 1 is accepted", result.returncode == 0, str(result.returncode))


# --------------------------------------------------------------------------- #
# D. session → chat runner
# --------------------------------------------------------------------------- #

def test_session_chat_selection() -> None:
    print("\n— D. tools/session_chat.py — plan and selection —")
    sys.path.insert(0, str(ROOT / "tools"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("session_chat", ROOT / "tools" / "session_chat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with _temp_sessions() as root:
        make_session(root, "account_ok", cookies=[cookie(30)], email="ok@x.com")
        make_session(root, "account_dead", cookies=[cookie(-4)], email="dead@x.com")
        accounts = module.load_accounts()
        check("all saved sessions are listed", len(accounts) == 2, str(len(accounts)))
        check("selection by email substring works",
              len(module.load_accounts("ok@")) == 1)
        check("selection by folder name works",
              len(module.load_accounts("account_dead")) == 1)
        check("an unknown pattern selects nothing",
              module.load_accounts("nobody@") == [])

        usable, lines = module.format_plan(accounts)
        check("only the healthy session is usable", usable == 1, str(lines))
        check("the blind one is skipped with a reason",
              any("⛔ dead@x.com" in line for line in lines), str(lines))
        check("the plan shows the device it will open with",
              any("device" in line for line in lines), str(lines))

        healthy = next(a for a in accounts if a["email"] == "ok@x.com")
        blind = next(a for a in accounts if a["email"] == "dead@x.com")
        ok, reason = module.classify(healthy)
        check("a healthy session is classified usable", ok and "loadable" in reason, reason)
        bad, bad_reason = module.classify(blind)
        check("a blind session without credentials is skipped", bad is False, bad_reason)


class _temp_sessions:
    def __init__(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = {k: os.environ.get(k) for k in
                     ("EVA_SESSIONS_DIR", "EVA_ACCOUNTS_FILE")}

    def __enter__(self):
        os.environ["EVA_SESSIONS_DIR"] = str(self.root)
        os.environ.pop("EVA_ACCOUNTS_FILE", None)
        for module_name in [m for m in list(sys.modules) if m.startswith("browser.")]:
            pass
        return self.root

    def __exit__(self, *exc):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()
        return False


def test_session_chat_runner() -> None:
    print("\n— D2. session_chat: dry-run wiring, watchdog, summary —")
    import importlib.util
    spec = importlib.util.spec_from_file_location("session_chat2", ROOT / "tools" / "session_chat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with _temp_sessions() as root:
        folder = make_session(root, "account_run", cookies=[cookie(30)], email="run@x.com")
        account = {"email": "run@x.com", "session_dir": str(folder),
                   "storage_state_path": str(folder / "storage_state.json")}

        # ---- automation is built in restore mode (session only, no password) --
        built = {}

        class FakeAutomation:
            def __init__(self, **kwargs):
                built.update(kwargs)
                self.is_running = False
                self.context = types.SimpleNamespace(new_page=lambda: "PAGE")
                self.stopped = False
                self.closed = False

            def set_log_callback(self, cb):
                self.log_cb = cb

            def set_status_callback(self, cb):
                pass

            def set_chat_callback(self, cb):
                self.chat_cb = cb

            def set_session_callback(self, cb):
                pass

            def _launch_camoufox(self):
                return "BROWSER"

            def _ensure_identity(self):
                return {"user_agent": "UA", "os": "Windows"}

            def _verify_fingerprint_before_login(self, page, purpose="login"):
                built["verified_purpose"] = purpose
                return True

            def restore_saved_account_session(self, page):
                built["restored_page"] = page
                return True

            def _close_camoufox(self):
                self.closed = True

            def stop(self):
                self.stopped = True

            def run(self):
                built["ran"] = True
                self.chat_cb("user", "hey there")     # stranger SMS
                self.chat_cb("bot", "hi :)")          # our reply
                time.sleep(0.05)

        import browser.browser_automation as ba_module
        original = ba_module.ChitchatAutomation
        ba_module.ChitchatAutomation = FakeAutomation
        try:
            lines = []
            automation = module.build_automation(account, headless=True, thread_id=1,
                                                 log=lines.append)
            check("restore mode is used (never the password)",
                  built.get("account_mode") == "restore", str(built.get("account_mode")))
            check("headless flag passed through", built.get("headless") is True)
            check("the account dict is attached", built.get("account") is account)
            check("callbacks are wired for SMS and replies",
                  hasattr(automation, "chat_cb") and hasattr(automation, "log_cb"))

            ok, reason = module.connect_only(automation, log=lines.append)
            check("--dry-run verifies the fingerprint before restoring",
                  built.get("verified_purpose") == "dry run")
            check("--dry-run restores the saved session", ok is True, reason)
            check("--dry-run closes the browser", automation.closed is True)
            check("--dry-run reports the result",
                  any("session restored=True" in line for line in lines), str(lines))

            summary = module.run_account(account, headless=True, minutes=0.01,
                                         dry_run=False, thread_id=1, log=lines.append)
            check("run() is used for a real run", built.get("ran") is True)
            check("stranger SMS counted", summary["messages_in"] == 1, str(summary))
            check("our replies counted", summary["messages_out"] == 1, str(summary))
            check("a finished run is reported as finished",
                  summary["reason"] == "finished", summary["reason"])
            check("the run is marked ok", summary["ok"] is True, str(summary))
            check("duration is recorded", summary["seconds"] >= 0)

            # a stop request reaches the automation
            stop_lines = []

            class SlowAutomation(FakeAutomation):
                def run(self):
                    built["ran_slow"] = True
                    deadline = time.time() + 5
                    while not self.stopped and time.time() < deadline:
                        time.sleep(0.05)
                    built["stopped_by_timer"] = self.stopped

            ba_module.ChitchatAutomation = SlowAutomation
            summary = module.run_account(account, headless=True, minutes=0.02,
                                         dry_run=False, thread_id=1, log=stop_lines.append)
            check("the watchdog stops a long run", built.get("stopped_by_timer") is True)
            check("the time limit is reported in the summary",
                  "time limit" in summary["reason"], summary["reason"])
            check("the stop is logged",
                  any("minute limit reached" in line for line in stop_lines), str(stop_lines))
        finally:
            ba_module.ChitchatAutomation = original


def test_session_chat_cli() -> None:
    print("\n— D3. tools/session_chat.py CLI —")
    tool = str(ROOT / "tools" / "session_chat.py")
    with _temp_sessions() as root:
        make_session(root, "account_ok", cookies=[cookie(30)], email="ok@x.com")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, tool, "--all", "--plan-only"],
                                capture_output=True, text=True, env=env, timeout=180)
        check("--plan-only runs without a browser", result.returncode == 0
              and "SESSION → CHAT" in result.stdout, result.stdout[-200:] + result.stderr[-200:])
        check("--plan-only lists the account and the device",
              "ok@x.com" in result.stdout and "device" in result.stdout,
              result.stdout[-400:])
        check("--plan-only reports usability", "can be used" in result.stdout)

        result = subprocess.run([sys.executable, tool, "--account", "nobody@x.com",
                                 "--plan-only"], capture_output=True, text=True,
                                env=env, timeout=180)
        check("an unknown account exits 2", result.returncode == 2, str(result.returncode))


def main() -> int:
    print("=" * 72)
    print("EVA thread scheduler + session→chat tests (test_thread_scheduler.py)")
    print("=" * 72)
    test_capacity_plan()
    test_scaling()
    test_freeze_protection()
    test_degradation()
    test_manager_integration()
    test_planner_cli()
    test_session_chat_selection()
    test_session_chat_runner()
    test_session_chat_cli()
    print("\n" + "=" * 72)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
