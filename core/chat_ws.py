"""Live Chat over the site's own WebSocket (session cookies, no DOM polling).

Built on :mod:`core.socketio` and the protocol captured from a real
chitchat.gg session (``docs/WS_CHAT_PROTOCOL.md``):

    server → 42["matchUpdate",{"match":{"conversation":{…}},"inQueue":false}]
    server → 42["chatMessage",{"message":{"author":{"username":…},"content":…}}]
    server → 42["typing",{"userId":…,"conversationId":…}]
    client → 42["presenceSync"]      (joined the presence channel)
    client → 42["<send event>",{…}]  (our reply)

The *send* event name is the one piece the captured HAR does not contain (it
only shows received ``chatMessage`` frames).  This module therefore:

1. uses the event name discovered from the site's own JS bundle
   (:func:`discover_send_event`, used by ``tools/ws_chat.py --probe``),
2. falls back to a configured name / ``data/ws_config.json``,
3. otherwise starts from ``chatMessage`` and only tries the alternatives when
   ``allow_probe`` is on — a live account is never spammed with guesses,
4. confirms delivery by waiting for the server's echo of our own message
   (the real server broadcasts to the sender too, as the capture shows),
5. lets the caller fall back to DOM typing when nothing is confirmed.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from core.socketio import SocketIOClient

DEFAULT_WS_URL = "wss://api.chitchat.gg/socket.io/?EIO=4&transport=websocket"

#: Event names a Socket.IO chat backend may use for "send this message".
SEND_EVENT_CANDIDATES: Tuple[str, ...] = (
    "chatMessage", "sendMessage", "message", "sendChatMessage",
    "message:send", "chat:message", "send",
)

#: Payload shapes, most likely first (the capture shows messages as
#: ``{"message": {...}}`` on the way in, so the flat form is the guess to beat).
PAYLOAD_SHAPES: Tuple[str, ...] = ("flat", "nested", "minimal")

_SEND_HINT = re.compile(r"(send|message|chat|msg)", re.I)


# --------------------------------------------------------------------------- #
#  session cookies
# --------------------------------------------------------------------------- #

def cookies_header(cookies: Iterable[Dict[str, Any]],
                   domains: Tuple[str, ...] = ("chitchat.gg",)) -> str:
    """Build a ``Cookie:`` header from saved browser cookies (session-only)."""
    parts: List[str] = []
    seen = set()
    for cookie in cookies or []:
        if not isinstance(cookie, dict) or not cookie.get("name"):
            continue
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if domains and not any(domain.endswith(d) for d in domains):
            continue
        name = str(cookie["name"])
        if name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={cookie.get('value', '')}")
    return "; ".join(parts)


def load_session_cookies(storage_state_path: str) -> List[Dict[str, Any]]:
    """Read the cookies out of a saved ``storage_state.json``."""
    try:
        data = json.loads(Path(storage_state_path).read_text(encoding="utf-8"))
    except Exception:
        return []
    cookies = data.get("cookies") if isinstance(data, dict) else None
    return list(cookies or [])


def discover_send_event(js_text: str, limit: int = 5) -> List[str]:
    """Rank the ``socket.emit("…")`` names found in the site's chat bundle.

    A real bundle contains many emits (presence, typing, …); chat related names
    are returned first so the caller can try them in order.
    """
    text = str(js_text or "")
    names: List[str] = []
    for pattern in (r"""\.emit\(\s*["']([\w:.\-]{2,40})["']""",
                    r"""emit\(\s*["']([\w:.\-]{2,40})["']""",
                    r"""["']([\w:.\-]*(?:send|message|chat)[\w:.\-]*)["']\s*,"""):
        for match in re.finditer(pattern, text):
            name = match.group(1)
            if name in names or name in ("connect", "disconnect"):
                continue
            names.append(name)
    ranked = sorted(names, key=lambda n: (0 if _SEND_HINT.search(n) else 1, names.index(n)))
    return ranked[:limit]


# --------------------------------------------------------------------------- #
#  the transport
# --------------------------------------------------------------------------- #

class ChatWebSocket:
    """Session-authenticated socket chat: incoming SMS in, replies out."""

    def __init__(self, *,
                 cookie_header: str = "",
                 cookies: Optional[Iterable[Dict[str, Any]]] = None,
                 url: str = DEFAULT_WS_URL,
                 my_username: str = "",
                 send_event: str = "",
                 log: Callable[[str], None] = print,
                 on_message: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 on_match: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_state: Optional[Callable[[str], None]] = None,
                 origin: str = "https://app.chitchat.gg",
                 user_agent: str = "",
                 allow_probe: bool = False,
                 echo_timeout: float = 6.0,
                 client_factory: Optional[Callable[..., SocketIOClient]] = None,
                 namespace_payload: Optional[Dict[str, Any]] = None):
        self.url = str(url)
        self.my_username = str(my_username or "")
        self.send_event = str(send_event or "")
        self.sent_event: str = ""          # the one that actually worked
        self.log = log
        self.on_message = on_message
        self.on_match = on_match
        self.on_state = on_state
        self.allow_probe = bool(allow_probe)
        self.echo_timeout = float(echo_timeout)
        self.origin = origin
        if not cookie_header and cookies is not None:
            cookie_header = cookies_header(cookies)
        self.cookie_header = cookie_header

        headers = {"Origin": self.origin, "Referer": f"{self.origin}/chat",
                   "Accept-Language": "en-US,en;q=0.9"}
        if user_agent:
            headers["User-Agent"] = user_agent
        if cookie_header:
            headers["Cookie"] = cookie_header

        self.client = (client_factory or SocketIOClient)(
            self.url, headers=headers, log=log, on_event=self._on_event,
            on_open=self._on_open, on_close=self._on_close,
            namespace_payload=namespace_payload or {"release": "unknown"},
        )

        self.conversation_id = ""
        self.participants: List[Dict[str, Any]] = []
        self._handlers: "queue.Queue[Any]" = queue.Queue()
        self._handler_thread: Optional[threading.Thread] = None
        self.messages: List[Dict[str, Any]] = []
        self.messages_in = 0
        self.messages_out = 0
        self.echoes: List[str] = []
        self.closed_by_peer = False
        self._lock = threading.Lock()
        self._echo_event = threading.Event()
        self._echo_text = ""

    # ------------------------------------------------------------------ #
    #  lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> "ChatWebSocket":
        if self._handler_thread is None:
            self._handler_thread = threading.Thread(
                target=self._run_handlers, name="chat-ws-handlers", daemon=True)
            self._handler_thread.start()
        self.client.start()
        return self

    def _run_handlers(self) -> None:
        """Run the user's callbacks away from the socket reader thread.

        A handler may well block (ChatRuleBot, a DOM fallback, sending the reply
        and waiting for its echo), so it must never run inside the read loop —
        that would deadlock the very frame it is waiting for.
        """
        while True:
            callback, args = self._handlers.get()
            try:
                callback(*args)
            except Exception as error:
                self.log(f"[WS] handler failed: {type(error).__name__}: {error}")

    def _dispatch(self, callback: Optional[Callable], *args) -> None:
        if callback is None:
            return
        self._handlers.put((callback, args))

    def wait_connected(self, timeout: float = 20.0) -> bool:
        return self.client.wait_connected(timeout)

    def stop(self, reason: str = "chat stop") -> None:
        try:
            self.client.close(reason)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  socket events
    # ------------------------------------------------------------------ #
    def _on_open(self, _client) -> None:
        self.log(f"[WS] authenticated with the saved session "
                 f"({len(self.cookie_header.split(';')) if self.cookie_header else 0} "
                 f"cookies sent)")
        self.client.emit("presenceSync")
        self.log("[WS] presenceSync sent — waiting for the match")

    def _on_close(self, _client) -> None:
        self.log("[WS] socket closed — the caller decides whether to fall back to the DOM")

    def _on_event(self, event: str, payload: Any) -> None:
        if event == "matchUpdate":
            self._handle_match(payload)
        elif event == "chatMessage":
            self._handle_message(payload)
        elif event == "typing":
            user_id = (payload or {}).get("userId") if isinstance(payload, dict) else ""
            if user_id and user_id != self.client.pid:
                self.log(f"[WS] typing… (user {str(user_id)[:8]})")
        elif event == "onlineFriends":
            self.log(f"[WS] onlineFriends: {payload}")

    # ------------------------------------------------------------------ #
    #  protocol helpers
    # ------------------------------------------------------------------ #
    def _handle_match(self, payload: Any) -> None:
        match = (payload or {}).get("match") if isinstance(payload, dict) else None
        match = match or {}
        conversation = match.get("conversation") or {}
        closure = match.get("closure") or conversation.get("closure") or {}
        if conversation.get("id"):
            self.conversation_id = str(conversation["id"])
        participants = conversation.get("participants") or []
        if participants:
            self.participants = participants
        names = [str((p.get("profile") or {}).get("username") or "")
                 for p in participants]
        names = [n for n in names if n]
        if names and not self.my_username:
            # Learn who we are: the socket knows its own profile id.
            for entry in participants:
                profile = entry.get("profile") or {}
                if str(profile.get("id") or "") == self.client.pid:
                    self.my_username = str(profile.get("username") or "")
                    break
        self.log(f"[WS] matchUpdate: conversation={self.conversation_id[:12]} "
                 f"participants={names} closed={bool(closure.get('closed'))}")
        if closure.get("closed"):
            self.closed_by_peer = True
            self.log(f"[WS] the chat was closed (by={str(closure.get('closedBy'))[:8]}, "
                     f"reason={closure.get('closeReason')})")
        if not closure.get("closed"):
            self._dispatch(self.on_match, match)

    def _handle_message(self, payload: Any) -> None:
        message = (payload or {}).get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            return
        author = (message.get("author") or {}).get("username") or ""
        text = str(message.get("content") or "")
        with self._lock:
            self.messages.append(message)
            mine = bool(self.my_username) and author == self.my_username
            if mine:
                self.messages_out += 1
            else:
                self.messages_in += 1
        if not self.my_username and text in self.echoes:
            mine = True
        if mine:
            self.log(f"[WS] echo of our own message ({message.get('status')}): {text[:60]}")
            if text and text == self._echo_text:
                self._echo_event.set()
            return
        self.log(f"[WS] incoming SMS from {author}: {text[:80]}")
        self._dispatch(self.on_message, text, message)

    # ------------------------------------------------------------------ #
    #  sending
    # ------------------------------------------------------------------ #
    def _payload_for(self, shape: str, text: str) -> Dict[str, Any]:
        if shape == "nested":
            return {"conversationId": self.conversation_id,
                    "message": {"content": text, "type": "TEXT"}}
        if shape == "minimal":
            return {"content": text}
        return {"conversationId": self.conversation_id, "content": text,
                "type": "TEXT", "status": "SENT"}

    def send_message(self, text: str, *, wait_for_echo: bool = True,
                     timeout: Optional[float] = None) -> Tuple[bool, str]:
        """Send one message. Returns ``(confirmed, how)``.

        ``confirmed`` is True when the server echoed the message back (the
        capture shows the sender receives its own ``chatMessage``), which is the
        only reliable proof that the right event name was used.
        """
        text = str(text or "").strip()
        if not text:
            return False, "empty"
        if not self.client.connected:
            return False, "not connected"
        timeout = float(timeout if timeout is not None else self.echo_timeout)

        configured = bool(self.sent_event or self.send_event)
        events = [self.sent_event or self.send_event] if configured else []
        events.append("chatMessage")
        if self.allow_probe:
            events.extend([e for e in SEND_EVENT_CANDIDATES if e not in events])
        seen = set()
        ordered = [e for e in events if e and not (e in seen or seen.add(e))]

        # Only a *known* event gets the extra payload shapes: for a guess we do
        # not want to fire three variants of the same wrong event.
        shapes = list(PAYLOAD_SHAPES) if configured else ["flat"]
        for event in ordered:
            for shape in shapes:
                payload = self._payload_for(shape, text)
                self._echo_event.clear()
                self._echo_text = text
                with self._lock:
                    self.echoes.append(text)
                if not self.client.emit(event, payload):
                    return False, "socket write failed"
                self.log(f"[WS] sent {event} ({shape}) → {text[:60]}")
                if not wait_for_echo:
                    return True, f"{event}/{shape} (unconfirmed)"
                if self._echo_event.wait(timeout):
                    self.sent_event = event
                    self.log(f"[WS] ✓ delivery confirmed by the server echo "
                             f"(event={event}, shape={shape})")
                    return True, f"{event}/{shape}"
                self.log(f"[WS] no echo for {event}/{shape} within {timeout:.1f}s")
                shapes = ["flat"]        # keep probing events, one shape is enough
                break
        return False, "no echo for any candidate event"

    # ------------------------------------------------------------------ #
    #  reporting
    # ------------------------------------------------------------------ #
    def stats(self) -> Dict[str, Any]:
        base = self.client.stats() if hasattr(self.client, "stats") else {}
        base.update({
            "conversation": self.conversation_id,
            "my_username": self.my_username,
            "participants": [str((p.get("profile") or {}).get("username") or "")
                             for p in self.participants],
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
            "confirmed_event": self.sent_event,
            "closed_by_peer": self.closed_by_peer,
        })
        return base


# --------------------------------------------------------------------------- #
#  bundle probing (needs a live page)
# --------------------------------------------------------------------------- #

BUNDLE_HINT = re.compile(r"/assets/[^\"')\s]*chat[^\"')\s]*\.js", re.I)


def chat_bundle_urls(html: str, base: str = "https://app.chitchat.gg") -> List[str]:
    """Find the chat bundle URL(s) in a page's HTML/JS."""
    urls: List[str] = []
    for match in BUNDLE_HINT.finditer(str(html or "")):
        path = match.group(0)
        if path not in urls:
            urls.append(path)
    return [f"{base}{p}" if p.startswith("/") else p for p in urls]


def probe_send_events_from_page(page, log: Callable[[str], None] = print) -> List[str]:
    """Ask a live page for its own bundle and rank the send event names.

    Runs inside the browser that already holds the session, so no extra
    network access is needed beyond what the page does anyway.
    """
    try:
        html = page.content()
    except Exception as error:
        log(f"[WS] could not read the page for the probe: {error}")
        return []
    urls = chat_bundle_urls(html)
    if not urls:
        urls = ["https://app.chitchat.gg/assets/js-direct-chat.js"]
    names: List[str] = []
    for url in urls[:3]:
        try:
            js = page.evaluate(
                """async (url) => {
                     const r = await fetch(url, {credentials: 'include'});
                     return r.ok ? await r.text() : ''; }""", url)
        except Exception as error:
            log(f"[WS] bundle fetch failed ({url}): {error}")
            continue
        found = discover_send_event(js)
        if found:
            log(f"[WS] {url} → candidate send events: {found}")
            for name in found:
                if name not in names:
                    names.append(name)
    return names


__all__ = ["ChatWebSocket", "DEFAULT_WS_URL", "SEND_EVENT_CANDIDATES",
           "cookies_header", "load_session_cookies", "discover_send_event",
           "chat_bundle_urls", "probe_send_events_from_page"]
