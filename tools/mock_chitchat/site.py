"""Local chitchat.gg stand-in: test pages + a local HTTP mock backend.

Two pieces:

* :class:`ChatBackend` — a thread-safe message store. Every participant (the
  bot's browser, the "stranger" browser, any test client) reads/writes it over
  the mock HTTP API, allowing the client pipeline to be tested offline.
* :func:`page_html` — the three pages the bot needs:
  ``/`` (home, "Start Text Chat" button), ``/start/new`` (captcha gate that
  hands over to the chat) and ``/chat/`` (the chat UI whose markup matches
  ``browser/chat_reader.py``).

The markup deliberately mirrors the real site shape that the bot already
handles (``#connected-text``, ``li.select-text`` bubbles with
``span.font-bold`` + ``span.emoji-content``, a ``bg-warning`` SKIP button and a
``textarea[name="message"]`` that sends on Enter), so nothing in
``browser/browser_automation.py`` has to change for the offline E2E test.
"""

from __future__ import annotations

import json
import random
import threading
import time
from datetime import datetime
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

STRANGER_NAME = "Stranger42"

#: The endpoint the mock chat page dials.  It is the real one on purpose: the
#: pages are served over https, so they cannot open an insecure ws:// socket —
#: the test harness forwards this connection to the local stand-in instead.
DEFAULT_WS_URL = "wss://api.chitchat.gg/socket.io/?EIO=4&transport=websocket"


class ChatBackend:
    """Thread-safe chat history shared by every participant."""

    def __init__(self, log=print):
        self._lock = threading.Lock()
        self._messages: List[Dict[str, Any]] = []
        self._log = log
        self.cookie_seen: Dict[str, Any] = {}
        self._presence: Dict[str, float] = {}
        self._presence_cv = threading.Condition(self._lock)
        # Debug telemetry: what each page thinks it is showing (the pages push
        # this themselves, so the harness never has to touch another thread's
        # Playwright objects).
        self.states: List[Dict[str, Any]] = []
        self._conversation_id = "mock-" + "".join(
            random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(10))
        self._tokens: Dict[str, str] = {}
        self._message_hooks: List[Callable[[Dict[str, Any]], None]] = []
        self._api_send_template: Dict[str, Any] = {}
        self.dumped_html: Dict[str, Any] = {}
        self._dump_wanted: Dict[str, int] = {}
        self.api_trace: List[str] = []

    # -- presence ---------------------------------------------------------
    def hello(self, author: str) -> None:
        """Record that a participant opened the chat page."""
        with self._presence_cv:
            self._presence[str(author or "?")] = time.time()
            self._presence_cv.notify_all()

    def leave(self, author: str) -> None:
        """Mark a participant as gone (the mock's "skip" action)."""
        with self._presence_cv:
            self._presence.pop(str(author or "?"), None)
            self._presence_cv.notify_all()

    def wait_for_participant(self, name: str, timeout: float = 90.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._presence_cv:
            while str(name) not in self._presence:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._presence_cv.wait(min(1.0, remaining))
            return True

    # -- the app's own send request (learned by the backend) ---------------
    def conversation(self) -> str:
        """The stand-in's conversation id (the real page reads it from the URL)."""
        with self._lock:
            return str(self._conversation_id)

    def register_tokens(self, mapping: Dict[str, str]) -> None:
        """Register the local mock's token-to-test-account mapping."""
        with self._lock:
            self._tokens.update({str(k): str(v) for k, v in (mapping or {}).items() if k})

    def set_api_send_template(self, template: Dict[str, Any]) -> None:
        with self._lock:
            self._api_send_template = dict(template or {})

    def api_send_template(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._api_send_template) if self._api_send_template else None

    def sender_for_token(self, token: Any, *, fallback: str = "") -> str:
        """Resolve a token using the local mock's test-account mapping."""
        value = str(token or "").strip()
        if value:
            with self._lock:
                for candidate, username in self._tokens.items():
                    if str(candidate) and str(candidate) in value:
                        return str(username)
        return str(fallback or "")

    # -- messages ---------------------------------------------------------
    def on_message_added(self, callback) -> None:
        """Called with every new message (the ws stand-in broadcasts them)."""
        self._message_hooks.append(callback)

    def add(self, author: str, text: str, nonce: str = "") -> Dict[str, Any]:
        with self._lock:
            message = {
                "id": len(self._messages) + 1,
                "author": str(author or "?"),
                "text": str(text or ""),
                "ts": time.time(),
            }
            if nonce:
                # The real server echoes the client's nonce back on the message,
                # which is how "this one is mine" is recognised on the socket.
                message["nonce"] = str(nonce)
            self._messages.append(message)
        try:
            self._log(f"[MockChat] {message['author']}: {message['text']}")
        except Exception:
            pass
        for hook in list(self._message_hooks):
            try:
                hook(dict(message))
            except Exception:
                pass
        return message

    def since(self, after_id: int) -> Tuple[List[Dict[str, Any]], int]:
        """Messages newer than ``after_id``; ``next`` is the newest id seen."""
        with self._lock:
            after_id = max(0, int(after_id or 0))
            messages = [m for m in self._messages if int(m["id"]) > after_id]
            return messages, len(self._messages)

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._messages)

    # -- debug telemetry --------------------------------------------------
    def note_api(self, line: str) -> None:
        """Keep a bounded trace of the pages' API calls (for debug artifacts)."""
        with self._lock:
            self.api_trace.append(f"[{datetime.now().strftime('%H:%M:%S')}] {line}")
            if len(self.api_trace) > 500:
                del self.api_trace[:-500]

    def trace_tail(self, count: int = 300) -> List[str]:
        with self._lock:
            return list(self.api_trace[-count:])


    def note_state(self, where: str, payload: Dict[str, Any]) -> None:
        entry = {"where": str(where), "ts": time.time(), **(payload or {})}
        with self._lock:
            self.states.append(entry)

    def request_dump(self, where: str = "bot") -> None:
        """Ask the page to send its own HTML on its next poll."""
        with self._lock:
            self._dump_wanted[str(where)] = self._dump_wanted.get(str(where), 0) + 1

    def take_dump_request(self, where: str = "bot") -> bool:
        with self._lock:
            if self._dump_wanted.get(str(where), 0) > 0:
                self._dump_wanted[str(where)] -= 1
                return True
            return False

    def note_dump(self, where: str, html: str) -> None:
        with self._lock:
            self.dumped_html[str(where)] = str(html or "")

    # -- evidence ---------------------------------------------------------
    def note_cookies(self, path: str, cookie_header: str) -> None:
        names = []
        for chunk in str(cookie_header or "").split(";"):
            name = chunk.split("=", 1)[0].strip()
            if name:
                names.append(name)
        with self._lock:
            self.cookie_seen = {"path": path, "count": len(names), "names": names}


