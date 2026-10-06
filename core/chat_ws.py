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
import os
import queue
import random
import re
import string
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
                 my_id: str = "",
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
        self.my_id = str(my_id or "")
        self.send_event = str(send_event or "")
        self.sent_event: str = ""          # the one that actually worked
        # The site's own HTTP send request, once learned (see build_send_template).
        self.http_send: Dict[str, Any] = {}
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
        #: replay/tests: run callbacks on the calling thread instead of the worker
        self.deliver_inline = False
        self.messages: List[Dict[str, Any]] = []
        self.messages_in = 0
        self.messages_out = 0
        self.echoes: List[str] = []
        self.nonces: List[str] = []          # nonces of our own sent messages
        self._echo_nonce = ""
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
        if self.deliver_inline:
            try:
                callback(*args)
            except Exception as error:
                self.log(f"[WS] handler failed: {type(error).__name__}: {error}")
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
            # ``userId`` is a *profile* id; the socket ``pid`` is a session id, so
            # they never match — only our own profile id means "we are typing".
            if user_id and user_id != self.my_id:
                self.log(f"[WS] typing… (user {str(user_id)[:8]})")
        elif event == "onlineFriends":
            self.log(f"[WS] onlineFriends: {payload}")

    @staticmethod
    def _new_nonce() -> str:
        """A client-generated id for one message (the site echoes it back).

        The capture shows our own message coming back with a ``nonce`` while the
        other side's messages have none — so a matching nonce is the most exact
        "this is my echo" signal there is.
        """
        alphabet = string.ascii_letters + string.digits + "_-"
        return "".join(random.choice(alphabet) for _ in range(21))

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
            # The capture shows ``pid`` is a *session* id, not a profile id, so it
            # cannot identify us here.  Use the profile we already know (from the
            # page or from our own echo) and otherwise leave the name for later.
            for entry in participants:
                profile = entry.get("profile") or {}
                if self.my_id and str(profile.get("id") or "") == self.my_id:
                    self.my_username = str(profile.get("username") or "")
                    break
        count = match.get("messageCount") if isinstance(match, dict) else None
        last = match.get("lastMessage") if isinstance(match, dict) else None
        extra = ""
        if count is not None:
            extra += f" messages={count}"
        if last:
            extra += f" last={str(last)[:10]}"
        self.log(f"[WS] matchUpdate: conversation={self.conversation_id[:12]} "
                 f"participants={names} closed={bool(closure.get('closed'))}{extra}")
        if closure.get("closed"):
            self.closed_by_peer = True
            self.log(f"[WS] the chat was closed (by={str(closure.get('closedBy'))[:8]}, "
                     f"reason={closure.get('closeReason')})")
        if not closure.get("closed"):
            self._dispatch(self.on_match, match)

    def _is_mine(self, message: Dict[str, Any], author: str, text: str) -> bool:
        """Decide whether an incoming ``chatMessage`` is our own message.

        Three independent signals, because the capture shows the old assumption
        (``pid`` == own profile id) is wrong:

        1. the author id we learned from an earlier echo of our own message,
        2. the username the page reported (the app knows who is logged in),
        3. the text of a message we just sent (``nonce`` is the site's own marker
           for "sent by this client" and is used as a last resort).
        """
        author_id = str((message.get("author") or {}).get("id") or "")
        if self.my_id and author_id and author_id == self.my_id:
            return True
        if self.my_username and author and author == self.my_username:
            return True
        if text and text in self.echoes:
            return True
        nonce = str(message.get("nonce") or "")
        if nonce and nonce in self.nonces:
            return True
        if nonce and not (self.my_username or self.my_id):
            # Nothing else identifies us yet: the site only echoes a nonce to the
            # client that sent it.
            return True
        return False

    def _handle_message(self, payload: Any) -> None:
        message = (payload or {}).get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            return
        author = str((message.get("author") or {}).get("username") or "")
        author_id = str((message.get("author") or {}).get("id") or "")
        text = str(message.get("content") or "")
        with self._lock:
            self.messages.append(message)
            mine = self._is_mine(message, author, text)
            if mine:
                # Learn who we are from our own echo — the strongest identity
                # signal available to the socket (pid is only a session id).
                if author_id and not self.my_id:
                    self.my_id = author_id
                if author and not self.my_username:
                    self.my_username = author
                self.messages_out += 1
            else:
                self.messages_in += 1
        if mine:
            self.log(f"[WS] echo of our own message ({message.get('status')}): {text[:60]}")
            nonce = str(message.get("nonce") or "")
            if (text and text == self._echo_text) or (nonce and nonce == self._echo_nonce):
                self._echo_event.set()
            return
        self.log(f"[WS] incoming SMS from {author}: {text[:80]}")
        self._dispatch(self.on_message, text, message)

    # ------------------------------------------------------------------ #
    #  offline replay (no socket): feed captured server frames
    # ------------------------------------------------------------------ #
    def feed(self, frame: str) -> None:
        """Process one raw *server* frame without a socket.

        Used by ``tools/ws_chat.py --replay`` and the tests to check what the bot
        would have seen in a real capture (``_webSocketMessages`` of type
        ``receive``).
        """
        text = str(frame or "").strip()
        if not text:
            return
        if text.startswith("0"):
            return                      # engine.io handshake
        if text.startswith("40"):
            try:
                ack = json.loads(text[2:] or "{}")
            except Exception:
                ack = {}
            self.client.sid = str(ack.get("sid") or self.client.sid)
            self.client.pid = str(ack.get("pid") or self.client.pid)
            self.log("[WS] (replay) socket.io connected: "
                     f"sid={self.client.sid[:10]} pid={self.client.pid[:10]} "
                     "(pid = session id, not a profile id)")
            return
        if text.startswith("41"):
            self.closed_by_peer = True
            return
        if text.startswith("42"):
            try:
                event, *rest = json.loads(text[2:])
            except Exception:
                return
            payload = rest[0] if rest else None
            self.client.event_counts[str(event)] = \
                self.client.event_counts.get(str(event), 0) + 1
            self._on_event(str(event), payload)
            return
        # "2" (ping) / "3" (pong) / anything else: nothing for the chat layer

    # ------------------------------------------------------------------ #
    #  sending
    # ------------------------------------------------------------------ #
    def _payload_for(self, shape: str, text: str, nonce: str = "") -> Dict[str, Any]:
        if shape == "nested":
            payload = {"conversationId": self.conversation_id,
                       "message": {"content": text, "type": "TEXT"}}
        elif shape == "minimal":
            payload = {"content": text}
        else:
            payload = {"conversationId": self.conversation_id, "content": text,
                       "type": "TEXT", "status": "SENT"}
        if nonce and shape != "minimal":
            # Same field name the site uses for a client-generated message id.
            payload["nonce"] = nonce
        return payload

    def note_sent_text(self, text: str) -> None:
        """Remember a message that left through another path (the DOM fallback).

        Without this the page's own echo would look like an incoming message and
        the bot would answer itself.
        """
        text = str(text or "").strip()
        if not text:
            return
        with self._lock:
            self.echoes.append(text)
            del self.echoes[:-200]

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
        if self.http_send:
            return self._send_via_http(text, timeout=timeout, wait_for_echo=wait_for_echo)

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
        nonce = self._new_nonce()
        with self._lock:
            self.nonces.append(nonce)
            del self.nonces[:-200]
        for event in ordered:
            for shape in shapes:
                payload = self._payload_for(shape, text, nonce)
                self._echo_event.clear()
                self._echo_text = text
                self._echo_nonce = nonce
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

    def set_http_send(self, template: Optional[Dict[str, Any]]) -> None:
        """Use (or forget) the site's own HTTP send request for replies."""
        self.http_send = dict(template or {})
        if self.http_send:
            self.log(f"[WS] replies will use the site's own send request: "
                     f"{self.http_send.get('method')} {self.http_send.get('url')}")

    def _send_via_http(self, text: str, *, timeout: float,
                       wait_for_echo: bool = True) -> Tuple[bool, str]:
        """Send through the learned request and confirm it on the socket echo."""
        nonce = self._new_nonce()
        with self._lock:
            self.nonces.append(nonce)
            del self.nonces[:-200]
        self._echo_event.clear()
        self._echo_text = text
        self._echo_nonce = nonce
        with self._lock:
            self.echoes.append(text)
        ok, detail, status = send_template_request(
            self.http_send, cookie_header=self.cookie_header, text=text, nonce=nonce,
            conversation_id=self.conversation_id, log=self.log)
        if not ok:
            return False, f"http failed ({detail})"
        self.log(f"[WS] sent over HTTP ({status}) → {text[:60]}")
        if not wait_for_echo:
            return True, f"http/{status} (unconfirmed)"
        if self._echo_event.wait(timeout):
            self.log(f"[WS] ✓ delivery confirmed by the server echo (http/{status})")
            return True, f"http/{status}"
        # The site's own endpoint took it; the echo may simply have been missed.
        self.log(f"[WS] the site accepted the message (HTTP {status}) but no socket "
                 f"echo was seen within {timeout:.1f}s")
        return True, f"http/{status} (echo not seen)"

    # ------------------------------------------------------------------ #
    #  reporting
    # ------------------------------------------------------------------ #
    def stats(self) -> Dict[str, Any]:
        base = self.client.stats() if hasattr(self.client, "stats") else {}
        base.update({
            "conversation": self.conversation_id,
            "my_username": self.my_username,
            "my_id": self.my_id,
            "participants": [str((p.get("profile") or {}).get("username") or "")
                             for p in self.participants],
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
            "confirmed_event": self.sent_event,
            "http_send": (f"{self.http_send.get('method')} {self.http_send.get('url')}"
                          if self.http_send else ""),
            "closed_by_peer": self.closed_by_peer,
        })
        return base


