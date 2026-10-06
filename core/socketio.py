"""A dependency-free Engine.IO v4 + Socket.IO client (stdlib only).

chitchat.gg talks to ``wss://api.chitchat.gg/socket.io/?EIO=4&transport=websocket``
(see ``docs/WS_CHAT_PROTOCOL.md`` — captured from a real session).  The bot needs
that transport for two reasons:

* the incoming SMS can be read from ``chatMessage`` events instead of polling
  the DOM, and
* replies can be emitted from the backend without typing into the page.

Both directions require a WebSocket with the account's session cookies, so this
module implements the small part of the protocol we need on top of :mod:`socket`
+ :mod:`ssl` — no new dependency for the Windows build:

    server → 0{"sid":…,"pingInterval":25000,"pingTimeout":20000,…}
    client → 40{"release":"…"}                (Socket.IO namespace connect)
    server → 40{"sid":…,"pid":…}
    client → 42["presenceSync"] / 42["sendMessage",{…}]
    server → 42["chatMessage",{…}]
    server → 2 → client answers 3             (Engine.IO heartbeat)

Everything is optional and fails soft: an unreachable socket raises/returns
False and the caller can fall back to the DOM automation.
"""

from __future__ import annotations

import base64
import json
import os
import random
import socket
import ssl
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def parse_ws_url(url: str) -> Tuple[str, int, str, bool, str]:
    """``wss://host:port/path?query`` → (host, port, path+query, tls, hostname)."""
    parsed = urlparse(str(url))
    scheme = (parsed.scheme or "wss").lower()
    tls = scheme in ("wss", "https")
    host = parsed.hostname or ""
    port = int(parsed.port or (443 if tls else 80))
    path = parsed.path or "/socket.io/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return host, port, path, tls, host