# --------------------------------------------------------------------------- #
#  Pages
# --------------------------------------------------------------------------- #

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
 body { font-family: system-ui, sans-serif; margin: 0; background: #0f1117; color: #e6e8ef; }
 .panel { max-width: 640px; margin: 32px auto; background: #171a23; border-radius: 12px; padding: 20px; }
 button { font: inherit; padding: 8px 14px; border-radius: 8px; border: 0; cursor: pointer; }
 .bg-warning { background: #f0b429; color: #221a00; }
 .bg-primary { background: #4c6ef5; color: #fff; }
 ol { list-style: none; margin: 0; padding: 0; min-height: 120px; }
 li { padding: 6px 4px; }
 textarea { width: 100%; min-height: 44px; border-radius: 8px; border: 1px solid #2a2f3d;
            background: #0f1117; color: inherit; padding: 8px; font: inherit; }
 .muted { color: #97a0b5; font-size: 13px; }
</style>
</head>
<body>
__BODY__
</body>
</html>
"""

_HOME_BODY = """
<div class="panel">
  <h1>chitchat mock</h1>
  <p class="muted">Local stand-in for app.chitchat.gg (offline E2E test).</p>
  <p>Say hi! Logged in as <strong>__ME__</strong></p>
  <button class="profile"><span class="truncate text-sm font-bold">__ME__</span></button>
  <button id="start-text-chat" class="bg-primary" style="margin-top:12px"
          onclick="location.href='/start/new'">Start Text Chat</button>
</div>
"""

_CAPTCHA_BODY = """
<div class="panel">
  <h1>Verify you are human</h1>
  <p class="muted">Mock captcha — clears itself automatically.</p>
  <div id="captcha-box" style="height:60px;border:1px dashed #2a2f3d;border-radius:8px"></div>
  <button class="bg-primary" onclick="location.href='/chat/'">Continue</button>
</div>
<script>
  setTimeout(function () { location.replace('/chat/'); }, 900);
</script>
"""

_CHAT_BODY = """
<div class="panel">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <span id="connected-text">You are now chatting with __STRANGER__</span>
    <button class="profile"><span class="truncate text-sm font-bold">__ME__</span></button>
  </div>
  <p class="muted">Text chat · messages flow through the local HTTP backend</p>
  <main>
    <ol class="overflow-y-auto" id="messages"></ol>
  </main>
  <textarea name="message" placeholder="Type a message..." autocomplete="off"></textarea>
  <div style="margin-top:10px;display:flex;gap:8px">
    <button id="skip" class="bg-warning">SKIP</button>
    <button id="start-text-chat" class="bg-primary" onclick="location.href='/chat/'">Start Text Chat</button>
  </div>
  <div id="ended" style="display:none;margin-top:12px">
    <p class="muted" id="ended-text"></p>
  </div>
  <p class="muted" id="status">connected as __ME__</p>
</div>
<script>
  window.MOCK_ME = __ME_JSON__;
  window.MOCK_DEBUG = __DEBUG_JSON__;
  var ME = __ME_JSON__;
  var API = __API_JSON__;
  var DEBUG = __DEBUG_JSON__;
  var WS_URL = __WS_JSON__;
  var TRANSPORT = __TRANSPORT_JSON__;
  var CONNECT_SOCKET = __CONNECT_SOCKET_JSON__;
  var since = 0;
  var rendered = {};
  var box = document.getElementById('messages');
  var status = document.getElementById('status');
  var sio = null;              // this page's own chat socket (ws mode)
  var conversationId = '';

  // The page knows which conversation it is in (the real one reads the URL).
  fetch(API + '/api/conversation').then(function (r) { return r.json(); })
    .then(function (data) {
      if (data && data.conversationId) { conversationId = data.conversationId; }
    }).catch(function () {});

  // ---- ws mode: the page speaks Engine.IO v4 + Socket.IO itself ----------
  function sioSend(event, payload) {
    if (!sio || sio.readyState !== 1) { return false; }
    sio.send('42' + JSON.stringify(payload === undefined ? [event] : [event, payload]));
    return true;
  }

  // The mock intentionally models chat writes as HTTP POSTs and uses the socket
  // for incoming events/echoes. The supplied live WS entry has no client send
  // frame, but does not show the other requests or establish the live write
  // transport, nor what the real server would do with a socket send.
  function newNonce() {
    var chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
    var out = '';
    for (var i = 0; i < 21; i++) { out += chars.charAt(Math.floor(Math.random() * chars.length)); }
    return out;
  }

  function sendViaHttp(text) {
    var token = (document.cookie.match(/(?:^|;\s*)token=([^;]+)/) || [])[1] || '';
    return fetch(API + '/api/conversations/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversationId: conversationId, content: text,
                             type: 'TEXT', status: 'SENT', token: token,
                             nonce: newNonce() })
    }).then(function (r) { return r.ok; });
  }

  function connectSocket() {
    // WS_URL already carries ?EIO=4&transport=websocket; the mock adds its
    // identity hint. The supplied live WS entry does not establish auth details.
    var url = WS_URL + (WS_URL.indexOf('as=') >= 0 ? ''
                        : (WS_URL.indexOf('?') >= 0 ? '&' : '?')
                          + 'as=' + encodeURIComponent(ME));
    status.textContent = 'connecting to the chat socket …';
    sio = new WebSocket(url);
    sio.onopen = function () { status.textContent = 'socket open as ' + ME; };
    sio.onmessage = function (ev) {
      var data = String(ev.data || '');
      if (data.charAt(0) === '0') {                 // engine.io handshake
        sio.send('40' + JSON.stringify({ release: 'mock-e2e' }));
        return;
      }
      if (data === '2') { sio.send('3'); return; }  // ping → pong
      if (data.charAt(0) !== '4') { return; }
      if (data.charAt(1) === '0') {                 // socket.io connected
        sio.send('42' + JSON.stringify(['presenceSync']));
        status.textContent = 'connected as ' + ME + ' (socket)';
        return;
      }
      if (data.charAt(1) !== '2') { return; }
      var frame = JSON.parse(data.slice(2));
      var event = frame[0];
      var payload = frame[1] || {};
      if (event === 'matchUpdate') {
        var match = payload.match || {};
        var conversation = match.conversation || {};
        var closure = match.closure || conversation.closure || {};
        if (conversation.id) { conversationId = conversation.id; }
        if (closure.closed) {
          window.MOCK_CHAT_ENDED = true;
          status.textContent = 'the chat was closed over the socket';
        }
        return;
      }
      if (event === 'chatMessage' && payload.message) {
        var message = payload.message;
        bubble({
          id: message.id || message.nonce || String(Math.random()),
          author: (message.author && message.author.username) || '?',
          text: message.content || ''
        });
        status.textContent = 'connected as ' + ME + ' (socket · '
                             + box.children.length + ' messages)';
      }
    };
    sio.onclose = function () { status.textContent = 'the chat socket closed'; };
  }

  function bubble(msg) {
    if (rendered[msg.id]) { return; }        // one bubble per message, ever
    rendered[msg.id] = 1;
    var li = document.createElement('li');
    li.className = 'select-text';
    li.dataset.messageId = msg.id;
    li.style.textAlign = (msg.author === ME) ? 'right' : 'left';
    var who = document.createElement('span');
    who.className = 'font-bold';
    who.setAttribute('role', 'button');
    who.textContent = msg.author;
    var txt = document.createElement('span');
    txt.className = 'emoji-content';
    txt.textContent = ' ' + msg.text;
    li.appendChild(who);
    li.appendChild(txt);
    box.appendChild(li);
  }

  function poll() {
    fetch(API + '/api/messages?since=' + since)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        (data.messages || []).forEach(bubble);
        if (typeof data.next === 'number') { since = data.next; }
        if (data.messages && data.messages.length) {
          status.textContent = 'connected as ' + ME + ' · ' + since + ' messages';
        }
        if (data.dump) { sendDump(); }          // the harness asked for the DOM
      })
      .catch(function () {});
  }

  function stateOf() {
    var items = Array.from(document.querySelectorAll('main ol li.select-text'));
    var last = items.length ? items[items.length - 1].innerText : '';
    var input = document.querySelector('textarea[name="message"]');
    var connected = document.getElementById('connected-text');
    return {
      author: ME,
      path: location.pathname,
      bubbles: items.length,
      last: last.replace(/\s+/g, ' ').trim().slice(0, 120),
      input: input ? String(input.value || '').slice(0, 80) : null,
      input_disabled: input ? !!input.disabled : null,
      connected: connected ? connected.innerText.trim().slice(0, 80) : null,
      ended: !!window.MOCK_CHAT_ENDED
    };
  }

  function sendState() {
    fetch(API + '/api/state', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(stateOf())
    }).catch(function () {});
  }

  function sendDump() {
    fetch(API + '/api/dump', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ author: ME, html: document.documentElement.outerHTML })
    }).catch(function () {});
  }

  if (DEBUG) { setInterval(sendState, 2000); sendState(); }
  fetch(API + '/api/hello', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ author: ME })
  }).catch(function () {});
  if (TRANSPORT === 'ws' && WS_URL && CONNECT_SOCKET) {
    connectSocket();
  } else if (TRANSPORT === 'ws' && WS_URL) {
    // This mock page normally has a socket, but a WS-only test may disable it:
    // the page then only writes messages (HTTP) and never listens on a socket.
    status.textContent = 'page socket disabled (message writes still go out)';
  } else {
    setInterval(poll, 700);
    poll();
  }

  function send() {
    var el = document.querySelector('textarea[name="message"]');
    var text = (el.value || '').trim();
    if (!text) { return; }
    el.value = '';
    if (TRANSPORT === 'ws' && WS_URL) {
      // This mock uses an out-of-band HTTP write; the socket handles receives.
      sendViaHttp(text);
      return;
    }
    fetch(API + '/api/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ author: ME, text: text })
    }).then(function () { poll(); });
  }

  document.querySelector('textarea[name="message"]').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' && !ev.shiftKey) {
      ev.preventDefault();
      send();
    }
  });
  document.getElementById('skip').addEventListener('click', function (ev) {
    ev.preventDefault();
    if (!sioSend('skip', { conversationId: conversationId })) {
      fetch(API + '/api/leave', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ author: ME })
      }).catch(function () {});
    }
    document.querySelector('textarea[name="message"]').disabled = true;
    document.getElementById('ended').style.display = 'block';
    document.getElementById('ended-text').textContent =
      '__STRANGER__ has skipped this chat';
    document.getElementById('connected-text').textContent =
      '__STRANGER__ has skipped this chat';
    // The real site only shows START once the chat has really ended.
    var again = document.createElement('button');
    again.className = 'bg-primary';
    again.textContent = 'START';
    again.onclick = function () { location.href = '/chat/'; };
    document.getElementById('ended').appendChild(again);
    window.MOCK_CHAT_ENDED = true;
  });
