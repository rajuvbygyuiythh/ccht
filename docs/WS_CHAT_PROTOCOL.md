# chitchat.gg chat WebSocket — captured protocol (v2.8)

Source: a DevTools **WebSocket capture of a real chitchat.gg chat** (HAR export +
Network screenshots, brought in by the user on 2026-10-06).  Everything below is
what the capture shows; items marked *inferred* are the parts the capture cannot
prove and which the bot therefore verifies at runtime instead of trusting.

## The socket

| | |
|---|---|
| URL | `wss://api.chitchat.gg/socket.io/?EIO=4&transport=websocket` |
| Origin | `https://app.chitchat.gg` |
| Transport | Engine.IO **v4** + Socket.IO (namespace `/`) |
| Page | `https://app.chitchat.gg/chat/new/<conversationId>` |
| Auth | the account's session cookies (`token`, `_pk_id.*`, `mock_cc_session` on the stand-in) |

## Frames, in order

| Direction | Frame | Meaning |
|-----------|-------|---------|
| server → client | `0{"sid":"…","upgrades":[],"pingInterval":25000,"pingTimeout":20000,"maxPayload":1000000}` | Engine.IO handshake |
| client → server | `40{"release":"fe4859ad7dbcce70f8c68229cd37a5edbe8cc661"}` | Socket.IO connect (the site's release id) |
| server → client | `40{"sid":"…","pid":"…"}` | namespace connected — `pid` is the **session id**, *not* a profile id (see below) |
| client → server | `42["presenceSync"]` | joined the presence channel |
| server → client | `42["onlineFriends",[]]` | friends list (empty in the capture) |
| server → client | `42["matchUpdate",{…}]` | the match/conversation the account is in |
| server → client | `42["typing",{"userId":"…","conversationId":"…"}]` | the other side is typing |
| server → client | `42["chatMessage",{"message":{…}}]` | a chat message (see below) |
| server → client | `2` / client → server `3` | Engine.IO ping/pong (every ~25 s) |

`matchUpdate` payload (capture):

```json
{"match": {"conversation": {
     "id": "6a9c69939f61be3d78cf3d9a",
     "participants": [{"profile": {"id": "…", "username": "sadia.6.7",
                                   "avatar": "…", "badges": [],
                                   "createdAt": "…",
                                   "preferences": {"allowFriendRequests": true}},
                       "userId": "…"}],
     "mediaAllowedBy": [], "category": "ENCOUNTER",
     "createdAt": "…", "updatedAt": "…"},
   "users": [{"userId": "…", "inactive": false}],
   "closure": {"closed": false}, "paused": false},
 "inQueue": false}
```

`chatMessage` payload (capture):

```json
{"message": {"id": "…", "conversationId": "6a9c69939f61be3d78cf3d9a",
             "author": {"id": "…", "username": "Stranger42", "avatar": "…",
                        "badges": [], "createdAt": "…",
                        "preferences": {"allowFriendRequests": true}},
             "content": "hey there!", "type": "TEXT", "attachments": [],
             "createdAt": "…", "status": "SENT", "nonce": "…", "flags": 0,
             "reactions": []}}
```

Ending a chat (capture): another `matchUpdate` whose conversation carries

```json
"closure": {"closed": true, "closeReason": "INTENTIONAL", "closedBy": "<profile id>"}
```

## Who am I? (the capture corrects two easy mistakes)

*The socket ``pid`` is a session id.* In the capture it is a 20-char nanoid
(`bcLYPx5BjAjHVzzBAbdt`) while the account's *profile* id in `matchUpdate` is a
24-char hex value (`6ac278aff4b9c168f1a77460`).  They never match, so the account
cannot be found by comparing `pid` with a participant.  The bot therefore learns
its own identity from, in order:

1. the page (`browser/chat_reader.py` → `myUsername`), i.e. what the app shows for
   the logged-in account — `tools/ws_chat.py --identity` stores it once;
2. its **own echo**: the first message we send comes back with our author id, and
   that id is remembered (`ChatWebSocket.my_id`);
3. the **nonce**: the capture shows our own messages coming back carrying
   `nonce` (the client-generated id) and **no** `flags`, while the other side's
   messages carry `flags: 0` and no `nonce`.  A matching nonce is therefore the
   most exact "this one is my echo" signal — and the bot now sends a nonce with
   every message it emits.

`matchUpdate` also carries `messageCount` and `lastMessage` (kept in the log), and
the closure carries `closedBy` — a *profile* id, e.g. the stranger who pressed
SKIP.

## Proving a chat ran on the socket alone

The conversation *is* the socket, but a bug could silently hide behind the DOM
fallback.  ``tools/e2e_mock_chat.py`` therefore has a WS-only mode:

```bash
python3 tools/e2e_mock_chat.py --transport ws --bot-page-blind --debug \
        --artifacts /home/user/ws_e2e_artifacts --log /home/user/demo_ws_chat.log
```

* the bot's page is served a **blind DOM** — the user's messages are never handed
  to that context, so they cannot be read from the page at all;
* the bot's page opens **no** socket, so the only socket the account has is the
  backend client (``tools/session_chat.py --transport ws``);
* the user is a second real browser, connected to the same stand-in with a real
  browser WebSocket;
* the verdict fails if the reply went through the DOM fallback (``(dom fallback)``
  in the log) or if any user text shows up in the bot page's DOM dumps.

A passing run therefore proves: detection came from ``chatMessage`` events, the
reply went out **through the site's own send request**, the server echo confirmed
it, and the cookies of the restored session authenticated both the socket and the
out-of-band request (the stand-in identifies the account by its cookie jar —
exactly how the real endpoint does).

The stand-in is **strict**: ``accept_socket_sends=False`` makes it ignore chat
frames arriving over the socket, the way the capture shows the real endpoint
behaving.  So a run can only pass by using the real mechanism:

| Log line to look for | What it proves |
|----------------------|----------------|
| ``sent chatMessage over the socket — ignored`` | the socket path is blocked, like the real site |
| ``learned the site's own send request: POST …`` | the page's own write was captured, field names and all |
| ``http send -> POST http://… status=200`` | the reply was written from the backend |
| ``sent over HTTP (200)`` + ``delivery confirmed by the server echo (http/200)`` | the server accepted it *and* the echo came back with our nonce |

## Sending a message — the socket does **not** carry it (capture-proven)

This is the correction round 3 is built on.  The full capture lists **every**
client → server frame:

```
40{"release":"fe4859ad…"}      <- the socket.io connect
42["presenceSync"]   x3
3                    x3        <- engine.io pongs
```

Nothing else.  Five messages in that capture are *ours* ("hi", "gd", "u", "21",
"u") — and they appear as **received** ``chatMessage`` frames carrying the
client's ``nonce`` (``cwTmmxJoKguG3QiZzZgW5``, ``BHX1CEXO7HUSCgOuImWot``, …).  A
message cannot be sent by a frame that was never sent, so:

> the site writes the message **out of band (HTTP)** and only *listens* on the
> socket for the echo — an emit on this socket is not how the site sends.

A backend bot therefore has to write the message the same way.  Our bot does:

1. **learn the request** — a sniffer watches the page's own network traffic
   (``core.chat_ws.SendRequestSniffer``) and stores the very request the app used:
   method, URL, body, and which body fields hold the text, the nonce and the
   conversation id (``build_send_template``).  It is persisted to
   ``data/ws_config.json`` (``http_send``), so later runs start straight from the
   backend.
2. **replay it** — ``send_template_request()`` posts the same request from the
   backend with the account's session cookies, inserting our text, our nonce and
   the conversation id (``ChatWebSocket._send_via_http``).
3. **confirm on the socket** — the reply is only reported as delivered when the
   server's echo of *our* message arrives over the socket (matched by nonce),
   exactly the way the capture shows it.
4. if no request has been learned yet, the reply falls back to typing in the page
   (which is itself what teaches the sniffer the request) — and the socket emit is
   the last resort, which the real endpoint ignores.

```bash
python tools/ws_chat.py --show-config          # what is already known
python tools/ws_chat.py --forget-config        # forget it (learn again fresh)
python tools/session_chat.py --account EMAIL --transport ws --minutes 30
```

`--no-http-send` (CLI) ignores the learned request and forces the socket emit,
for experiments.

## The one unknown: the *send* event name

The capture contains only handshake / `presenceSync` / pong frames from the
client — the user's own message was typed **before** the capture started, so the
client frame that sends a message is not in it.  The bot never guesses blindly:

1. `data/ws_config.json` (`send_event`) or `--ws-send-event NAME` — if known;
2. `tools/ws_chat.py --probe` reads the site's own chat bundle in the browser,
   ranks `socket.emit("…")` names and stores the first one;
3. otherwise the first candidate (`chatMessage`, then `sendMessage`, `message`,
   `sendChatMessage`, …) is used and **delivery is confirmed by the server
   echo**: the real backend broadcasts a `chatMessage` back to its sender
   (*inferred*, confirmed with the stand-in), so a confirmed echo is the proof
   that the right event name was used;
4. if nothing is confirmed, the caller can still fall back to typing in the page
   (`run_account_ws(..., dom_fallback=True)`, the default in the CLI).

To nail the name from a capture: open DevTools → Network → WS, click the socket,
send one message, then export the HAR and run

```bash
python3 tools/ws_chat.py --har ws.txt     # prints the client frames it found
```

## Replaying a capture offline (does the bot understand *this* recording?)

```bash
python3 tools/ws_chat.py --replay ws.txt                 # what the bot would see
python3 tools/ws_chat.py --replay ws.txt --username sadia.6.7
python3 tools/ws_chat.py --identity --account EMAIL      # learn the username once
```

`--replay` feeds the captured server frames through the exact parsing the live
bot uses and prints the conversation, the participants, which messages would be
answered, the closure, and the event counts — no browser, no network.

Both *file styles* work: a DevTools HAR export (``Copy all as HAR``) **and** a
plain text ``ws.txt`` dump (the WS pane copied/pasted, with ``↑``/``↓`` arrows,
``send``/``receive`` words, or ``{"type": "send", "data": "42[… ]"}`` objects).
A line without any direction marker is classified from the frame itself — only
events the client alone emits (``presenceSync``, ``sendMessage``, ``skip``…) count
as outgoing, everything else (``chatMessage``, ``typing``, ``matchUpdate``…) is
incoming.

To prove the *whole* path offline, the test suite keeps two copies of one
capture: ``test_fixtures/chitchat_ws_capture.json`` (HAR) and
``test_fixtures/chitchat_ws_dump.txt`` (plain dump). Both must split and replay
identically (20 received / 7 sent, 2 incoming / 5 ours).  The test
suite replays `test_fixtures/chitchat_ws_capture.json`, a fixture with the frame
sequence and payload shapes of the real capture (ids replaced): it must report
`2 incoming / 5 own`, `messages=7`, `last=MSG_0007` and the closure.

Working assumptions the capture *does* prove (all covered by tests):

| Fact from the capture | Where the bot uses it |
|-----------------------|------------------------|
| `pid` ≠ profile id | identity comes from the page/echo, never from `pid` |
| own messages come back with `nonce`, no `flags` | echo/dedupe + "is this mine" |
| the other side's messages have `flags: 0` | "this is an incoming SMS" |
| `onlineFriends` answers `presenceSync` | connect handshake |
| closure: `matchUpdate` + `conversation.closure{closed, closeReason, closedBy}` | ending a chat cleanly |
| Engine.IO ping `2` every 25 s → answer `3` | the connection stays alive |
| no client chat frame at all in the capture | replies go out over the site's own HTTP request, the socket only hears the echo |
| the ping interval (25 s) is longer than a short read timeout | an idle socket must not be torn down (`SocketIOClient` treats silence below `pingInterval + pingTimeout` as normal) |

## Where the bot uses it

| File | Role |
|------|------|
| `core/socketio.py` | dependency-free Engine.IO v4 + Socket.IO client (stdlib `socket`/`ssl`), auto-reconnect, heartbeat, frame log |
| `core/chat_ws.py` | the chat layer: cookies → `Cookie` header, `matchUpdate`/`chatMessage` parsing, own-message echo detection, send+confirm, bundle probe helpers |
| `tools/ws_chat.py` | CLI: `--list`, `--listen`, `--send`, `--reply` (`ChatRuleBot` over the socket), `--probe`, `--har`, `--bundle` |
| `tools/session_chat.py --transport ws` | the backend runner: session in the browser, conversation over the socket |
| `tools/mock_chitchat/ws_server.py` | the offline stand-in that speaks exactly these frames (used by `test_ws_transport.py` and the E2E test) |

Confirmed live-path behaviour (offline E2E, `--transport ws`): the restored
session's cookies are sent on the socket handshake, the account is identified by
them, incoming messages arrive as `chatMessage` events, replies are emitted and
confirmed by the echo, and the user's browser renders the reply — see
`/home/user/WS_CHAT_SUMMARY.md`.
