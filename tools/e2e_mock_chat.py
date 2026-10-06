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
import traceback
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
    """Timestamped log that also mirrors every line to a file.

    ``--debug`` adds a second channel: :meth:`debug` lines are always written to
    the log file (so a failure can be inspected afterwards) but only printed to
    the terminal when debugging is on.
    """

    def __init__(self, path: Path, debug: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self.lines = []
        self.debug_enabled = bool(debug)
        self.debug_lines = []
        self.stdout_broken = False

    def _write(self, line: str, to_stdout: bool = True) -> None:
        with self._lock:
            self.lines.append(line)
            self._file.write(line + "\n")
            self._file.flush()
        if to_stdout and not self.stdout_broken:
            try:
                print(line, flush=True)
            except (BrokenPipeError, OSError, ValueError):
                # e.g. the output was piped into `head` and the reader is gone.
                # Never let logging break the run: the file keeps everything.
                self.stdout_broken = True

    def __call__(self, message: str) -> None:
        self._write(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def debug(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] [DEBUG] {message}"
        with self._lock:
            self.debug_lines.append(line)
        self._write(line, to_stdout=self.debug_enabled)

    def tail(self, count: int = 80) -> str:
        with self._lock:
            return "\n".join(self.lines[-count:])

    def close(self):
        try:
            self._file.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  debugging: what to look at when a check fails
# --------------------------------------------------------------------------- #

DIAGNOSIS = {
    "browser launched (real Chromium)": [
        "look at [Browser] lines: is EVA_CHROMIUM_EXECUTABLE correct and executable?",
        "run: python3 tools/e2e_mock_chat.py --connect-only --debug",
    ],
    "account identity + own Chrome profile": [
        "look at [Identity] lines; a registry/owner conflict shows up there",
        "check EVA_PROFILES_DIR is writable",
    ],
    "device fingerprint applied before the site": [
        "the stealth line comes from browser/browser_engine.py apply_chromium_stealth()",
        "if missing: the engine is not chromium (browser_engine in data/config.json)",
    ],
    "saved session imported (cookies)": [
        "look at [Browser] ✓ imported the saved session: N cookie(s)",
        "if 0: storage_state.json in the session dir is empty/corrupt",
    ],
    "session restored + verified by the site": [
        "look at [Restore] lines and the route-hook cookie count (how many cookies were sent)",
        "a redirect to /login means the saved cookies are dead",
    ],
    "chat page opened for the account": [
        "the page never reached /chat/ — see bot_page.html in the artifacts",
        "check [MockSite] lines for the last document the browser loaded",
    ],
    "the user's message detected": [
        "look for [SMS]/Stranger: lines in the log and 'bubbles=' in the DOM traces",
        "if the DOM shows the bubble but nothing was detected: browser/chat_reader.py selectors",
        "if the DOM shows no bubble: the page's own /api/messages polling is stuck",
    ],
    "the bot replied through ChatRuleBot": [
        "look for 'ChatRuleBot reply:' (engine) or a FAILED line (then it fell back)",
        "typing/sending errors show up as 'Failed to send' from send_chat_message()",
    ],
    "reply visible in the user's browser": [
        "the reply left the bot but never reached the user's DOM",
        "compare the transcript in transcript.json with user_page.html",
    ],
    "no user-side error": [
        "user_events.log has the console/pageerror/requestfailed lines",
        "user_screenshot.png shows what the user's browser displayed",
    ],
}


def diagnose(checks: dict, stranger_error: str = "") -> list:
    """Turn the check table into concrete 'look here' hints."""
    hints = []
    for name, ok in checks.items():
        if ok:
            continue
        hints.append(f"✗ {name}")
        for line in DIAGNOSIS.get(name, ["no hint recorded for this check"]):
            hints.append(f"    · {line}")
    if stranger_error:
        hints.append(f"  user-side exception: {stranger_error}")
    return hints


def format_state(where: str, state: dict) -> str:
    """One compact line describing what a page is showing."""
    if not state:
        return f"{where}: <no state>"
    return (f"{where}: path={state.get('path')} bubbles={state.get('bubbles')} "
            f"last={str(state.get('last'))[:70]!r} input={str(state.get('input'))[:40]!r} "
            f"connected={str(state.get('connected'))[:45]!r} ended={state.get('ended')}")


class DebugRecorder:
    """Collects browser diagnostics and writes them to an artifact folder.

    Playwright's sync objects belong to the thread that created them, so the
    bot's page is only ever touched from the bot's own thread (the automation
    calls :meth:`capture_page` from ``_close_camoufox``) — everything else is
    either an event listener (fires on the owning thread) or telemetry the page
    pushes to the mock backend itself.
    """

    def __init__(self, *, artifacts_dir: Path, log: RunLog, enabled: bool = False):
        self.root = Path(artifacts_dir)
        self.log = log
        self.enabled = bool(enabled)
        self.dir: Path = None
        self._lock = threading.Lock()
        self.events = {"bot": [], "user": []}
        self.network = []
        self.pages = {"bot": None, "user": None}
        self.captured = {}

    # -- live capture -----------------------------------------------------
    def watch(self, page, where: str) -> None:
        """Attach console/error/network listeners to a page (owning thread)."""
        with self._lock:
            self.pages[where] = page
        try:
            page.on("console", lambda m: self.record(where, "console", f"{m.type}: {m.text[:300]}"))
            page.on("pageerror", lambda e: self.record(where, "pageerror", str(e)[:600]))
            page.on("requestfailed",
                    lambda r: self.record(where, "requestfailed", f"{r.url} → {r.failure}"))
            page.on("framenavigated",
                    lambda f: self.record(where, "nav", str(f.url)[:200]))
            page.on("request", lambda r: self._network(where, r))
        except Exception as error:
            self.record(where, "watch-error", str(error))

    def _network(self, where: str, request) -> None:
        try:
            line = f"{where} {request.method} {request.url}"
            with self._lock:
                self.network.append(line)
            if self.enabled:
                self.log.debug(f"[net] {line}")
        except Exception:
            pass          # a listener must never raise into Playwright

    def record(self, where: str, kind: str, text: str) -> None:
        try:
            line = f"{kind}: {text}"
            with self._lock:
                self.events.setdefault(where, []).append(line)
            self.log.debug(f"[{where}] {line}")
        except Exception:
            pass

    # -- artifacts --------------------------------------------------------
    def capture_page(self, page, where: str) -> dict:
        """Screenshot + DOM of one page. MUST run on the page's own thread."""
        result = {"where": where, "ok": False, "html": "", "screenshot": ""}
        if page is None:
            return result
        try:
            if page.is_closed():
                self.log.debug(f"[artifacts] {where}: page is already closed — skipped")
                return result
        except Exception:
            pass
        try:
            target = self.ensure_dir()
        except Exception as error:
            self.log.debug(f"[artifacts] {where}: {error}")
            return result
        try:
            html = page.content()
            path = target / f"{where}_page.html"
            path.write_text(html, encoding="utf-8")
            result["html"] = str(path)
            result["ok"] = True
        except Exception as error:
            self.record(where, "dump-error", f"content(): {error}")
        try:
            path = target / f"{where}_screenshot.png"
            page.screenshot(path=str(path), timeout=15000)
            result["screenshot"] = str(path)
        except Exception as error:
            self.record(where, "dump-error", f"screenshot(): {error}")
        with self._lock:
            self.captured[where] = result
        self.log.debug(f"[artifacts] {where}: {result['html']} {result['screenshot']}")
        return result

    def ensure_dir(self) -> Path:
        with self._lock:
            if self.dir is None:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                self.dir = self.root / stamp
                self.dir.mkdir(parents=True, exist_ok=True)
            return self.dir

    def dump(self, *, backend=None, checks: dict = None, reason: str = "",
             results: dict = None) -> Path:
        """Write everything we know to the artifact folder (never raises)."""
        target = self.ensure_dir()
        try:
            (target / "events_bot.log").write_text(
                "\n".join(self.events.get("bot") or ["<none>"]) + "\n", encoding="utf-8")
            (target / "events_user.log").write_text(
                "\n".join(self.events.get("user") or ["<none>"]) + "\n", encoding="utf-8")
            (target / "network.log").write_text(
                "\n".join(self.network or ["<none>"]) + "\n", encoding="utf-8")
        except Exception as error:
            self.log.debug(f"[artifacts] write failed: {error}")

        if backend is not None:
            try:
                (target / "transcript.json").write_text(
                    json.dumps(backend.all(), indent=2, ensure_ascii=False), encoding="utf-8")
                (target / "mock_api.log").write_text(
                    "\n".join(backend.trace_tail()) + "\n", encoding="utf-8")
                states = []
                for entry in backend.states:
                    states.append(format_state(str(entry.get("where")), entry))
                (target / "dom_trace.log").write_text(
                    "\n".join(states or ["<no page telemetry>"]) + "\n", encoding="utf-8")
                for who, html in (backend.dumped_html or {}).items():
                    safe = str(who).replace("/", "_").replace(" ", "_")
                    (target / f"dom_from_page_{safe}.html").write_text(
                        str(html or ""), encoding="utf-8")
                (target / "cookies.json").write_text(
                    json.dumps(backend.cookie_seen or {}, indent=2, ensure_ascii=False),
                    encoding="utf-8")
            except Exception as error:
                self.log.debug(f"[artifacts] backend dump failed: {error}")

        summary = ["E2E chat test artifacts", "=" * 72, f"reason: {reason or 'n/a'}", ""]
        if checks:
            summary.append("checks:")
            for name, ok in checks.items():
                summary.append(f"  {'✓' if ok else '✗'} {name}")
            summary.append("")
            hints = diagnose(checks, reason if reason.startswith(("Timeout", "Runtime")) else "")
            if hints:
                summary.append("what to look at:")
                summary.extend(hints)
                summary.append("")
        if results:
            summary.append("results:")
            summary.extend(f"  {k}: {v}" for k, v in results.items())
            summary.append("")
        summary.append("files in this folder:")
        for item in sorted(target.iterdir()) if target.is_dir() else []:
            summary.append(f"  {item.name}")
        summary.append("")
        summary.append("last log lines:")
        summary.append(self.log.tail(80))
        try:
            (target / "SUMMARY.txt").write_text("\n".join(summary), encoding="utf-8")
        except Exception as error:
            self.log.debug(f"[artifacts] summary failed: {error}")
        return target


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
                 name="Stranger42", start_event=None, debug=False, recorder=None):
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
        self.traceback = ""
        self.finished = threading.Event()
        self.debug = bool(debug)
        self.recorder = recorder
        self.page = None

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
                       log=self.log, debug=self.debug)
        page = context.new_page()
        if self.recorder is not None:
            self.recorder.watch(page, "user")
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
            self.traceback = traceback.format_exc()
            self.log(f"[Stranger] ✗ {self.error}")
            if self.debug:
                self.log.debug("[user] traceback:\n" + self.traceback)
        finally:
            self.done_event.set()
            self.finished.set()
            if page is not None:
                try:
                    page.screenshot(path=str(Path("/tmp/e2e_stranger_last.png")))
                    self.log(f"[Stranger] screenshot of the user's window: "
                             f"/tmp/e2e_stranger_last.png")
                except Exception:
                    pass
            if self.recorder is not None and page is not None:
                # The user's page belongs to this thread — capture it here.
                self.recorder.capture_page(page, "user")
            try:
                self.page = page
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
    parser.add_argument("--debug", action="store_true",
                        help="trace every API call, page console line and network "
                             "request, print DOM state every 2 s and always keep "
                             "the artifacts (screenshots + page HTML + traces)")
    parser.add_argument("--artifacts", default="/home/user/e2e_artifacts",
                        help="folder for the debug/failure artifacts")
    parser.add_argument("--no-artifacts", action="store_true",
                        help="never write artifacts (even when a check fails)")
    parser.add_argument("--debug-interval", type=float, default=2.0,
                        help="seconds between DOM state traces in --debug")
    parser.add_argument("--hard-timeout", type=float, default=0.0,
                        help="absolute safety stop in seconds (0 = minutes*60+150); "
                             "artifacts are written before exiting")
    args = parser.parse_args()

    log = RunLog(Path(args.log), debug=args.debug)
    recorder = DebugRecorder(artifacts_dir=Path(args.artifacts), log=log,
                             enabled=args.debug)
    log("=" * 72)
    log("[E2E] saved account session → real browser → chat with a user")
    if args.debug:
        log("[E2E] DEBUG mode: API/console/network traces + DOM snapshots + artifacts")
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
                    debug=args.debug,
                    on_page=lambda path, who: log(f"[MockSite] bot browser loaded {path}"))
                log("[MockSite] ✓ route hook installed on the account's browser context")
                # Watch every page this account opens (console/errors/network).
                # Listeners fire on the automation's own thread, so this is safe.
                try:
                    for page in list(context.pages):
                        recorder.watch(page, "bot")
                    context.on("page", lambda page: recorder.watch(page, "bot"))
                except Exception as error:
                    log(f"[DEBUG] could not watch the bot's pages: {error}")
            return browser

        def _close_camoufox(self):
            """Capture the bot's page (owning thread!) before the browser goes."""
            try:
                page = None
                pages = []
                try:
                    pages = list(getattr(self.context, "pages", []) or [])
                except Exception:
                    pages = []
                for candidate in pages:
                    try:
                        if "/chat" in str(candidate.url):
                            page = candidate
                    except Exception:
                        continue
                recorder.capture_page(page or (pages[-1] if pages else None), "bot")
            except Exception as error:
                log(f"[DEBUG] bot page capture failed: {error}")
            return super()._close_camoufox()

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
        if not args.no_artifacts and (args.debug or not ok):
            try:
                target = recorder.dump(
                    backend=backend, checks={"session restored": bool(ok)},
                    reason=reason, results={"log": str(log.path)})
                log(f"[E2E] artifacts: {target}")
            except Exception as error:
                log(f"[E2E] artifact dump failed: {error}")
        automation_module.ChitchatAutomation = original_cls
        try:
            server.shutdown()
        except Exception:
            pass
        log.close()
        return 0 if ok else 1

    stop_all = threading.Event()

    # Absolute safety net: a debug run must never hang forever.  When the limit
    # is reached the artifacts are written and the process exits with code 2.
    hard_limit = float(args.hard_timeout) if args.hard_timeout else (args.minutes * 60 + 150)

    def hard_stop():
        if hard_limit <= 0 or stop_all.wait(hard_limit):
            return
        try:
            log(f"[E2E] ✗ hard timeout ({hard_limit:.0f}s) reached — writing artifacts "
                f"and exiting (a browser call is stuck)")
            log(f"[E2E] last DOM states: " + " | ".join(
                format_state("page", e) for e in list(backend.states)[-2:]))
            target = recorder.dump(backend=backend,
                                   reason=f"hard timeout after {hard_limit:.0f}s",
                                   results={"log": str(log.path)})
            log(f"[E2E] artifacts: {target}")
        except Exception as error:
            log(f"[E2E] artifact dump failed: {error}")
        log.close()
        os._exit(2)

    threading.Thread(target=hard_stop, name="e2e-hard-stop", daemon=True).start()

    # The pages push their own DOM state to the mock backend; this thread just
    # reads that (plain Python, no Playwright cross-thread calls).
    def state_watcher():
        seen = 0
        while not stop_all.is_set():
            stop_all.wait(max(0.5, float(args.debug_interval) / 2.0))
            try:
                states = list(backend.states)
            except Exception:
                continue
            for entry in states[seen:]:
                seen += 1
                where = "bot" if str(entry.get("author")) == bot_name else "user"
                log.debug("[dom] " + format_state(where, entry))

    if args.debug:
        backend.request_dump(bot_name)          # the bot's DOM, for the artifacts

    done_event = threading.Event()
    stranger = StrangerBrowser(backend=backend, api_base=api_base, bot_name=bot_name,
                               log=log, done_event=done_event, name=site.STRANGER_NAME,
                               debug=args.debug, recorder=recorder)
    stranger.start()

    watcher = threading.Thread(target=state_watcher, name="e2e-state-watcher", daemon=True)
    watcher.start()

    def stop_when_done():
        done_event.wait(timeout=240)
        time.sleep(6.0)          # let the final reply land in the log
        automation = captured.get("automation")
        if automation is not None:
            log("[E2E] the conversation is complete — stopping the bot session")
            # Last DOM snapshot of the bot's page (captured on its own thread by
            # _close_camoufox) is what the artifacts will show.
            try:
                backend.request_dump(bot_name)
            except Exception:
                pass
            time.sleep(1.5)      # give the page a poll cycle to send it
            try:
                automation.stop()
            except Exception as error:
                log(f"[E2E] stop() warning: {error}")
        stop_all.set()

    stopper = threading.Thread(target=stop_when_done, name="e2e-stopper", daemon=True)
    stopper.start()

    log("[E2E] starting the real session runner (tools/session_chat.run_account)")
    summary = session_chat.run_account(usable_account, headless=not args.visible,
                                       minutes=args.minutes, thread_id=1, log=log)

    done_event.wait(timeout=30)
    stranger.join(timeout=30)
    stop_all.set()
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

    results = {
        "log": str(log.path),
        "session": usable_account.get("email"),
        "cookies_sent": cookies.get("count", 0),
        "messages_in": summary.get("messages_in"),
        "messages_out": summary.get("messages_out"),
        "user_replies": stranger.replies,
    }
    if not passed:
        log("")
        log("[E2E] what to look at / ki check korben:")
        for line in diagnose(checks, stranger.error):
            log(f"    {line}")

    artifacts = None
    if not args.no_artifacts and (args.debug or not passed):
        try:
            backend.request_dump("Stranger42")
            artifacts = recorder.dump(backend=backend, checks=checks,
                                      reason=stranger.error or summary.get("reason") or "",
                                      results=results)
            log(f"[E2E] artifacts: {artifacts}")
            log(f"[E2E]   read SUMMARY.txt first, then events_*.log / dom_trace.log")
        except Exception as error:
            log(f"[E2E] artifact dump failed: {error}")

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
