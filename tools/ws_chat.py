#!/usr/bin/env python3
"""Chat through the site's own WebSocket — session cookies, no DOM polling.

    python3 tools/ws_chat.py --list                       # which saved sessions exist
    python3 tools/ws_chat.py --account EMAIL --listen      # watch the live socket
    python3 tools/ws_chat.py --account EMAIL --reply       # answer with ChatRuleBot
    python3 tools/ws_chat.py --account EMAIL --send "hi"   # send one message
    python3 tools/ws_chat.py --account EMAIL --reply --probe   # learn the send event

    python3 tools/ws_chat.py --har ws.txt                  # protocol report from a HAR
    python3 tools/ws_chat.py --replay ws.txt               # what the bot sees in it
    #   ^ a DevTools HAR export or a plain text frame dump, both work
    python3 tools/ws_chat.py --identity --account EMAIL    # read the username off the app
    python3 tools/ws_chat.py --bundle js-direct-chat.js    # rank the emit names

The socket talks ``wss://api.chitchat.gg/socket.io/?EIO=4&transport=websocket``
with the cookies of the saved session (see ``docs/WS_CHAT_PROTOCOL.md``).  The
one thing the captured HAR does not contain is the *send* event name, so
``--probe`` fetches the site's chat bundle in the browser, ranks
``socket.emit("…")`` names and remembers the one that the server echoes back
(``data/ws_config.json``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.chat_ws import (ChatWebSocket, DEFAULT_WS_URL, chat_bundle_urls,  # noqa: E402
                          cookies_header, discover_send_event, load_session_cookies)
from core.socketio import SocketIOClient  # noqa: E402

WS_CONFIG = REPO / "data" / "ws_config.json"


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------- #
#  saved sessions
# --------------------------------------------------------------------------- #

def load_accounts(pattern: str = "") -> List[Dict[str, Any]]:
    from test_session_health import _load_automation_module
    _load_automation_module()
    from browser.account_session_store import load_saved_account_sessions
    accounts = load_saved_account_sessions() or []
    if pattern:
        wanted = pattern.lower()
        accounts = [a for a in accounts
                    if wanted in str(a.get("email") or "").lower()
                    or wanted in str(a.get("session_dir") or "").lower()]
    return accounts


def ws_config() -> Dict[str, Any]:
    try:
        return json.loads(WS_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_ws_config(**updates) -> None:
    data = ws_config()
    data.update({k: v for k, v in updates.items() if v})
    try:
        WS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        WS_CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log(f"[WS] remembered the confirmed send event in {WS_CONFIG.relative_to(REPO)}")
    except Exception as error:
        log(f"[WS] could not save {WS_CONFIG.name}: {error}")


# --------------------------------------------------------------------------- #
#  HAR report — what does the capture actually contain?
# --------------------------------------------------------------------------- #

def har_entries(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The entries of a HAR, whether it is the full file or the ``log`` object."""
    if isinstance(data, dict):
        log_part = data.get("log")
        if isinstance(log_part, dict) and isinstance(log_part.get("entries"), list):
            return list(log_part["entries"])
        if isinstance(data.get("entries"), list):
            return list(data["entries"])
    return []



# --- plain text capture support -------------------------------------------
# Events the *client* sends.  Everything else on a ``42`` frame is something
# the server sent us; that is only used when the dump has no direction marker.
# For a bare frame without any direction marker, only the events *only* the
# client emits decide the direction: the server emits `typing`/`chatMessage`
# too, so those stay "received" by default.
CLIENT_EVENTS = {"presenceSync", "sendMessage", "send_message", "syncPresence",
                 "skipMatch", "skip", "leave"}

_SEND_HINTS = ("send", "sent", "out", "up", "client", "c→s", "c->s", "request")
_RECV_HINTS = ("recv", "received", "receive", "in", "down", "server", "s→c",
               "s->c", "response", "message")
_SEND_MARKERS = ("↑", "→", "->", "=>")
_RECV_MARKERS = ("↓", "←", "<-", "<=")
# Longest first, so "received" wins over "recv" and "->" over "-".
_ALL_MARKERS = (_SEND_MARKERS + _RECV_MARKERS
                + tuple(sorted(_SEND_HINTS + _RECV_HINTS, key=len, reverse=True)))

# A frame starts with 1-2 frame digits (0, 2, 3, 40, 41, 42, 45) followed by
# either the JSON payload or another digit: ``42[…]``, ``0{…}``, ``40{…}``.
_FRAME_START_RE = re.compile(r"([0-9]{1,2}(?:\[|\{|[0-9]))")


