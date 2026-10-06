"""core/thread_scheduler.py — adaptive thread management (no PC hang, no lag).

Why this exists
---------------
The bot can run many browser sessions at once, but the *number of threads a PC
can carry is not a constant*: it depends on total RAM, free RAM, CPU load and
whatever else the user is doing.  Before this module the thread count was fixed
by the user and the only protection was the Resource Governor (which can stop
*new launches* but never shrinks the running set).

The scheduler watches the machine every few seconds and adapts:

```
   target = f(free RAM, RAM %, CPU %, cooldowns, min/max, ramp-up)
        │
        ├─ load low   → add a thread            (scale up, one step at a time)
        ├─ load high  → stop the newest thread  (scale down, keeps headroom)
        └─ load red   → PAUSE every worker between chats, resume when it cools
```

Three cooperating layers, from softest to hardest:

1. **Pause gate** (this module) — workers ask ``pause_gate().wait_if_paused()``
   between chats.  Nothing is killed; browsers keep their session but do no
   work until the pressure drops.  This is what keeps Windows responsive.
2. **ThreadScheduler** (this module) — grows/shrinks the active worker count
   with hysteresis + cooldown, and spreads the initial ramp-up.
3. **ResourceGovernor** (``core/resource_governor.py``) — throttles launches,
   recycles contexts, redline protection.

Everything degrades to a no-op when psutil is missing or when any call raises,
and the sampler can be injected, so the module is fully unit-testable offline.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

try:  # psutil is already a project dependency; import lazily for minimal installs
    import psutil

    _PSUTIL_OK = True
except Exception:  # pragma: no cover
    psutil = None
    _PSUTIL_OK = False


# --------------------------------------------------------------------------- #
#  Defaults (mirrors config.json → thread_scheduler)
# --------------------------------------------------------------------------- #

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    # Let the bot pick the thread count itself.
    "adaptive": True,
    # Hard bounds the scheduler may never cross.
    "min_threads": 1,
    "max_threads": 6,
    # Threads to open first; the rest are added slowly (ramp).
    "start_threads": 2,
    # RAM budget per browser/profile (Chromium profile ≈ 350-500 MB in practice).
    "ram_per_browser_mb": 450,
    # RAM that must stay free for Windows + the user's apps.
    "reserve_mb": 2048,
    # Load must be BELOW these to add a thread.
    "cpu_scale_up": 0.60,
    "mem_scale_up": 0.60,
    # Load above these removes a thread.
    "cpu_scale_down": 0.85,
    "mem_scale_down": 0.80,
    # Above this everything pauses (freeze protection).
    "pause_above": 0.90,
    # Once paused, work resumes when the load drops under this.
    "resume_below": 0.70,
    # Never let free RAM drop below this (pause protects it).
    "pause_when_free_below_mb": 700,
    # Seconds between two scale actions (prevents oscillation).
    "scale_cooldown_seconds": 45.0,
    "scale_up_step": 1,
    "scale_down_step": 1,
    # Spread the first N threads over this many seconds.
    "ramp_seconds": 20.0,
    # How often the sampler runs (the GUI calls tick() ~1/s).
    "sample_interval": 3.0,
    # Extra idle seconds a worker waits between chats (0 = off).
    "rest_between_sessions_seconds": 0.0,
    # Log a load line every N seconds (0 = only on changes).
    "log_interval_seconds": 60.0,
}


def _pick(overrides: Optional[Dict[str, Any]], key: str, fallback: Any) -> Any:
    if isinstance(overrides, dict) and overrides.get(key) is not None:
        return overrides[key]
    return fallback


# --------------------------------------------------------------------------- #
#  Pause gate — shared "do not work right now" flag
# --------------------------------------------------------------------------- #

class PauseGate:
    """A cooperative pause flag shared by every worker.

    ``wait_if_paused()`` blocks (in 0.2 s slices) while the gate is paused, and
    returns immediately when the bot is running or shutting down.  Workers call
    it between chats, so a pause costs nothing and never kills a browser.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._resume = threading.Event()
        self._resume.set()          # set = running, clear = paused
        self._reason = ""
        self._since = 0.0

    # ---- state ----
    def is_paused(self) -> bool:
        return not self._resume.is_set()

    def reason(self) -> str:
        with self._lock:
            return self._reason

    def paused_for(self) -> float:
        with self._lock:
            if not self._reason:
                return 0.0
            return max(0.0, time.time() - self._since)

    def pause(self, reason: str = "high system load") -> bool:
        """Pause all workers.  Returns True when this call changed the state."""
        with self._lock:
            if not self._resume.is_set():
                self._reason = reason
                return False
            self._reason = reason
            self._since = time.time()
            self._resume.clear()
            return True

    def resume(self) -> bool:
        """Let the workers continue.  Returns True when this changed the state."""
        with self._lock:
            if self._resume.is_set():
                return False
            self._reason = ""
            self._resume.set()
            return True

    # ---- worker side ----
    def wait_if_paused(self, *, stop_event=None, log_fn=None, poll: float = 0.2,
                       heartbeat: float = 30.0) -> bool:
        """Block while paused.  Returns True if work may continue, False to stop.

        ``stop_event`` (a ``threading.Event``) aborts the wait, so a paused bot
        still shuts down instantly when the user presses Stop.
        """
        if not self.is_paused():
            return True
        if log_fn:
            log_fn(f"[Scheduler] paused — {self.reason()} (waiting for the PC to cool)")
        waited = 0.0
        last_report = 0.0
        try:
            if stop_event is not None and stop_event.is_set():
                return False
        except Exception:
            stop_event = None
        while self.is_paused():
            if stop_event is not None:
                try:
                    if stop_event.is_set():
                        return False
                except Exception:
                    pass
            time.sleep(poll)
            waited += poll
            if log_fn and waited - last_report >= heartbeat:
                last_report = waited
                log_fn(f"[Scheduler] still paused after {waited:.0f}s — "
                       f"{self.reason()}")
            if waited > 3600.0:      # safety valve: never wait forever
                if log_fn:
                    log_fn("[Scheduler] pause exceeded 1h — continuing anyway")
                return True
        if log_fn:
            log_fn(f"[Scheduler] resumed after {waited:.1f}s")
        return True


