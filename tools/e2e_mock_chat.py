#!/usr/bin/env python3
"""Offline end-to-end test: a saved account session chats with a real user.

chitchat.gg cannot be reached from the test sandbox, so this script serves a
local stand-in for it (``tools/mock_chitchat``) and then runs the **real**
pipeline against it:

    launch browser → account Identity/Chrome profile → fingerprint check →
    restore the saved cookies/session → open the chat → wait for an incoming
    message → ChatRuleBot reply → the "user" sees the reply on their screen

Nothing in the bot is stubbed: ``tools/session_chat.py``'s ``run_account()``
drives ``browser/browser_automation.py`` unchanged.  The only addition is a
Playwright route hook that answers ``https://app.chitchat.gg/**`` with the
mock pages, so the saved cookies for that host are still sent for real and
the bot keeps using its normal URLs.

Usage:
    python3 tools/e2e_mock_chat.py                       # headless, ~1 minute
    python3 tools/e2e_mock_chat.py --session data/account_sessions/account_xxx
    python3 tools/e2e_mock_chat.py --log /tmp/e2e.log --visible
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEFAULT_SESSION = "data/account_sessions/account_063cce9f4343619ac9bdfe89"
MOCK_URL = "https://app.chitchat.gg/start/new"
CHAT_URL = "https://app.chitchat.gg/chat/"


# --------------------------------------------------------------------------- #
#  logging
# --------------------------------------------------------------------------- #

class RunLog:
    """Timestamped log that also mirrors every line to a file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self.lines = []

    def __call__(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        with self._lock:
            self.lines.append(line)
            print(line, flush=True)
            self._file.write(line + "\n")
            self._file.flush()

    def close(self):
        try:
            self._file.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  session preparation
# --------------------------------------------------------------------------- #

def prepare_session(source: Path, work_dir: Path, log) -> dict:
    """Copy one saved account into a scratch sessions dir and refresh it.

    The repository's own session files are never touched: the copy is what the
    pipeline restores, exactly as it would restore the original.
    """
    if not source.is_dir():
        raise SystemExit(f"session dir not found: {source}")
    key = source.name
    if key.startswith("account_"):
        key = key[len("account_"):]
    target = work_dir / f"account_{key}"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)

    meta_path = target / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    meta.setdefault("schema_version", 1)
    meta["account_key"] = key
    meta["restore_url"] = MOCK_URL
    meta["saved_at"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    storage = target / "storage_state.json"
    cookies = []
    if storage.is_file():
        try:
            cookies = json.loads(storage.read_text(encoding="utf-8")).get("cookies") or []
        except Exception:
            cookies = []
    log(f"[Prep] copied {source.relative_to(REPO) if source.is_relative_to(REPO) else source} "
        f"→ {target}")
    log(f"[Prep] saved session refreshed: {len(cookies)} cookies, restore_url={MOCK_URL}")
    return {
        "account_key": key,
        "email": meta.get("email") or f"session-{key[:8]}",
        "session_dir": str(target),
        "storage_state_path": str(storage),
        "restore_url": MOCK_URL,
        "account_type": meta.get("account_type") or "login_account",
    }


def bot_display_name(account: dict) -> str:
    """The name the mock site shows for the bot (matches the real site's style)."""
    email = str(account.get("email") or "eva")
    return (email.split("@", 1)[0] or "eva")[:20]


# --------------------------------------------------------------------------- #
#  the "user" on the other side: a second real browser
# --------------------------------------------------------------------------- #

class StrangerBrowser(threading.Thread):
    """A real Chromium that types messages at the mock site and reads replies."""

    def __init__(self, *, backend, api_base, bot_name, log, done_event,
                 name="Stranger42", start_event=None):
        super().__init__(name="stranger-browser", daemon=True)
        self.backend = backend
        self.api_base = api_base
        self.bot_name = bot_name
        self.log = log
        self.done_event = done_event
        self.start_event = start_event or threading.Event()
        self.name = name
        self.replies = []
        self.error = ""
        self.finished = threading.Event()

    # -- helpers ---------------------------------------------------------
    def _launch(self):
        from playwright.sync_api import sync_playwright
        from tools.mock_chitchat import routes

        pw = sync_playwright().start()
        args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--disable-gpu", "--disable-blink-features=AutomationControlled"]
        extra = str(os.environ.get("EVA_CHROMIUM_EXTRA_ARGS") or "").strip()
        if extra:
            args.extend(part for part in extra.split() if part)
        options = {"headless": self.headless, "args": args}
        executable = str(os.environ.get("EVA_CHROMIUM_EXECUTABLE") or "").strip()
        if executable:
            options["executable_path"] = executable
        browser = pw.chromium.launch(**options)
        context = browser.new_context(viewport={"width": 1280, "height": 900},
                                      locale="en-US")
        routes.install(context, self.backend, me=self.name, api_base="",
                       log=self.log)
        page = context.new_page()
        return pw, browser, context, page

    def _bubbles_from(self, page, author):
        return page.evaluate(
            """(name) => Array.from(document.querySelectorAll('li.select-text'))
                 .filter(li => (li.querySelector('span.font-bold')?.textContent || '').trim() === name)
                 .map(li => (li.querySelector('span.emoji-content')?.textContent || '').trim())""",
            author)

    def _wait_for_reply(self, page, index, timeout_ms=90000):
        page.wait_for_function(
            """(payload) => Array.from(document.querySelectorAll('li.select-text'))
                 .filter(li => (li.querySelector('span.font-bold')?.textContent || '').trim() === payload.name)
                 .length >= payload.index""",
            arg={"name": self.bot_name, "index": index},
            timeout=timeout_ms)

    def _say(self, page, text):
        box = page.locator('textarea[name="message"]')
        box.click()
        box.fill("")
        box.type(text, delay=18)
        page.keyboard.press("Enter")
        self.log(f"[Stranger] typed: {text}")

    # -- thread body -----------------------------------------------------
    def run(self):
        self.headless = os.environ.get("E2E_STRANGER_HEADLESS", "1") != "0"
        page = None
        try:
            self.log("[Stranger] launching a second real browser (the 'user' side)")
            pw, browser, context, page = self._launch()
            page.goto(f"{CHAT_URL}?as={self.name}", wait_until="domcontentloaded",
                      timeout=30000)
            page.wait_for_selector('textarea[name="message"]', timeout=20000)
            self.log(f"[Stranger] connected to the site as {self.name} "
                     f"(url={page.url})")
            self.start_event.set()

            if not self.backend.wait_for_participant(self.bot_name, timeout=120):
                raise RuntimeError(f"bot account {self.bot_name} never appeared in the chat")
            self.log(f"[Stranger] the bot account {self.bot_name} is present in the chat")

            self._wait_before_first_message()
            self._say(page, "hey there! are you real?")
            self._wait_for_reply(page, 1)
            self.replies = self._bubbles_from(page, self.bot_name)
            self.log(f"[Stranger] got reply #1 from {self.bot_name}: {self.replies[-1]}")

            self._say(page, "nice :) what do you do for fun?")
            self._wait_for_reply(page, 2)
            self.replies = self._bubbles_from(page, self.bot_name)
            self.log(f"[Stranger] got reply #2 from {self.bot_name}: {self.replies[-1]}")
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            self.log(f"[Stranger] ✗ {self.error}")
        finally:
            self.done_event.set()
            self.finished.set()
            if page is not None:
                try:
                    page.screenshot(path=str(Path("/tmp/e2e_stranger_last.png")))
                    self.log("[Stranger] screenshot of the user's window: "
                             "/tmp/e2e_stranger_last.png")
                except Exception:
                    pass
            try:
                browser.close()
                pw.stop()
            except Exception:
                pass

    def _wait_before_first_message(self):
        # Small pause so the bot finishes its restore/connect sequence first.
        time.sleep(float(os.environ.get("E2E_FIRST_MESSAGE_DELAY", "6")))


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session", default=DEFAULT_SESSION,
                        help="saved account dir to restore (copied first)")
    parser.add_argument("--log", default="/home/user/demo_real_chat.log")
    parser.add_argument("--minutes", type=float, default=1.0,
                        help="hard stop for the bot session (the demo stops earlier)")
    parser.add_argument("--visible", action="store_true",
                        help="show the bot's browser window")
    parser.add_argument("--stranger-visible", action="store_true",
                        help="show the user's browser window")
    parser.add_argument("--connect-only", action="store_true",
                        help="only prove the session connects (no conversation)")
    args = parser.parse_args()

    log = RunLog(Path(args.log))
    log("=" * 72)
    log("[E2E] saved account session → real browser → chat with a user")
    log("=" * 72)

    work_dir = Path("/tmp/eva_e2e_sessions")
    work_dir.mkdir(parents=True, exist_ok=True)
    os.environ["EVA_SESSIONS_DIR"] = str(work_dir)
    os.environ.setdefault("EVA_PROFILES_DIR", "/tmp/eva_e2e_profiles")

    # Convenience for restricted machines (e.g. this sandbox): if a Chromium
    # binary was provided out-of-band, use it and point the loader at its libs.
    if not os.environ.get("EVA_CHROMIUM_EXECUTABLE") and Path("/tmp/chromium").exists():
        os.environ["EVA_CHROMIUM_EXECUTABLE"] = "/tmp/chromium"
        os.environ["EVA_CHROMIUM_EXTRA_ARGS"] = (
            "--no-sandbox --disable-setuid-sandbox --disable-dev-shm-usage")
        if Path("/tmp/chromium-libs/lib").is_dir():
            os.environ["LD_LIBRARY_PATH"] = (
                "/tmp/chromium-libs/lib:" + os.environ.get("LD_LIBRARY_PATH", ""))
    if args.stranger_visible:
        os.environ["E2E_STRANGER_HEADLESS"] = "0"

    source = Path(args.session)
    if not source.is_absolute():
        source = REPO / source
    account = prepare_session(source, work_dir, log)
    account["_display_name"] = bot_display_name(account)

    # -- local stand-in for chitchat.gg ---------------------------------- #
    from tools.mock_chitchat import routes, site

    backend = site.ChatBackend(log=log)
    server, api_base = site.start_server(backend, log=log)
    log("[MockSite] the bot's pages + the chat API are served for app.chitchat.gg "
        "through a Playwright route hook (same-origin, real HTTPS requests)")
    log(f"[MockSite] optional: the same stand-in is browsable at {api_base}/chat/")

    # -- route hook for the bot's own browser ----------------------------- #
    # Import through the shared test helper: on Linux the module's Windows/Qt
    # imports (winsound, PyQt6) are stubbed there, exactly like the test suite.
    from test_session_health import _load_automation_module

    automation_module, how = _load_automation_module()
    if automation_module is None:
        log(f"[E2E] ✗ could not import the bot: {how}")
        return 3
    log(f"[E2E] loaded browser.browser_automation ({how})")

    original_cls = automation_module.ChitchatAutomation
    captured = {}
    bot_name = account["_display_name"]

    class MockChitchatAutomation(original_cls):
        """The real bot, with the mock site wired into its browser context."""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured["automation"] = self

        def _launch_camoufox(self):
            browser = super()._launch_camoufox()
            context = getattr(browser, "_context", None)
            if context is None:
                try:
                    context = (browser.contexts or [None])[0]
                except Exception:
                    context = None
            if context is not None:
                routes.install(
                    context, backend, me=bot_name, api_base="", log=log,
                    on_page=lambda path, who: log(f"[MockSite] bot browser loaded {path}"))
                log("[MockSite] ✓ route hook installed on the account's browser context")
            return browser

    automation_module.ChitchatAutomation = MockChitchatAutomation

    # -- run the real pipeline + the user's browser together -------------- #
    from tools import session_chat  # noqa: E402  (after env is set)

    accounts = session_chat.load_accounts()
    target_dir = str(Path(account["session_dir"]).resolve())
    usable = [a for a in accounts
              if str(Path(str(a.get("session_dir") or "")).resolve()) == target_dir]
    if not usable:
        log(f"[E2E] ✗ the copied session was not found by the loader "
            f"({len(accounts)} accounts seen)")
        automation_module.ChitchatAutomation = original_cls
        server.shutdown()
        log.close()
        return 2
    usable_account = usable[0]
    log(f"[E2E] session chosen by the loader: {usable_account.get('email')} "
        f"({usable_account.get('session_dir')})")

    if args.connect_only:
        autom = session_chat.build_automation(usable_account, headless=not args.visible,
                                             thread_id=1, log=log)
        ok, reason = session_chat.connect_only(autom, log=log)
        log("=" * 72)
        log(f"[E2E] connect-only result: ok={ok} · {reason}")
        log("=" * 72)
        automation_module.ChitchatAutomation = original_cls
        try:
            server.shutdown()
        except Exception:
            pass
        log.close()
        return 0 if ok else 1

    done_event = threading.Event()
    stranger = StrangerBrowser(backend=backend, api_base=api_base, bot_name=bot_name,
                               log=log, done_event=done_event, name=site.STRANGER_NAME)
    stranger.start()

    def stop_when_done():
        done_event.wait(timeout=240)
        time.sleep(6.0)          # let the final reply land in the log
        automation = captured.get("automation")
        if automation is not None:
            log("[E2E] the conversation is complete — stopping the bot session")
            try:
                automation.stop()
            except Exception as error:
                log(f"[E2E] stop() warning: {error}")

    stopper = threading.Thread(target=stop_when_done, name="e2e-stopper", daemon=True)
    stopper.start()

    log("[E2E] starting the real session runner (tools/session_chat.run_account)")
    summary = session_chat.run_account(usable_account, headless=not args.visible,
                                       minutes=args.minutes, thread_id=1, log=log)

    done_event.wait(timeout=30)
    stranger.join(timeout=30)
    automation_module.ChitchatAutomation = original_cls   # restore the real class

    # -- verdict ---------------------------------------------------------- #
    lines = log.lines
    def has(pattern: str) -> bool:
        return any(pattern in line for line in lines)

    checks = {
        "browser launched (real Chromium)": has("[Browser]") and has("Using the installed browser"),
        "account identity + own Chrome profile": has("[Identity] reusing the same browser"),
        "device fingerprint applied before the site": has("[Stealth] Chromium anti-detect applied"),
        "saved session imported (cookies)": has("imported the saved session")
                                            or has("cookies come from the profile itself"),
        "session restored + verified by the site": has("[Restore] ✓ saved session verified"),
        "chat page opened for the account": has("bot browser loaded /chat/"),
        "the user's message detected": has("Stranger:") or has("[SMS]"),
        "the bot replied through ChatRuleBot": has("ChatRuleBot reply") or has("[REPLY]"),
        "reply visible in the user's browser": bool([r for r in stranger.replies
                                                    if r and r.strip()]),
        "no user-side error": not stranger.error,
    }
    passed = all(checks.values())

    log("")
    log("=" * 72)
    log("[E2E] result")
    log("=" * 72)
    for name, value in checks.items():
        log(f"  {'✓' if value else '✗'} {name}")
    log(f"  bot session summary: {json.dumps(summary, default=str)}")
    log(f"  user-side replies seen: {json.dumps(stranger.replies, ensure_ascii=False)}")
    if stranger.error:
        log(f"  user-side error: {stranger.error}")
    cookies = backend.cookie_seen or {}
    log(f"  cookies sent by the restored session: {cookies.get('count', 0)} "
        f"({', '.join((cookies.get('names') or [])[:12])})")
    log(f"  chat transcript: {json.dumps(backend.all(), ensure_ascii=False)}")
    log(f"[E2E] {'PASS' if passed else 'FAIL'} — full log: {log.path}")

    try:
        server.shutdown()
    except Exception:
        pass
    log.close()
    return 0 if passed else 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