def _direction_of(line: str) -> str:
    """``"send"``/``"recv"``/``""`` for one dump line (``""`` = unknown)."""
    stripped = line.strip()
    if stripped.startswith("{"):
        # Chrome's WS pane copies frames as {"type": "send", "data": "42[…]"}.
        obj = None
        try:
            obj = json.loads(stripped)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            hint = str(obj.get("type") or obj.get("direction") or obj.get("source")
                       or "").strip().lower()
            if hint in _SEND_HINTS:
                return "send"
            if hint in _RECV_HINTS:
                return "recv"
    low = stripped.lower()
    for marker in _ALL_MARKERS:                 # explicit arrows/words first
        if low.startswith(marker):
            return "send" if marker in _SEND_MARKERS + _SEND_HINTS else "recv"
    return ""


def _frame_payload(line: str) -> str:
    """The frame text inside one dump line (markers and JSON wrappers removed)."""
    stripped = line.strip()
    for _ in range(2):                          # marker → maybe JSON → frame
        if stripped.startswith("{"):
            obj = None
            try:
                obj = json.loads(stripped)
            except Exception:
                obj = None
            if isinstance(obj, dict) and obj.get("data") is not None:
                stripped = str(obj.get("data")).strip()
                continue
        low = stripped.lower()
        for marker in _ALL_MARKERS:
            if low.startswith(marker):
                stripped = stripped[len(marker):].strip()
                break
        else:
            break
    match = _FRAME_START_RE.search(stripped)
    if match:
        # The frame may contain spaces ({"content": "Hey, m"}), so keep the whole
        # rest of the line — only trailing commas/quotes from a wrapper go away.
        return stripped[match.start():].strip().rstrip('",')
    return stripped


def _guess_direction(frame: str) -> str:
    """Fallback when a dump line carries no direction marker."""
    text = frame.strip()
    if text.startswith("0") or text.startswith("40") or text.startswith("41") \
            or text.startswith("45") or text == "2":
        return "recv"                       # open/hello, connect, disconnect, ping
    if text == "3" or text.startswith("1") or text.startswith("43"):
        return "send"                       # pong, close, ack
    if text.startswith("42"):
        try:
            parsed = json.loads(text[2:])
        except Exception:
            return ""
        if isinstance(parsed, list) and parsed and str(parsed[0]) in CLIENT_EVENTS:
            return "send"
        return "recv"
    return ""


def raw_frames(path: str) -> Tuple[List[str], List[str]]:
    """``(receive, send)`` frames out of a plain text dump (``ws.txt``)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    receive: List[str] = []
    send: List[str] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        frame = _frame_payload(line)
        if not frame:
            continue
        direction = _direction_of(line) or _guess_direction(frame)
        if direction == "send":
            send.append(frame)
        elif direction == "recv":
            receive.append(frame)
    return receive, send


def har_frames(path: str) -> Tuple[List[str], List[str]]:
    """``(receive, send)`` frames out of a capture, in capture order.

    Accepts a HAR export *or* a plain text ``ws.txt`` dump — whatever the user
    saved from DevTools.  A dump is detected by the file not being JSON (or by
    the HAR carrying no frames at all).
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    receive: List[str] = []
    send: List[str] = []
    entries: List[Dict[str, Any]] = []
    try:
        entries = har_entries(json.loads(text))
    except Exception:
        entries = []
    for entry in entries:
        for frame in entry.get("_webSocketMessages", []) or []:
            data_text = str(frame.get("data") or "")
            if frame.get("type") == "send":
                send.append(data_text)
            else:
                receive.append(data_text)
    if receive or send:
        return receive, send
    return raw_frames(path)


