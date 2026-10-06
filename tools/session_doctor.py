#!/usr/bin/env python3
"""tools/session_doctor.py — "why didn't my saved account load into the browser?"

Every account the bot can restore lives in a session folder
(``storage_state.json`` + ``metadata.json``) under ``account_sessions/`` or
``data/account_sessions/``.  A session whose cookies are gone, empty or corrupt
is **blind**: the browser opens, the site still shows the login page, and the
worker fails late.  This tool tells you that *before* you start the bot.

Usage
-----
::

    python tools/session_doctor.py --list
    python tools/session_doctor.py --list --json
    python tools/session_doctor.py --check sadia.6.7@gmail.com
    python tools/session_doctor.py --check all
    python tools/session_doctor.py --prune --dry-run     # show what would move
    python tools/session_doctor.py --prune               # move blind ones to _dead/
    python tools/session_doctor.py --live-check EMAIL    # open a real browser
    python tools/session_doctor.py --refresh-metadata    # write health into metadata
    python tools/session_doctor.py --profiles            # per-account Chrome profile
    python tools/session_doctor.py --identities          # per-account device profile
    python tools/session_doctor.py --identity EMAIL      # one account's device
    python tools/session_doctor.py --new-identity EMAIL --apply   # new device

Output states: ``ok`` · ``expiring`` (< N days) · ``expired`` · ``empty`` ·
``corrupt`` · ``missing`` — the last four are blind.  ``--check`` also says
whether the account can be repaired: the bot logs it in again when
``accounts.txt`` has a matching ``email:password`` line
(``EVA_ACCOUNTS_FILE`` overrides the file).

Exit codes: 0 = everything loadable, 2 = at least one blind session found,
1 = usage error.
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

from browser import session_health as health


def _accounts(export: bool = False):
    """Account dicts (same loader the GUI/restore mode uses)."""
    from browser.account_session_store import load_saved_account_sessions
    return load_saved_account_sessions()


def _fmt_days(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.1f}d"
    except (TypeError, ValueError):
        return "-"


def cmd_list(args) -> int:
    accounts = _accounts()
    if args.accounts_file:
        credentials = health.load_credentials([args.accounts_file])
    else:
        credentials = health.load_credentials()
    rows = []
    for account in accounts:
        plan = health.plan_for_account(
            account, credentials=credentials,
            warn_days=args.warn_days, allow_repair=not args.no_repair)
        h = plan.get("health") or {}
        rows.append({
            "email": account.get("email") or "(no email)",
            "serial": account.get("session_key", "")[:8],
            "state": h.get("state"),
            "blind": bool(h.get("blind")),
            "days_left": h.get("days_left"),
            "cookies": h.get("cookies", 0),
            "live_auth_cookies": h.get("live_auth_cookies", 0),
            "action": plan.get("action"),
            "has_credentials": bool(plan.get("has_credentials")),
            "reason": plan.get("reason"),
            "session_dir": account.get("session_dir"),
        })
    if args.json:
        print(json.dumps({
            "summary": health.summarize(accounts, warn_days=args.warn_days,
                                        credentials=credentials),
            "accounts": rows,
        }, indent=2, ensure_ascii=False))
    else:
        summary = health.summarize(accounts, warn_days=args.warn_days,
                                   credentials=credentials)
        print("=" * 78)
        print("SAVED SESSIONS — can the browser load them?")
        print("=" * 78)
        print(f"{'#':>3}  {'email':<34} {'state':<9} {'left':>7} {'ck/live':>8}  action")
        print("-" * 78)
        for index, row in enumerate(rows, 1):
            flag = "  ⛔" if row["blind"] and row["action"] != "repair" else ""
            print(f"{index:>3}  {row['email'][:34]:<34} {str(row['state']):<9} "
                  f"{_fmt_days(row['days_left']):>7} "
                  f"{row['cookies']}/{row['live_auth_cookies']:<6}  "
                  f"{row['action']}{flag}")
        print("-" * 78)
        print(health.format_summary(summary))
        blind_skipped = [r for r in rows if r["blind"] and r["action"] != "repair"]
        if blind_skipped:
            print()
            print("BLIND SESSIONS (the browser cannot load these):")
            for row in blind_skipped:
                print(f"  ✗ {row['email']}: {row['reason']}")
            print("  → add 'email:password' lines to accounts.txt so the bot can log in "
                  "again (repair), or re-create the session with Login mode.")
        if rows and all(r["action"] == "skip" for r in rows):
            return 2
        return 2 if blind_skipped else 0
    return 2 if any(r["blind"] and r["action"] != "repair" for r in rows) else 0


def cmd_check(args) -> int:
    credentials = health.load_credentials(
        [args.accounts_file] if args.accounts_file else None)
    targets = [t.strip().lower() for t in (args.check or [])]
    accounts = _accounts()
    if not targets or targets == ["all"]:
        selected = accounts
    else:
        selected = [a for a in accounts if str(a.get("email", "")).lower() in targets]
        missing = [t for t in targets if t not in
                   {str(a.get("email", "")).lower() for a in accounts}]
    if not selected:
        print("[!] no matching sessions found")
        return 2
    worst = 0
    for account in selected:
        plan = health.plan_for_account(account, credentials=credentials,
                                       warn_days=args.warn_days,
                                       allow_repair=not args.no_repair)
        h = plan.get("health") or {}
        print("-" * 70)
        print(f"account : {account.get('email')}")
        print(f"state   : {h.get('state')} ({h.get('reason')})")
        print(f"cookies : {h.get('cookies', 0)} total, {h.get('auth_cookies', 0)} chitchat, "
              f"{h.get('live_auth_cookies', 0)} live, {h.get('session_cookies', 0)} session-only")
        print(f"expiry  : days_left={h.get('days_left')}")
        print(f"plan    : {plan.get('action')} — {plan.get('reason')}")
        print(f"creds   : {'yes (repair possible)' if plan.get('has_credentials') else 'no'}")
        if arg_show_path(args):
            print(f"folder  : {account.get('session_dir')}")
        if plan.get("action") == "skip":
            worst = 2
    print("-" * 70)
    if worst:
        print("RESULT: at least one session is BLIND and cannot be repaired — the bot")
        print("        will skip it instead of launching a broken browser.")
    else:
        print("RESULT: every checked session can be loaded (or repaired).")
    return worst


def arg_show_path(args) -> bool:
    return bool(getattr(args, "paths", False))


def cmd_prune(args) -> int:
    result = health.prune_dead_sessions(dry_run=not args.apply, warn_days=args.warn_days)
    verb = "would move" if result["dry_run"] else "moved"
    print(f"prune ({'dry run' if result['dry_run'] else 'applied'}): "
          f"{verb} {result['count']} dead session(s)")
    for path in result.get("would_move") or result.get("moved") or []:
        print(f"  → {path}")
    print(f"  kept: {len(result['kept'])} alive session(s), "
          f"skipped: {len(result['skipped'])} (banned / already parked)")
    if result["dry_run"] and result["count"]:
        print("  rerun with --apply to move them into account_sessions/_dead/")
    return 0


def cmd_refresh_metadata(args) -> int:
    accounts = _accounts()
    updated = 0
    for account in accounts:
        h = health.session_health(account, warn_days=args.warn_days)
        if health.record_health(account, h):
            updated += 1
            print(f"  {account.get('email')}: {h.get('state')} "
                  f"(blind={h.get('blind')})")
    print(f"metadata updated for {updated} session(s)")
    return 0


def cmd_live_check(args) -> int:
    credentials = health.load_credentials(
        [args.accounts_file] if args.accounts_file else None)
    targets = [t.strip().lower() for t in (args.live_check or [])]
    accounts = _accounts()
    selected = [a for a in accounts if not targets or str(a.get("email", "")).lower() in targets]
    if not selected:
        print("[!] no matching sessions")
        return 2
    worst = 0
    for account in selected:
        print(f"live-check: {account.get('email')} ...")
        result = health.live_check(account, log=print)
        print(f"  → {result.get('status')}: {result.get('reason')}")
        account["live_status"] = result.get("status")
        account["live_reason"] = result.get("reason")
        if result.get("status") == "live":
            health.update_metadata(account, last_live_check_ok=True,
                                   live_checked_at=result.get("reason"))
        elif result.get("status") == "blind":
            worst = 2
            health.record_health(account, None, blind=True, blind_reason=result.get("reason"))
            if health.credentials_for(account, credentials):
                print("      repair available via accounts.txt — the bot will log in again")
    return worst


def cmd_identities(args) -> int:
    """Show the permanent device profile (browser identity) of every account."""
    try:
        from browser import browser_identity as identity
    except Exception as error:
        print(f"[!] browser_identity unavailable: {error}")
        return 1
    data = identity.audit()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    print("=" * 78)
    print("DEVICE PROFILES — which browser each account will open")
    print("=" * 78)
    print(f"  mode        : {data['profile_mode']}"
          f"{'  (one real profile per account)' if data['profile_mode'] == 'persistent' else '  (shared browser, isolated context)'}")
    print(f"  registry    : {data['registry']}")
    print(f"  identities  : {data['total']}")
    print("-" * 78)
    print("  account                            platform     engine     viewport   timezone")
    print(identity.format_identity_table())
    print("-" * 78)
    if data["unique"]:
        print("  ✓ every account has its own device (no two accounts share a browser)")
    else:
        print("  ⛔ DEVICE COLLISION — these accounts would look like the same machine:")
        for item in data["duplicates"]:
            print(f"     {' , '.join(item['accounts'])}")
    print("\n  A device profile is created on the account's first launch and then never")
    print("  changes, so the account always reappears as the same browser.  Use")
    print("  --new-identity EMAIL --apply to give an account a brand-new device.")
    return 0


def cmd_profiles(args) -> int:
    """Show the Chrome profile each account owns (and who claimed it)."""
    try:
        from browser import browser_identity as identity
    except Exception as error:
        print(f"[!] browser_identity unavailable: {error}")
        return 1
    accounts = _accounts()
    if not accounts:
        print("[!] no saved sessions")
        return 2
    cfg = identity.config()
    rows = []
    problems = 0
    for account in accounts:
        status = identity.profile_status(account, cfg=cfg)
        rows.append(status)
        if status["claimed_by_other"]:
            problems += 1
    if args.json:
        print(json.dumps({"mode": identity.profile_mode(cfg), "profiles": rows},
                         indent=2, ensure_ascii=False))
        return 2 if problems else 0
    print("=" * 78)
    print("CHROME PROFILES — one account = one separate browser profile")
    print("=" * 78)
    print(f"  mode: {identity.profile_mode(cfg)}   "
          f"root: {identity.profiles_root()}")
    print("-" * 78)
    print("  account                              on disk  used  logged-in  owner")
    for row in rows:
        print(f"  {row['account_key'][:34]:<34} "
              f"{'yes' if row['exists'] else 'no ':<7} "
              f"{'yes' if row['used'] else 'no ':<5} "
              f"{(row['saved_at'] or '-')[:19]:<19} "
              f"{'this account' if row['owned_by_this_account'] else ('OTHER ACCOUNT!' if row['claimed_by_other'] else '-')}")
    print("-" * 78)
    if problems:
        print(f"  ⛔ {problems} profile(s) are claimed by a different account — those")
        print("     accounts would be moved to their own new profile on the next run.")
    else:
        print("  ✓ no account shares another account's Chrome profile")
    print("")
    print("  Profiles are created on the account's first launch and reused every run,")
    print("  so cookies, history and fingerprint stay the same for that account.")
    return 2 if problems else 0


def cmd_identity(args) -> int:
    """Show one account's full device profile (or rotate it)."""
    try:
        from browser import browser_identity as identity
    except Exception as error:
        print(f"[!] browser_identity unavailable: {error}")
        return 1
    targets = [t.strip().lower() for t in (args.identity or [])]
    accounts = _accounts()
    selected = [a for a in accounts
                if not targets or str(a.get("email", "")).lower() in targets]
    if not selected:
        print("[!] no matching sessions")
        return 2
    for account in selected:
        found = identity.identity_for(account, create=False)
        print(f"\n{account.get('email')}")
        print(f"  session folder : {account.get('session_dir')}")
        if not found:
            print("  device         : (not created yet — made on the first launch)")
            continue
        print(f"  device         : {identity.describe(found)}")
        print(f"  user agent     : {found.get('user_agent')}")
        print(f"  screen         : {(found.get('screen') or {}).get('width')}x"
              f"{(found.get('screen') or {}).get('height')} @ {found.get('device_scale_factor')}x")
        print(f"  platform/locale: {found.get('platform')} · {found.get('locale')} · "
              f"{found.get('timezone_id')}")
        print(f"  profile folder : {found.get('profile_dir') or '-'}"
              f"{'  (persistent mode)' if found.get('profile_dir') else ''}")
        print(f"  created        : {found.get('created_at')}  (rotations: {found.get('rotations') or 0})")
        print(f"  signature      : {identity.signature_of(found)[:64]}…")
        status = identity.profile_status(account)
        print(f"  chrome profile : {status['profile_dir']}  "
              f"[{'used' if status['used'] else 'empty'}, "
              f"{'owned by this account' if status['owned_by_this_account'] else ('OTHER ACCOUNT' if status['claimed_by_other'] else 'unowned')}]")
    return 0