class SocketIOClient:
    """A reconnecting Socket.IO v4 client with a callback for every event."""

    def __init__(self, url: str, *,
                 headers: Optional[Dict[str, str]] = None,
                 on_event: Optional[Callable[[str, Any], None]] = None,
                 on_open: Optional[Callable[["SocketIOClient"], None]] = None,
                 on_close: Optional[Callable[["SocketIOClient"], None]] = None,
                 namespace_payload: Optional[Dict[str, Any]] = None,
                 log: Callable[[str], None] = print,
                 timeout: float = 20.0,
                 auto_reconnect: bool = True,
                 reconnect_max: float = 30.0,
                 connect_timeout: float = 15.0,
                 socket_factory: Optional[Callable[..., socket.socket]] = None):
        self.url = str(url)
        self.headers = {"User-Agent": _DEFAULT_UA, **(headers or {})}
        self.on_event = on_event
        self.on_open = on_open
        self.on_close = on_close
        self.namespace_payload = dict(namespace_payload or {"release": "unknown"})
        self.log = log
        self.timeout = float(timeout)
        self.auto_reconnect = bool(auto_reconnect)
        self.reconnect_max = float(reconnect_max)
        self.connect_timeout = float(connect_timeout)
        self.socket_factory = socket_factory or socket.create_connection

        self.sock: Optional[socket.socket] = None
        self.sid = ""
        self.pid = ""
        self.connected = False
        self.last_error = ""
        self.closed_by_user = False
        self._send_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected_event = threading.Event()
        self.event_counts: Dict[str, int] = {}
        self.server_frames: List[str] = []
        self.sent_frames: List[str] = []

    # ------------------------------------------------------------------ #
    #  public API
    # ------------------------------------------------------------------ #
    def start(self) -> "SocketIOClient":
        if self._thread and self._thread.is_alive():
            return self
        self.closed_by_user = False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="socketio-client",
                                        daemon=True)
        self._thread.start()
        return self

    def wait_connected(self, timeout: float = 20.0) -> bool:
        """Block until the Socket.IO namespace is connected."""
        return self._connected_event.wait(max(0.0, float(timeout)))

    def emit(self, event: str, payload: Any = None) -> bool:
        """Send ``42["event", payload]``. Returns False when not connected."""
        parts = [json.dumps(event)]
        if payload is not None:
            parts.append(json.dumps(payload, separators=(",", ":")))
        return self._send_text("42[" + ",".join(parts) + "]")

    def send_raw(self, text: str) -> bool:
        return self._send_text(text)

    def close(self, reason: str = "client stop") -> None:
        self.closed_by_user = True
        self._stop.set()
        try:
            if self.sock is not None:
                self._send_frame(b"", opcode=0x8)
                self.sock.close()
        except Exception:
            pass
        self.connected = False
        self.log(f"[WS] closed ({reason})")

    # ------------------------------------------------------------------ #
    #  connection handling
    # ------------------------------------------------------------------ #
    def _connect(self) -> None:
        host, port, path, tls, server_name = parse_ws_url(self.url)
        raw = self.socket_factory((host, port), self.connect_timeout)
        raw.settimeout(max(5.0, self.timeout))
        if tls:
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=server_name)
        self.sock = raw
        self._handshake(host, port, path)
        self.log(f"[WS] websocket upgraded → {self.url.split('?')[0]}")

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        headers = {
            "Host": f"{host}:{port}",
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Version": "13",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            **self.headers,
        }
        request = f"GET {path} HTTP/1.1\r\n" + "".join(
            f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n"
        self.sock.sendall(request.encode("latin-1"))

        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake closed by the server")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0] if lines else ""
        if "101" not in status:
            raise ConnectionError(f"websocket upgrade refused: {status}")
        expected = base64.b64encode(
            hashlib_sha1((key + WS_GUID).encode())).decode()
        accept = ""
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                if name.strip().lower() == "sec-websocket-accept":
                    accept = value.strip()
        if accept and accept != expected:
            raise ConnectionError("bad Sec-WebSocket-Accept (proxy interference?)")
        self._buffer = rest

    # ------------------------------------------------------------------ #
    #  frame level
    # ------------------------------------------------------------------ #
    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def _recv_message(self) -> Tuple[int, bytes]:
        while True:
            header = self._read_exact(2)
            fin = bool(header[0] & 0x80)
            opcode = header[0] & 0x0F
            length = header[1] & 0x7F
            masked = bool(header[1] & 0x80)
            if length == 126:
                length = int.from_bytes(self._read_exact(2), "big")
            elif length == 127:
                length = int.from_bytes(self._read_exact(8), "big")
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                return 0x8, b""
            if not fin and opcode != 0:
                parts = [payload]
                while True:
                    more = self._read_exact(2)
                    more_fin = bool(more[0] & 0x80)
                    more_len = more[1] & 0x7F
                    if more_len == 126:
                        more_len = int.from_bytes(self._read_exact(2), "big")
                    elif more_len == 127:
                        more_len = int.from_bytes(self._read_exact(8), "big")
                    more_mask = self._read_exact(4) if (more[1] & 0x80) else b""
                    part = self._read_exact(more_len) if more_len else b""
                    if more_mask:
                        part = bytes(b ^ more_mask[i % 4] for i, b in enumerate(part))
                    parts.append(part)
                    if more_fin:
                        break
                payload = b"".join(parts)
            return opcode, payload

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> bool:
        sock = self.sock
        if sock is None:
            return False
        length = len(payload)
        header = bytearray([0x80 | opcode])
        mask_bit = 0x80
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(mask_bit | 127)
            header += length.to_bytes(8, "big")
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            try:
                sock.sendall(bytes(header) + masked)
                return True
            except OSError as error:
                self.last_error = f"{type(error).__name__}: {error}"
                return False

    def _send_text(self, text: str) -> bool:
        if not self.connected:
            return False
        ok = self._send_frame(str(text).encode("utf-8"))
        if ok:
            if len(self.sent_frames) < 500:
                self.sent_frames.append(str(text))
        return ok

    # ------------------------------------------------------------------ #
    #  protocol level
    # ------------------------------------------------------------------ #
    def _handle_text(self, text: str) -> None:
        if len(self.server_frames) < 500:
            self.server_frames.append(text)
        if text.startswith("0"):
            try:
                handshake = json.loads(text[1:])
            except Exception:
                handshake = {}
            self.sid = str(handshake.get("sid") or "")
            ping_ms = int(handshake.get("pingInterval") or 25000)
            self.log(f"[WS] engine.io open: sid={self.sid} ping={ping_ms}ms "
                     f"upgrades={handshake.get('upgrades')}")
            payload = json.dumps(self.namespace_payload, separators=(",", ":"))
            self._send_frame(f"40{payload}".encode("utf-8"))
            return
        if text == "2":
            self._send_frame(b"3")
            if len(self.sent_frames) < 500:
                self.sent_frames.append("3")     # heartbeat pong
            return
        if text.startswith("40"):
            try:
                ack = json.loads(text[2:] or "{}")
            except Exception:
                ack = {}
            self.pid = str(ack.get("pid") or "")
            self.connected = True
            self._connected_event.set()
            self.log(f"[WS] socket.io connected: sid={ack.get('sid')} "
                     f"pid={self.pid}")
            if self.on_open is not None:
                try:
                    self.on_open(self)
                except Exception as error:
                    self.log(f"[WS] on_open handler failed: {error}")
            return
        if text.startswith("41"):
            self.log("[WS] server closed the namespace")
            self.connected = False
            return
        if text.startswith("42"):
            try:
                event, *rest = json.loads(text[2:])
            except Exception as error:
                self.log(f"[WS] unparsable event frame: {error}")
                return
            payload = rest[0] if rest else None
            self.event_counts[str(event)] = self.event_counts.get(str(event), 0) + 1
            if self.on_event is not None:
                try:
                    self.on_event(str(event), payload)
                except Exception as error:
                    self.log(f"[WS] on_event({event}) handler failed: {error}")
            return
        if text.startswith("1"):
            self.log("[WS] engine.io close requested by the server")
            self.connected = False

    # ------------------------------------------------------------------ #
    #  run loop
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._buffer = b""
                self._connected_event.clear()
                self._connect()
                backoff = 1.0
                while not self._stop.is_set():
                    opcode, payload = self._recv_message()
                    if opcode == 0x8:
                        raise ConnectionError("server closed the websocket")
                    if opcode in (0x1, 0x2):
                        self._handle_text(payload.decode("utf-8", "replace"))
            except Exception as error:
                self.last_error = f"{type(error).__name__}: {error}"
                was_connected = self.connected
                self.connected = False
                if was_connected or not self._stop.is_set():
                    self.log(f"[WS] connection lost: {self.last_error}")
                try:
                    if self.sock is not None:
                        self.sock.close()
                except Exception:
                    pass
                self.sock = None
                if was_connected and self.on_close is not None:
                    try:
                        self.on_close(self)
                    except Exception:
                        pass
            if self.closed_by_user or self._stop.is_set() or not self.auto_reconnect:
                break
            self.log(f"[WS] reconnecting in {backoff:.1f}s …")
            if self._stop.wait(backoff):
                break
            backoff = min(self.reconnect_max, backoff * 2)
        self.connected = False
        self._connected_event.clear()

    # ------------------------------------------------------------------ #
    #  helpers
    # ------------------------------------------------------------------ #
    def stats(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "connected": self.connected,
            "sid": self.sid,
            "pid": self.pid,
            "events": dict(sorted(self.event_counts.items(),
                                  key=lambda item: -item[1])),
            "frames_in": len(self.server_frames),
            "frames_out": len(self.sent_frames),
            "last_error": self.last_error,
        }


def hashlib_sha1(data: bytes) -> bytes:
    import hashlib
    return hashlib.sha1(data).digest()


__all__ = ["SocketIOClient", "parse_ws_url", "WS_GUID"]