def replay_report(path: str, username: str = "", log_fn=None) -> int:
    """Replay a capture offline and report what the bot's socket layer sees.

    This is the answer to "does the bot detect the SMS in *this* recording?" —
    no browser, no network, just the captured server frames through the exact
    parsing the live bot uses.
    """
    log_fn = log_fn or log
    from core.chat_ws import ChatWebSocket
    receive, send = har_frames(path)
    incoming: List[str] = []
    matches: List[Dict[str, Any]] = []

    chat = ChatWebSocket(cookie_header="", url="", my_username=username,
                         log=log_fn,
                         on_message=lambda text, _msg: incoming.append(text),
                         on_match=lambda match: matches.append(match))
    chat.deliver_inline = True           # offline: no socket, no worker thread
    for frame in receive:
        chat.feed(frame)
    events = dict(chat.client.event_counts)

    log_fn("=" * 72)
    log_fn(f"replay report — {path}")
    log_fn("=" * 72)
    log_fn(f"frames in the capture: {len(receive)} received · {len(send)} sent")
    log_fn(f"conversation: {chat.conversation_id or '<none>'}")
    log_fn(f"participants: {chat.stats().get('participants')}")
    log_fn(f"identity: username={chat.my_username or '<unknown>'} "
           f"profile_id={chat.my_id or '<unknown>'} "
           f"(socket pid={chat.client.pid or '<none>'} — a session id, not a profile)")
    log_fn(f"events seen: {json.dumps(events, ensure_ascii=False)}")
    log_fn(f"messages: {len(chat.messages)} total · {chat.messages_in} incoming · "
           f"{chat.messages_out} ours")
    log_fn(f"incoming SMS the bot would answer: {json.dumps(incoming, ensure_ascii=False)}")
    if chat.closed_by_peer:
        log_fn("the chat was closed over the socket (closure frame handled)")
    if matches:
        log_fn(f"matchUpdate frames: {len(matches)}")
    ok = bool(incoming)
    if not ok and chat.messages:
        log_fn("✗ every message in this capture looked like our own — pass "
               "--username with the account's name (or run --identity once)")
    elif not incoming:
        log_fn("✗ no chatMessage frame in this capture (nothing to detect)")
    else:
        log_fn(f"✓ {len(incoming)} incoming message(s) detected, "
               f"{chat.messages_out} own message(s) separated — the socket layer "
               "understands this capture")
    return 0 if ok else 1


def har_report(path: str) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    sends: List[str] = []
    recvs: List[str] = []
    urls: List[str] = []
    for entry in har_entries(data):
        request = entry.get("request") or {}
        if str(request.get("url") or "").startswith(("ws://", "wss://")):
            urls.append(request["url"])
        for frame in entry.get("_webSocketMessages", []) or []:
            frame_data = str(frame.get("data") or "")
            if frame.get("type") == "send":
                sends.append(frame_data)
            else:
                recvs.append(frame_data)

    log("=" * 72)
    log(f"HAR report — {path}")
    log("=" * 72)
    for url in dict.fromkeys(urls):
        log(f"socket: {url}")

    def inventory(frames: List[str], label: str) -> None:
        counts: Dict[str, int] = {}
        for frame in frames:
            if frame.startswith("42["):
                try:
                    name = "42 event: " + str(json.loads(frame[2:])[0])
                except Exception:
                    name = "42 (unparsable)"
            elif frame.startswith("40"):
                name = "40 socket.io connect"
            elif frame.startswith("0"):
                name = "0 engine.io handshake"
            elif frame == "2":
                name = "2 ping (server)"
            elif frame == "3":
                name = "3 pong (client)"
            elif frame.startswith("41"):
                name = "41 namespace disconnect"
            else:
                name = f"other: {frame[:24]}"
            counts[name] = counts.get(name, 0) + 1
        log(f"\n{label} ({len(frames)} frames):")
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            log(f"  {count:4d}  {name}")

    inventory(sends, "client → server")
    inventory(recvs, "server → client")

    # The interesting question: is the *send* of a chat message in here?
    chat_sends = [f for f in sends
                  if f.startswith("42[") and "presenceSync" not in f
                  and "typing" not in f]
    log("")
    if chat_sends:
        log("✓ a client-side chat message event IS captured — use this name:")
        for frame in chat_sends[:3]:
            try:
                log(f"    event: {json.loads(frame[2:])[0]}")
            except Exception:
                pass
            log(f"    raw:   {frame[:200]}")
    else:
        log("✗ no client-side chat *send* frame in this capture (only handshake,")
        log("  presenceSync and pongs). To nail the exact send event, in DevTools →")
        log("  Network → WS, click the socket, then type + send one message and")
        log("  export the HAR again; or run --probe on a machine that can reach the")
        log("  site (it reads the event name straight out of the chat bundle).")
    for frame in recvs[:3]:
        if frame.startswith("42["):
            log(f"\nfirst server event frame: {frame[:200]}")
            break
    return 0


def bundle_report(path: str) -> int:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    names = discover_send_event(text, limit=12)
    log(f"candidate send events in {path}:")
    for name in names:
        log(f"  {name}")
    if not names:
        log("  (none found — is this the right bundle?)")
    return 0


