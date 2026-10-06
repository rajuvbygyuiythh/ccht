#!/usr/bin/env python3
"""test_chat_detect.py — stranger-SMS detection (the "bot can't see my sms" fix).

Layers tested:

A. :class:`browser.chat_reader.MessageTracker` (pure Python) — the incremental
   new-message detection that replaced the old ``len(dom) > last_count``
   arithmetic.  Runs everywhere.
B. The real DOM extraction JS (:data:`browser.chat_reader.CHAT_EXTRACT_JS`)
   against fixture pages — runs when ``node`` + ``jsdom`` are available
   (``npm install jsdom`` in the project root, or set ``EVA_JSDOM_DIR``),
   otherwise those checks are skipped with a note.
C. Extractor wiring: the page payload is honoured, our own echoed text is
   never answered, diagnostics + DOM dump work.
D. Browser-layer integration: ``ChitchatAutomation.extract_chat_from_page``
   falls back to the legacy selectors, the "0 messages parsed" warning fires
   once, and the counter-drift bug stays fixed.

Run:  python test_chat_detect.py
"""
from __future__ import annotations

import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from browser import chat_reader
from browser.chat_reader import MessageTracker, extract_with_diag, describe_diag

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


def msg(speaker: str, text: str, mid=None) -> dict:
    return {"speaker": speaker, "message": text, "mid": mid}


# ---------------------------------------------------------------------------
# A. MessageTracker
# ---------------------------------------------------------------------------

def test_tracker() -> None:
    print("\n— A. incremental new-message detection (MessageTracker) —")

    t = MessageTracker()
    new = t.sync([msg("Stranger", "hey")])
    check("first poll reports the stranger's sms", [m["message"] for m in new] == ["hey"])

    new = t.sync([msg("Stranger", "hey"), msg("You", "hi cutie")])
    check("our own reply is not reported back as new",
          [m["message"] for m in new] == ["hi cutie"] and new[0]["speaker"] == "You")

    new = t.sync([msg("Stranger", "hey"), msg("You", "hi cutie"), msg("Stranger", "how r u")])
    check("a later stranger sms is detected",
          [m["message"] for m in new] == ["how r u"])

    # ---- regression: own message never rendered by the site -------------
    # Old logic: last_count += 1 after every send, even though the DOM did
    # not grow -> counter runs ahead -> the stranger's next sms is invisible.
    old_last_count = 0
    old_last_count += 1                      # we sent our first message
    dom_after_stranger = [msg("Stranger", "hello?")]   # site hides our bubble
    old_detected = len(dom_after_stranger) > old_last_count
    check("OLD count logic really did miss the sms (documents the bug)", old_detected is False)

    t2 = MessageTracker()
    t2.snapshot([])                          # nothing visible at chat start
    t2.sync([])                              # we sent a message, site shows nothing
    detected = t2.sync(dom_after_stranger)
    check("NEW tracker still detects it when our own message is not rendered",
          [m["message"] for m in detected] == ["hello?"])

    # ---- repeated identical texts ---------------------------------------
    t3 = MessageTracker()
    t3.sync([msg("Stranger", "hi")])
    new = t3.sync([msg("Stranger", "hi"), msg("Stranger", "hi")])
    check("a repeated sms is reported exactly once", len(new) == 1)
    new = t3.sync([msg("Stranger", "hi"), msg("Stranger", "hi"), msg("Stranger", "hi")])
    check("third identical sms is detected too", len(new) == 1)

    # ---- extra multi-line "pending" replies (bot side) -------------------
    t4 = MessageTracker()
    t4.sync([msg("Stranger", "heyy")])
    t4.sync([msg("Stranger", "heyy"), msg("You", "👻👻"), msg("You", "we can chat on snap")])
    new = t4.sync([msg("Stranger", "heyy"), msg("You", "👻👻"), msg("You", "we can chat on snap"),
                   msg("Stranger", "send it")])
    check("pending multi-message sends do not break detection",
          [m["message"] for m in new] == ["send it"])

    # ---- new chat / DOM reset -------------------------------------------
    t5 = MessageTracker()
    t5.sync([msg("Stranger", "old chat 1"), msg("You", "old reply"), msg("Stranger", "old chat 2")])
    t5.reset()
    new = t5.sync([msg("Stranger", "new stranger says hi")])
    check("after a chat reset only the new chat's messages are reported",
          [m["message"] for m in new] == ["new stranger says hi"])

    t6 = MessageTracker()
    new = t6.snapshot([msg("Stranger", "history 1"), msg("Stranger", "history 2")])
    check("snapshot() seeds a baseline without replying", new == [])
    new = t6.sync([msg("Stranger", "history 1"), msg("Stranger", "history 2"),
                   msg("Stranger", "brand new")])
    check("baseline + delta works (first-message mode)", len(new) == 1 and new[0]["message"] == "brand new")

    # ---- message ids are preferred over text ----------------------------
    t7 = MessageTracker()
    t7.sync([msg("Stranger", "hi", mid="101")])
    new = t7.sync([msg("Stranger", "hi", mid="101"), msg("Stranger", "hi", mid="102")])
    check("distinct message ids are treated as distinct sms", len(new) == 1)

    # ---- edge cases ------------------------------------------------------
    t8 = MessageTracker()
    check("empty/None DOM is handled", t8.sync([]) == [] and t8.sync(None) == [])
    texts = MessageTracker()
    texts.sync([msg("Stranger", "yo")])
    got = texts.new_stranger_texts([msg("Stranger", "yo"), msg("You", "hey"), msg("Stranger", "u there?")])
    check("new_stranger_texts filters bot lines out", got == ["u there?"])