_PAUSE_GATE = PauseGate()


def pause_gate() -> PauseGate:
    """The process-wide pause gate (workers use this)."""
    return _PAUSE_GATE


def reset_pause_gate() -> None:
    """Test/maintenance helper: clear any leftover pause state."""
    _PAUSE_GATE.resume()


# --------------------------------------------------------------------------- #
#  Capacity planning
# --------------------------------------------------------------------------- #

def _default_sampler() -> Dict[str, Any]:
    """Return ``{cpu, mem, free_mb, total_mb, cores}`` (best effort)."""
    data: Dict[str, Any] = {"cpu": 0.0, "mem": 0.0, "free_mb": 0.0,
                            "total_mb": 0.0, "cores": os.cpu_count() or 1,
                            "ok": False}
    if not _PSUTIL_OK:
        return data
    try:
        data["cpu"] = float(psutil.cpu_percent(interval=None)) / 100.0
        mem = psutil.virtual_memory()
        data["mem"] = float(mem.percent) / 100.0
        data["free_mb"] = float(mem.available) / (1024 * 1024)
        data["total_mb"] = float(mem.total) / (1024 * 1024)
        data["ok"] = True
    except Exception:
        pass
    return data


def capacity_plan(config: Optional[Dict[str, Any]] = None,
                  sample: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """How many browser sessions can this PC carry right now?

    RAM is the binding constraint (each Chromium profile costs ~350-500 MB), so
    the plan is ``(free RAM - reserve) / ram_per_browser_mb``, capped by CPU
    cores and by the configured min/max.
    """
    cfg = dict(DEFAULTS)
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if v is not None})
    data = sample if isinstance(sample, dict) else _default_sampler()
    free_mb = float(data.get("free_mb") or 0.0)
    total_mb = float(data.get("total_mb") or 0.0)
    cores = int(data.get("cores") or 1)
    per_browser = max(150.0, float(cfg.get("ram_per_browser_mb") or 450))
    reserve = max(0.0, float(cfg.get("reserve_mb") or 2048))
    usable_mb = max(0.0, free_mb - reserve)
    ram_threads = int(usable_mb // per_browser)
    cpu_threads = max(1, int(cores * 0.75))
    floor = max(1, int(cfg.get("min_threads") or 1))
    ceiling = max(floor, int(cfg.get("max_threads") or 6))
    recommended = max(floor, min(ceiling, ram_threads, cpu_threads))
    start = int(cfg.get("start_threads") or 1)
    start = max(floor, min(recommended, start)) if recommended else floor
    return {
        "sampled": bool(data.get("ok")),
        "cpu_percent": round(float(data.get("cpu") or 0.0) * 100.0, 1),
        "mem_percent": round(float(data.get("mem") or 0.0) * 100.0, 1),
        "free_mb": round(free_mb),
        "total_mb": round(total_mb),
        "cores": cores,
        "ram_per_browser_mb": per_browser,
        "reserve_mb": reserve,
        "ram_threads": ram_threads,
        "cpu_threads": cpu_threads,
        "recommended_threads": recommended,
        "start_threads": start,
        "min_threads": floor,
        "max_threads": ceiling,
    }


def format_plan(plan: Dict[str, Any]) -> str:
    return (
        f"CPU {plan['cpu_percent']:.0f}% · RAM {plan['mem_percent']:.0f}% "
        f"({plan['free_mb']} MB free of {plan['total_mb']} MB, {plan['cores']} cores) "
        f"→ recommended {plan['recommended_threads']} browser(s) "
        f"(RAM allows {plan['ram_threads']}, CPU allows {plan['cpu_threads']}, "
        f"limits {plan['min_threads']}-{plan['max_threads']})"
    )


# --------------------------------------------------------------------------- #
#  Scheduler
# --------------------------------------------------------------------------- #

class ThreadScheduler:
    """Grows/shrinks the worker count and pauses work when the PC struggles."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        sampler: Optional[Callable[[], Dict[str, Any]]] = None,
        log_fn: Optional[Callable[[str], None]] = None,
        set_threads_cb: Optional[Callable[[int, str], bool]] = None,
        pause_cb: Optional[Callable[[str], None]] = None,
        resume_cb: Optional[Callable[[], None]] = None,
        gate: Optional[PauseGate] = None,
    ) -> None:
        self.cfg = dict(DEFAULTS)
        if isinstance(config, dict):
            self.cfg.update({k: v for k, v in config.items() if v is not None})
        self.enabled = bool(self.cfg.get("enabled", True))
        self.adaptive = bool(self.cfg.get("adaptive", True))
        self._sampler = sampler or _default_sampler
        self.log_fn = log_fn
        self.set_threads_cb = set_threads_cb
        self.pause_cb = pause_cb
        self.resume_cb = resume_cb
        self.gate = gate or _PAUSE_GATE

        self.target_threads = max(1, int(self.cfg.get("start_threads") or 1))
        self.last_action = "init"
        self.last_reason = "startup"
        self.last_sample: Dict[str, Any] = {}
        self.plan = capacity_plan(self.cfg)

        self._started_at = time.time()
        self._last_sample_at = 0.0
        self._last_scale_at = 0.0
        self._last_log_at = 0.0
        self._scaled_up_once = False
        self._overload_since = 0.0
        # Without psutil (or an injected sampler) there is nothing to measure,
        # so the scheduler quietly disables itself instead of guessing.
        self.enabled = bool(self.cfg.get("enabled", True)) and (
            _PSUTIL_OK or sampler is not None or bool(self.cfg.get("_force_enabled")))
        self._lock = threading.RLock()

    # ---- helpers ----
    def _log(self, message: str) -> None:
        if self.log_fn:
            try:
                self.log_fn(message)
            except Exception:
                pass

    def _sample(self, now: float) -> Dict[str, Any]:
        interval = max(0.5, float(self.cfg.get("sample_interval") or 3.0))
        if self.last_sample and (now - self._last_sample_at) < interval:
            return self.last_sample
        try:
            data = self._sampler() or {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("cpu", 0.0)
        data.setdefault("mem", 0.0)
        data.setdefault("free_mb", 0.0)
        self.last_sample = data
        self._last_sample_at = now
        return data

    def _ram_allows(self, data: Dict[str, Any], wanted: int) -> bool:
        free_mb = float(data.get("free_mb") or 0.0)
        reserve = float(self.cfg.get("reserve_mb") or 2048)
        per_browser = float(self.cfg.get("ram_per_browser_mb") or 450)
        needed = reserve + per_browser * max(0, wanted)
        return free_mb >= needed

    def _load_is_low(self, data: Dict[str, Any]) -> bool:
        return (float(data.get("cpu") or 0.0) <= float(self.cfg.get("cpu_scale_up", 0.6))
                and float(data.get("mem") or 0.0) <= float(self.cfg.get("mem_scale_up", 0.6)))

    def _load_is_high(self, data: Dict[str, Any]) -> bool:
        return (float(data.get("cpu") or 0.0) >= float(self.cfg.get("cpu_scale_down", 0.85))
                or float(data.get("mem") or 0.0) >= float(self.cfg.get("mem_scale_down", 0.8)))

    def _load_is_red(self, data: Dict[str, Any]) -> bool:
        free_mb = float(data.get("free_mb") or 0.0)
        floor_mb = float(self.cfg.get("pause_when_free_below_mb") or 700)
        return (float(data.get("cpu") or 0.0) >= float(self.cfg.get("pause_above", 0.9))
                or float(data.get("mem") or 0.0) >= float(self.cfg.get("pause_above", 0.9))
                or (data.get("free_mb") and free_mb < floor_mb))

    def _load_is_cool(self, data: Dict[str, Any]) -> bool:
        free_mb = float(data.get("free_mb") or 0.0)
        floor_mb = float(self.cfg.get("pause_when_free_below_mb") or 700)
        return (float(data.get("cpu") or 0.0) <= float(self.cfg.get("resume_below", 0.7))
                and float(data.get("mem") or 0.0) <= float(self.cfg.get("resume_below", 0.7))
                and (not data.get("free_mb") or free_mb > floor_mb * 1.25))

    # ---- public API ----
    def refresh_plan(self, sample: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.plan = capacity_plan(self.cfg, sample=sample)
        return self.plan

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "target_threads": self.target_threads,
                "paused": self.gate.is_paused(),
                "pause_reason": self.gate.reason(),
                "last_action": self.last_action,
                "last_reason": self.last_reason,
                "sample": dict(self.last_sample),
                "plan": dict(self.plan),
                "enabled": self.enabled,
                "adaptive": self.adaptive,
            }

    def status_line(self) -> str:
        snap = self.snapshot()
        data = snap["sample"]
        ceiling = (snap.get("plan") or {}).get("max_threads")
        thread_text = str(snap["target_threads"]) + (f"/{ceiling}" if ceiling else "")
        return (
            f"[Scheduler] threads={thread_text} · "
            f"CPU {float(data.get('cpu', 0)) * 100:.0f}% · "
            f"RAM {float(data.get('mem', 0)) * 100:.0f}% · "
            f"free {float(data.get('free_mb', 0)):.0f} MB"
            + (f" · PAUSED ({snap['pause_reason']})" if snap["paused"] else "")
            + f" · {snap['last_action']}"
        )

    def tick(self, now: Optional[float] = None) -> Dict[str, Any]:
        """One decision cycle.  Safe to call every second (it self-throttles)."""
        if not self.enabled:
            return {"action": "disabled"}
        now = float(now if now is not None else time.time())
        data = self._sample(now)
        ramp = max(0.0, float(self.cfg.get("ramp_seconds") or 0.0))

        # --- 1. freeze protection -------------------------------------------
        if self.gate.is_paused():
            if self._load_is_cool(data) or (ramp and now - self._started_at < 1.0):
                if self.resume_cb:
                    try:
                        self.resume_cb()
                    except Exception:
                        pass
                if self.gate.resume():
                    self.last_action, self.last_reason = "resumed", "load cooled down"
                    self._log(f"[Scheduler] ✓ resumed — "
                              f"CPU {float(data.get('cpu', 0)) * 100:.0f}% · "
                              f"RAM {float(data.get('mem', 0)) * 100:.0f}%")
                return {"action": "resumed", "sample": data}
            return {"action": "paused-wait", "sample": data}

        if self._load_is_red(data):
            # Shed a thread as well as pausing: a paused PC that still has too
            # many open browsers will stay hot, so shrinking helps the pause end
            # sooner.
            floor = max(1, int(self.plan.get("min_threads") or self.cfg.get("min_threads") or 1))
            cooldown = max(0.0, float(self.cfg.get("scale_cooldown_seconds") or 45.0))
            if (self.adaptive and self.target_threads > floor
                    and (now - self._last_scale_at) >= cooldown):
                step = max(1, int(self.cfg.get("scale_down_step") or 1))
                new_target = max(floor, self.target_threads - step)
                self._apply(new_target, now, "PC under heavy load — shedding a thread")
            if self.gate.pause("PC under heavy load"):
                self.last_action, self.last_reason = "paused", "PC under heavy load"
                self._log(f"[Scheduler] ⛔ paused all workers — "
                          f"CPU {float(data.get('cpu', 0)) * 100:.0f}% · "
                          f"RAM {float(data.get('mem', 0)) * 100:.0f}% · "
                          f"free {float(data.get('free_mb', 0)):.0f} MB "
                          f"(browsers stay open, they just wait)")
                if self.pause_cb:
                    try:
                        self.pause_cb("PC under heavy load")
                    except Exception:
                        pass
            return {"action": "paused", "sample": data}

        # --- 2. adaptive thread count ---------------------------------------
        if not self.adaptive:
            return {"action": "steady", "sample": data}

        cooldown = max(0.0, float(self.cfg.get("scale_cooldown_seconds") or 45.0))
        can_scale = (now - self._last_scale_at) >= cooldown
        ceiling = max(1, int(self.plan.get("max_threads") or self.cfg.get("max_threads") or 6))
        floor = max(1, int(self.plan.get("min_threads") or self.cfg.get("min_threads") or 1))

        if self._load_is_high(data) and self.target_threads > floor:
            if can_scale:
                step = max(1, int(self.cfg.get("scale_down_step") or 1))
                new_target = max(floor, self.target_threads - step)
                self._apply(new_target, now, "load is high — dropping a thread")
                return {"action": "scaled-down", "target": new_target, "sample": data}
            return {"action": "want-down", "sample": data}

        if self._load_is_low(data) and self.target_threads < ceiling:
            ramp_wait = ramp > 0 and (now - self._started_at) < ramp and self.target_threads >= int(
                self.cfg.get("start_threads") or 1)
            if can_scale and not ramp_wait and self._ram_allows(data, self.target_threads + 1):
                step = max(1, int(self.cfg.get("scale_up_step") or 1))
                new_target = min(ceiling, self.target_threads + step)
                self._apply(new_target, now, "load allows one more thread")
                return {"action": "scaled-up", "target": new_target, "sample": data}
            return {"action": "want-up", "sample": data}

        # --- 3. occasional status line --------------------------------------
        log_every = float(self.cfg.get("log_interval_seconds") or 0.0)
        if log_every > 0 and (now - self._last_log_at) >= log_every:
            self._last_log_at = now
            self._log(self.status_line())
        return {"action": "steady", "sample": data}

    def _apply(self, new_target: int, now: float, reason: str) -> None:
        old = self.target_threads
        self.target_threads = new_target
        self._last_scale_at = now
        self.last_action = "scaled-up" if new_target > old else "scaled-down"
        self.last_reason = reason
        arrow = "↑" if new_target > old else "↓"
        self._log(f"[Scheduler] {arrow} threads {old} → {new_target} ({reason})")
        if self.set_threads_cb:
            try:
                self.set_threads_cb(new_target, reason)
            except Exception as error:
                self._log(f"[Scheduler] apply failed: {error}")


__all__ = [
    "DEFAULTS", "PauseGate", "ThreadScheduler", "capacity_plan", "format_plan",
    "pause_gate", "reset_pause_gate",
]