# --------------------------------------------------------------------------- #
#  live socket
# --------------------------------------------------------------------------- #

def run_live(args) -> int:
    accounts = load_accounts(args.account or "")
    if not accounts:
        log("no saved session matched — run `python tools/session_chat.py --plan-only` first")
        return 2
    account = accounts[0]
    storage = str(account.get("storage_state_path") or "")
    cookies = load_session_cookies(storage)
    header = cookies_header(cookies)
    log(f"[WS] session: {account.get('email')} · {len(cookies)} cookies "
        f"({len(header.split(';')) if header else 0} for chitchat.gg)")

    config = ws_config()
    send_event = args.send_event or config.get("send_event") or ""
    if send_event:
        log(f"[WS] send event: {send_event} (from {'--send-event' if args.send_event else 'data/ws_config.json'})")

    chat = ChatWebSocket(
        cookie_header=header,
        url=args.url or config.get("url") or DEFAULT_WS_URL,
        my_username=args.username or config.get("username") or "",
        send_event=send_event,
        allow_probe=args.probe,
        log=log,
        on_message=lambda text, _msg: log(f"[SMS]   user: {text}"),
        on_match=lambda match: log("[WS] match is live — you can chat now"),
    )
    chat.start()
    if not chat.wait_connected(args.wait):
        log(f"[WS] ✗ could not connect within {args.wait:.0f}s: {chat.client.last_error}")
        return 1

    if args.identity and not chat.my_username:
        log("[WS] reading the account's own username from the app (one-time login "
            "check, headless) …")
        username = identity_via_browser(account, log)
        if username:
            chat.my_username = username
            save_ws_config(username=username)
            log(f"[WS] identity: username={username!r}")

    if args.probe:
        probed = probe_in_browser(account, log)
        if probed:
            log(f"[WS] probe suggests: {probed}")
            chat.send_event = probed[0]

    stop = threading.Event()
    if args.send:
        ok, how = chat.send_message(args.send)
        log(f"[WS] {'✓ sent' if ok else '✗ not confirmed'} ({how})")
        if not args.keep_open:
            stop.set()

    if args.reply:
        start_reply_bot(chat, stop)

    deadline = time.time() + args.minutes * 60 if args.minutes else 0
    log(f"[WS] listening for {args.minutes:.0f} min — Ctrl+C stops"
        if args.minutes else "[WS] listening — Ctrl+C stops")
    try:
        while not stop.is_set():
            if deadline and time.time() > deadline:
                log("[WS] time limit reached")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("[WS] stopped by the user")
    stats = chat.stats()
    log("[WS] summary: " + json.dumps(
        {k: stats.get(k) for k in ("connected", "events", "messages_in", "messages_out",
                                   "confirmed_event", "conversation", "participants",
                                   "my_username", "my_id")},
        ensure_ascii=False))
    chat.stop()
    return 0


def start_reply_bot(chat: ChatWebSocket, stop: threading.Event) -> None:
    """Answer incoming messages with the same engine the GUI uses."""
    try:
        from chat.rule_bot import ChatRuleBot
    except Exception as error:
        log(f"[ChatRuleBot] unavailable: {error}")
        return
    bot = ChatRuleBot()
    state = bot.new_conversation()
    log(f"[ChatRuleBot] active — state keys={len(state)}")

    def answer(text: str, _raw: Dict[str, Any]) -> None:
        try:
            reply = bot.reply(text, state)
        except Exception as error:
            log(f"[ChatRuleBot] reply failed: {error}")
            return
        ok, how = chat.send_message(reply)
        log(f"[REPLY] bot: {reply}  ({'confirmed' if ok else 'NOT confirmed'} · {how})")
        if ok:
            save_ws_config(send_event=chat.sent_event)

    chat.on_message = answer


def read_username_from_page(page, log_fn=log) -> str:
    """The account's own username, read from the open app page."""
    try:
        from browser import chat_reader
        _messages, diag = chat_reader.extract_with_diag(page)
        return str((diag or {}).get("myUsername") or "").strip()
    except Exception as error:
        log_fn(f"[WS] could not read the username from the page: {error}")
        return ""


