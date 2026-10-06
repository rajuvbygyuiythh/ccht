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
| server → client | `40{"sid":"…","pid":"…"}` | namespace connected — `pid` **is the account's own profile id** |
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