# --------------------------------------------------------------------------- #
#  the site's own send path (HTTP) — see the note in the module docstring
# --------------------------------------------------------------------------- #

#: Body keys that carry the message text, most likely first.
CONTENT_FIELDS: Tuple[str, ...] = ("content", "text", "message", "body", "msg")

#: Body keys that carry the client-generated nonce.
NONCE_FIELDS: Tuple[str, ...] = ("nonce", "clientMessageId", "clientId", "tempId",
                                 "idempotencyKey")

#: Body keys that carry the conversation id.
CONVERSATION_FIELDS: Tuple[str, ...] = ("conversationId", "conversation", "chatId",
                                        "roomId", "matchId")

#: Request headers worth replaying (everything else is per-connection).
REPLAY_HEADERS: Tuple[str, ...] = ("content-type", "accept", "authorization",
                                   "x-csrf-token", "x-xsrf-token", "x-requested-with",
                                   "referer", "origin", "user-agent",
                                   "accept-language")

#: Paths that are page bookkeeping, never a message send.
_NOT_SEND_PATHS = ("/state", "/dump", "/hello", "/leave", "/presence", "/typing")

_WS_CONFIG_ENV = "EVA_WS_CONFIG"


def ws_config_path() -> Path:
    """Where the learned socket/HTTP settings live (tests override the env)."""
    override = os.environ.get(_WS_CONFIG_ENV) or ""
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "data" / "ws_config.json"


