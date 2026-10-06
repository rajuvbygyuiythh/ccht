#!/usr/bin/env python3
"""Offline tests for the chitchat stand-in + the persistent-session import.

Covers the three defects the real-browser E2E exposed and the mock's contract
with the bot's selectors:

1. ``launch_persistent_context()`` has no ``storage_state`` argument, so a
   saved session must be imported right after the profile opens
   (:func:`browser.browser_engine._import_storage_state`).
2. A page identity with a dot ("sadia.6.7") must not end up as JS
   (``window.sadia.6.7`` — a syntax error that silently killed the chat page).
3. The mock chat page must expose exactly the markup
   ``browser/chat_reader.py`` and ``browser_automation.check_if_disconnected``
   look for — and no START button while the chat is still running.

Run: ``python3 test_mock_chitchat.py``
"""

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if "winsound" not in sys.modules:
    sys.modules["winsound"] = types.ModuleType("winsound")

from browser.browser_engine import _import_storage_state  # noqa: E402
from tools.mock_chitchat import site  # noqa: E402

PASSED = 0
FAILED = 0


def check(label, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


class FakeContext:
    """Records what the import would do to a real BrowserContext."""

    def __init__(self):
        self.cookies = []
        self.scripts = []

    def add_cookies(self, cookies):
        for cookie in cookies:
            if not cookie.get("name") or cookie.get("sameSite") not in (
                    "Strict", "Lax", "None"):
                raise ValueError(f"bad cookie: {cookie!r}")
        self.cookies.extend(cookies)

    def add_init_script(self, script):
        self.scripts.append(script)


def test_storage_state_import():
    print("\n[storage state import]")
    state = {
        "cookies": [
            {"name": "token", "value": "abc", "domain": ".chitchat.gg", "path": "/",
             "sameSite": "Lax"},
            {"name": "legacy", "value": "x", "domain": ".chitchat.gg", "path": "/",
             "sameSite": "no_restriction"},          # normalized to None
            {"name": "", "value": "empty"},           # skipped
            {"value": "nameless"},                    # skipped
        ],
        "origins": [
            {"origin": "https://app.chitchat.gg",
             "localStorage": [{"name": "auth", "value": "1"},
                              {"name": "flag", "value": 'he said "hi"'}]},
        ],
    }
    context = FakeContext()
    count = _import_storage_state(context, state)
    check("only valid cookies are imported", count == 2, f"got {count}")
    check("sameSite is normalized",
          [c["sameSite"] for c in context.cookies] == ["Lax", "None"],
          str([c["sameSite"] for c in context.cookies]))
    check("localStorage becomes an init script", len(context.scripts) == 1)
    check("quotes in localStorage survive",
          'he said \\"hi\\"' in context.scripts[0] if context.scripts else False,
          context.scripts[0][:120] if context.scripts else "")

    # a file path works too, and a missing/broken file is never fatal
    tmp = Path("/tmp/eva_test_storage_state.json")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    context2 = FakeContext()
    check("a saved file path is accepted", _import_storage_state(context2, str(tmp)) == 2)
    check("a missing file returns 0", _import_storage_state(FakeContext(), "/tmp/nope.json") == 0)
    check("garbage returns 0", _import_storage_state(FakeContext(), {"cookies": "nope"}) == 0)


def test_one_bad_cookie_does_not_lose_the_rest():
    print("\n[resilience]")

    class PickyContext(FakeContext):
        def add_cookies(self, cookies):
            if len(cookies) > 1:
                raise ValueError("batch rejected")
            super().add_cookies(cookies)

    context = PickyContext()
    count = _import_storage_state(context, {"cookies": [
        {"name": "a", "value": "1", "domain": ".x"},
        {"name": "b", "value": "2", "domain": ".x"},
    ]})
    check("falls back to one-by-one", count == 2 and len(context.cookies) == 2, f"got {count}")


def test_mock_pages():
    print("\n[mock pages]")
    chat = site.page_html("/chat/", me="sadia.6.7")
    check("connected marker for the bot", 'id="connected-text"' in chat
          and "You are now chatting with" in chat)
    check("my-username markup matches chat_reader", 'class="truncate text-sm font-bold"' in chat)
    check("message container matches chat_reader", 'class="overflow-y-auto"' in chat)
    check("message input matches the bot's selector",
          'name="message"' in chat and 'placeholder="Type a message..."' in chat)
    check("SKIP button is the connected-state style", 'class="bg-warning"' in chat)
    check("no START button while the chat runs",
          ">START<" not in chat and "textContent = 'START'" in chat)
    check("identity with a dot stays valid JS",
          'window.MOCK_ME = "sadia.6.7";' in chat and "window.sadia" not in chat)
    check("API calls are same-origin", 'var API = "";' in chat)

    home = site.page_html("/", me="sadia.6.7")
    check("home page offers Start Text Chat", "Start Text Chat" in home
          and "/start/new" in home)
    captcha = site.page_html("/start/new", me="sadia.6.7")
    check("captcha gate hands over to the chat", "location.replace('/chat/')" in captcha)


def test_backend():
    print("\n[chat backend]")
    backend = site.ChatBackend(log=lambda _m: None)
    backend.add("Stranger42", "hey")
    backend.add("sadia.6.7", "hi")
    first, next_index = backend.since(0)
    check("since(0) returns everything", len(first) == 2 and next_index == 2)
    later, next_index = backend.since(next_index)
    check("since(last) returns nothing twice", later == [] and next_index == 2)
    backend.add("Stranger42", "again")
    later, next_index = backend.since(2)
    check("new messages come after the old ones",
          [m["text"] for m in later] == ["again"] and next_index == 3)
    backend.hello("Eva")
    check("presence is tracked", backend.wait_for_participant("Eva", timeout=0.1))
    backend.leave("Eva")
    check("leaving clears presence", not backend.wait_for_participant("Eva", timeout=0.1))


def test_debug_helpers():
    print("\n[debug helpers]")
    from tools import e2e_mock_chat as e2e

    state = {"path": "/chat/", "bubbles": 2, "last": "Stranger42 hey",
             "input": "", "connected": "You are now chatting with Stranger42",
             "ended": False}
    line = e2e.format_state("bot", state)
    check("state line has the useful fields",
          "bubbles=2" in line and "Stranger42" in line and "ended=False" in line, line)
    check("empty state does not crash", e2e.format_state("bot", {}) == "bot: <no state>")

    checks = {"browser launched (real Chromium)": True,
              "the user's message detected": False}
    hints = e2e.diagnose(checks, "TimeoutError: nope")
    check("only failed checks get hints", not any("browser launched" in h for h in hints))
    check("failed check gets a concrete hint",
          any("the user's message detected" in h for h in hints)
          and any("chat_reader" in h for h in hints), str(hints))
    check("the user-side exception is included",
          any("TimeoutError" in h for h in hints))


def test_run_log_never_breaks_the_run():
    print("\n[log robustness]")
    from tools import e2e_mock_chat as e2e

    path = Path("/tmp/eva_test_runlog.log")
    if path.exists():
        path.unlink()
    log = e2e.RunLog(path)
    log("normal line")
    log.debug("hidden debug line")           # debug off: file only
    check("debug lines go to the file even when not printed",
          "hidden debug line" in path.read_text())

    import builtins

    original_print = builtins.print

    def boom(*a, **k):
        raise BrokenPipeError(32, "Broken pipe")

    builtins.print = boom
    raised = ""
    try:
        log("this must not raise even though stdout is gone")
    except Exception as error:                     # pragma: no cover
        raised = repr(error)
    finally:
        builtins.print = original_print
    check("a closed stdout never raises", not raised, raised)
    check("the logger remembers that stdout is gone", log.stdout_broken is True)
    check("the line still reached the log file",
          "stdout is gone" in path.read_text())
    log.close()


def test_debug_artifacts():
    print("\n[debug artifacts]")
    from tools import e2e_mock_chat as e2e
    from tools.mock_chitchat import site

    class FakePage:
        def __init__(self, html="<html><body>hi</body></html>", fail=False):
            self._html = html
            self._fail = fail
            self.url = "https://app.chitchat.gg/chat/"

        def is_closed(self):
            return False

        def content(self):
            if self._fail:
                raise RuntimeError("page is gone")
            return self._html

        def screenshot(self, path=None, timeout=None):
            if self._fail:
                raise RuntimeError("no screenshot")
            Path(path).write_bytes(b"PNG")

        def on(self, *_a, **_k):
            return None

    backend = site.ChatBackend(log=lambda _m: None)
    backend.add("Stranger42", "hey")
    backend.note_state("sadia.6.7", {"bubbles": 1, "last": "Stranger42 hey"})
    backend.note_dump("sadia.6.7", "<html>dumped</html>")

    log = e2e.RunLog(Path("/tmp/eva_test_artifacts.log"), debug=True)
    recorder = e2e.DebugRecorder(artifacts_dir=Path("/tmp/eva_test_artifacts"), log=log,
                                 enabled=True)
    recorder.watch(FakePage(), "bot")
    result = recorder.capture_page(FakePage(), "bot")
    check("capture writes the DOM", Path(result["html"]).read_text().startswith("<html>"))
    check("capture writes a screenshot", Path(result["screenshot"]).is_file())
    broken = recorder.capture_page(FakePage(fail=True), "bot")
    check("a dead page is captured as a failure, not an exception",
          broken["ok"] is False and broken["html"] == "")
    target = recorder.dump(backend=backend,
                           checks={"the user's message detected": False,
                                   "browser launched (real Chromium)": True},
                           reason="TimeoutError: no reply", results={"log": "x"})
    names = sorted(p.name for p in target.iterdir())
    check("summary is written", "SUMMARY.txt" in names, str(names))
    check("page DOM + screenshot are in the folder",
          "bot_page.html" in names and "bot_screenshot.png" in names, str(names))
    check("event log is written", "events_bot.log" in names, str(names))
    check("mock API trace is written", "mock_api.log" in names, str(names))
    check("page-pushed DOM is written", "dom_from_page_sadia.6.7.html" in names, str(names))
    check("chat transcript is written", "transcript.json" in names, str(names))
    check("cookie evidence is written", "cookies.json" in names, str(names))
    summary = (target / "SUMMARY.txt").read_text()
    check("summary lists the failed check + a hint",
          "✗ the user's message detected" in summary and "chat_reader" in summary)
    check("summary carries the user-side exception", "TimeoutError: no reply" in summary)
    log.close()


def test_route_layer_api():
    print("\n[route hook API]")
    from tools.mock_chitchat import routes, site

    class FakeRequest:
        def __init__(self, url, method="GET", payload=None):
            self.url = url
            self.method = method
            self._payload = payload

        @property
        def post_data_json(self):
            return self._payload

        @property
        def headers(self):
            return {"cookie": "token=abc; mock_cc_session=1"}

    class FakeRoute:
        def __init__(self):
            self.calls = []

        def fulfill(self, **kwargs):
            self.calls.append(kwargs)
            return kwargs

    backend = site.ChatBackend(log=lambda _m: None)
    traces = []
    handler = None

    class FakeContext:
        def route(self, glob, fn):
            nonlocal handler
            handler = fn

    routes.install(FakeContext(), backend, me="sadia.6.7", api_base="",
                   debug=True, log=lambda m: traces.append(m))
    check("the route hook is installed", callable(handler))

    route = FakeRoute()
    handler(route, FakeRequest("https://app.chitchat.gg/api/hello", "POST",
                               {"author": "sadia.6.7"}))
    check("hello registers presence", backend.wait_for_participant("sadia.6.7", timeout=0.1))
    check("debug trace logs the API call",
          any("/api/hello" in t for t in traces), str(traces[-2:]))

    handler(FakeRoute(), FakeRequest("https://app.chitchat.gg/api/send", "POST",
                                     {"author": "sadia.6.7", "text": "hello there"}))
    check("send stores the message",
          [m["text"] for m in backend.all()] == ["hello there"])

    route = FakeRoute()
    handler(route, FakeRequest("https://app.chitchat.gg/api/messages?since=0"))
    payload = route.calls[-1]["json"]
    check("messages are returned to the page",
          payload["messages"][0]["text"] == "hello there" and payload["next"] == 1)

    handler(FakeRoute(), FakeRequest("https://app.chitchat.gg/api/state", "POST",
                                     {"author": "sadia.6.7", "bubbles": 1}))
    check("state telemetry is stored", backend.states[-1]["bubbles"] == 1)

    handler(FakeRoute(), FakeRequest("https://app.chitchat.gg/api/dump", "POST",
                                     {"author": "sadia.6.7", "html": "<html>page</html>"}))
    check("page DOM dump is stored",
          "page" in (backend.dumped_html.get("sadia.6.7") or ""))

    route = FakeRoute()
    handler(route, FakeRequest("https://app.chitchat.gg/chat/?as=Stranger42"))
    html = route.calls[-1]["body"]
    check("a page request renders the stand-in", "You are now chatting with" in html)
    check("the ?as= identity is honoured", 'window.MOCK_ME = "Stranger42"' in html)

    backend.request_dump("sadia.6.7")
    route = FakeRoute()
    handler(route, FakeRequest("https://app.chitchat.gg/api/messages?since=0"))
    check("a dump request is handed to the page",
          route.calls[-1]["json"].get("dump") is True)


if __name__ == "__main__":
    test_storage_state_import()
    test_one_bad_cookie_does_not_lose_the_rest()
    test_mock_pages()
    test_backend()
    test_debug_helpers()
    test_run_log_never_breaks_the_run()
    test_debug_artifacts()
    test_route_layer_api()
    print("\n" + "=" * 72)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    print("=" * 72)
    sys.exit(1 if FAILED else 0)
