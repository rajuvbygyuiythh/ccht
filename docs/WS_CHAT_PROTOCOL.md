# chitchat.gg chat transport — captured protocol (v2.8)

This note describes the anonymized fixture
`test_fixtures/chitchat_ws_capture.json` and a user-supplied DevTools excerpt
containing one WebSocket request. The fixture replaces account/conversation
identifiers with test values. The supplied WS entry shows the receive protocol
and contains **no outgoing chat-message frame**; it does not include the other
HTTP requests shown in DevTools, so it cannot identify the message-write
endpoint.

## Site and session flow

The observed chat page is `https://app.chitchat.gg/chat/new/<conversationId>`.
The wider app flow is landing page → **Start Text Chat** →
`/start/new` (verification/queue) → chat route. A saved browser session may be
used by the app, but the supplied WebSocket request lists `cookies: []` and has
no `Cookie` header. That may be an export/redaction omission; this excerpt alone
does **not** establish how the live socket is authenticated. The Python client
can attach a configured cookie jar, but that capability and the mock's synthetic
cookie test are not proof of the live site's auth mechanism. Never commit local
session state or learned request configuration.

## Socket connection

| Property | Captured value / meaning |
|---|---|
| URL | `wss://api.chitchat.gg/socket.io/?EIO=4&transport=websocket` |
| Origin | `https://app.chitchat.gg` |
| Protocol | Engine.IO v4 + Socket.IO namespace `/` |
| Heartbeat | server ping `2`, client pong `3`; observed interval about 25 seconds (`pingTimeout` 20 seconds) |
| Authentication | not shown in the supplied WS request (`cookies: []`, no `Cookie` header); the socket `pid` is not the account profile id |

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

In the supplied WebSocket entry, the client → server frames are one Socket.IO
connect, three `presenceSync` frames and three pongs. **There is no client
chat-send frame on this socket.** This says nothing about the other 166 network
requests that were not included in the excerpt.

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
        {"profile": {"id": "<24-hex profile id>", "username": "stranger_user"}, "userId": "…"},
        {"profile": {"id": "<24-hex profile id>", "username": "test_user"}, "userId": "…"}
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
session identifier (the anonymized replay fixture uses `PID_SESSION_0000001`).
It is **not** the account's 24-hex profile id. Do not match the account to a
conversation participant using `pid`.

The recorded message variants distinguish the sender:

```json
// Other participant's message: flags is present; nonce is absent.
{"message":{"id":"…","conversationId":"…","author":{"id":"…","username":"stranger_user"},"content":"hey there!","type":"TEXT","attachments":[],"status":"SENT","flags":0,"reactions":[]}}

// Our own server echo: nonce is present; flags is absent.
{"message":{"id":"…","conversationId":"…","author":{"id":"…","username":"test_user"},"content":"hi","type":"TEXT","attachments":[],"status":"SENT","nonce":"<client-generated nonce>","reactions":[]}}
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

## Outgoing message path: what this WebSocket entry proves

The supplied WS entry shows own messages arriving as **received** `chatMessage`
frames with nonces, while no client chat-send frame appears on this socket. It
therefore establishes that this recorded socket was not used to send chat text.
The excerpt contains only the WebSocket request, not the other network entries,
so it does **not** identify or prove the out-of-band write as HTTP (or reveal its
endpoint). `--transport ws` means the bot receives chat over Socket.IO; it does
not imply that replies are emitted over Socket.IO.

The project has an HTTP send adapter for cases where the page's own request is
available. `SendRequestSniffer` can watch the browser context and
`build_send_template()` records the request method, URL, body shape, field names
and replayable headers. `send_template_request()` then replays that template
with the configured session state and a fresh nonce; `ChatWebSocket` can wait
for the matching echo. This describes the implementation and mock contract, not
an endpoint extracted from the supplied WS-only entry. If the live request has
not been learned, do not guess the endpoint or emit arbitrary socket events.

The learned template is stored in local `data/ws_config.json`. It can contain
account-specific request headers (including auth/CSRF data), so this file is
ignored by Git and should be treated as private local state. The standard live
runner ignores mock-only and legacy/unverified templates. `--no-http-send`,
`--probe`, and `--ws-send-event` explicitly opt into experimental socket sends.
Without a verified HTTP template or explicit opt-in, the client fails closed
instead of guessing `chatMessage`. The supplied capture neither shows a
chat-send event nor proves whether the server would accept one.

To identify the live write path, include the relevant Fetch/XHR/HTTP entries
from the full HAR (redact cookie, authorization and CSRF values) or let the live
`SendRequestSniffer` observe a page send. `--bundle` ranks candidate
`socket.emit("…")` names; `--probe` may attempt one experimentally. Neither a
bundle name nor the supplied WebSocket entry proves server acceptance.

## HAR and plain-text dump replay

Run the same offline parser against either a DevTools HAR or a plain `ws.txt`
frame dump:

```bash
python tools/ws_chat.py --replay ws.txt
python tools/ws_chat.py --replay ws.txt --username test_user
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

## Mock blind-page test and limits

```bash
python3 tools/e2e_mock_chat.py --transport ws --bot-page-blind --debug \
  --artifacts /tmp/ws_e2e_artifacts --log /tmp/demo_ws_chat.log
```

In this mock E2E, the bot's page is not given the stranger's messages and opens
no socket. The backend Socket.IO client still receives the messages; the strict
stand-in ignores chat sends over the socket, so replies use the mock HTTP
adapter and the stand-in broadcasts its echo back over Socket.IO. A passing run
would validate **WebSocket-based detection with a blind bot page** and the mock
HTTP request/echo mechanics. It would not validate the live write transport,
endpoint, authentication, or how the real server handles socket chat sends.

This checkout's offline Python suites and capture replays are verified. Browser
E2E was not rerun here because Playwright/Chromium are not installed in this
environment.

## Code map

| File | Responsibility |
|---|---|
| `core/socketio.py` | Engine.IO v4 + Socket.IO client, heartbeat/reconnect |
| `core/chat_ws.py` | incoming chat parsing, identity/nonce handling, configurable HTTP adapter and echo matching |
| `tools/session_chat.py` | restore session; use backend Socket.IO receive path and a verified page-request template when available |
| `tools/ws_chat.py` | CLI for live listening, send/probe, HAR reports and offline replay |
| `tools/mock_chitchat/ws_server.py` | offline Socket.IO stand-in used by transport tests/E2E |
| `test_fixtures/chitchat_ws_capture.json` | HAR-shaped test capture |
| `test_fixtures/chitchat_ws_dump.txt` | equivalent plain-text test dump |
