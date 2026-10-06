#!/usr/bin/env python3
"""tools/chat_detect_debug.py — "why can't the bot see the stranger's SMS?"

Shows exactly what the SMS detector reads from a chat page, using the same
code the bot runs (``browser/chat_reader.py``), and — when the markup has
changed — *derives* the new selectors from the page itself
(``browser/selector_doctor.py``) so the fix needs no code edit.

Two ways to use it:

1. Saved page (no login needed)
   -------------------------------------
   On the bot PC open the chat page in the browser -> right-click -> "Save as"
   -> "Webpage, HTML only" -> save as ``chat_page.html``, then:

       python tools/chat_detect_debug.py --html C:\\path\\chat_page.html

2. Live URL (opens a fresh headless browser)

       python tools/chat_detect_debug.py --url https://chitchat.gg/

What you get: every SMS the bot can read (speaker + text + how the speaker was
identified), plus the diagnostics of the selectors that matched.

Markup changed?  Ask the doctor
-------------------------------
The bot already heals itself at runtime, but you can drive it by hand:

    python tools/chat_detect_debug.py --html chat_page.html --suggest
    python tools/chat_detect_debug.py --html chat_page.html --save
    python tools/chat_detect_debug.py --html chat_page.html --explain

``--suggest`` prints the selectors the doctor derived from the page (and
verifies them by reading the messages back), ``--explain`` shows the
candidate list with its scoring, and ``--save`` merges the result into
``config/chat_selectors.json`` — ``browser/chat_reader.py`` loads that file on
every poll, so the bot reads the new markup immediately (no code change).

``--offline`` does all of this with the standard library only (no Playwright,
no node/jsdom); the saved HTML is parsed directly.

Exit code: 0 = SMS found (or a usable selector suggestion was produced),
2 = nothing found (markup changed), 1 = usage error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from browser import chat_reader

try:
    from browser import selector_doctor
    _DOCTOR_ERROR = ""
except Exception as exc:  # pragma: no cover - only if the file is missing
    selector_doctor = None
    _DOCTOR_ERROR = f"{type(exc).__name__}: {exc}"


def _print_result(payload, source: str) -> int:
    messages = payload.get("messages") or []
    diag = payload.get("diag") or {}
    print(f"\nsource: {source}")
    print("-" * 70)
    if messages:
        for i, m in enumerate(messages, 1):
            print(f"{i:3d}. {m.get('speaker', '?'):9s} | {m.get('message', '')}"
                  f"   (via {m.get('via') or '?'})")
    else:
        print("(no messages detected)")
    print("-" * 70)
    print("diagnostics:")
    print("  " + chat_reader.describe_diag(diag))
    sample = diag.get("sampleClasses") or []
    if sample:
        print(f"  sample rows: {sample[:5]}")
    if diag.get("rawTextLength"):
        print(f"  chat text visible on the page: {diag['rawTextLength']} characters")
    if diag.get("healed"):
        print("  SELF-HEALED: the reader already found the new markup and learned"
              " the selectors below for the next polls.")
    if diag.get("suggestionFile"):
        print(f"  suggestion saved: {diag['suggestionFile']}")
    print("-" * 70)
    if not messages:
        print("RESULT: 0 SMS detected — the chat markup is different from what the")
        print("        bot expects.")
        return 2
    print(f"RESULT: {len(messages)} message(s) detected — the bot can read this page.")
    return 0


# --------------------------------------------------------------------------
# selector doctor integration
# --------------------------------------------------------------------------

def _run_doctor(report, *, explain: bool = False, save: bool = False) -> int:
    """Print (and optionally save) a discovery report. 0 = usable."""
    if selector_doctor is None:
        print(f"\n[!] selector doctor unavailable ({_DOCTOR_ERROR})")
        return 2
    print("\n" + "=" * 70)
    print(selector_doctor.format_report(report, explain=explain))
    print("=" * 70)
    if save:
        if not report.ok:
            print("[!] nothing to save — no chat list found on this page.")
            return 2
        path = selector_doctor.save_config(
            report, chat_reader.selector_config_path(),
            note="written by tools/chat_detect_debug.py")
        print(f"[OK] selectors saved to {path}")
        print("     browser/chat_reader.py reads this file on every poll —")
        print("     restart the bot (or wait for the next poll) and it will use them.")
        print("     Delete the file to fall back to the built-in selector list.")
    if not report.ok:
        return 2
    return 0 if report.confidence >= 0.4 else 2


def _discover_html(html: str, *, my_username=None):
    if selector_doctor is None:
        return None
    report = selector_doctor.discover_html(html, my_username=my_username)
    return report


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def run_html(path: Path, recent, my_username, *, suggest=False, explain=False,
             save=False, autodiscover=True, offline=False) -> int:
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[!] cannot read {path}: {exc}")
        return 1

    code = 2
    if not offline:
        payload = _evaluate_html_offline(html, recent, my_username,
                                         autodiscover=autodiscover)
        if payload is None:
            print("[!] no browser engine available to evaluate the page.")
            print("    Install one of:")
            print("      pip install playwright && python -m playwright install chromium")
            print("      npm install jsdom   (then: node --version must work)")
            print("    …or rerun with --offline to analyse the saved HTML with the")
            print("    standard library only (selector doctor still works).")
            if not (suggest or save or explain):
                return 1
        else:
            code = _print_result(payload, str(path))
    else:
        print("[info] --offline: extraction engine skipped (standard library only)")

    if code != 0 or suggest or save or explain:
        report = _discover_html(html, my_username=my_username)
        if report is not None:
            doctor_code = _run_doctor(report, explain=explain, save=save)
            if code != 0:
                code = doctor_code
    return code


def _evaluate_html_offline(html: str, recent, my_username, autodiscover: bool = True):
    """Run the extractor against saved HTML without needing a live chat page."""
    # Prefer Playwright (already a project dependency).
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html, wait_until="domcontentloaded")
            messages, diag = chat_reader.extract_with_diag(
                page, recent_sent=recent, my_username=my_username,
                autodiscover=autodiscover)
            browser.close()
            return {"messages": messages, "diag": diag}
    except Exception as exc:
        print(f"[info] Playwright path unavailable ({type(exc).__name__}), trying jsdom…")

    # Fallback: node + jsdom (used by the test-suite too).
    import json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    jsdom_dir = None
    import os
    candidates = []
    if os.environ.get("EVA_JSDOM_DIR"):
        candidates.append(Path(os.environ["EVA_JSDOM_DIR"]))
    candidates += [ROOT, ROOT / "tests", Path.cwd()]
    for base in candidates:
        if (base / "node_modules" / "jsdom").is_dir():
            jsdom_dir = base
            break
    if not node or not jsdom_dir:
        return None

    harness = (
        "const fs=require('fs');const {JSDOM}=require('jsdom');"
        "const [, , htmlPath, jsPath, recentJson, hintJson]=process.argv;"
        "const dom=new JSDOM(fs.readFileSync(htmlPath,'utf8'));"
        "global.document=dom.window.document;global.window=dom.window;"
        "const fn=eval('('+fs.readFileSync(jsPath,'utf8').trim()+')');"
        "console.log(JSON.stringify(fn({recentSent:JSON.parse(recentJson||'[]'),"
        "myUsernameHint:(hintJson&&hintJson!=='null')?JSON.parse(hintJson):null})));"
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "extract.js").write_text(chat_reader.CHAT_EXTRACT_JS, encoding="utf-8")
        (tmp_dir / "harness.js").write_text(harness, encoding="utf-8")
        html_file = tmp_dir / "page.html"
        html_file.write_text(html, encoding="utf-8")
        env = dict(os.environ, NODE_PATH=str(Path(jsdom_dir) / "node_modules"))
        res = subprocess.run(
            [node, str(tmp_dir / "harness.js"), str(html_file), str(tmp_dir / "extract.js"),
             json.dumps(list(recent or [])), json.dumps(my_username)],
            capture_output=True, text=True, env=env, timeout=180,
        )
        if res.returncode != 0:
            print(f"[info] jsdom run failed: {res.stderr.strip()[:200]}")
            return None
        payload = json.loads(res.stdout)
        if isinstance(payload, dict):
            return {"messages": payload.get("messages") or [], "diag": payload.get("diag") or {}}
        return {"messages": payload or [], "diag": {}}


def run_url(url: str, recent, my_username, *, suggest=False, explain=False,
            save=False, autodiscover=True) -> int:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:
        print(f"[!] Playwright is required for --url ({exc}).")
        print("    pip install playwright && python -m playwright install chromium")
        print("    …or save the chat page from your browser and use --html (--offline works too).")
        return 1
    report = None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        messages, diag = chat_reader.extract_with_diag(
            page, recent_sent=recent, my_username=my_username,
            autodiscover=autodiscover)
        if not messages:
            path = chat_reader.dump_dom(page)
            if path:
                print(f"[info] page HTML saved: {path}")
        if (suggest or save or explain or not messages) and selector_doctor is not None:
            try:
                report = selector_doctor.discover_page(page)
            except Exception as exc:
                print(f"[info] discovery failed: {type(exc).__name__}: {exc}")
        browser.close()
    code = _print_result({"messages": messages, "diag": diag}, url)
    if report is not None:
        doctor_code = _run_doctor(report, explain=explain, save=save)
        if suggest or save or explain:
            code = doctor_code if code != 0 else code
    return code


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Show what the bot can read from a chat page, and derive the "
                    "selectors when the site markup changed.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--html", help="saved chat page (.html) — no login needed")
    src.add_argument("--url", help="live URL to open in a headless browser")
    ap.add_argument("--recent", action="append", default=[],
                    help="a message the bot recently sent (repeatable; helps label own bubbles)")
    ap.add_argument("--my-username", default=None, help="the bot's own username on the page")
    ap.add_argument("--suggest", action="store_true",
                    help="derive the selectors from the page and print them (selector doctor)")
    ap.add_argument("--save", action="store_true",
                    help="merge the derived selectors into config/chat_selectors.json")
    ap.add_argument("--explain", action="store_true",
                    help="show the candidate lists and how they were scored")
    ap.add_argument("--offline", action="store_true",
                    help="standard library only (no Playwright/jsdom) — saved pages")
    ap.add_argument("--no-autodiscover", action="store_true",
                    help="do not let the reader self-heal during this run")
    ap.add_argument("--dump-js", help="write the extractor JS to this file and exit")
    args = ap.parse_args()

    if args.dump_js:
        Path(args.dump_js).write_text(chat_reader.CHAT_EXTRACT_JS, encoding="utf-8")
        print(f"extractor JS written to {args.dump_js}")
        return 0

    if args.html:
        return run_html(Path(args.html).expanduser(), args.recent, args.my_username,
                        suggest=args.suggest, explain=args.explain, save=args.save,
                        autodiscover=not args.no_autodiscover, offline=args.offline)
    return run_url(args.url, args.recent, args.my_username,
                   suggest=args.suggest, explain=args.explain, save=args.save,
                   autodiscover=not args.no_autodiscover)


if __name__ == "__main__":
    raise SystemExit(main())
