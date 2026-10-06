"""Bridge between the Python bot and the Go Concurrency Engine.

This module launches the compiled Go engine (``chitchat-engine``) as a
subprocess and communicates with it over line-delimited JSON on stdin/stdout.
It exposes a small, synchronous API that ``thread_manager.py`` uses to:

* get an adaptive worker-count recommendation,
* register workers and send heartbeats,
* detect hung workers (watchdog),
* acquire rate-limit tokens before acting on the target site,
* forward engine log/event messages to the bot's log window.

Design notes
------------
* **Backward compatible**: if the Go binary is missing or fails to start, every
  method degrades gracefully (returns a safe default, logs once) so the bot
  runs exactly as it did before — purely in Python.  The engine is an
  *accelerator/supervisor*, not a hard dependency.
* **No blocking**: the engine pushes periodic ``status``/``log``/``event``
  messages on its own.  A background reader thread drains those into queues so
  the main thread never blocks waiting for output.
* **Frozen-EXE aware**: when the bot is bundled with PyInstaller, the Go binary
  lives beside the EXE (or inside ``_internal``); ``_resolve_engine_path`` finds
  it in both frozen and source layouts.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Binary location
# ---------------------------------------------------------------------------

def _resolve_engine_path(explicit: Optional[str] = None) -> Optional[str]:
    """Locate the chitchat-engine executable.

    Resolution order:
    1. Explicit argument.
    2. CHITCHAT_ENGINE env var.
    3. Beside this file (source layout: go-engine/chitchat-engine).
    4. Beside the frozen EXE (PyInstaller onedir).
    5. Inside _internal (PyInstaller onefile/onedir data folder).
    6. On PATH.
    """
    candidates: List[str] = []
    if explicit:
        # An explicit path is taken as-is: if it doesn't exist, fail rather
        # than silently falling back to auto-detection (which would hide
        # misconfiguration in frozen-EXE deployments).
        if os.path.isfile(explicit):
            return explicit
        # Also try with .exe appended for Windows convenience.
        if not explicit.lower().endswith(".exe") and os.path.isfile(explicit + ".exe"):
            return explicit + ".exe"
        return None
    env = os.environ.get("CHITCHAT_ENGINE")
    if env:
        candidates.append(env)

    here = Path(__file__).resolve().parent.parent
    # Source layout: go-engine/chitchat-engine relative to bot root.
    candidates.append(str(here / "go-engine" / "chitchat-engine"))
    candidates.append(str(here / "chitchat-engine"))

    # Frozen layout.
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(str(exe_dir / "chitchat-engine"))
        candidates.append(str(exe_dir / "_internal" / "chitchat-engine"))
        candidates.append(str(exe_dir / "_internal" / "go-engine" / "chitchat-engine"))

    # Add .exe variants for Windows.
    expanded: List[str] = []
    for c in candidates:
        expanded.append(c)
        if not c.lower().endswith((".exe",)):
            expanded.append(c + ".exe")

    for c in expanded:
        if c and os.path.isfile(c):
            return c
    return None


# ---------------------------------------------------------------------------
# Engine bridge
# ---------------------------------------------------------------------------

class EngineBridge:
    """Manages the Go engine subprocess and JSON IPC.

    Usage::

        bridge = EngineBridge()
        bridge.start()
        bridge.set_config(max_workers=6)
        bridge.register_worker(1)
        rec = bridge.recommended_workers()   # int
        ok, wait_ms = bridge.acquire(1)       # rate limit
        bridge.heartbeat(1)
        hung = bridge.pop_hung_workers()      # list[int]
        events = bridge.drain_events()
        bridge.shutdown()
    """

    def __init__(self, engine_path: Optional[str] = None, config_path: Optional[str] = None):
        self._explicit_path = engine_path
        self._config_path = config_path
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

        # Latest status snapshot (updated by reader thread).
        self._latest_status: Dict[str, Any] = {}
        self._status_lock = threading.Lock()

        # Queues for async messages the engine pushes.
        self._log_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._event_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._hung_queue: "queue.Queue[int]" = queue.Queue()
        self._acquire_replies: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._launch_replies: "queue.Queue[Dict[str, Any]]" = queue.Queue()

        self._reader_thread: Optional[threading.Thread] = None
        self._started = False
        self._start_failed = False
        self._start_error: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Launch the Go engine subprocess. Returns True on success.

        On failure, sets an internal flag and returns False; every subsequent
        call degrades to a no-op/safe-default so the bot keeps working.
        """
        if self._started or self._start_failed:
            return self._started

        path = _resolve_engine_path(self._explicit_path)
        if not path:
            self._start_failed = True
            self._start_error = "chitchat-engine binary not found"
            return False

        config = self._config_path or _default_config_path()
        try:
            args = [path]
            if config and os.path.isfile(config):
                args += ["--config", config]
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                # Put the engine in its own process group so we can clean it up
                # reliably on Windows without orphaning it.
                creationflags=_subprocess_creation_flags(),
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            self._start_failed = True
            self._start_error = str(exc)
            return False

        self._started = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="engine-bridge-reader", daemon=True
        )
        self._reader_thread.start()
        return True

    def is_running(self) -> bool:
        return self._started and self._proc is not None and self._proc.poll() is None

    def shutdown(self) -> None:
        """Politely stop the engine. Safe to call multiple times."""
        if not self._started or self._proc is None:
            return
        try:
            self._send({"cmd": "shutdown"})
        except Exception:
            pass
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._started = False

    # -- low-level IPC -----------------------------------------------------

    def _send(self, obj: Dict[str, Any]) -> None:
        """Write one JSON command line to the engine stdin."""
        if not self.is_running():
            return
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            assert self._proc is not None and self._proc.stdin is not None
            try:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except Exception:
                # Engine died — mark as not running so callers degrade.
                self._started = False

    # -- reader thread -----------------------------------------------------

    def _reader_loop(self) -> None:
        """Background thread that drains engine stdout into queues."""
        assert self._proc is not None and self._proc.stdout is not None
        for raw in self._proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "status":
            with self._status_lock:
                self._latest_status = msg
        elif mtype == "acquire_result":
            self._acquire_replies.put(msg)
        elif mtype == "launch_result":
            self._launch_replies.put(msg)
        elif mtype == "log":
            self._log_queue.put(msg)
        elif mtype == "event":
            self._event_queue.put(msg)
            event = msg.get("event")
            if event == "worker_hung":
                wid = msg.get("worker_id")
                if isinstance(wid, int):
                    self._hung_queue.put(wid)
            elif event == "worker_recovered":
                wid = msg.get("worker_id")
                if isinstance(wid, int):
                    # Remove from hung set if present (best-effort).
                    try:
                        # Drain & re-queue without this id.
                        recovered = []
                        while True:
                            try:
                                recovered.append(self._hung_queue.get_nowait())
                            except queue.Empty:
                                break
                        for w in recovered:
                            if w != wid:
                                self._hung_queue.put(w)
                    except Exception:
                        pass
        elif mtype == "ctx_result":
            # Go engine context-pool responses (ctx_create/ctx_release).  The
            # Python side manages its own context pool (context_pool.py), so we
            # simply log the result rather than silently dropping it.
            ok = msg.get("ok")
            self._log_queue.put({
                "level": "info" if ok else "warn",
                "msg": f"context-pool response: ok={ok}",
            })

    # -- public API --------------------------------------------------------

    def set_config(
        self,
        max_workers: Optional[int] = None,
        min_workers: Optional[int] = None,
        cpu_threshold: Optional[float] = None,
        mem_threshold: Optional[float] = None,
        heartbeat_timeout_s: Optional[float] = None,
        sample_interval_ms: Optional[int] = None,
        rate_capacity: Optional[float] = None,
        rate_refill: Optional[float] = None,
        launch_max_concurrent: Optional[int] = None,
    ) -> None:
        """Update engine config at runtime."""
        payload: Dict[str, Any] = {"cmd": "set_config"}
        if max_workers is not None:
            payload["max_workers"] = max_workers
        if min_workers is not None:
            payload["min_workers"] = min_workers
        if cpu_threshold is not None:
            payload["cpu_threshold"] = cpu_threshold
        if mem_threshold is not None:
            payload["mem_threshold"] = mem_threshold
        if heartbeat_timeout_s is not None:
            payload["heartbeat_timeout_s"] = heartbeat_timeout_s
        if sample_interval_ms is not None:
            payload["sample_interval_ms"] = sample_interval_ms
        if rate_capacity is not None:
            payload["rate_capacity"] = rate_capacity
        if rate_refill is not None:
            payload["rate_refill"] = rate_refill
        if launch_max_concurrent is not None:
            payload["launch_max_concurrent"] = launch_max_concurrent
        self._send(payload)

    def register_worker(self, worker_id: int) -> None:
        self._send({"cmd": "register", "worker_id": worker_id})

    def unregister_worker(self, worker_id: int) -> None:
        self._send({"cmd": "unregister", "worker_id": worker_id})

    def heartbeat(self, worker_id: int) -> None:
        self._send({"cmd": "heartbeat", "worker_id": worker_id, "ts": int(time.time())})

    def set_ctx_max(self, max_ctx: int) -> None:
        """Tell the engine the maximum number of browser contexts allowed."""
        self._send({"cmd": "ctx_set_max", "max": int(max_ctx)})

    def request_status(self) -> None:
        """Ask the engine for an immediate status snapshot."""
        self._send({"cmd": "status"})

    def acquire(self, worker_id: int, timeout: float = 2.0) -> tuple:
        """Ask the rate limiter for one token.

        Returns ``(ok, wait_ms)``.  ``ok=True`` means act now; ``ok=False``
        means wait approximately ``wait_ms`` milliseconds before retrying.
        Degrades to ``(True, 0)`` (always allow) if the engine is unavailable.
        """
        if not self.is_running():
            return True, 0.0
        self._send({"cmd": "acquire", "worker_id": worker_id})
        try:
            reply = self._acquire_replies.get(timeout=timeout)
        except queue.Empty:
            # Engine didn't reply in time — allow the action (fail-open).
            return True, 0.0
        return bool(reply.get("ok")), float(reply.get("wait_ms", 0.0))

    def launch_acquire(
        self,
        max_concurrent: Optional[int] = None,
        timeout: float = 3.0,
    ) -> tuple:
        """Ask the engine for permission to launch a browser.

        The engine checks (a) a global cap on simultaneous browser launches
        and (b) current CPU/memory pressure.  Returns ``(ok, wait_ms)``.

        ``ok=True``  — proceed to launch now; you MUST call ``launch_release``
        once the browser is fully up so the slot is freed for the next worker.
        ``ok=False`` — the machine is too busy or the launch gate is full;
        sleep approximately ``wait_ms`` milliseconds and retry.

        Degrades to ``(True, 0)`` (always allow) if the engine is unavailable,
        so the bot keeps working even when the Go engine is absent.
        """
        if not self.is_running():
            return True, 0.0
        payload: Dict[str, Any] = {"cmd": "launch_acquire"}
        if max_concurrent is not None:
            payload["launch_max_concurrent"] = int(max_concurrent)
        self._send(payload)
        try:
            reply = self._launch_replies.get(timeout=timeout)
        except queue.Empty:
            # Engine didn't reply in time — allow the launch (fail-open).
            return True, 0.0
        return bool(reply.get("ok")), float(reply.get("wait_ms", 0.0))

    def launch_release(self) -> None:
        """Tell the engine a browser finished launching (frees a gate slot)."""
        if not self.is_running():
            return
        self._send({"cmd": "launch_release"})

    def set_launch_max(self, max_concurrent: int) -> None:
        """Set the maximum number of browsers allowed to launch at once."""
        if not self.is_running():
            return
        self._send({"cmd": "launch_set_max", "launch_max_concurrent": int(max_concurrent)})

    def reset_launch_gate(self) -> None:
        """Reset the browser-launch gate to a clean state.

        BUG C fix: if a previous run was stopped while browsers were mid-
        launch, the gate's ``inFlight`` counter could be left > 0, so the
        NEXT run's ``launch_acquire`` calls would be denied forever (the
        gate thinks launches are still in progress).  This resets the gate
        at the start of every run so stale counters never block new runs.
        Safe to call when the engine is down (no-op).
        """
        if not self.is_running():
            return
        try:
            self._send({"cmd": "launch_reset"})
        except Exception:
            pass

    def mem_pressure(self) -> None:
        """Tell the engine the machine hit the hard RAM redline.

        The engine shrinks the launch cap to 1 and emits a ``mem_pressure``
        event.  The actual process killing is done in Python (psutil) by the
        Resource Governor; this call coordinates the Go-side launch state so
        no new browsers pile on while we are killing one.  Safe to call when
        the engine is down (no-op).
        """
        if not self.is_running():
            return
        try:
            self._send({"cmd": "mem_pressure"})
        except Exception:
            pass

    def signal_recycle(self) -> None:
        """Tell the engine to pause context creation for a recycle cycle.

        The engine pauses the context pool and emits a ``recycle`` event.
        Python's tick() picks this up and calls pool.recycle_oldest().  The
        engine auto-resumes context creation after 5s as a safety net.  Safe
        to call when the engine is down (no-op).
        """
        if not self.is_running():
            return
        try:
            self._send({"cmd": "recycle"})
        except Exception:
            pass

    def launch_adapt(self, full_cap: int = 0) -> None:
        """Ask the engine to adaptively shrink/restore the launch cap.

        Called periodically from tick() so the launch gate reacts to resource
        pressure even between explicit ``launch_acquire`` calls.  If
        ``full_cap`` is given (>0) and pressure is low, the cap is restored to
        that value.  Safe to call when the engine is down (no-op).
        """
        if not self.is_running():
            return
        try:
            obj = {"cmd": "launch_adapt"}
            if full_cap and full_cap > 0:
                obj["launch_max_concurrent"] = int(full_cap)
            self._send(obj)
        except Exception:
            pass

    def recommended_workers(self) -> int:
        """Latest recommended worker count from the adaptive scaler."""
        with self._status_lock:
            rec = self._latest_status.get("recommended_workers")
        if isinstance(rec, int) and rec > 0:
            return rec
        # No status yet — caller should use its own default.
        return -1

    def resource_snapshot(self) -> Dict[str, float]:
        """Latest CPU/mem readings as a dict."""
        with self._status_lock:
            s = self._latest_status
        return {
            "cpu": float(s.get("cpu", 0.0)),
            "mem": float(s.get("mem", 0.0)),
            "cpu_avg": float(s.get("cpu_avg", 0.0)),
            "mem_avg": float(s.get("mem_avg", 0.0)),
            "launches_in_flight": int(s.get("launches_in_flight", 0)),
            "launch_max_concurrent": int(s.get("launch_max_concurrent", 0)),
            "launches_approved": int(s.get("launches_approved", 0)),
            "launches_denied": int(s.get("launches_denied", 0)),
        }

    def pop_hung_workers(self) -> List[int]:
        """Return worker IDs flagged as hung since the last call."""
        result: List[int] = []
        while True:
            try:
                result.append(self._hung_queue.get_nowait())
            except queue.Empty:
                break
        return result

    def drain_logs(self) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        while True:
            try:
                result.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        return result

    def drain_events(self) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        while True:
            try:
                result.append(self._event_queue.get_nowait())
            except queue.Empty:
                break
        return result

    def start_error(self) -> Optional[str]:
        return self._start_error


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_config_path() -> Optional[str]:
    """Resolve the shared config.json path (mirrors config_loader.get_config_path)."""
    try:
        from core.config_loader import get_config_path  # local import to avoid cycles
        p = get_config_path()
        return str(p) if p else None
    except Exception:
        # Fallback: beside the project root data/config.json.
        here = Path(__file__).resolve().parent.parent
        candidate = here / "data" / "config.json"
        return str(candidate) if candidate.is_file() else None


def _subprocess_creation_flags() -> int:
    """Return CREATE_NEW_PROCESS_GROUP on Windows, 0 elsewhere."""
    if os.name == "nt":
        try:
            import ctypes
            return ctypes.windll.kernel32.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        except Exception:
            return 0
    return 0


# ---------------------------------------------------------------------------
# Module-level singleton (optional convenience)
# ---------------------------------------------------------------------------

_singleton: Optional[EngineBridge] = None
_singleton_lock = threading.Lock()


def get_engine() -> EngineBridge:
    """Return a process-wide singleton EngineBridge (lazily started)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = EngineBridge()
        return _singleton