# ---------------------------------------------------------------------------
# B. real DOM extraction JS via node + jsdom
# ---------------------------------------------------------------------------

HARNESS_JS = r"""
const fs = require('fs');
const { JSDOM } = require('jsdom');
const [, , htmlPath, jsPath, recentJson, hintJson] = process.argv;
const dom = new JSDOM(fs.readFileSync(htmlPath, 'utf8'));
global.document = dom.window.document;
global.window = dom.window;
const fn = eval('(' + fs.readFileSync(jsPath, 'utf8').trim() + ')');
const out = fn({
  recentSent: JSON.parse(recentJson || '[]'),
  myUsernameHint: (hintJson && hintJson !== 'null') ? JSON.parse(hintJson) : null
});
console.log(JSON.stringify(out));
"""

FIXTURES = {
    # fixture file -> (recent_sent, my_username_hint, expected [(speaker, text)], notes)
    "chat_v1_original.html": ([], None, [("Stranger", "hey there"), ("You", "hi cutie"),
                                         ("Stranger", "how r u")], "original site markup"),
    "chat_v2_own_no_username.html": ([], None, [("Stranger", "yo"), ("You", "heyy"),
                                                ("Stranger", "u there?")], "own bubbles carry no username"),
    "chat_v3_plain_li.html": (["hii"], "Eva", [("Stranger", "hey"), ("You", "hii"),
                                               ("Stranger", "from?")], "no select-text / no emoji-content"),
    "chat_v4_div_messages.html": ([], None, [("Stranger", "sup"), ("You", "nm wbu"),
                                             ("Stranger", "just chillin")], "div-based message rows"),
    "chat_v5_nomatch.html": ([], "me", [("Stranger", "hello?"), ("You", "hey")],
                             "unknown layout -> Name: text fallback"),
}


def _find_jsdom_dir() -> str | None:
    candidates = []
    env = os.environ.get("EVA_JSDOM_DIR")
    if env:
        candidates.append(Path(env))
    candidates += [ROOT, ROOT / "tests", Path.cwd()]
    for base in candidates:
        if (base / "node_modules" / "jsdom").is_dir():
            return str(base)
    return None


def _run_fixture(node: str, jsdom_dir: str, workdir: Path, html: Path, recent, hint):
    cmd = [node, str(workdir / "run_extract.js"), str(html),
           str(workdir / "extract.js"), json.dumps(recent), json.dumps(hint)]
    env = dict(os.environ)
    env["NODE_PATH"] = str(Path(jsdom_dir) / "node_modules")
    res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:400] or "node failed")
    return json.loads(res.stdout)


