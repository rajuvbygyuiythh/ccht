#!/usr/bin/env python3
"""tools/session_chat.py — run a SAVED session end-to-end (cookie/token → chat).

This is the backend pipeline in one command: it takes an account that already
has a saved session (``storage_state.json`` and/or its own Chrome profile),
opens the browser with *only that session* (no password, no login form),
verifies the fingerprint, enters the app, waits for incoming messages
(stranger SMS), replies with the same engine the GUI uses, and keeps the
cookies fresh — headless by default, so it also works on a server.

Pipeline (every step is logged with a tag you can grep):

```
[Session]  health + plan            browser/session_health.py
[Identity] device profile           browser/browser_identity.py
[Profile]  account's own Chrome profile (owner-guarded)
[Fingerprint] verify before login
[Restore]  cookies/localStorage restored from storage_state.json
[SMS]      incoming stranger message (chat_reader.MessageTracker)
[REPLY]    generated reply sent to the stranger
[Session]  cookies refreshed (keep-alive)
```

Usage
-----
::

    # one account, headless, stop after 30 minutes
    python tools/session_chat.py --account sadia.6.7@gmail.com --minutes 30

    # every saved account that is not blind (one after another)
    python tools/session_chat.py --all --minutes 15

    # watch it work (real window)
    python tools/session_chat.py --account EMAIL --visible

    # connection test only: open + verify + restore, then close
    python tools/session_chat.py --account EMAIL --dry-run

    # what would run?  (health + identity + profile, no browser)
    python tools/session_chat.py --all --plan-only

Exit codes: 0 = ran and finished normally, 2 = nothing usable (no session /
blind without credentials), 1 = usage or startup error.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


# --------------------------------------------------------------------------- #
#  account selection
# --------------------------------------------------------------------------- #

def _health():
    try:
        from browser import session_health
        return session_health
    except Exception:
        return None


def load_accounts(pattern=None):
    """Saved accounts, filtered by ``pattern`` (email substring, case-free)."""
    from browser.account_session_store import load_saved_account_sessions
    accounts = load_saved_account_sessions() or []
    if not pattern:
        return accounts
    wanted = str(pattern).strip().lower()
    return [a for a in accounts
            if wanted in str(a.get("email") or "").lower()
            or wanted in str(a.get("session_dir") or "").lower()]


def classify(account):
    """Return ``(usable, reason)`` for one account: restore / repair / skip."""
    health = _health()
    if health is None:
        return True, "health module unavailable — trying anyway"
    try:
        plan = health.plan_for_account(account)
    except Exception as error:
        return True, f"plan failed ({error}) — trying anyway"
    action = plan.get("action")
    reason = plan.get("reason") or action or "unknown"
    if action == health.ACTION_SKIP and plan.get("blind"):
        return False, reason
    if plan.get("blind"):
        return True, f"blind but repairable — {reason}"
    return True, reason


def format_plan(accounts):
    """Human-readable plan table (also used by --plan-only and tests)."""
    try:
        from browser import browser_identity as identity
    except Exception:
        identity = None
    lines = []
    usable = 0
    for account in accounts:
        ok, reason = classify(account)
        usable += 1 if ok else 0
        mark = "✓" if ok else "⛔"
        lines.append(f"  {mark} {str(account.get('email') or account.get('session_dir')):<36} "
                     f"{reason}")
        if identity is not None:
            try:
                found = identity.identity_for(account, create=False)
                if found is None:
                    # nothing launched yet — show the device it WOULD open with
                    preview = identity.derive_identity(identity.account_key_for(account))
                    lines.append(f"      device (created on the first launch): "
                                 f"{identity.describe(preview)}")
                else:
                    lines.append(f"      device: {identity.describe(found)}")
                status = identity.profile_status(account)
                if status.get("profile_dir"):
                    lines.append(f"      profile: {status['profile_dir']} "
                                 f"[{'exists' if status['exists'] else 'new'}]")
            except Exception:
                pass
    return usable, lines


# --------------------------------------------------------------------------- #
#  running
# --------------------------------------------------------------------------- #

def build_automation(account, *, headless=True, thread_id=1, log=print):
    """Create the worker for one saved session (restore mode, session only)."""
    from browser.browser_automation import ChitchatAutomation
    automation = ChitchatAutomation(
        account=account,
        account_mode="restore",     # ← cookie/session only, never the password
        headless=headless,
        thread_id=thread_id,
    )
    automation.set_log_callback(lambda message: log(f"  {message}"))
    automation.set_status_callback(lambda status: log(f"  [status] {status}"))
    automation.set_chat_callback(
        lambda who, text: log(f"  [SMS]   {who}: {text}" if who == "user"
                              else f"  [REPLY] bot: {text}"))
    automation.set_session_callback(lambda state: log(f"  [session] {state}"))
    return automation


def connect_only(automation, log=print):
    """Open + verify + restore a saved session, then close (connection test)."""
    from browser import browser_identity as identity
    try:
        automation.is_running = True
        browser = automation._launch_camoufox()
        if browser is None:
            return False, "browser launch failed"
        log("  [Identity] " + identity.describe(automation._ensure_identity()))
        page = None
        try:
            # Persistent-profile mode: the profile context is created at launch
            # and ``automation.context`` is only attached later by the session
            # attempt, so pick it up from the browser handle here.
            if automation.context is None:
                context = getattr(browser, "_context", None)
                if context is None:
                    try:
                        context = (browser.contexts or [None])[0]
                    except Exception:
                        context = None
                automation.context = context
            # The account's anti-detect layer is normally applied by the session
            # attempt; this dry run bypasses it, so apply it here too — otherwise
            # the fingerprint check below would compare the profile with a bare
            # browser and report a false mismatch.
            try:
                from browser.browser_engine import apply_chromium_stealth
                _kwargs, _fingerprint = automation._identity_context_options()
                apply_chromium_stealth(automation.context, log_fn=log,
                                       fingerprint=_fingerprint or None)
            except Exception as error:
                log(f"  [Identity] stealth layer not applied: {error}")
            if automation.context is not None:
                page = automation.context.new_page()
            else:
                # Persistent-profile mode: the context belongs to the browser
                # handle, ``automation.context`` is only attached by a session
                # attempt (this dry run deliberately skips that).
                context = getattr(browser, "_context", None)
                if context is None:
                    try:
                        context = (browser.contexts or [None])[0]
                    except Exception:
                        context = None
                if context is None:
                    return False, "no browser context to open a page with"
                automation.context = context
                page = context.new_page()
        except Exception as error:
            return False, f"could not open a page: {error}"
        verified = automation._verify_fingerprint_before_login(page, purpose="dry run")
        restored = automation.restore_saved_account_session(page)
        log(f"  [Result] fingerprint verified={verified} · session restored={restored}")
        return bool(restored), "session restored" if restored else "session could not be restored"
    except Exception as error:
        return False, f"{type(error).__name__}: {error}"
    finally:
        try:
            automation.is_running = False
            automation._close_camoufox()
        except Exception:
            pass


def _page_username(automation, page, log=print):
    """Read the account's own username from the open chat page (best effort)."""
    try:
        from browser import chat_reader
        _messages, diag = chat_reader.extract_with_diag(page)
        name = str((diag or {}).get("myUsername") or "").strip()
        if name:
            log(f"  [WS] account username from the page: {name}")
            return name
    except Exception as error:
        log(f"  [WS] could not read the username from the page: {error}")
    return ""