</script>
"""


def page_html(path: str,
              *,
              me: str = "EvaUser",
              api_base: str = "",
              stranger: str = STRANGER_NAME,
              debug: bool = False,
              ws_url: str = "",
              transport: str = "dom",
              connect_socket: bool = True) -> str:  # noqa: D417
    """Return the HTML for one mock page.

    ``transport="ws"`` makes the chat page speak Engine.IO v4 + Socket.IO to
    ``ws_url`` itself (the same frames the real site uses) instead of polling
    the local HTTP API, so a socket-aware bot can be tested end to end.
    """
    path = str(path or "/")
    if path.startswith("/chat"):
        body, title = _CHAT_BODY, "chitchat mock · chat"
    elif path.startswith("/start/new"):
        body, title = _CAPTCHA_BODY, "chitchat mock · verify"
    else:
        body, title = _HOME_BODY, "chitchat mock · home"
    html = _PAGE.replace("__TITLE__", title).replace("__BODY__", body)
    # The JSON variants are filled first: they carry the name as a *quoted* JS
    # string, so names with dots/spaces ("sadia.6.7") stay valid script.
    html = html.replace("__CONNECT_SOCKET_JSON__",
                        "true" if connect_socket else "false")
    html = (html.replace("__ME_JSON__", json.dumps(str(me)))
                .replace("__API_JSON__", json.dumps(str(api_base or "").rstrip("/")))
                .replace("__WS_JSON__", json.dumps(str(ws_url or DEFAULT_WS_URL)))
                .replace("__TRANSPORT_JSON__", json.dumps(str(transport or "dom")))
                .replace("__DEBUG_JSON__", "true" if debug else "false"))
    return (html.replace("__ME__", html_escape(str(me)))
                .replace("__STRANGER__", html_escape(str(stranger))))


# --------------------------------------------------------------------------- #
#  HTTP server
# --------------------------------------------------------------------------- #

def make_handler(backend: ChatBackend,
                 *,
                 me_for_page=lambda headers: "EvaUser",
                 api_base_for_page=lambda headers: "",
                 ws_url_for_page=lambda headers: "",
                 transport: str = "dom",
                 log=print):
    """Build an ``http.server`` handler bound to ``backend``."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # silence the default access log
            return

        # -- helpers ------------------------------------------------------
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Cache-Control", "no-store")

        def _send(self, code, body: bytes, content_type="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, code=200):
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        # -- routes -------------------------------------------------------
        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/messages":
                since = int((parse_qs(parsed.query).get("since") or ["0"])[0] or 0)
                messages, next_index = backend.since(since)
                return self._json({"messages": messages, "next": next_index,
                                   "dump": backend.take_dump_request()})
            if path == "/api/all":
                return self._json({"messages": backend.all()})
            if path == "/api/conversation":
                return self._json({"conversationId": backend.conversation()})
            if path in ("/", "/start/new", "/chat", "/chat/"):
                me = me_for_page(self.headers)
                html = page_html(path, me=me, api_base=api_base_for_page(self.headers),
                                 ws_url=ws_url_for_page(self.headers),
                                 transport=transport)
                return self._send(200, html.encode("utf-8"))
            return self._send(404, b"not found")

        def _author_from_cookie(self):
            """Read the sender label from the local mock's test cookie."""
            cookie = self.headers.get("Cookie") or ""
            for part in cookie.split(";"):
                name, _, value = part.strip().partition("=")
                if name.strip() == "token" and value.strip():
                    return backend.sender_for_token(value.strip())
            return ""

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path not in ("/api/send", "/api/hello", "/api/leave",
                                   "/api/state", "/api/dump",
                                   "/api/conversations/send"):
                return self._send(404, b"not found")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, code=400)
            if parsed.path == "/api/hello":
                backend.hello(payload.get("author"))
                return self._json({"ok": True})
            if parsed.path == "/api/leave":
                backend.leave(payload.get("author"))
                return self._json({"ok": True})
            if parsed.path == "/api/state":
                backend.note_state(payload.get("author") or "?", payload)
                return self._json({"ok": True})
            if parsed.path == "/api/dump":
                backend.note_dump(payload.get("author") or "bot", payload.get("html"))
                return self._json({"ok": True})
            if parsed.path == "/api/conversations/send":
                # the mock page's HTTP write (the mock socket carries the echo)
                author = backend.sender_for_token(
                    payload.get("token"),
                    fallback=str(payload.get("author") or self._author_from_cookie()))
                message = backend.add(author,
                                      payload.get("content") or payload.get("text"),
                                      nonce=payload.get("nonce"))
                return self._json({"ok": True, "message": message})
            message = backend.add(payload.get("author"), payload.get("text"))
            return self._json({"ok": True, "message": message})

    return Handler


def start_server(backend: ChatBackend, *, host="127.0.0.1", port=0, log=print):
    """Start the mock site in a background thread; returns (server, base_url)."""
    handler = make_handler(backend, log=log)
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="mock-chitchat")
    thread.start()
    base_url = f"http://{host}:{server.server_address[1]}"
    try:
        log(f"[MockSite] serving the chitchat stand-in at {base_url}")
    except Exception:
        pass
    return server, base_url