def test_dom_extraction() -> None:
    print("\n— B. DOM extraction against fixture pages (jsdom) —")
    node = shutil.which("node")
    jsdom_dir = _find_jsdom_dir()
    if not node or not jsdom_dir:
        reason = "node not installed" if not node else "jsdom not installed"
        print(f"  [SKIP] {reason} — install with: npm install jsdom   "
              f"(or point EVA_JSDOM_DIR at a folder that has node_modules/jsdom)")
        return

    fixture_dir = ROOT / "tests" / "fixtures" / "chat_dom"
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        (workdir / "extract.js").write_text(chat_reader.CHAT_EXTRACT_JS, encoding="utf-8")
        (workdir / "run_extract.js").write_text(HARNESS_JS, encoding="utf-8")
        for name, (recent, hint, expected, note) in FIXTURES.items():
            html = fixture_dir / name
            if not html.exists():
                check(f"{name} fixture present", False, f"missing {html}")
                continue
            try:
                out = _run_fixture(node, jsdom_dir, workdir, html, recent, hint)
            except Exception as exc:
                check(f"{name} extraction runs", False, str(exc))
                continue
            got = [(m["speaker"], m["message"]) for m in out.get("messages", [])]
            check(f"{name}: detects the right sms ({note})", got == expected,
                  f"\n         got      {got}\n         expected {expected}")
            diag = out.get("diag") or {}
            check(f"{name}: diagnostics reported", bool(diag.get("container") or diag.get("itemSelector")),
                  describe_diag(diag))
            if name == "chat_v5_nomatch.html":
                check("unknown layout used the text-line fallback",
                      diag.get("itemSelector") == "text-line-fallback", describe_diag(diag))
            if name in ("chat_v1_original.html", "chat_v4_div_messages.html"):
                check(f"{name}: no duplicated messages (ancestor filter)",
                      len(got) == len(set(got)), f"got {got}")


# ---------------------------------------------------------------------------
# C. module wiring / post-processing
# ---------------------------------------------------------------------------

class FakePage:
    """Minimal page stub: returns a canned JS result, records the args."""

    def __init__(self, payload):
        self.payload = payload
        self.args = None

    def evaluate(self, expr, arg=None):
        self.args = arg
        return self.payload

    def content(self):
        return "<html><body>fake</body></html>"


def test_wiring() -> None:
    print("\n— C. extractor wiring & post-processing —")

    payload = {"messages": [
        {"speaker": "Stranger", "message": "hey", "via": "username"},
        {"speaker": "Stranger", "message": "heyy", "via": "default"},   # echoes our send
    ], "diag": {"container": "main ol", "items": 2}}
    page = FakePage(payload)
    messages, diag = extract_with_diag(page, recent_sent=["heyy"], my_username="Eva_21")
    check("sender of a JS payload is honoured", messages[0]["speaker"] == "Stranger")
    check("our own echoed text is re-labelled (never answered)",
          messages[1]["speaker"] == "You" and messages[1]["via"] == "sent")
    check("recent_sent + username hint are passed to the page",
          page.args and page.args["recentSent"] == ["heyy"] and page.args["myUsernameHint"] == "Eva_21")
    check("diag is merged from the page payload", diag.get("container") == "main ol")
    check("describe_diag renders a readable summary", "container=main ol" in describe_diag(diag))

    broken = FakePage(None)
    msgs, diag2 = extract_with_diag(broken, recent_sent=[])
    check("a broken/None payload returns [] instead of raising", msgs == [])

    with tempfile.TemporaryDirectory() as tmp:
        path = chat_reader.dump_dom(FakePage(None), root_dir=tmp)
        check("dump_dom writes an HTML file for debugging",
              path is not None and Path(path).exists())

    check("legacy extractor still exists as a fallback", hasattr(chat_reader, "legacy_extract"))


