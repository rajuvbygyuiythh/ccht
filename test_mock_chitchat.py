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


if __name__ == "__main__":
    test_storage_state_import()
    test_one_bad_cookie_does_not_lose_the_rest()
    test_mock_pages()
    test_backend()
    print("\n" + "=" * 72)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    print("=" * 72)
    sys.exit(1 if FAILED else 0)
