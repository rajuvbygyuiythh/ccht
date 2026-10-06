#!/usr/bin/env python3
"""tools/chat_detect_debug.py — "why can't the bot see the stranger's SMS?"

Shows exactly what the SMS detector reads from a chat page, using the same
code the bot runs (``browser/chat_reader.py``).

Two ways to use it:

1. Saved page (no login needed)
   -------------------------------------
   On the bot PC open the chat page in the browser -> right-click -> "Save as"
   -> "Webpage, HTML only" -> save as ``chat_page.html``, then:

       python tools/chat_detect_debug.py --html C:\\path\\chat_page.html

2. Live URL (opens a fresh headless browser)

       python tools/chat_detect_debug.py --url https://chitchat.gg/

What you get: every SMS the bot can read (speaker + text + how the speaker was
identified), plus the diagnostics of the selectors that matched.  If the list
is empty, the page markup changed — share the printed diagnostic line (or the
``logs/chat_dom_dump_*.html`` written with ``EVA_DUMP_CHAT_DOM=1``) and the
selectors can be pointed at the new markup.

Exit code: 0 = SMS found, 2 = nothing found (markup changed), 1 = usage error.
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
    print("-" * 70)
    if not messages:
        print("RESULT: 0 SMS detected — the chat markup is different from what the")
        print("        bot expects. Send this output (and logs/chat_dom_dump_*.html")
        print("        if EVA_DUMP_CHAT_DOM=1 is set) so the selectors can be fixed.")
        return 2
    print(f"RESULT: {len(messages)} message(s) detected — the bot can read this page.")
    return 0


def run_html(path: Path, recent, my_username) -> int:
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[!] cannot read {path}: {exc}")
        return 1
    payload = _evaluate_html_offline(html, recent, my_username)
    if payload is None:
        print("[!] no browser engine available to evaluate the page.")
        print("    Install one of:")
        print("      pip install playwright && python -m playwright install chromium")
        print("      npm install jsdom   (then: node --version must work)")
        return 1
    return _print_result(payload, str(path))


def _evaluate_html_offline(html: str, recent, my_username):
    """Run the extractor against saved HTML without needing a live chat page."""
    # Prefer Playwright (already a project dependency).
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html, wait_until="domcontentloaded")
            messages, diag = chat_reader.extract_with_diag(
                page, recent_sent=recent, my_username=my_username)
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
            capture_output=True, text=True, env=env, timeout=120,
        )
        if res.returncode != 0:
            print(f"[info] jsdom run failed: {res.stderr.strip()[:200]}")
            return None
        return json.loads(res.stdout)


def run_url(url: str, recent, my_username) -> int:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:
        print(f"[!] Playwright is required for --url ({exc}).")
        print("    pip install playwright && python -m playwright install chromium")
        return 1
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        messages, diag = chat_reader.extract_with_diag(
            page, recent_sent=recent, my_username=my_username)
        if not messages:
            path = chat_reader.dump_dom(page)
            if path:
                print(f"[info] page HTML saved: {path}")
        browser.close()
    return _print_result({"messages": messages, "diag": diag}, url)


def main() -> int:
    ap = argparse.ArgumentParser(description="Show what the bot can read from a chat page.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--html", help="saved chat page (.html) — no login needed")
    src.add_argument("--url", help="live URL to open in a headless browser")
    ap.add_argument("--recent", action="append", default=[],
                    help="a message the bot recently sent (repeatable; helps label own bubbles)")
    ap.add_argument("--my-username", default=None, help="the bot's own username on the page")
    ap.add_argument("--dump-js", help="write the extractor JS to this file and exit")
    args = ap.parse_args()

    if args.dump_js:
        Path(args.dump_js).write_text(chat_reader.CHAT_EXTRACT_JS, encoding="utf-8")
        print(f"extractor JS written to {args.dump_js}")
        return 0

    if args.html:
        return run_html(Path(args.html).expanduser(), args.recent, args.my_username)
    return run_url(args.url, args.recent, args.my_username)


if __name__ == "__main__":
    raise SystemExit(main())