# ---------------------------------------------------------------------------
# D. browser-layer integration (real ChitchatAutomation methods)
# ---------------------------------------------------------------------------

def _load_automation_module():
    """Import browser.browser_automation, stubbing Windows/Qt deps if needed."""
    sys.path.insert(0, str(ROOT))

    def _stub_platform():
        if "winsound" not in sys.modules:
            sys.modules["winsound"] = types.ModuleType("winsound")
        if "PyQt6" not in sys.modules:
            class _Sig:  # minimal pyqtSignal stand-in
                def __init__(self, *a, **k):
                    pass

                def connect(self, *a, **k):
                    pass

                def emit(self, *a, **k):
                    pass

            pkg = types.ModuleType("PyQt6")
            core = types.ModuleType("PyQt6.QtCore")
            core.QThread = type("QThread", (), {"__init__": lambda self, *a, **k: None})
            core.pyqtSignal = lambda *a, **k: _Sig()
            pkg.QtCore = core
            sys.modules["PyQt6"] = pkg
            sys.modules["PyQt6.QtCore"] = core

    try:
        import browser.browser_automation as ba
        return ba, "real dependencies"
    except Exception:
        pass
    sys.modules.pop("browser.browser_automation", None)
    _stub_platform()
    try:
        import browser.browser_automation as ba
        return ba, "stubbed Qt/Windows deps (offline check)"
    except Exception as exc:
        return None, f"unavailable: {type(exc).__name__}: {exc}"


class ScriptedPage:
    """Page stub that replays a scripted DOM (one entry per poll).

    ``script`` entries are the *DOM truth*: a list of
    ``{'speaker', 'message'}`` dicts.  The stub answers both the new
    extractor JS and the legacy one, exactly like a real page would.
    """

    def __init__(self, script, my_username=None):
        self.script = [[dict(m) for m in row] for row in script]
        self.index = 0
        self.expressions = []
        self.my_username = my_username

    def set_poll(self, index: int) -> None:
        self.index = min(index, len(self.script) - 1)

    @property
    def current(self):
        return self.script[min(self.index, len(self.script) - 1)]

    def evaluate(self, expr, arg=None):
        self.expressions.append(expr)
        dom = self.current
        if expr == chat_reader.CHAT_EXTRACT_JS:
            return {
                "messages": [dict(m) for m in dom],
                "diag": {"container": "main ol", "itemSelector": "li.select-text",
                         "items": len(dom), "myUsername": self.my_username,
                         "speakerSources": {"username": len(dom)}, "rawTextLength": 42},
            }
        # legacy extractor (inline JS in browser_automation.py) -> plain list
        return [dict(m) for m in dom]


def _make_bot(ba):
    bot = object.__new__(ba.ChitchatAutomation)
    logs: list[str] = []
    bot.log = lambda message: logs.append(str(message))
    bot._recent_sent_texts = collections.deque(maxlen=12)
    bot._my_username = None
    bot._last_detect_diag = {}
    bot._empty_extract_polls = 0
    bot._detect_warned = False
    return bot, logs


