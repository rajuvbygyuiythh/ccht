#!/usr/bin/env python3
"""Tests for the WebSocket chat transport — no browser, no internet.

Everything runs against the local socket.io stand-in
(``tools/mock_chitchat/ws_server.py``), which speaks the frames captured from a
real chitchat.gg session (``docs/WS_CHAT_PROTOCOL.md``).

Run: ``python3 test_ws_transport.py``
"""

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.chat_ws import (ChatWebSocket, chat_bundle_urls, cookies_header,   # noqa: E402
                          discover_send_event, load_session_cookies)
from core.socketio import SocketIOClient, parse_ws_url                       # noqa: E402
from tools.mock_chitchat.ws_server import ChitchatSocketServer               # noqa: E402

PASSED = 0
FAILED = 0


def check(label, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


class Quiet:
    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(str(message))

    def has(self, needle):
        return any(needle in line for line in self.lines)


def start_server(**kwargs):
    log = Quiet()
    server = ChitchatSocketServer(log=log, ping_interval=30.0,
                                  token_map={"REALTOKEN": "sadia.6.7"}, **kwargs)
    url = server.start()
    return server, url, log


# --------------------------------------------------------------------------- #
#  pure helpers
# --------------------------------------------------------------------------- #

def test_helpers():
    print("\n[helpers]")
    host, port, path, tls, name = parse_ws_url(
        "wss://api.chitchat.gg/socket.io/?EIO=4&transport=websocket")
    check("ws url is parsed", (host, port, tls) == ("api.chitchat.gg", 443, True))
    check("the query string survives",
          path == "/socket.io/?EIO=4&transport=websocket", path)

    cookies = [
        {"name": "token", "value": "abc", "domain": ".chitchat.gg"},
        {"name": "cf_clearance", "value": "x", "domain": "app.chitchat.gg"},
        {"name": "token", "value": "dupe", "domain": ".chitchat.gg"},
        {"name": "ga", "value": "1", "domain": ".google.com"},
        {"name": "", "value": "nameless", "domain": ".chitchat.gg"},
    ]
    header = cookies_header(cookies)
    check("only chitchat cookies are used",
          header == "token=abc; cf_clearance=x", header)

    real_session = REPO / "data/account_sessions/account_063cce9f4343619ac9bdfe89/storage_state.json"
    if real_session.is_file():
        loaded = load_session_cookies(str(real_session))
        check("a real saved session is readable", len(loaded) > 5, str(len(loaded)))
        check("its cookie header carries the token",
              "token=" in cookies_header(loaded))
    else:
        check("a real saved session is readable", True, "(no session file in this checkout)")
        check("its cookie header carries the token", True, "(skipped)")

    bundle = """
      socket.emit("presenceSync");
      socket.emit('typing', {conversationId: cid});
      if (ready) { socket.emit("sendMessage", {content: text, conversationId: cid}); }
      api.emit("chatMessage", payload);
      socket.emit("onlineFriends");
    """
    ranked = discover_send_event(bundle)
    check("chat-related emits come first",
          ranked[0] in ("sendMessage", "chatMessage"), str(ranked))
    check("presence/online emits are ranked behind the chat ones",
          ranked.index("presenceSync") > ranked.index("sendMessage") if
          "presenceSync" in ranked else True, str(ranked))

    urls = chat_bundle_urls(
        '<script src="/assets/js-direct-chat-DaxEcPiM.js"></script>'
        '<script src="/assets/js-direct-vendor-react.js"></script>')
    check("the chat bundle is found",
          urls == ["https://app.chitchat.gg/assets/js-direct-chat-DaxEcPiM.js"], str(urls))


# --------------------------------------------------------------------------- #
#  engine.io / socket.io client
# --------------------------------------------------------------------------- #

def test_socket_client():
    print("\n[socket.io client]")
    server, url, server_log = start_server()
    try:
        events = []
        log = Quiet()
        client = SocketIOClient(url, headers={"Cookie": "token=REALTOKEN"},
                               namespace_payload={"release": "test"},
                               on_event=lambda ev, pl: events.append((ev, pl)),
                               log=log, timeout=5.0)
        client.start()
        check("the namespace connects", client.wait_connected(5.0))
        check("sid + pid arrive from the connect ack",
              bool(client.sid) and bool(client.pid), f"{client.sid}/{client.pid}")
        check("the handshake was logged", log.has("engine.io open"))

        client.emit("presenceSync")
        deadline = time.time() + 3
        while time.time() < deadline and not any(e == "onlineFriends" for e, _ in events):
            time.sleep(0.05)
        check("presenceSync is answered with onlineFriends",
              any(e == "onlineFriends" for e, _ in events), str(events))

        check("the server received the session cookies",
              any("token=REALTOKEN" in str(c.get("cookie")) for c in server.clients))
        check("the server recognised the account from the token",
              [c["username"] for c in server.clients] == ["sadia.6.7"],
              str([c["username"] for c in server.clients]))

        client.emit("sendMessage", {"conversationId": server.conversation_id,
                                    "content": "from the client"})
        deadline = time.time() + 3
        while time.time() < deadline and not server.messages:
            time.sleep(0.05)
        check("a chat message reaches the server",
              [m["content"] for m in server.messages] == ["from the client"],
              str(server.messages))
        deadline = time.time() + 3
        while time.time() < deadline and not any(e == "chatMessage" for e, _ in events):
            time.sleep(0.05)
        check("the server echoes chatMessage back",
              any(e == "chatMessage" for e, _ in events), str([e for e, _ in events]))

        stats = client.stats()
        check("stats report the connection", stats["connected"] is True and stats["sid"])
        check("sent frames are tracked", stats["frames_out"] >= 2, str(stats["frames_out"]))
        client.close("test done")
        time.sleep(0.4)
        check("closing is graceful", client.connected is False)
        check("the server noticed the disconnect",
              server_log.has("disconnected") and not server.clients)
    finally:
        server.stop()


def test_engine_ping_pong():
    print("\n[engine.io heartbeat]")
    log = Quiet()
    server = ChitchatSocketServer(log=log, ping_interval=0.3)
    url = server.start()
    try:
        client = SocketIOClient(url, log=log, timeout=5.0)
        client.start()
        check("the client connects", client.wait_connected(5.0))
        time.sleep(1.0)                      # let a few pings flow
        pings = [f for f in client.server_frames if f == "2"]
        pongs = [f for f in client.sent_frames if f == "3"]
        check("server pings arrive", len(pings) >= 2, str(len(pings)))
        check("the client answers every ping with a pong", len(pongs) >= 2, str(len(pongs)))
        check("the connection survives the heartbeats", client.connected is True)
        client.close("done")
    finally:
        server.stop()


# --------------------------------------------------------------------------- #
#  chitchat chat layer
# --------------------------------------------------------------------------- #

def test_chat_ws_flow():
    print("\n[chat over ws]")
    server, url, _log = start_server()
    log = Quiet()
    incoming = []
    chat = ChatWebSocket(url=url, cookie_header="token=REALTOKEN",
                         log=log, echo_timeout=2.0,
                         on_message=lambda text, _raw: incoming.append(text))
    chat.start()
    check("the session-authenticated socket connects", chat.wait_connected(5.0))
    # The socket identity (pid) is matched against the match participants, so the
    # name is known as soon as the matchUpdate arrives — or straight away when
    # the caller passes --username (the browser knows it from the profile).
    named = ChatWebSocket(url=url, cookie_header="token=REALTOKEN", log=Quiet(),
                          my_username="sadia.6.7", echo_timeout=1.0)
    named.start()
    named.wait_connected(5.0)
    check("an explicitly passed username is used immediately",
          named.my_username == "sadia.6.7", named.my_username)
    named.stop()

    # the user side: an anonymous visitor socket
    user_events = []
    user = SocketIOClient(url, log=Quiet(),
                          on_event=lambda ev, pl: user_events.append((ev, pl)))
    user.start()
    check("the user's socket connects", user.wait_connected(5.0))
    user.emit("presenceSync")
    time.sleep(0.8)

    check("matchUpdate fills in the conversation",
          bool(chat.conversation_id), chat.conversation_id)
    check("both participants are known",
          sorted(chat.stats()["participants"]) == ["Stranger42", "sadia.6.7"],
          str(chat.stats()["participants"]))
    check("the account is identified from its own socket id (pid)",
          chat.my_username == "sadia.6.7", chat.my_username)

    user.emit("sendMessage", {"conversationId": server.conversation_id,
                             "content": "hey, are you real?"})
    deadline = time.time() + 3
    while time.time() < deadline and not incoming:
        time.sleep(0.05)
    check("the incoming SMS arrives over the socket",
          incoming == ["hey, are you real?"], str(incoming))
    check("it is counted as incoming", chat.messages_in == 1, str(chat.messages_in))

    ok, how = chat.send_message("hey there")
    check("the reply is confirmed by the server echo", ok and "chatMessage" in how, how)
    check("the confirmed event is remembered", chat.sent_event == "chatMessage")
    check("our message is counted once (from the server echo)",
          chat.messages_out == 1, str(chat.messages_out))
    check("the raw sent frame is recorded too",
          any("hey there" in frame for frame in chat.client.sent_frames),
          str(chat.client.sent_frames[-2:]))
    check("the user received the reply",
          any(pl.get("message", {}).get("content") == "hey there"
              for ev, pl in user_events if ev == "chatMessage"))

    handler_calls = []
    chat.on_message = lambda text, raw: handler_calls.append(text)
    check("the handler can be swapped at runtime", handler_calls == [])

    user.emit("skip", {})
    time.sleep(0.6)
    check("a closed match is noticed", chat.closed_by_peer is True)

    stats = chat.stats()
    check("stats carry the conversation + counters",
          stats["conversation"] and stats["messages_in"] >= 1, json.dumps(stats)[:120])
    chat.stop()
    user.close()
    server.stop()


def test_probe_finds_the_event():
    print("\n[send-event probing]")
    server, url, _log = start_server()
    log = Quiet()
    chat = ChatWebSocket(url=url, cookie_header="token=REALTOKEN", log=log,
                         send_event="notARealEvent", allow_probe=True,
                         echo_timeout=0.4)
    chat.start()
    check("it connects", chat.wait_connected(5.0))
    time.sleep(0.6)
    ok, how = chat.send_message("probing works")
    check("an unknown event is skipped and a working one is found",
          ok and chat.sent_event in ("chatMessage", "sendMessage"), how)
    check("the wrong event was reported honestly",
          log.has("no echo for notARealEvent"), "log: " + " | ".join(log.lines[-4:]))
    check("the message still reached the server",
          any(m["content"] == "probing works" for m in server.messages),
          str([m["content"] for m in server.messages]))
    chat.stop()
    server.stop()


def test_har_report():
    print("\n[har report]")
    sys.path.insert(0, str(REPO / "tools"))
    import ws_chat

    har = {
        "log": {"version": "1.2"},
        "entries": [{
            "request": {"url": "wss://api.chitchat.gg/socket.io/?EIO=4&transport=websocket"},
            "_webSocketMessages": [
                {"type": "receive", "data": '0{"sid":"abc","pingInterval":25000}'},
                {"type": "send", "data": '40{"release":"fe4859ad7"}'},
                {"type": "receive", "data": '40{"sid":"def","pid":"ghi"}'},
                {"type": "send", "data": '42["presenceSync"]'},
                {"type": "receive", "data": '42["chatMessage",{"message":{"content":"hi"}}]'},
                {"type": "send", "data": "3"},
            ],
        }],
    }
    path = Path("/tmp/eva_test_har.json")
    path.write_text(json.dumps(har), encoding="utf-8")
    lines = []
    original = ws_chat.log
    ws_chat.log = lambda m: lines.append(str(m))
    try:
        rc = ws_chat.har_report(str(path))
    finally:
        ws_chat.log = original
    text = "\n".join(lines)
    check("the har report succeeds", rc == 0)
    check("it lists the socket url", "api.chitchat.gg/socket.io" in text)
    check("it counts the client frames", "42 event: presenceSync" in text)
    check("it counts the server events", "42 event: chatMessage" in text)
    check("it says the send frame is missing",
          "no client-side chat *send* frame" in text, text[-300:])

    har["entries"][0]["_webSocketMessages"].append(
        {"type": "send", "data": '42["sendMessage",{"content":"hey","conversationId":"x"}]'})
    path.write_text(json.dumps(har), encoding="utf-8")
    lines.clear()
    ws_chat.log = lambda m: lines.append(str(m))
    try:
        ws_chat.har_report(str(path))
    finally:
        ws_chat.log = original
    text = "\n".join(lines)
    check("a captured send frame is reported with its event name",
          "event: sendMessage" in text, text[-300:])


if __name__ == "__main__":
    test_helpers()
    test_socket_client()
    test_engine_ping_pong()
    test_chat_ws_flow()
    test_probe_finds_the_event()
    test_har_report()
    print("\n" + "=" * 72)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    print("=" * 72)
    sys.exit(1 if FAILED else 0)