def cmd_new_identity(args) -> int:
    """Give an account a brand-new device (e.g. after a ban or a leak)."""
    try:
        from browser import browser_identity as identity
    except Exception as error:
        print(f"[!] browser_identity unavailable: {error}")
        return 1
    targets = [t.strip().lower() for t in (args.new_identity or [])]
    if not targets:
        print("[!] --new-identity needs at least one EMAIL (or 'all')")
        return 1
    accounts = _accounts()
    if targets == ["all"]:
        selected = accounts
    else:
        selected = [a for a in accounts if str(a.get("email", "")).lower() in targets]
    if not selected:
        print("[!] no matching sessions")
        return 2
    for account in selected:
        label = account.get("email")
        before = identity.identity_for(account, create=False)
        if not args.apply:
            print(f"[dry run] {label}: a new device would be created"
                  + (f" (currently {identity.describe(before)})" if before else ""))
            continue
        new = identity.rotate_identity(account, reason="manual --new-identity", log_fn=print)
        if new:
            print(f"[done] {label}: {identity.describe(new)}")
        else:
            print(f"[!] {label}: could not create a new device")
    if not args.apply:
        print("\nNothing changed.  Re-run with --apply to create the new device(s).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check which saved sessions the browser can actually load.")
    ap.add_argument("--list", action="store_true", help="table of all saved sessions")
    ap.add_argument("--check", nargs="*", metavar="EMAIL",
                    help="detailed check for the given emails (or 'all')")
    ap.add_argument("--prune", action="store_true",
                    help="park blind sessions in _dead/ (dry run unless --apply)")
    ap.add_argument("--apply", action="store_true", help="with --prune: really move them")
    ap.add_argument("--refresh-metadata", action="store_true",
                    help="write the health block into every metadata.json")
    ap.add_argument("--live-check", nargs="*", metavar="EMAIL",
                    help="open a real headless browser and verify the session")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--paths", action="store_true", help="show session folders")
    ap.add_argument("--warn-days", type=float, default=health.DEFAULT_WARN_DAYS,
                    help=f"'expiring' threshold in days (default {health.DEFAULT_WARN_DAYS})")
    ap.add_argument("--profiles", action="store_true",
                    help="show the Chrome profile owned by every account")
    ap.add_argument("--identities", action="store_true",
                    help="show the permanent device profile of every account")
    ap.add_argument("--identity", nargs="*", metavar="EMAIL",
                    help="show one account's device profile in detail")
    ap.add_argument("--new-identity", nargs="*", metavar="EMAIL",
                    help="give an account a brand-new device (dry run unless --apply)")
    ap.add_argument("--no-repair", action="store_true",
                    help="ignore accounts.txt (report blind sessions as unusable)")
    ap.add_argument("--accounts-file", default=None,
                    help="path to accounts.txt (default: project root / cwd)")
    args = ap.parse_args()

    if args.new_identity is not None:
        return cmd_new_identity(args)
    if args.identity is not None:
        return cmd_identity(args)
    if args.profiles:
        return cmd_profiles(args)
    if args.identities:
        return cmd_identities(args)
    if args.live_check is not None:
        return cmd_live_check(args)
    if args.check is not None:
        return cmd_check(args)
    if args.prune:
        return cmd_prune(args)
    if args.refresh_metadata:
        return cmd_refresh_metadata(args)
    # default (and --list): the table
    return cmd_list(args)


if __name__ == "__main__":
    raise SystemExit(main())
