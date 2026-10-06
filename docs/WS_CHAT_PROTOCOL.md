# chitchat.gg chat transport — captured protocol (v2.8)

This note describes the DevTools capture shape represented by
`test_fixtures/chitchat_ws_capture.json`, plus what the current client and mock
stand-in do with it. The fixture replaces account/conversation identifiers with
test values. The capture establishes the WebSocket receive protocol; it does
**not** contain an outgoing chat-message frame.

## Site and session flow

The observed chat page is `https://app.chitchat.gg/chat/new/<conversationId>`.
The wider app flow is landing page → **Start Text Chat** →
`/start/new` (verification/queue) → chat route. A saved browser session restores
its cookie jar; the socket handshake uses the same session cookies. A valid
saved session avoids re-entering a password, but expired sessions still need
repair/re-authentication. Never commit local session state or learned request
configuration.

## Socket connection

| Property | Captured value / meaning |
|---|---|
| URL | `wss://api.chitchat.gg/socket.io/?EIO=4&transport=websocket` |
| Origin | `https://app.chitchat.gg` |
| Protocol | Engine.IO v4 + Socket.IO namespace `/` |
| Heartbeat | server ping `2`, client pong `3`; observed interval about 25 seconds (`pingTimeout` 20 seconds) |
| Authentication | saved session cookie jar; the socket `pid` is not the account profile id |

The frame sequence is direction-sensitive:

| Direction | Frame | Meaning |
|---|---|---|
| server → client | `0{"sid":"…","upgrades":[],"pingInterval":25000,"pingTimeout":20000,"maxPayload":1000000}` | Engine.IO open/handshake |
| client → server | `40{"release":"…"}` | Socket.IO connect (site release id) |
| server → client | `40{"sid":"…","pid":"…"}` | namespace connected |
| client → server | `42["presenceSync"]` | presence subscription/sync |
| server → client | `42["onlineFriends",[]]` | friends list response |
| server → client | `42["matchUpdate",{…}]` | match, conversation, participants and closure state |
| server → client | `42["typing",{"userId":"…","conversationId":"…"}]` | typing notification |
| server → client | `42["chatMessage",{"message":{…}}]` | incoming message or the account's own server echo |
| server → client / client → server | `2` / `3` | ping / pong |

In the supplied recording, the complete client → server WebSocket inventory is
one Socket.IO connect, three `presenceSync` frames and three pongs. **There is no
client chat-send frame.**

## Payload shapes and identity

A `matchUpdate` has a `match.conversation` object, participants, `users`, a
`closure` state and queue/pause flags. A later update can include
`messageCount`, `lastMessage` and a closed conversation, for example:

```json
{
  "match": {
    "conversation": {
      "id": "<24-hex conversation id>",
      "participants": [
        {"profile": {"id": "<24-hex profile id>", "username": "Stranger42"}, "userId": "…"},
        {"profile": {"id": "<24-hex profile id>", "username": "sadia.6.7"}, "userId": "…"}
      ],
      "messageCount": 7,
      "lastMessage": "<message id>",
      "closure": {"closed": false}
    },
    "users": [{"userId": "…", "inactive": false}],
    "closure": {"closed": false},
    "paused": false
  },
  "inQueue": false
}
```

The Socket.IO `pid` shown in the connect acknowledgement is a 20-character
session identifier (for example, the captured `bcLYPx5BjAjHVzzBAbdt`). It is
**not** the account's 24-hex profile id. Do not match the account to a
conversation participant using `pid`.

The recorded message variants distinguish the sender:

```json
// Other participant's message: flags is present; nonce is absent.
{"message":{"id":"…","conversationId":"…","author":{"id":"…","username":"Stranger42"},"content":"hey there!","type":"TEXT","attachments":[],"status":"SENT","flags":0,"reactions":[]}}

// Our own server echo: nonce is present; flags is absent.
{"message":{"id":"…","conversationId":"…","author":{"id":"…","username":"sadia.6.7"},"content":"hi","type":"TEXT","attachments":[],"status":"SENT","nonce":"<client-generated nonce>","reactions":[]}}
```

In the capture an own echo carries a client-generated nonce (21 characters in
the observed examples); an incoming message carries `flags: 0` and no nonce.
The client uses the learned own profile id, the page username, sent-text
tracking, and known/matching nonce to separate echoes from incoming SMS. The
absence/presence of `flags` is useful capture evidence, not the only identity
check. Profile, conversation and message identifiers in the capture are
24-hex-shaped values.

A closed match is another `matchUpdate`, with closure data such as:

```json
{"closed":true,"closeReason":"INTENTIONAL","closedAt":"…","closedBy":"<profile id>"}
```

`typing` carries `userId` and `conversationId`. The event can be ambiguous in a
**bare** text dump because either client or server might emit a typing event;
the parser therefore defaults bare `typing` to receive. Explicit arrows or
`send`/`receive` labels take precedence.

## The actual send path: HTTP write, WebSocket echo