def run_account_ws(account, *, headless=True, minutes=30.0, thread_id=1, log=print,
                   ws_url=None, send_event="", dom_fallback=True, probe=False):
    """Restore the session in the browser, then chat over the site's WebSocket.

    The browser stays open (and logged in) as the session holder, but the
    conversation itself runs on the socket: incoming SMS arrive as
    ``chatMessage`` events and the replies are emitted from the backend.  If the
    socket cannot confirm a delivery, the reply falls back to typing into the
    page (``dom_fallback``), so a chat is never lost.
    """
    from core.chat_ws import DEFAULT_WS_URL, ChatWebSocket, cookies_header

    # Whatever a previous run learned (--probe / --identity) is reused, so the
    # backend can start chatting without any extra flags.
    learned = {}
    try:
        learned = json.loads((ROOT / "data" / "ws_config.json").read_text(encoding="utf-8"))
    except Exception:
        learned = {}
    send_event = send_event or str(learned.get("send_event") or "")
    if send_event:
        log(f"  [WS] send event from data/ws_config.json: {send_event}")

    started = time.time()
    counts = {"messages_in": 0, "messages_out": 0}
    stop_reason = "finished"
    log(f"[Session] running {account.get('email')} over WebSocket (session-only)")
    automation = build_automation(account, headless=headless, thread_id=thread_id,
                                  log=log)
    chat = None
    page = None
    try:
        automation.is_running = True
        browser = automation._launch_camoufox()
        if browser is None:
            return {"account": account.get("email"), "ok": False,
                    "reason": "browser launch failed", "seconds": 0.0, **counts}
        context = getattr(browser, "_context", None)
        if context is None:
            try:
                context = (browser.contexts or [None])[0]
            except Exception:
                context = None
        automation.context = context

        # The account's anti-detect layer before the site is touched, exactly
        # like a normal run — the page is the session holder, and the socket
        # then reuses the cookies that the restored session provided.
        try:
            from browser.browser_engine import apply_chromium_stealth
            _kwargs, _fingerprint = automation._identity_context_options()
            apply_chromium_stealth(context, log_fn=log, fingerprint=_fingerprint or None)
        except Exception as error:
            log(f"  [Identity] stealth layer not applied: {error}")
        # The account's saved cookies are imported by the restore step below.
        page = context.new_page()
        if not automation.restore_saved_account_session(page):
            return {"account": account.get("email"), "ok": False,
                    "reason": "session restore failed",
                    "seconds": round(time.time() - started, 1), **counts}

        cookies = []
        try:
            cookies = context.cookies()
        except Exception as error:
            log(f"  [WS] could not read the live cookies: {error}")
        header = cookies_header(cookies)
        log(f"  [WS] {len(cookies)} browser cookies · "
            f"{len(header.split(';')) if header else 0} for chitchat.gg")

        username = _page_username(automation, page, log=log) \
            or str(learned.get("username") or "")
        if username:
            log(f"  [WS] account identity: username={username!r}")
        else:
            log("  [WS] account identity unknown — messages from the other side "
                "are still detected, but our own echo is recognised by its nonce")
        if probe:
            try:
                from core.chat_ws import probe_send_events_from_page
                candidates = probe_send_events_from_page(page, log=log)
                if candidates and not send_event:
                    send_event = candidates[0]
                    log(f"  [WS] probe chose the send event: {send_event}")
            except Exception as error:
                log(f"  [WS] probe failed: {error}")

        from chat.rule_bot import ChatRuleBot
        bot = ChatRuleBot()
        state = bot.new_conversation()
        log(f"  [ChatRuleBot] active over WS — state keys={len(state)}")

        def answer(text, _raw):
            counts["messages_in"] += 1
            log(f"  [SMS]   user: {text}")
            try:
                reply = bot.reply(text, state)
            except Exception as error:
                log(f"  [ChatRuleBot] reply failed: {type(error).__name__}: {error}")
                return
            try:
                delay = automation._get_reply_delay()
                if delay > 0:
                    time.sleep(delay)
            except Exception:
                pass
            ok, how = chat.send_message(reply)
            if ok:
                counts["messages_out"] += 1
                log(f"  [REPLY] bot: {reply}   (ws · {how})")
                return
            log(f"  [WS] reply not confirmed over the socket ({how})")
            if dom_fallback and page is not None:
                try:
                    if automation.send_chat_message(page, reply, 1):
                        counts["messages_out"] += 1
                        # The page's own socket will echo it back — remember the
                        # text so the echo is not mistaken for a new SMS.
                        try:
                            chat.note_sent_text(reply)
                        except Exception:
                            pass
                        log(f"  [REPLY] bot: {reply}   (dom fallback)")
                except Exception as error:
                    log(f"  [WS] dom fallback failed: {error}")

        chat = ChatWebSocket(cookie_header=header, url=ws_url or DEFAULT_WS_URL,
                             my_username=username, send_event=send_event,
                             allow_probe=bool(probe), log=log, on_message=answer)
        chat.start()
        if not chat.wait_connected(30.0):
            log(f"  [WS] socket did not connect: {chat.client.last_error}")
            return {"account": account.get("email"), "ok": False,
                    "reason": f"ws connect failed ({chat.client.last_error})",
                    "seconds": round(time.time() - started, 1), **counts}
        log(f"  [WS] live — conversation={chat.conversation_id[:12]} "
            f"participants={chat.stats().get('participants')}")

        deadline = time.time() + minutes * 60 if minutes else 0
        while automation.is_running:
            if deadline and time.time() > deadline:
                stop_reason = f"time limit ({minutes:.0f} min)"
                log(f"  [Timer] {minutes:.0f} minute limit reached — stopping")
                break
            time.sleep(0.5)
        if chat is not None and chat.closed_by_peer:
            stop_reason = "the chat was closed by the user"
        stats = chat.stats() if chat is not None else {}
        log(f"  [WS] events: {json.dumps(stats.get('events') or {}, ensure_ascii=False)}")
        return {"account": account.get("email"), "ok": True, "reason": stop_reason,
                "seconds": round(time.time() - started, 1),
                "ws": {k: stats.get(k) for k in ("conversation", "participants",
                                                 "confirmed_event", "events")},
                **counts}
    except Exception as error:
        log(f"  [Error] {type(error).__name__}: {error}")
        return {"account": account.get("email"), "ok": False,
                "reason": f"error: {type(error).__name__}",
                "seconds": round(time.time() - started, 1), **counts}
    finally:
        try:
            if chat is not None:
                chat.stop()
        except Exception:
            pass
        try:
            automation.is_running = False
            automation._close_camoufox()
        except Exception:
            pass