def load_ws_config() -> Dict[str, Any]:
    try:
        data = json.loads(ws_config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_ws_config(**updates: Any) -> Dict[str, Any]:
    data = load_ws_config()
    data.update({k: v for k, v in updates.items() if v is not None})
    try:
        path = ws_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
    return data


def build_send_template(method: str, url: str, body: Dict[str, Any],
                        headers: Optional[Dict[str, str]] = None,
                        text: str = "") -> Dict[str, Any]:
    """Normalise one real send request into a replayable template.

    The field names are detected from the body itself (the value that is the
    message text, an id-looking value, a nonce-looking value), so the bot never
    has to guess the site's schema.
    """
    body = dict(body or {})
    content_field = ""
    for key in CONTENT_FIELDS:
        if isinstance(body.get(key), str) and body[key] == text and text:
            content_field = key
            break
    if not content_field:
        for key in CONTENT_FIELDS:
            if isinstance(body.get(key), str) and body[key]:
                content_field = key
                break
    if not content_field:
        for key, value in body.items():
            if isinstance(value, str) and value == text and text:
                content_field = key
                break
    nonce_field = ""
    for key in NONCE_FIELDS:
        if isinstance(body.get(key), str) and body[key]:
            nonce_field = key
            break
    if not nonce_field:
        for key, value in body.items():
            if (isinstance(value, str) and 12 <= len(value) <= 40
                    and re.fullmatch(r"[A-Za-z0-9_-]+", value)
                    and value not in (body.get(content_field),)):
                if not re.fullmatch(r"[0-9a-f]{24}", value):
                    nonce_field = key
                    break
    conversation_field = ""
    for key in CONVERSATION_FIELDS:
        # Present-but-empty still counts: the request *shape* is what matters
        # (the page may not have learned the id yet).
        if key in body and isinstance(body.get(key), str):
            conversation_field = key
            break
    if not conversation_field:
        for key, value in body.items():
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{24}", value):
                conversation_field = key
                break
    keep_headers = {}
    for key, value in (headers or {}).items():
        low = str(key).lower()
        if low in REPLAY_HEADERS and str(value).strip():
            keep_headers[str(key)] = str(value)
        if low.startswith("x-") and low not in ("x-requested-with",) and str(value).strip():
            keep_headers[str(key)] = str(value)
    return {
        "method": str(method or "POST").upper(),
        "url": str(url),
        "body": body,
        "headers": keep_headers,
        "content_field": content_field or "content",
        "nonce_field": nonce_field,
        "conversation_field": conversation_field,
        "learned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def look_like_send_request(method: str, url: str, body: Any) -> bool:
    """Is this request the app sending a chat message?"""
    if str(method or "").upper() not in ("POST", "PUT", "PATCH"):
        return False
    lowered = str(url or "").lower()
    if any(bit in lowered for bit in _NOT_SEND_PATHS):
        return False
    if not isinstance(body, dict) or not body:
        return False
    if not any(isinstance(body.get(k), str) and body.get(k) for k in CONTENT_FIELDS):
        return False
    return True


def send_template_request(template: Dict[str, Any], *, cookie_header: str = "",
                          text: str = "", nonce: str = "", conversation_id: str = "",
                          timeout: float = 10.0,
                          log: Callable[[str], None] = print) -> Tuple[bool, str, int]:
    """Replay a learned send request from the backend. ``(ok, detail, status)``."""
    import urllib.error
    import urllib.request

    if not isinstance(template, dict) or not template.get("url"):
        return False, "no template", 0
    body = dict(template.get("body") or {})
    content_field = str(template.get("content_field") or "content")
    if content_field not in body:
        for key in CONTENT_FIELDS:
            if key in body:
                content_field = key
                break
    url = str(template["url"])
    base = str(template.get("base") or "").strip()          # harness/proxy hook
    if base:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        base_parts = urlsplit(base if "://" in base else f"http://{base}")
        url = urlunsplit((base_parts.scheme or parts.scheme,
                          base_parts.netloc or parts.netloc,
                          parts.path, parts.query, parts.fragment))
    body[content_field] = str(text)
    nonce_field = str(template.get("nonce_field") or "")
    if nonce_field:
        body[nonce_field] = str(nonce)
    conversation_field = str(template.get("conversation_field") or "")
    if conversation_field and conversation_id:
        body[conversation_field] = str(conversation_id)
    elif conversation_id:
        for key in CONVERSATION_FIELDS:
            if key in body:
                body[key] = str(conversation_id)
                break
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload,
                                     method=str(template.get("method") or "POST"))
    headers = dict(template.get("headers") or {})
    headers.setdefault("Content-Type", "application/json")
    if cookie_header:
        headers["Cookie"] = cookie_header
    for key, value in headers.items():
        try:
            request.add_header(str(key), str(value))
        except Exception:
            continue
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            status = int(getattr(response, "status", 200) or 200)
            snippet = ""
            try:
                snippet = response.read(400).decode("utf-8", "replace")
            except Exception:
                snippet = ""
        log(f"[WS] http send → {template.get('method')} {url} status={status}")
        return 200 <= status < 300, snippet[:200], status
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read(300).decode("utf-8", "replace")
        except Exception:
            detail = str(error)
        log(f"[WS] http send failed: {error.code} {detail[:160]}")
        return False, f"HTTP {error.code} {detail[:160]}", int(error.code or 0)
    except Exception as error:
        log(f"[WS] http send error: {type(error).__name__}: {error}")
        return False, f"{type(error).__name__}: {error}", 0


class SendRequestSniffer:
    """Watch a browser context for the app's *own* message-send request.

    Nothing is ever typed by the sniffer itself: it just listens.  When the bot's
    page sends a message (the DOM fallback does exactly that), the request goes
    through here and is turned into a :func:`build_send_template` template, so
    the next reply can be sent from the backend — the same way the site does it.
    """

    def __init__(self, log: Callable[[str], None] = print, conversation_id: str = "",
                 on_template: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.log = log
        self.conversation_id = str(conversation_id or "")
        self.on_template = on_template
        self.template: Dict[str, Any] = {}
        self.requests_seen = 0
        self._context = None
        self._event = threading.Event()

    # -- Playwright hook (runs on the browser's own thread) ----------------
    def on_request(self, request: Any) -> None:
        try:
            method = str(getattr(request, "method", "") or "")
            if method.upper() not in ("POST", "PUT", "PATCH"):
                return
            url = str(getattr(request, "url", "") or "")
            if "chitchat" not in url and "127.0.0.1" not in url and "localhost" not in url:
                return
            data = None
            try:
                data = request.post_data
            except Exception:
                data = None
            if not data:
                return
            try:
                body = json.loads(data)
            except Exception:
                return
            if not look_like_send_request(method, url, body):
                return
            self.requests_seen += 1
            text = ""
            for key in CONTENT_FIELDS:
                if isinstance(body.get(key), str):
                    text = body[key]
                    break
            headers = {}
            try:
                headers = dict(request.headers or {})
            except Exception:
                headers = {}
            template = build_send_template(method, url, body, headers, text=text)
            self.template = template
            nonce_field = template["nonce_field"] or "-"
            conversation_field = template["conversation_field"] or "-"
            self.log(f"[WS] learned the site's own send request: {template['method']} "
                     f"{template['url']} (content={template['content_field']!r}, "
                     f"nonce={nonce_field!r}, conversation={conversation_field!r})")
            if self.on_template is not None:
                try:
                    self.on_template(template)
                except Exception as error:
                    self.log(f"[WS] send-template callback failed: {error}")
            self._event.set()
        except Exception as error:
            self.log(f"[WS] request sniffer: {type(error).__name__}: {error}")

    # -- ownership ---------------------------------------------------------
    def install(self, context: Any) -> None:
        try:
            context.on("request", self.on_request)
            self._context = context
            self.log("[WS] watching the page for the site's own message-send request")
        except Exception as error:
            self.log(f"[WS] could not watch the page requests: {error}")

    def stop(self) -> None:
        if self._context is None:
            return
        try:
            self._context.remove_listener("request", self.on_request)
        except Exception:
            pass
        self._context = None

    def wait(self, timeout: float = 2.0) -> Dict[str, Any]:
        self._event.wait(max(0.1, float(timeout)))
        return self.template


def http_send_clues(script_text: str) -> List[str]:
    """Candidate send endpoints inside a saved JS bundle (offline discovery)."""
    clues: List[str] = []
    for match in re.finditer(r"""(?P<quote>["'`])(?P<path>/[A-Za-z0-9_./{}$:\-]{3,120})(?P=quote)""",
                             str(script_text or "")):
        path = match.group("path")
        if not re.search(r"(message|chat|conversation)", path, re.I):
            continue
        if path not in clues:
            clues.append(path)
    return clues[:20]


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
