#!/usr/bin/env python3
"""tools/thread_planner.py — how many browser threads can THIS PC carry?

Reads the live CPU/RAM situation (psutil), the configured limits
(``config.json`` → ``thread_scheduler``) and prints a concrete plan:

* the safe/comfortable number of parallel browsers right now,
* the ladder the adaptive scheduler will follow while the bot runs,
* what happens when the PC gets busy (pause / scale down behaviour),
* an optional stress-free simulation (``--simulate``) that shows the decisions
  the scheduler would take for three load levels — no browser involved.

Usage
-----
::

    python tools/thread_planner.py
    python tools/thread_planner.py --json
    python tools/thread_planner.py --simulate
    python tools/thread_planner.py --threads 10        # what would 10 threads do?

Exit code: 0 when the current request is comfortable, 2 when the PC would be
overloaded with the configured maximum.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from core.thread_scheduler import (ThreadScheduler, capacity_plan,  # noqa: E402
                                   format_plan)


def _cfg() -> dict:
    try:
        from core.config_loader import load_thread_scheduler
        return load_thread_scheduler()
    except Exception:
        return {}


def _simulate(cfg: dict) -> None:
    """Show the scheduler's decisions for low / high / red load."""
    scenarios = [
        ("idle PC        ", {"cpu": 0.12, "mem": 0.35, "free_mb": 9000, "ok": True}),
        ("normal         ", {"cpu": 0.45, "mem": 0.55, "free_mb": 5200, "ok": True}),
        ("busy           ", {"cpu": 0.88, "mem": 0.82, "free_mb": 2500, "ok": True}),
        ("about to freeze", {"cpu": 0.97, "mem": 0.93, "free_mb": 400, "ok": True}),
    ]
    print("\n  Simulation (no browser is started — pure decision logic):")
    print("  " + "-" * 74)
    print(f"  {'load':<16} {'threads':<9} {'action':<16} reason")
    print("  " + "-" * 74)
    for label, sample in scenarios:
        logs: list[str] = []
        scheduler = ThreadScheduler(
            dict(cfg, adaptive=True, sample_interval=0, ramp_seconds=0,
                 scale_cooldown_seconds=0, log_interval_seconds=0),
            sampler=lambda s=sample: dict(s),
            log_fn=logs.append,
        )
        scheduler.plan = capacity_plan(scheduler.cfg, sample=sample)
        scheduler.target_threads = int(cfg.get("start_threads") or 2)
        result = scheduler.tick(now=10_000.0)
        action = result.get("action", "?")
        threads = result.get("target", scheduler.target_threads)
        reason = scheduler.last_reason if action.startswith("scaled") else (
            scheduler.gate.reason() if scheduler.gate.is_paused() else "no change needed")
        print(f"  {label} {threads:<9} {action:<16} {reason}")
        try:
            from core.thread_scheduler import reset_pause_gate
            reset_pause_gate()
        except Exception:
            pass
    print("  " + "-" * 74)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--simulate", action="store_true",
                    help="show the scheduler's decisions for four load levels")
    ap.add_argument("--threads", type=int, default=None,
                    help="check a specific thread count against this PC")
    args = ap.parse_args()

    cfg = _cfg()
    plan = capacity_plan(cfg)
    requested = args.threads

    if args.json:
        payload = {"plan": plan, "config": cfg, "requested": requested}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print("=" * 78)
    print("THREAD PLANNER — how much can this PC carry?")
    print("=" * 78)
    print(f"  {format_plan(plan)}")
    if not plan["sampled"]:
        print("  [!] psutil is not available — the numbers above are estimates;")
        print("      install psutil (pip install psutil) for live measurements.")
    print("-" * 78)
    print(f"  RAM budget per browser : {plan['ram_per_browser_mb']:.0f} MB")
    print(f"  RAM kept free for Windows/apps : {plan['reserve_mb']:.0f} MB")
    print(f"  configured limits      : min {plan['min_threads']} · "
          f"max {plan['max_threads']} · start {plan['start_threads']}")
    print(f"  adaptive scheduling    : "
          f"{'ON' if cfg.get('adaptive', True) else 'OFF'}")
    print("-" * 78)

    if requested is not None:
        comfortable = plan["recommended_threads"]
        ok = requested <= comfortable
        print(f"  You asked for {requested} thread(s); this PC is comfortable "
              f"with {comfortable}.")
        if ok:
            print("  ✓ OK — the scheduler will still scale down if load rises.")
        else:
            print(f"  ⚠ Not recommended right now.  The bot will start "
                  f"{comfortable} and grow later only if the load allows it.")
            need_mb = (requested * plan["ram_per_browser_mb"]) + plan["reserve_mb"]
            print(f"    (that many browsers want ≈ {need_mb:.0f} MB free RAM; "
                  f"{plan['free_mb']} MB is free)")
        print("-" * 78)

    print("  While the bot runs the scheduler will:")
    print(f"    • start {plan['start_threads']} thread(s) and add one more while "
          f"CPU < {cfg.get('cpu_scale_up', 0.6) * 100:.0f}% and RAM < "
          f"{cfg.get('mem_scale_up', 0.6) * 100:.0f}%")
    print(f"    • remove a thread when CPU > {cfg.get('cpu_scale_down', 0.85) * 100:.0f}% "
          f"or RAM > {cfg.get('mem_scale_down', 0.8) * 100:.0f}%")
    print(f"    • PAUSE every worker (browsers stay open) above "
          f"{cfg.get('pause_above', 0.9) * 100:.0f}% load or below "
          f"{cfg.get('pause_when_free_below_mb', 700):.0f} MB free RAM, and resume "
          f"under {cfg.get('resume_below', 0.7) * 100:.0f}%")
    print("    • keep the Resource Governor as the last line of defence "
          "(context recycling / redline)")
    print("=" * 78)

    if args.simulate:
        _simulate(cfg)

    comfortable = plan["recommended_threads"]
    if requested is not None and requested > comfortable:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