def test_integration() -> None:
    print("\n— D. browser-layer integration (ChitchatAutomation) —")
    ba, note = _load_automation_module()
    if ba is None:
        print(f"  [SKIP] browser_automation not importable — {note}")
        return
    print(f"  [info] {note}")

    # --- new reader handles the SMS path ----------------------------------
    bot, logs = _make_bot(ba)
    page = ScriptedPage([[{"speaker": "Stranger", "message": "hey u there"}]],
                        my_username="Eva_21")
    msgs = bot.extract_chat_from_page(page)
    check("New reader handles the SMS path",
          len(msgs) == 1 and msgs[0]["message"] == "hey u there")
    check("My username is cached after the first successful read (used on later polls)",
          bot._my_username == "Eva_21")
    check("First poll used the new multi-selector extractor",
          page.expressions and page.expressions[0] == chat_reader.CHAT_EXTRACT_JS)

    # --- the page is dominated by a changed markup: legacy fallback --------
    class LegacyOnlyPage(ScriptedPage):
        def evaluate(self, expr, arg=None):
            self.expressions.append(expr)
            if expr == chat_reader.CHAT_EXTRACT_JS:
                return {"messages": [], "diag": {"container": None, "items": 0}}
            return [{"speaker": "Stranger", "message": "legacy path works"}]

    bot2, logs2 = _make_bot(ba)
    legacy_page = LegacyOnlyPage([[]])
    msgs = bot2.extract_chat_from_page(legacy_page)
    check("Falls back to the legacy extractor when the new reader finds nothing",
          [m["message"] for m in msgs] == ["legacy path works"])
    check("Fallback is logged for the user",
          any("legacy selector set used" in line for line in logs2))
    check("Legacy fallback keeps the original selector set",
          len(legacy_page.expressions) == 2 and "li.select-text" in legacy_page.expressions[1])

    # --- "0 messages parsed" warning --------------------------------------
    bot3, logs3 = _make_bot(ba)
    bot3._last_detect_diag = {"container": None, "itemSelector": None, "items": 0,
                              "myUsername": None, "speakerSources": {}, "rawTextLength": 12}
    empty_page = ScriptedPage([[]])
    for _ in range(6):
        bot3.extract_chat_from_page(empty_page)
    bot3._log_detect_health(empty_page, 3)
    check("A blind page raises a loud warning",
          len([l for l in logs3 if "[Detect] WARNING" in l]) == 1)
    check("The warning explains the selectors tried", any("container=" in l for l in logs3))
    bot3._log_detect_health(empty_page, 3)
    check("The warning is not repeated every poll",
          len([l for l in logs3 if "[Detect] WARNING" in l]) == 1)

    # --- sent-text memory --------------------------------------------------
    bot4, _ = _make_bot(ba)
    for i in range(20):
        bot4._record_sent_text(f"  line   {i}  ")
    check("Sent texts are normalized and capped",
          len(bot4._recent_sent_texts) == 12 and bot4._recent_sent_texts[-1] == "line 19")

    # --- counter-drift regression, end to end ------------------------------
    # The site never renders our own bubbles: the old counter arithmetic ran
    # ahead of the DOM and went blind; the tracker must still see every SMS.
    script = [
        [],                                                    # chat just opened
        [{"speaker": "Stranger", "message": "hi"}],            # SMS #1
        [{"speaker": "Stranger", "message": "hi"},
         {"speaker": "Stranger", "message": "u there?"}],      # SMS #2
        [{"speaker": "Stranger", "message": "hi"},
         {"speaker": "Stranger", "message": "u there?"},
         {"speaker": "Stranger", "message": "im from uk"}],    # SMS #3
    ]
    bot5, _ = _make_bot(ba)
    page5 = ScriptedPage(script)
    tracker = bot5._new_message_tracker()
    page5.set_poll(0)
    tracker.snapshot(bot5.extract_chat_from_page(page5))      # we open the chat

    seen_new: list[str] = []
    seen_old: list[str] = []
    old_counter = 0
    for poll in range(1, len(script)):
        page5.set_poll(poll)
        for m in tracker.sync(bot5.extract_chat_from_page(page5)):
            if m["speaker"] == "Stranger":
                seen_new.append(m["message"])
        bot5._record_sent_text("sup")                         # our reply, invisible in DOM
        old_counter += 1                                      # the old buggy bookkeeping
        dom = script[poll]
        if len(dom) > old_counter:
            seen_old.extend(m["message"] for m in dom[old_counter:]
                            if m["speaker"] == "Stranger")

    check("Loop with the tracker sees every stranger SMS",
          seen_new == ["hi", "u there?", "im from uk"], f"got {seen_new}")
    check("Loop with the old counter logic would have missed SMS",
          seen_old != ["hi", "u there?", "im from uk"], f"got {seen_old}")


def main() -> int:
    print("=" * 72)
    print("EVA chat-SMS detection tests (test_chat_detect.py)")
    print("=" * 72)
    test_tracker()
    test_dom_extraction()
    test_wiring()
    test_integration()
    print("\n" + "=" * 72)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