def identity_via_browser(account: Dict[str, Any], log_fn) -> str:
    """Open the saved session (headless) and read who this account is."""
    from test_session_health import _load_automation_module
    automation_module, how = _load_automation_module()
    if automation_module is None:
        log_fn(f"[WS] browser lookup unavailable: {how}")
        return ""
    automation = automation_module.ChitchatAutomation(
        account=account, account_mode="restore", headless=True, thread_id=1)
    automation.set_log_callback(lambda m: None)
    try:
        automation.is_running = True
        browser = automation._launch_camoufox()
        context = getattr(browser, "_context", None) or (browser.contexts or [None])[0]
        if context is None:
            return ""
        from browser.browser_engine import apply_chromium_stealth
        _kwargs, fingerprint = automation._identity_context_options()
        apply_chromium_stealth(context, log_fn=None, fingerprint=fingerprint or None)
        page = context.new_page()
        if not automation.restore_saved_account_session(page):
            log_fn("[WS] could not restore the session for the lookup")
            return ""
        return read_username_from_page(page, log_fn)
    except Exception as error:
        log_fn(f"[WS] identity lookup failed: {type(error).__name__}: {error}")
        return ""
    finally:
        try:
            automation.is_running = False
            automation._close_camoufox()
        except Exception:
            pass


def probe_in_browser(account: Dict[str, Any], log_fn) -> List[str]:
    """Open the saved session in a browser and read the chat bundle's emits."""
    from test_session_health import _load_automation_module
    automation_module, how = _load_automation_module()
    if automation_module is None:
        log_fn(f"[WS] browser probe unavailable: {how}")
        return []
    log_fn("[WS] opening the session in a browser to read the chat bundle …")
    automation = automation_module.ChitchatAutomation(
        account=account, account_mode="restore", headless=True, thread_id=1)
    automation.set_log_callback(lambda m: None)
    try:
        automation.is_running = True
        browser = automation._launch_camoufox()
        context = getattr(browser, "_context", None) or (browser.contexts or [None])[0]
        if context is None:
            return []
        from browser.browser_engine import apply_chromium_stealth
        _kwargs, fingerprint = automation._identity_context_options()
        apply_chromium_stealth(context, log_fn=None, fingerprint=fingerprint or None)
        page = context.new_page()
        if not automation.restore_saved_account_session(page):
            log_fn("[WS] could not restore the session for the probe")
            return []
        from core.chat_ws import probe_send_events_from_page
        names = probe_send_events_from_page(page, log=log_fn)
        if names:
            save_ws_config(send_event=names[0])
        return names
    except Exception as error:
        log_fn(f"[WS] probe failed: {type(error).__name__}: {error}")
        return []
    finally:
        try:
            automation.is_running = False
            automation._close_camoufox()
        except Exception:
            pass


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", default="", help="saved session (email substring)")
    parser.add_argument("--list", action="store_true", help="list saved sessions")
    parser.add_argument("--listen", action="store_true", help="just watch the socket")
    parser.add_argument("--send", default="", help="send one message and exit")
    parser.add_argument("--keep-open", action="store_true",
                        help="keep listening after --send")
    parser.add_argument("--reply", action="store_true",
                        help="answer incoming messages with ChatRuleBot")
    parser.add_argument("--probe", action="store_true",
                        help="read the send event name from the chat bundle (browser)")
    parser.add_argument("--send-event", default="", help="override the send event name")
    parser.add_argument("--username", default="", help="this account's own username")
    parser.add_argument("--url", default="", help=f"socket URL (default {DEFAULT_WS_URL})")
    parser.add_argument("--minutes", type=float, default=0.0, help="stop after N minutes")
    parser.add_argument("--wait", type=float, default=25.0, help="connect timeout (s)")
    parser.add_argument("--har", default="", help="print a protocol report for a HAR export")
    parser.add_argument("--replay", default="",
                        help="replay a capture offline (HAR or ws.txt dump): "
                             "what would the bot see in it?")
    parser.add_argument("--identity", action="store_true",
                        help="read the account's own username from the app (browser, "
                             "one-time; remembered in data/ws_config.json)")
    parser.add_argument("--bundle", default="", help="rank emit names in a saved JS bundle")
    args = parser.parse_args()

    if args.list:
        accounts = load_accounts("")
        log(f"{len(accounts)} saved session(s):")
        for account in accounts:
            cookies = load_session_cookies(str(account.get("storage_state_path") or ""))
            log(f"  {account.get('email')} · {len(cookies)} cookies · "
                f"{account.get('session_dir')}")
        return 0
    if args.har:
        return har_report(args.har)
    if args.replay:
        return replay_report(args.replay,
                             username=args.username or str(ws_config().get("username") or ""))
    if args.bundle:
        return bundle_report(args.bundle)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