The full capture shows own messages returning as **received** `chatMessage`
frames with nonces, while the client sent no chat frame. Therefore the captured
site writes message text out of band over HTTP and uses the socket to receive
updates/echoes. `--transport ws` means the bot receives chat over Socket.IO; it
does not mean that the captured site sends chat text over WebSocket.

The live runner supports the observed path as follows:

1. `SendRequestSniffer` watches the browser context's requests and recognizes
   the app's own message-send request. `build_send_template()` records its
   method, URL, body shape, content/nonce/conversation fields and replayable
   headers.
2. `send_template_request()` replays that request from the backend, with the
   saved session cookies and a fresh client nonce.
3. `ChatWebSocket` waits for the matching `chatMessage` echo. The HTTP status
   shows the request was accepted; the nonce-matched socket echo is the delivery
   confirmation.
4. If no request template is known yet, the page can make its own first send
   (the DOM fallback) so the sniffer can learn the request. Optional socket
   emits remain for experiments/other builds, but the captured endpoint ignores
   socket chat sends.

The learned template is stored in local `data/ws_config.json`. It can contain
account-specific request headers (including auth/CSRF data), so this file is
ignored by Git and should be treated as private local state. `--no-http-send`
forces the experimental socket path; it is not the captured site's normal send
mechanism.

A WebSocket-only dump cannot reveal an HTTP endpoint or a missing Socket.IO send
event. To inspect the app's write request, capture the relevant Fetch/XHR
request in DevTools (or let the live `SendRequestSniffer` observe a page send).
`--probe` and `--bundle` can rank candidate `socket.emit("…")` names, but a name
found in a bundle is not proof that the server accepts it.

## HAR and plain-text dump replay

Run the same offline parser against either a DevTools HAR or a plain `ws.txt`
frame dump:

```bash
python tools/ws_chat.py --replay ws.txt
python tools/ws_chat.py --replay ws.txt --username sadia.6.7
python tools/ws_chat.py --har ws.txt       # inventory the WebSocket frames
python tools/ws_chat.py --identity --account EMAIL
```

Plain dumps may use `↑`/`↓`, `send`/`receive` words, or JSON wrappers such as
`{"type":"receive","data":"42[…]"}`. A marker or wrapper gives explicit
direction. If both are present, the leading arrow/word wins, so keep it
consistent with the wrapper (for example `↓ {"type":"receive",…}`). For an
unmarked line, Engine.IO/Socket.IO frame type is used; only
client-exclusive event names (`presenceSync`, `sendMessage`, `send_message`,
`syncPresence`, `skipMatch`, `skip`, `leave`) are guessed as sends. Ambiguous
`typing`, `chatMessage`, `matchUpdate`, `onlineFriends` and other events default
to receive. A bare direction guess is only a heuristic; preserve DevTools
markers when possible.

The two fixtures, `test_fixtures/chitchat_ws_capture.json` (HAR) and
`test_fixtures/chitchat_ws_dump.txt` (plain dump), must split and replay the same
way: 20 received frames / 7 sent frames; event counts `onlineFriends: 4`,
`matchUpdate: 2`, `typing: 2`, `chatMessage: 7`; 2 incoming messages and 5 own
echoes; closure handled. The plain dump also includes an **unmarked `typing`
frame** to verify that this ambiguous event defaults to receive. The fixture
identity is `test_user`; using that username is important so the own echoes are
not mistaken for incoming messages.

`--identity --account EMAIL` reads the logged-in username from the page once and
stores it with the other local learning. `data/ws_config.json` is ignored and
must not be committed.

## Blind-page proof and limits

```bash
python3 tools/e2e_mock_chat.py --transport ws --bot-page-blind --debug \
  --artifacts /tmp/ws_e2e_artifacts --log /tmp/demo_ws_chat.log
```

In this mock E2E, the bot's page is not given the stranger's messages and opens
no socket. The backend Socket.IO client still receives the messages; the strict
stand-in ignores chat sends over the socket, so replies must use the learned
HTTP request. The stand-in then broadcasts the message echo back over Socket.IO.
A passing run therefore proves **WebSocket-based detection with a blind bot
page**, and the HTTP write/echo-confirmation path; it does not prove that replies
are written through WebSocket.

This checkout's offline Python suites and capture replays are verified. Browser
E2E was not rerun here because Playwright/Chromium are not installed in this
environment.

## Code map

| File | Responsibility |
|---|---|
| `core/socketio.py` | Engine.IO v4 + Socket.IO client, heartbeat/reconnect |
| `core/chat_ws.py` | incoming chat parsing, identity/nonce handling, HTTP template send and echo confirmation |
| `tools/session_chat.py` | restore session; use backend Socket.IO receive path and learned HTTP send |
| `tools/ws_chat.py` | CLI for live listening, send/probe, HAR reports and offline replay |
| `tools/mock_chitchat/ws_server.py` | offline Socket.IO stand-in used by transport tests/E2E |
| `test_fixtures/chitchat_ws_capture.json` | HAR-shaped test capture |
| `test_fixtures/chitchat_ws_dump.txt` | equivalent plain-text test dump |
