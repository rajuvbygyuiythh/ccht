#!/usr/bin/env python3
"""test_selector_doctor.py — site-markup-change handling.

The bot must keep reading the stranger's SMS when chitchat.gg is redesigned
(new classes, div rows instead of ol/li, hashed React class names, a
virtualised list, changed data-* attributes...).  Three layers make that work,
and all three are tested here:

A. ``browser/selector_doctor.py`` — derives the selectors from the page
   itself (standard library only, so it runs everywhere).
B. ``browser/chat_reader.py`` — loads ``config/chat_selectors.json`` before
   the built-in list and self-heals at runtime (discovery + retry) when the
   built-in selectors stop matching.
C. The CLI: ``tools/chat_detect_debug.py --suggest/--save/--explain/--offline``.

Plus a jsdom pass (skipped when node/jsdom are missing) that runs the *real*
extractor JS against the new-markup fixtures with the doctor's selectors, so
the emitted CSS is proven to work in a real query engine.

Safety rule tested here as well: on a page that is NOT a chat (landing page,
sidebar nav, article) neither the doctor nor the self-healing may produce
phantom SMS for the bot to answer.

Run:  python test_selector_doctor.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from browser import chat_reader
from browser import selector_doctor as doctor

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "chat_dom"
CHAT_FIXTURES = [
    "chat_v1_original.html",
    "chat_v2_own_no_username.html",
    "chat_v3_plain_li.html",
    "chat_v4_div_messages.html",
    "chat_v5_nomatch.html",
    "chat_v6_hashed_react.html",
    "chat_v7_virtual_list.html",
    "chat_v8_nested_wrapper.html",
]
NON_CHAT_FIXTURE = "not_a_chat.html"

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


def fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def base_selectors() -> dict:
    """The built-in selector sets the JS extractor ships with."""
    return {
        "containers": list(chat_reader._CONTAINER_SELECTORS),
        "items": list(chat_reader._ITEM_SELECTORS),
        "usernames": list(chat_reader._USERNAME_SELECTORS),
        "texts": list(chat_reader._TEXT_SELECTORS),
        "my_usernames": list(chat_reader._MY_USERNAME_SELECTORS),
        "own_hints": list(chat_reader._OWN_CLASS_HINTS),
        "other_hints": list(chat_reader._OTHER_CLASS_HINTS),
    }


def messages_with(text: str, sel: dict) -> list[dict]:
    tree = doctor.parse_html(text)
    return doctor.extract_messages(tree, sel)


# ---------------------------------------------------------------------------
# A. the doctor (offline, stdlib only)
# ---------------------------------------------------------------------------

def test_doctor_offline() -> None:
    print("\n— A. selector doctor derives the selectors from the markup —")

    reports = {}
    for name in CHAT_FIXTURES:
        report = doctor.discover_html(fixture(name))
        reports[name] = report
        detail = f"{name}: ok={report.ok} conf={report.confidence} {report.summary()}"
        check(f"{name}: chat list found", report.ok, detail)
        if not report.ok:
            continue
        check(f"{name}: enough rows detected", (report.get("rowCount") or 0) >= 2, detail)
        check(f"{name}: suggestion verified by reading it back",
              (report.get("verifiedMessages") or 0) >= 2, detail)
        check(f"{name}: confidence is usable", report.confidence >= 0.4, detail)

    # unknown markup must really be unknown for the built-in list, otherwise
    # the fixture proves nothing
    for name in ("chat_v6_hashed_react.html", "chat_v7_virtual_list.html",
                 "chat_v8_nested_wrapper.html"):
        got = messages_with(fixture(name), base_selectors())
        check(f"{name}: built-in selectors cannot read it (changed markup)",
              len(got) == 0, f"got {got}")

    # the doctor's selectors must read it
    for name, report in reports.items():
        if not report.ok:
            continue
        got = messages_with(fixture(name), report.selectors)
        check(f"{name}: doctor's selectors read the SMS", len(got) >= 2,
              f"got {[m['message'] for m in got]}")

    # own/other speaker hints where there is no username (alignment markup)
    for name in ("chat_v2_own_no_username.html", "chat_v7_virtual_list.html"):
        sel = reports[name].selectors
        check(f"{name}: alignment speaker hints derived",
              bool(sel.get("own_hints")) or bool(sel.get("other_hints")), f"{sel}")
    got = messages_with(fixture("chat_v2_own_no_username.html"),
                        reports["chat_v2_own_no_username.html"].selectors)
    speakers = [m["speaker"] for m in got]
    check("alignment hints label own bubbles as You", "You" in speakers, f"{speakers}")

    # hashed build classes must never end up in the selectors
    sel6 = reports["chat_v6_hashed_react.html"].selectors
    flat = [s for values in sel6.values() for s in values]
    check("hashed React/Tailwind classes are filtered out",
          not any(("css-" in s) or ("sc-" in s) for s in flat), f"{flat}")

    # data-* attributes are preferred over generic tags
    sel7 = reports["chat_v7_virtual_list.html"].selectors
    check("stable data-* attribute used for the rows",
          any("data-index" in s for s in sel7.get("items", [])), f"{sel7.get('items')}")

    # a 2-row "Name: message" page is still recognised
    rep5 = reports["chat_v5_nomatch.html"]
    check("small 2-row chat page recognised", rep5.ok and rep5.get("rowCount") == 2,
          rep5.summary())

    # and a marketing page must be rejected (no phantom SMS)
    rep_non = doctor.discover_html(fixture(NON_CHAT_FIXTURE))
    check("non-chat landing page rejected by the doctor", not rep_non.ok,
          rep_non.summary())
    if rep_non.ok:
        got = messages_with(fixture(NON_CHAT_FIXTURE), rep_non.selectors)
        check("non-chat page produced no messages", len(got) == 0, f"got {got}")

    print("\n— A2. selector subset matcher —")
    tree = doctor.parse_html(
        '<div class="a"><p class="x y" data-k="m1">one</p>'
        '<p class="x" data-k="m2">two</p></div>')
    root = tree
    check("tag.class matches", len(doctor.query_all(root, "p.x")) == 2)
    check("attribute equality", len(doctor.query_all(root, "[data-k='m1']")) == 1)
    check("attribute prefix", len(doctor.query_all(root, "[data-k^='m']")) == 2)
    check("attribute presence + class", len(doctor.query_all(root, "p.y[data-k]")) == 1)
    check("comma groups", len(doctor.query_all(root, "p.y, p.y")) == 1)
    container = doctor.query_all(root, "div.a")[0]
    check(":scope > child", len(doctor.query_all(container, ":scope > p", scope=container)) == 2)
    check("descendant combinator", len(doctor.query_all(root, "div p")) == 2)
    check("unsupported pseudo ignored", len(doctor.query_all(root, "p:x-nope")) == 2)
    check("no match stays empty", doctor.query_all(root, "table tr") == [])

    print("\n— A3. config round-trip —")
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "chat_selectors.json"
        rep = reports["chat_v6_hashed_react.html"]
        doctor.save_config(rep, cfg_path)
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        check("config file written", "items" in saved and saved["items"], f"{saved}")
        check("config file carries metadata",
              bool(saved.get("source")) and bool(saved.get("learned_at")),
              f"{ {k: saved[k] for k in ('source', 'learned_at') if k in saved} }")
        check("config file is what the reader expects",
              chat_reader.coerce_selectors(saved).get("items") == saved["items"])
        # merge keeps old entries after the new ones
        old = {"items": ["li.old"], "containers": ["div.old"], "_note": "keep me"}
        merged = doctor.merge_config(old, rep.to_config())
        check("merge puts new selectors first and keeps the old ones",
              merged["items"][0] == saved["items"][0] and "li.old" in merged["items"],
              f"{merged['items']}")
        check("merge keeps the old note", merged.get("_note") == "keep me")
        # a second save creates a backup
        doctor.save_config(rep, cfg_path)
        check("second save keeps a .bak backup", cfg_path.with_suffix(".json.bak").exists())


# ---------------------------------------------------------------------------
# B. reader wiring: config + self-healing (Python-backed page stub)
# ---------------------------------------------------------------------------

class TreePage:
    """Playwright-page stub backed by the standard-library HTML parser.

    ``evaluate`` mirrors the real extractor and the real DOM serializer, so
    the *Python* wiring (config loading, self-healing, diagnostics) can be
    tested everywhere.  The real JS is exercised in section C.
    """

    def __init__(self, html: str):
        self.html = html
        self.tree = doctor.parse_html(html)
        self.calls: list[str] = []
        self.serialize_calls = 0
        self.extract_calls = 0

    JS_KEYS = {"containers": "containers", "items": "items",
               "usernames": "usernames", "texts": "texts",
               "my_usernames": "myUsernames", "own_hints": "ownHints",
               "other_hints": "otherHints"}

    @classmethod
    def _merge(cls, extra: dict) -> dict:
        merged = {key: list(values) for key, values in base_selectors().items()}
        for key, js_key in cls.JS_KEYS.items():
            extra_values = (extra or {}).get(js_key)
            if extra_values:
                merged[key] = list(extra_values) + list(merged[key])
        return merged

    def _my_username(self, merged: dict, hint) -> str | None:
        if hint:
            return hint
        for sel in merged.get("my_usernames") or []:
            found = doctor.query_all(self.tree, sel)
            if found:
                text = doctor.node_text(found[0])
                if text and len(text) <= 40:
                    return text
        return None

    def evaluate(self, expr, arg=None):
        self.calls.append(expr)
        if expr == doctor.SERIALIZE_JS:
            self.serialize_calls += 1
            return self.tree
        if expr != chat_reader.CHAT_EXTRACT_JS:
            raise AssertionError("unexpected JS evaluated")
        self.extract_calls += 1
        arg = arg or {}
        extra = arg.get("sel") or {}
        merged = self._merge(extra)
        recent = list(arg.get("recentSent") or [])
        my_username = self._my_username(merged, arg.get("myUsernameHint"))
        messages = doctor.extract_messages(self.tree, merged, recent_sent=recent,
                                          my_username=my_username)
        container = None
        for sel in merged["containers"]:
            if doctor.query_all(self.tree, sel):
                container = sel
                break
        custom = {s for key in ("containers", "items", "usernames", "texts")
                  for s in (extra.get(key) or [])}
        used = bool(container and container in custom)
        item_used = None
        if messages and container:
            found = doctor.query_all(self.tree, container)
            if found:
                for sel in merged["items"]:
                    if doctor.outermost(doctor.query_all(found[0], sel, scope=found[0])):
                        item_used = sel
                        break
        diag = {
            "container": container, "itemSelector": item_used,
            "items": len(messages), "myUsername": my_username,
            "speakerSources": {}, "rawTextLength": len(doctor.node_text(self.tree)),
            "customSelectors": len(custom), "usedCustom": used,
        }
        return {"messages": messages, "diag": diag}


class BlindPage(TreePage):
    """A page whose *built-in* selectors stopped matching (site redesign).

    Used to prove the self-healing path: the first extraction must return
    nothing, so the doctor is consulted — and a non-chat page must still end
    up with no messages.
    """

    def evaluate(self, expr, arg=None):
        if expr == chat_reader.CHAT_EXTRACT_JS and not ((arg or {}).get("sel") or {}):
            self.calls.append(expr)
            self.extract_calls += 1
            return {"messages": [], "diag": {"container": None, "itemSelector": None,
                                             "items": 0, "usedCustom": False,
                                             "customSelectors": 0,
                                             "rawTextLength": len(doctor.node_text(self.tree))}}
        return super().evaluate(expr, arg)


def _set_config(path: Path | None) -> None:
    if path is None:
        os.environ.pop(chat_reader.SELECTOR_CONFIG_ENV, None)
    else:
        os.environ[chat_reader.SELECTOR_CONFIG_ENV] = str(path)
    chat_reader.load_selector_config(force=True)


def _reset_healing() -> None:
    chat_reader.reset_learned()
    chat_reader._LAST_DISCOVERY_TS = 0.0  # noqa: SLF001 - test hook
    chat_reader._CONFIG_CACHE.update(path=None, mtime=None, data={})  # noqa: SLF001


def test_reader_self_healing() -> None:
    print("\n— B. reader: config overrides + runtime self-healing —")
    old_cooldown = chat_reader.DISCOVERY_COOLDOWN
    chat_reader.DISCOVERY_COOLDOWN = 0
    html = fixture("chat_v6_hashed_react.html")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            cfg = tmp_dir / "chat_selectors.json"
            absent = tmp_dir / "no_config_here.json"
            _set_config(absent)
            chat_reader.suggestion_dir = lambda: tmp_dir  # type: ignore[assignment]

            # --- no config, changed markup -> self-healing -----------------
            _reset_healing()
            page = TreePage(html)
            messages, diag = chat_reader.extract_with_diag(page)
            check("healing: changed markup detected and messages recovered",
                  len(messages) == 4, f"{[m['message'] for m in messages]}")
            check("healing: diag says healed + which selectors",
                  diag.get("healed") is True and bool(diag.get("healedSelectors")),
                  chat_reader.describe_diag(diag))
            check("healing: discovery ran exactly once", diag.get("discoveryRuns") == 1,
                  str(diag.get("discoveryRuns")))
            check("healing: suggestion file written for the user",
                  bool(diag.get("suggestionFile")) and Path(diag["suggestionFile"]).exists(),
                  str(diag.get("suggestionFile")))
            check("healing: our own bubble is not answered",
                  [m["speaker"] for m in messages][1] == "You",
                  f"{[(m['speaker'], m['message']) for m in messages]}")

            # --- the learned selectors are reused (no repeated discovery) --
            page2 = TreePage(html)
            messages2, diag2 = chat_reader.extract_with_diag(page2)
            check("healing: learned selectors reused on the next poll",
                  len(messages2) == 4 and diag2.get("usedCustom") is True,
                  chat_reader.describe_diag(diag2))
            check("healing: no second discovery while the markup is unchanged",
                  page2.serialize_calls == 0, f"serialize calls {page2.serialize_calls}")

            # --- forget + cooldown behaviour -------------------------------
            _reset_healing()
            chat_reader.DISCOVERY_COOLDOWN = 999
            chat_reader.reset_learned()
            chat_reader.extract_with_diag(TreePage(html))   # discovers once...
            chat_reader.reset_learned()                    # ...but we forget again
            page3 = TreePage(html)
            messages3, diag3 = chat_reader.extract_with_diag(page3)
            check("healing: cooldown prevents discovery spam",
                  len(messages3) == 0 and diag3.get("discoverySkipped") == "cooldown"
                  and page3.serialize_calls == 0,
                  chat_reader.describe_diag(diag3))
            chat_reader.DISCOVERY_COOLDOWN = 0

            # --- autodiscover off ------------------------------------------
            _reset_healing()
            page4 = TreePage(html)
            messages4, diag4 = chat_reader.extract_with_diag(page4, autodiscover=False)
            check("autodiscover=False stays manual",
                  len(messages4) == 0 and page4.serialize_calls == 0,
                  chat_reader.describe_diag(diag4))

            # --- config file is used BEFORE the built-in selectors ---------
            _reset_healing()
            report = doctor.discover_html(html)
            doctor.save_config(report, cfg)
            _set_config(cfg)                    # the reader now reads this file
            chat_reader.load_selector_config(force=True)
            page5 = TreePage(html)
            messages5, diag5 = chat_reader.extract_with_diag(page5)
            check("config file: applied without any discovery",
                  len(messages5) == 4 and page5.serialize_calls == 0,
                  chat_reader.describe_diag(diag5))
            check("config file: marked as custom selectors in diag",
                  diag5.get("usedCustom") is True and (diag5.get("customSelectors") or 0) > 0,
                  chat_reader.describe_diag(diag5))

            # --- a broken config never breaks the bot ----------------------
            cfg.write_text("{ this is not json", encoding="utf-8")
            chat_reader.load_selector_config(force=True)
            _reset_healing()
            page6 = TreePage(fixture("chat_v1_original.html"))
            messages6, _ = chat_reader.extract_with_diag(page6)
            check("broken config file is ignored (built-in still works)",
                  len(messages6) == 3, f"{[m['message'] for m in messages6]}")

            # --- safety: a non-chat page produces no phantom SMS -----------
            cfg.unlink(missing_ok=True)
            _set_config(absent)
            _reset_healing()
            page7 = BlindPage(fixture(NON_CHAT_FIXTURE))
            messages7, diag7 = chat_reader.extract_with_diag(page7)
            check("non-chat page: healing invents no phantom messages",
                  messages7 == [], f"got {[m['message'] for m in messages7][:4]}")
            check("non-chat page: healing reported as failed, not silent success",
                  not diag7.get("healed") and bool(diag7.get("discovery")),
                  chat_reader.describe_diag(diag7))
            check("non-chat page: discovery really ran before giving up",
                  page7.serialize_calls >= 1, f"serialize={page7.serialize_calls}")

            # --- safety: a chat page with *changed* markup still heals -----
            page8 = BlindPage(html)
            messages8, diag8 = chat_reader.extract_with_diag(page8)
            check("blind chat page: healing still recovers the SMS",
                  len(messages8) == 4 and diag8.get("healed") is True,
                  chat_reader.describe_diag(diag8))

            # --- learn + persist on demand --------------------------------
            _reset_healing()
            _set_config(cfg)
            rep = doctor.discover_html(html)
            chat_reader.apply_selectors(rep.selectors, persist=True,
                                        note="unit test")
            check("apply_selectors(persist=True) writes the config file",
                  cfg.exists() and chat_reader.coerce_selectors(
                      json.loads(cfg.read_text(encoding="utf-8"))).get("items"),
                  str(cfg))
            check("learned selectors are exposed",
                  bool(chat_reader.learned_selectors().get("items")))
            chat_reader.reset_learned()
            check("reset_learned clears them", chat_reader.learned_selectors() == {})
            chat_reader.load_selector_config(force=True)
    finally:
        chat_reader.DISCOVERY_COOLDOWN = old_cooldown
        _set_config(None)
        chat_reader.load_selector_config(force=True)
        import browser.chat_reader as cr
        cr.suggestion_dir = lambda: cr.ROOT_DIR / "logs"  # restore original


# ---------------------------------------------------------------------------
# C. the real extractor JS + the doctor's CSS, in a real engine (jsdom)
# ---------------------------------------------------------------------------

HARNESS_JS = r"""
const fs = require('fs');
const { JSDOM } = require('jsdom');
const [, , htmlPath, extractPath, serializePath, selJson] = process.argv;
const dom = new JSDOM(fs.readFileSync(htmlPath, 'utf8'));
global.document = dom.window.document;
global.window = dom.window;
const extract = eval('(' + fs.readFileSync(extractPath, 'utf8').trim() + ')');
const serialize = eval('(' + fs.readFileSync(serializePath, 'utf8').trim() + ')');
const out = extract({recentSent: [], myUsernameHint: null, sel: JSON.parse(selJson || '{}')});
console.log(JSON.stringify({extract: out, tree: serialize()}));
"""


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


def test_real_js() -> None:
    print("\n— C. real extractor JS with the doctor's selectors (jsdom) —")
    node = shutil.which("node")
    jsdom_dir = _find_jsdom_dir()
    if not node or not jsdom_dir:
        reason = "node not installed" if not node else "jsdom not installed"
        print(f"  [SKIP] {reason} — install with: npm install jsdom "
              f"(or point EVA_JSDOM_DIR at a folder that has node_modules/jsdom)")
        return

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        (workdir / "extract.js").write_text(chat_reader.CHAT_EXTRACT_JS, encoding="utf-8")
        (workdir / "serialize.js").write_text(doctor.SERIALIZE_JS, encoding="utf-8")
        (workdir / "run.js").write_text(HARNESS_JS, encoding="utf-8")
        env = dict(os.environ, NODE_PATH=str(Path(jsdom_dir) / "node_modules"))

        for name in ("chat_v6_hashed_react.html", "chat_v7_virtual_list.html",
                     "chat_v8_nested_wrapper.html", "chat_v1_original.html"):
            html_path = FIXTURE_DIR / name
            report = doctor.discover_html(html_path.read_text(encoding="utf-8"))
            sel_arg = chat_reader.coerce_selectors(report.selectors)
            res = subprocess.run(
                [node, str(workdir / "run.js"), str(html_path),
                 str(workdir / "extract.js"), str(workdir / "serialize.js"),
                 json.dumps(sel_arg)],
                capture_output=True, text=True, env=env, timeout=120)
            if res.returncode != 0:
                check(f"{name}: jsdom run works", False, res.stderr.strip()[:300])
                continue
            out = json.loads(res.stdout)
            got = [(m["speaker"], m["message"]) for m in (out.get("extract") or {}).get("messages", [])]
            check(f"{name}: doctor's selectors work in the real extractor",
                  len(got) >= 2, f"got {got}")
            check(f"{name}: diagnostics show the custom selectors were used",
                  (out["extract"]["diag"].get("usedCustom") is True
                   or out["extract"]["diag"].get("customSelectors", 0) == 0),
                  chat_reader.describe_diag(out["extract"]["diag"]))

        # the serializer output must be usable by the Python doctor
        res = subprocess.run(
            [node, str(workdir / "run.js"),
             str(FIXTURE_DIR / "chat_v6_hashed_react.html"),
             str(workdir / "extract.js"), str(workdir / "serialize.js"), "{}"],
            capture_output=True, text=True, env=env, timeout=120)
        tree = json.loads(res.stdout)["tree"]
        rep = doctor.discover_tree(tree, source="jsdom-test")
        check("doctor works on a browser-serialized DOM too",
              rep.ok and (rep.get("verifiedMessages") or 0) >= 2, rep.summary())


# ---------------------------------------------------------------------------
# C2. browser layer: the bot logs the heal (and points at the tool)
# ---------------------------------------------------------------------------

def _load_automation_module():
    """Import browser.browser_automation, stubbing Windows/Qt deps if needed."""
    import types
    sys.path.insert(0, str(ROOT))

    def _stub_platform():
        if "winsound" not in sys.modules:
            sys.modules["winsound"] = types.ModuleType("winsound")
        if "PyQt6" not in sys.modules:
            class _Sig:
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


def test_browser_layer() -> None:
    print("\n— C2. browser layer: healing is logged for the user —")
    import collections
    ba, note = _load_automation_module()
    if ba is None:
        print(f"  [SKIP] browser_automation not importable — {note}")
        return
    print(f"  [info] {note}")

    html = fixture("chat_v6_hashed_react.html")

    class HealPage(BlindPage):
        pass

    old_cooldown = chat_reader.DISCOVERY_COOLDOWN
    chat_reader.DISCOVERY_COOLDOWN = 0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _set_config(Path(tmp) / "no_config.json")
            _reset_healing()
            chat_reader.suggestion_dir = lambda: Path(tmp)  # type: ignore[assignment]
            bot = object.__new__(ba.ChitchatAutomation)
            logs: list[str] = []
            bot.log = lambda message: logs.append(str(message))
            bot._recent_sent_texts = collections.deque(maxlen=12)
            bot._my_username = None
            bot._last_detect_diag = {}
            bot._empty_extract_polls = 0
            bot._detect_warned = False
            bot._heal_logged = False

            messages = bot.extract_chat_from_page(HealPage(html))
            check("browser layer: healed messages reach the chat loop",
                  len(messages) == 4, f"{[m['message'] for m in messages]}")
            check("browser layer: the heal is logged for the user",
                  any("markup CHANGED" in line for line in logs), f"{logs}")
            check("browser layer: the log points at the debug tool",
                  any("chat_detect_debug.py" in line for line in logs), f"{logs}")
            check("browser layer: heal logged only once",
                  sum("markup CHANGED" in line for line in logs) == 1)

            # a page that stays blind: the warning tells the user what to do
            bot2 = object.__new__(ba.ChitchatAutomation)
            logs2: list[str] = []
            bot2.log = lambda message: logs2.append(str(message))
            bot2._recent_sent_texts = collections.deque(maxlen=12)
            bot2._my_username = None
            bot2._last_detect_diag = {}
            bot2._empty_extract_polls = 0
            bot2._detect_warned = False
            bot2._heal_logged = False

            class DeadPage(HealPage):
                def evaluate(self, expr, arg=None):
                    if expr == chat_reader.CHAT_EXTRACT_JS:
                        self.calls.append(expr)
                        self.extract_calls += 1
                        return {"messages": [], "diag": {
                            "container": None, "itemSelector": None, "items": 0,
                            "rawTextLength": 120, "sampleClasses": ["div.unknown"],
                            "usedCustom": False, "customSelectors": 0}}
                    return super().evaluate(expr, arg)

                def evaluate_legacy(self):
                    return []

            dead = DeadPage(html)
            dead.evaluate = lambda expr, arg=None: (
                {"messages": [], "diag": {"container": None, "itemSelector": None,
                                          "items": 0, "rawTextLength": 120,
                                          "sampleClasses": ["div.unknown"],
                                          "usedCustom": False, "customSelectors": 0}}
                if expr == chat_reader.CHAT_EXTRACT_JS
                else [] if "li.select-text" in str(expr) else super().evaluate(expr, arg))
            messages2 = bot2.extract_chat_from_page(dead)
            check("browser layer: still blind page returns nothing", messages2 == [])
    finally:
        chat_reader.DISCOVERY_COOLDOWN = old_cooldown
        _set_config(None)
        import browser.chat_reader as cr
        cr.suggestion_dir = lambda: cr.ROOT_DIR / "logs"


# ---------------------------------------------------------------------------
# D. the CLI tool
# ---------------------------------------------------------------------------

def test_tool_cli() -> None:
    print("\n— D. tools/chat_detect_debug.py —")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "chat_selectors.json"
        env = dict(os.environ, EVA_CHAT_SELECTORS=str(cfg),
                   PYTHONIOENCODING="utf-8")
        tool = str(ROOT / "tools" / "chat_detect_debug.py")

        res = subprocess.run([sys.executable, tool, "--html",
                              str(FIXTURE_DIR / "chat_v6_hashed_react.html"),
                              "--offline", "--suggest", "--explain"],
                             capture_output=True, text=True, env=env, timeout=180)
        check("--offline --suggest finds the new markup",
              res.returncode == 0 and "selector doctor" in res.stdout,
              res.stdout[-400:] + res.stderr[-200:])
        check("--explain prints the candidate scoring",
              "confidence" in res.stdout and "candidates" in res.stdout)

        res = subprocess.run([sys.executable, tool, "--html",
                              str(FIXTURE_DIR / "chat_v6_hashed_react.html"),
                              "--offline", "--save"],
                             capture_output=True, text=True, env=env, timeout=180)
        check("--save writes config/chat_selectors.json (env override)",
              res.returncode == 0 and cfg.exists(), res.stdout[-400:])
        saved = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
        check("saved config is readable by the reader",
              bool(chat_reader.coerce_selectors(saved).get("items")), f"{saved}")

        res = subprocess.run([sys.executable, tool, "--html",
                              str(FIXTURE_DIR / NON_CHAT_FIXTURE),
                              "--offline", "--suggest"],
                             capture_output=True, text=True, env=env, timeout=180)
        check("non-chat page exits 2 (nothing to suggest)",
              res.returncode == 2, f"rc={res.returncode} {res.stdout[-300:]}")

        res = subprocess.run([sys.executable, tool, "--html",
                              str(FIXTURE_DIR / "chat_v1_original.html"),
                              "--offline", "--suggest"],
                             capture_output=True, text=True, env=env, timeout=180)
        check("known markup also reports cleanly", res.returncode == 0,
              f"rc={res.returncode} {res.stdout[-300:]}")


ORIGINAL_CONFIG: Path | None = None


def test_no_repo_pollution() -> None:
    print("\n— E. no stray files in the project —")
    stray = ROOT / "config" / "chat_selectors.json"
    check("tests did not leave a config/chat_selectors.json behind", not stray.exists(),
          str(stray))


def main() -> int:
    global ORIGINAL_CONFIG
    ORIGINAL_CONFIG = ROOT / "config" / "chat_selectors.json" \
        if (ROOT / "config" / "chat_selectors.json").exists() else None
    print("=" * 72)
    print("EVA chat-selector doctor tests (test_selector_doctor.py)")
    print("=" * 72)
    test_doctor_offline()
    test_reader_self_healing()
    test_real_js()
    test_browser_layer()
    test_tool_cli()
    test_no_repo_pollution()
    print("\n" + "=" * 72)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