def run_account(account, *, headless=True, minutes=30.0, dry_run=False,
                thread_id=1, log=print):
    """Run one saved session; returns a summary dict."""
    started = time.time()
    counts = {"messages_in": 0, "messages_out": 0}
    counters_lock = threading.Lock()
    stop_reason = "finished"

    def counting_log(line):
        with counters_lock:
            if "[SMS]" in line:
                counts["messages_in"] += 1
            elif "[REPLY]" in line:
                counts["messages_out"] += 1
        log(line)

    log(f"[Session] running {account.get('email')} (session-only restore)")
    automation = build_automation(account, headless=headless, thread_id=thread_id,
                                  log=counting_log)

    if dry_run:
        ok, reason = connect_only(automation, log=log)
        return {"account": account.get("email"), "ok": ok, "reason": reason,
                "seconds": round(time.time() - started, 1), **counts}

    stop_event = threading.Event()

    def _ask_stop(reason):
        nonlocal stop_reason
        stop_reason = reason
        stop_event.set()
        try:
            automation.stop()
        except Exception:
            pass

    def _watchdog():
        deadline = started + minutes * 60.0
        while not stop_event.is_set() and time.time() < deadline:
            stop_event.wait(1.0)
        if not stop_event.is_set():
            log(f"[Timer] {minutes:.0f} minute limit reached — stopping")
            _ask_stop(f"time limit ({minutes:.0f} min)")

    original_handler = None

    def _on_sigint(signum, frame):
        log("[Stop] Ctrl+C — closing the browser cleanly...")
        _ask_stop("Ctrl+C")

    try:
        original_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _on_sigint)
    except Exception:
        original_handler = None

    timer = None
    if minutes and minutes > 0:
        timer = threading.Thread(target=_watchdog, name="session-chat-timer", daemon=True)
        timer.start()
    try:
        automation.run()
    except KeyboardInterrupt:
        _ask_stop("Ctrl+C")
    except Exception as error:
        log(f"[Error] {type(error).__name__}: {error}")
        stop_reason = f"error: {type(error).__name__}"
    finally:
        stop_event.set()
        if timer is not None:
            timer.join(timeout=2.0)
        try:
            if original_handler is not None:
                signal.signal(signal.SIGINT, original_handler)
        except Exception:
            pass

    return {
        "account": account.get("email"),
        "ok": stop_reason not in ("error",) and not stop_reason.startswith("error"),
        "reason": stop_reason,
        "seconds": round(time.time() - started, 1),
        "chats": getattr(automation, "_session_stats", {}).get("total_chats")
        if isinstance(getattr(automation, "_session_stats", None), dict) else None,
        **counts,
    }


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--account", default=None,
                    help="saved account email (substring match)")
    ap.add_argument("--all", action="store_true", help="every usable saved account")
    ap.add_argument("--minutes", type=float, default=30.0,
                    help="stop after this many minutes (0 = run until stopped)")
    ap.add_argument("--headless", action="store_true", default=True,
                    help="run without a visible window (default)")
    ap.add_argument("--visible", action="store_true",
                    help="show the browser window")
    ap.add_argument("--dry-run", action="store_true",
                    help="open + verify + restore, then close (connection test)")
    ap.add_argument("--plan-only", action="store_true",
                    help="show the session plan without starting a browser")
    ap.add_argument("--transport", choices=("dom", "ws"), default="dom",
                    help="dom = read/type in the page (default), "
                         "ws = chat over the site's own WebSocket")
    ap.add_argument("--ws-url", default=None,
                    help="override the socket URL (default: api.chitchat.gg)")
    ap.add_argument("--ws-send-event", default="",
                    help="emit name used to send a message (tools/ws_chat.py --probe)")
    ap.add_argument("--ws-probe", action="store_true",
                    help="read the send event name from the chat bundle first")
    ap.add_argument("--no-dom-fallback", action="store_true",
                    help="do not type into the page when the socket cannot confirm")
    ap.add_argument("--delay", type=float, default=5.0,
                    help="seconds between accounts when --all is used")
    args = ap.parse_args()

    accounts = load_accounts(args.account)
    if not accounts:
        print("[!] no matching saved session — check `python tools/session_doctor.py --list`")
        return 2

    usable, lines = format_plan(accounts)
    print("=" * 78)
    print("SESSION → CHAT (cookie/token/session only, no password)")
    print("=" * 78)
    for line in lines:
        print(line)
    print("-" * 78)
    print(f"  {usable} of {len(accounts)} account(s) can be used")
    if args.plan_only:
        return 0 if usable else 2
    if not usable:
        print("[!] nothing usable — repair the sessions or add credentials to accounts.txt")
        return 2

    headless = not args.visible
    summaries = []
    for index, account in enumerate(accounts, start=1):
        ok, reason = classify(account)
        if not ok:
            print(f"\n[skip] {account.get('email')}: {reason}")
            continue
        print(f"\n{'=' * 78}")
        print(f"[{index}/{len(accounts)}] {account.get('email')}")
        print("=" * 78)
        summary = run_account(account, headless=headless, minutes=args.minutes,
                              dry_run=args.dry_run, thread_id=index, log=print)
        summaries.append(summary)
        print(f"[done] {summary['account']}: {summary['reason']} · "
              f"{summary['seconds']:.0f}s · SMS in {summary['messages_in']} · "
              f"replies out {summary['messages_out']}")
        if index < len(accounts) and args.delay > 0:
            time.sleep(args.delay)

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for summary in summaries:
        print(f"  {str(summary['account']):<36} {summary['reason']:<24} "
              f"in {summary['messages_in']:<4} out {summary['messages_out']:<4} "
              f"{summary['seconds']:.0f}s")
    return 0 if summaries else 2


if __name__ == "__main__":
    raise SystemExit(main())
