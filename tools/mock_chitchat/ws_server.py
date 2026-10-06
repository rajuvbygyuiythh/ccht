"""A stand-in chitchat socket server: Engine.IO v4 + Socket.IO over WebSocket.

Speaks the *same wire format* as the live site captured in the user's HAR:

    server → 0{"sid":"…","upgrades":[],"pingInterval":25000,"pingTimeout":20000,
                "maxPayload":1000000}
    client → 40{"release":"fe4859ad7dbcce70f8c68229cd37a5edbe8cc661"}
    server → 40{"sid":"…","pid":"…"}
    client → 42["presenceSync"]
    server → 42["onlineFriends",[]]
    server → 42["matchUpdate",{"match":{…},"inQueue":false}]
    server → 42["typing",{"userId":"…","conversationId":"…"}]
    server → 42["chatMessage",{"message":{"id":"…","conversationId":"…",
                "author":{"id":"…","username":"…"},"content":"…","type":"TEXT",…}}]
    server → 2                       (Engine.IO ping)
    client → 3                       (pong)

Everything is stdlib: the WebSocket handshake + RFC6455 frames are implemented
here, so the offline E2E test needs no extra packages and the mock can be
started with ``python3 -m tools.mock_chitchat.ws_server --port 0``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import socket
import string
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
RELEASE = "fe4859ad7dbcce70f8c68229cd37a5edbe8cc661"

#: Events a client may use to *send* a chat message.  The live capture only
#: shows received ``chatMessage`` frames, so the mock accepts every plausible
#: name and always answers with the canonical ``42["chatMessage", …]`` echo.
SEND_EVENTS = ("chatMessage", "sendMessage", "message", "sendChatMessage",
               "message:send", "chat:message")


def _nanoid(size: int = 21) -> str:
    alphabet = string.ascii_letters + string.digits + "_-"
    return "".join(random.choice(alphabet) for _ in range(size))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


# --------------------------------------------------------------------------- #
#  WebSocket plumbing (RFC6455, server side)
# --------------------------------------------------------------------------- #

class _WebSocket:
    """Minimal server-side WebSocket connection."""

    def __init__(self, sock: socket.socket, log=print):
        self.sock = sock
        self.log = log
        #: idle wake-up interval: a quiet peer must not look like a dead one
        self.idle_timeout = 5.0
        try:
            sock.settimeout(self.idle_timeout)
        except Exception:
            pass
        self.closed = False
        self._send_lock = threading.Lock()
        self._buffer = b""

    # -- handshake --------------------------------------------------------
    @staticmethod
    def accept(sock: socket.socket) -> Optional[Dict[str, str]]:
        """Read the HTTP upgrade request; answer 101. Returns the headers."""
        data = b""
        sock.settimeout(10.0)
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            data += chunk
            if len(data) > 65536:
                return None
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = head.split("\r\n")
        if not lines or "websocket" not in head.lower():
            sock.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return None
        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        key = headers.get("sec-websocket-key")
        if not key:
            sock.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return None
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        sock.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n")
        request_line = lines[0]
        headers["_request_line"] = request_line
        return headers

    # -- frames -----------------------------------------------------------
    def _raw_read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def recv(self) -> Tuple[int, bytes]:
        """Read one message, staying connected while the peer is just quiet.

        A socket.io peer sends nothing between heartbeats, so a read timeout is
        not a disconnect — only a closed socket is.
        """
        while True:
            try:
                return self._recv_frame()
            except (TimeoutError, socket.timeout):
                continue

    def _recv_frame(self) -> Tuple[int, bytes]:
        """Read one (possibly fragmented) message → (opcode, payload)."""
        while True:
            header = self._raw_read(2)
            fin = bool(header[0] & 0x80)
            opcode = header[0] & 0x0F
            masked = bool(header[1] & 0x80)
            length = header[1] & 0x7F
            if length == 126:
                length = int.from_bytes(self._raw_read(2), "big")
            elif length == 127:
                length = int.from_bytes(self._raw_read(8), "big")
            mask = self._raw_read(4) if masked else b""
            payload = self._raw_read(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            if opcode == 0x9:                     # ping → pong
                self.send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:                     # pong
                continue
            if opcode == 0x8:
                self.closed = True
                return 0x8, b""
            if not fin and opcode != 0:
                parts = [payload]
                while True:
                    cont_header = self._raw_read(2)
                    cont_fin = bool(cont_header[0] & 0x80)
                    cont_len = cont_header[1] & 0x7F
                    if cont_len == 126:
                        cont_len = int.from_bytes(self._raw_read(2), "big")
                    elif cont_len == 127:
                        cont_len = int.from_bytes(self._raw_read(8), "big")
                    cont_mask = self._raw_read(4) if (cont_header[1] & 0x80) else b""
                    part = self._raw_read(cont_len) if cont_len else b""
                    if cont_mask:
                        part = bytes(b ^ cont_mask[i % 4] for i, b in enumerate(part))
                    parts.append(part)
                    if cont_fin:
                        break
                payload = b"".join(parts)
            return opcode, payload

    def send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        if self.closed:
            return
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += length.to_bytes(2, "big")
        else:
            header.append(127)
            header += length.to_bytes(8, "big")
        with self._send_lock:
            try:
                self.sock.sendall(bytes(header) + payload)
            except OSError:
                self.closed = True

    def send_text(self, text: str) -> None:
        self.send_frame(str(text).encode("utf-8"))

    def send_json_event(self, event: str, payload: Any = None) -> None:
        parts = [json.dumps(event)]
        if payload is not None:
            parts.append(json.dumps(payload, separators=(",", ":")))
        self.send_text("42[" + ",".join(parts) + "]")

    def close(self) -> None:
        if not self.closed:
            try:
                self.send_frame(b"", opcode=0x8)
            except Exception:
                pass
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Socket.IO / Engine.IO server
# --------------------------------------------------------------------------- #

class ChitchatSocketServer:
    """Two-participant chat over socket.io, matching the captured frames."""

    def __init__(self, *, log: Callable[[str], None] = print,
                 ping_interval: float = 25.0, ping_timeout: float = 20.0,
                 token_map: Optional[Dict[str, str]] = None,
                 default_stranger: str = "Stranger42",
                 accept_socket_sends: bool = True,
                 on_event: Optional[Callable[[str, Any], None]] = None):
        self.log = log
        self.ping_interval = float(ping_interval)
        self.ping_timeout = float(ping_timeout)
        self.token_map = dict(token_map or {})
        self.default_stranger = default_stranger
        # Strict mode drops socket chat sends as a mock test contract so the
        # HTTP-adapter flow is exercised. The supplied live WS entry has no
        # client chat-send frame, but does not prove the real server rejects one
        # or establish that live writes use HTTP.
        self.accept_socket_sends = bool(accept_socket_sends)
        self.on_event = on_event
        self.conversation_id = "mock-" + _nanoid(10)
        self.messages: List[Dict[str, Any]] = []
        self.clients: List[Dict[str, Any]] = []
        # Who is part of the current match.  The real site keeps the conversation
        # participants visible in ``matchUpdate`` for as long as the match is
        # open, even while one side is momentarily offline, so the stand-in keeps
        # a roster next to the live connections.
        self.roster: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self.url = ""
        self.port = 0
        self.sent_to_client: List[Dict[str, Any]] = []   # debug evidence

    # -- lifecycle --------------------------------------------------------
    def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(8)
        self._server = server
        self.port = server.getsockname()[1]
        self.url = f"ws://{host}:{self.port}/socket.io/?EIO=4&transport=websocket"
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name="mock-socket-server")
        self._thread.start()
        try:
            self.log(f"[MockWS] socket.io stand-in listening on {self.url}")
        except Exception:
            pass
        return self.url

    def stop(self) -> None:
        with self._lock:
            clients = list(self.clients)
            self.clients = []
        for client in clients:
            try:
                client["ws"].close()
            except Exception:
                pass
        try:
            if self._server is not None:
                self._server.close()
        except Exception:
            pass

    def _accept_loop(self) -> None:
        while True:
            try:
                sock, _addr = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(sock,), daemon=True,
                             name="mock-socket-client").start()

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _cookie_value(cookie_header: str, name: str) -> str:
        for chunk in str(cookie_header or "").split(";"):
            if "=" in chunk:
                key, value = chunk.split("=", 1)
                if key.strip() == name:
                    return value.strip()
        return ""

    def _identify(self, headers: Dict[str, str]) -> Dict[str, str]:
        request = headers.get("_request_line", "")
        query = request.split("?", 1)[1].split(" ", 1)[0] if "?" in request else ""
        params = {}
        for part in query.split("&"):
            if "=" in part:
                key, value = part.split("=", 1)
                params[key] = value
        token = self._cookie_value(headers.get("cookie", ""), "token")
        username = params.get("as") or self.token_map.get(token) or ""
        if not username:
            username = f"{self.default_stranger}" if not token else f"user-{token[:6]}"
        # The capture shows the socket ``pid`` and the *profile* id are different
        # things (pid: 20-char nanoid, profile id: 24-char hex), so the stand-in
        # keeps them apart too.
        return {"username": username, "token": token,
                "id": _nanoid(24), "pid": _nanoid(20), "sid": _nanoid(20)}

    def broadcast(self, event: str, payload: Any) -> None:
        """Broadcast mock-backend events to connected socket-test clients."""
        self._broadcast(event, payload)

    def _broadcast(self, event: str, payload: Any) -> None:
        with self._lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client["ws"].send_json_event(event, payload)
                self.sent_to_client.append({"to": client["username"], "event": event,
                                            "ts": time.time()})
            except Exception:
                pass

    def _chat_message(self, sender: Dict[str, str], text: str,
                      extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        message = {
            "message": {
                "id": _nanoid(24),
                "conversationId": self.conversation_id,
                "author": {"id": sender["id"], "username": sender["username"],
                           "avatar": sender["id"], "badges": [],
                           "createdAt": _now_iso(),
                           "preferences": {"allowFriendRequests": True}},
                "content": str(text),
                "type": "TEXT",
                "attachments": [],
                "createdAt": _now_iso(),
                "status": "SENT",
                "flags": 0,
                "reactions": [],
            }
        }
        if extra:
            extra = dict(extra)
            if extra.pop("_drop_flags", False):
                message["message"].pop("flags", None)
            message["message"].update({k: v for k, v in extra.items() if v})
        with self._lock:
            self.messages.append(message["message"])
        return message

    def match_payload(self, closed: bool = False, closed_by: str = "") -> Dict[str, Any]:
        with self._lock:
            people = list(self.roster) or list(self.clients)
            participants = [{"profile": {"id": c["id"], "username": c["username"],
                                         "avatar": c["id"], "badges": [],
                                         "createdAt": _now_iso(),
                                         "preferences": {"allowFriendRequests": True}},
                             "userId": c["id"]}
                            for c in people]
        conversation = {
            "id": self.conversation_id,
            "participants": participants,
            "mediaAllowedBy": [],
            "category": "ENCOUNTER",
            "createdAt": _now_iso(),
            "updatedAt": _now_iso(),
        }
        if closed:
            conversation["closure"] = {"closed": True, "closeReason": "INTENTIONAL",
                                        "closedAt": _now_iso(), "closedBy": closed_by}
        with self._lock:
            conversation["messageCount"] = len(self.messages)
            if self.messages:
                conversation["lastMessage"] = self.messages[-1].get("id")
        return {"match": {"conversation": conversation,
                          "messageCount": conversation["messageCount"],
                          "lastMessage": conversation.get("lastMessage"),
                          "users": [{"userId": p["userId"], "inactive": False}
                                    for p in participants],
                          "closure": conversation.get("closure", {"closed": False}),
                          "paused": False},
                "inQueue": False}

    # -- per connection ---------------------------------------------------
    def _handle(self, sock: socket.socket) -> None:
        headers = _WebSocket.accept(sock)
        if headers is None:
            try:
                sock.close()
            except Exception:
                pass
            return
        ws = _WebSocket(sock, log=self.log)
        who = self._identify(headers)
        client = {"ws": ws, **who,
                  "cookie": headers.get("cookie", ""),
                  "user_agent": headers.get("user-agent", "")}
        with self._lock:
            self.clients.append(client)
            # Registering the connection joins the match (a reconnect after a
            # closure is a fresh match, hence the roster reset on closure below).
            # The backend client alone is a valid match: the real site pairs the
            # account over its session, the page socket is only a listener.
            self.roster = [c for c in self.roster
                           if c["username"] != client["username"]] + [client]
        self.log(f"[MockWS] {who['username']} connected "
                 f"(cookies={len([c for c in headers.get('cookie', '').split(';') if c.strip()])})")

        # Engine.IO handshake + Socket.IO namespace connect
        ws.send_text("0" + json.dumps({
            "sid": who["sid"], "upgrades": [],
            "pingInterval": int(self.ping_interval * 1000),
            "pingTimeout": int(self.ping_timeout * 1000),
            "maxPayload": 1000000}, separators=(",", ":")))

        ping_stop = threading.Event()

        def ping_loop():
            while not ping_stop.wait(max(0.2, self.ping_interval)):
                if ws.closed:
                    return
                ws.send_text("2")

        try:
            while not ws.closed:
                opcode, payload = ws.recv()
                if opcode == 0x8:
                    break
                text = payload.decode("utf-8", "replace")
                if text.startswith("40"):
                    ws.send_text("40" + json.dumps({"sid": who["sid"], "pid": who["pid"]},
                                                   separators=(",", ":")))
                    self.log(f"[MockWS] {who['username']} namespace connected "
                             f"(pid={who['id'][:8]}…)")
                    threading.Thread(target=ping_loop, daemon=True).start()
                    continue
                if text.startswith("41"):
                    break
                if text == "3":
                    continue        # Engine.IO pong
                if not text.startswith("42["):
                    continue
                try:
                    event, *rest = json.loads(text[2:])
                except Exception:
                    continue
                data = rest[0] if rest else None
                self._on_event(client, ws, event, data)
        except Exception as error:
            self.log(f"[MockWS] {who['username']} dropped: {type(error).__name__}: {error}")
        finally:
            ping_stop.set()
            with self._lock:
                if client in self.clients:
                    self.clients.remove(client)
            self._broadcast("matchUpdate", self.match_payload())
            ws.close()
            self.log(f"[MockWS] {who['username']} disconnected")

    def publish(self, message: Dict[str, Any]) -> None:
        """Mirror a mock HTTP-backend message onto the socket stand-in.

        The message log belongs to the mock HTTP backend; this broadcasts its
        event shape so both test clients hear a normal ``chatMessage``.
        """
        author = str(message.get("author") or "?")
        text = str(message.get("text") or "")
        if not text:
            return
        with self._lock:
            sender = next((c for c in self.clients if c["username"] == author), None)
            if sender is None:
                sender = {"id": _nanoid(24), "username": author}
            if any(str(m.get("id")) == str(message.get("id")) for m in self.messages):
                return                      # already mirrored (echo from the page)
            nonce = str(message.get("nonce") or "")
            node = {
                "id": _nanoid(24),
                "conversationId": self.conversation_id,
                "author": {"id": sender["id"], "username": author, "avatar": sender["id"],
                           "badges": [], "createdAt": _now_iso(),
                           "preferences": {"allowFriendRequests": True}},
                "content": text,
                "type": "TEXT",
                "attachments": [],
                "createdAt": _now_iso(),
                "status": "SENT",
                "reactions": [],
            }
            if nonce:
                node["nonce"] = nonce       # the real echo carries the client nonce
            else:
                node["flags"] = 0
            self.messages.append(node)
            payload = {"message": node}
        self._broadcast("chatMessage", payload)
        self._broadcast("matchUpdate", self.match_payload())

    def _on_event(self, client: Dict[str, Any], ws: _WebSocket,
                  event: str, data: Any) -> None:
        logger = self.log
        if self.on_event is not None:
            try:
                self.on_event(event, data)
            except Exception:
                pass
        logger(f"[MockWS] {client['username']} → {event} {json.dumps(data)[:120] if data is not None else ''}")

        if event == "presenceSync":
            ws.send_json_event("onlineFriends", [])
            # Both sides get the match as soon as somebody is present.
            self._broadcast("matchUpdate", self.match_payload())
            if len(self.clients) >= 2:
                other = [c for c in self.clients if c["username"] != client["username"]]
                for peer in other:
                    peer["ws"].send_json_event("typing", {
                        "userId": client["id"], "conversationId": self.conversation_id})
            return

        if event == "typing":
            self._broadcast("typing", {"userId": client["id"],
                                        "conversationId": self.conversation_id})
            return

        if event in SEND_EVENTS:
            if not self.accept_socket_sends:
                logger(f"[MockWS] {client['username']} sent {event} over the socket — "
                       f"ignored by strict mock mode (this is a test contract, not "
                       f"evidence about how the live server handles the event)")
                return
            text = ""
            if isinstance(data, str):
                text = data
            elif isinstance(data, dict):
                text = str(data.get("content") or data.get("text") or "")
                if not text and isinstance(data.get("message"), dict):
                    text = str(data["message"].get("content") or "")
            if not text:
                logger(f"[MockWS] {client['username']} sent {event} without a text payload "
                       f"— ignored")
                return
            extra = {}
            if isinstance(data, dict):
                for field in ("nonce", "type", "status"):
                    if data.get(field):
                        extra[field] = data[field]
            if extra.get("nonce"):
                extra["_drop_flags"] = True
            message = self._chat_message(client, text, extra=extra or None)
            # The real server echoes the message back to *everyone*, sender included.
            self._broadcast("chatMessage", message)
            return

        if event in ("skip", "endChat", "leave"):
            self._broadcast("matchUpdate", self.match_payload(closed=True,
                                                              closed_by=client["id"]))
            with self._lock:
                # The match is over: whoever is still connected starts fresh (a
                # new conversation id, like the real site hands out per match).
                self.roster = list(self.clients)
                self.conversation_id = "mock-" + _nanoid(10)
            return


def start_server(**kwargs) -> Tuple[ChitchatSocketServer, str]:
    server = ChitchatSocketServer(**kwargs)
    url = server.start()
    return server, url
