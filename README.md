# EVA Bot — Full Project (Diagram Flow Engine + Legacy Engine)

Complete EVA chitchat.gg bot: browser automation, PyQt6 GUI dashboard, CLI
runner, account/session management — now running the **updated diagram flow
logic** with **symmetric input/output txt categories**, plus the original
full engine kept as a selectable legacy option.

```
DEFAULT ENGINE (flow):   greeting -> age/gender -> country -> flirty
                         -> positive reply -> share snap -> END CHAT
LEGACY ENGINE:           EVA_ENGINE=legacy  (original 6-flow brain)
```

---

## The one rule (customizable without code)

```
data/input/<category>.txt   = WHAT THE USER SAYS   (trigger keywords)
data/output/<category>.txt  = WHAT THE BOT REPLIES (reply lines)

same file name = same category — edit the txt files, restart, done.
```

Example: user sends `m 21` → matches a line in `input/age_gender.txt` →
bot replies from `output/age_gender.txt` (`f19`).

| # | User says | Category | Next |
|---|-----------|----------|------|
| 1 | first SMS `hi` | `greeting.txt` | wait age/gender |
| 2 | `m21` | `age_gender.txt` | wait country |
| 3 | `from?` | `country.txt` | wait country answer |
| 4 | `usa` | → `flirty_questions.txt` | MIDDLE |
| 5a | `u horny?` / `send nudes` | `horny.txt` | MIDDLE |
| 5b | `ur cute` / `love u` | `middle_chat/flirty_reply.txt` | MIDDLE |
| 5c | `lol` / `ok` / `nice` | `middle_chat/warm_reply.txt` | MIDDLE |
| 5d | `gtg` / `bye` / `brb` | `middle_chat/busy_later.txt` | MIDDLE |
| 5e | `how are u` / `how old are u` | `how_are_you.txt` / `your_age.txt` | MIDDLE |
| 5f | **positive**: `yes` / `yeop` / `ur sc?` / `whats ur snap` | `share_snap.txt` | **END CHAT** |
| 5g | user offers snap: `my snap is…` | `snapchat.txt` | **END CHAT** |
| 5h | anything else | `flirty_questions.txt` | MIDDLE |
| 6 | after 8 bot replies | `closer.txt` | **END CHAT** |

Matching layers (one fails → next works): exact → substring →
slang-normalized → token-subset → smart fallback (regex, countries.txt).

**Bangla A-to-Z guide:** `data/output/info.txt` (কোন SMS পেলে কোন ফাইল
থেকে reply আসবে — সব বাংলায় ব্যাখ্যা করা)।

---

## Engine switch

```bash
python -m entry.main                # GUI (default = flow engine)
EVA_ENGINE=legacy python -m entry.main   # GUI with the original engine
python entry/cli_runner.py          # terminal runner
```

Both engines share `data/input/` + `data/output/` and the same
`ChatRuleBot` API, so the browser worker, GUI and tools work with either.

## Install & run (Windows)

```bat
install.bat      :: venv + all packages + Playwright Chromium (one run)
run.vbs          :: start the bot app (GUI, NO console window)
"EVA Bot" desktop :: created by install.bat - double-click app icon
run_console.bat  :: debug launcher (console visible)
test.bat         :: automated checks + demo + live REPL
```

Manual (any OS): `python -m venv .venv` → `pip install -r
data/requirements.txt` → `python -m playwright install chromium`.

## Test (all green)

| Script | Engine | Result |
|--------|--------|--------|
| `python test_flow.py` | flow | **58/58 passed** |
| `python demo_flow.py` | flow | **PASSED** (diagram walkthrough) |
| `python demo_chat.py` | flow | **ALL CHATS REACHED END** (failed on the original snapshot!) |
| `python test_live.py` | legacy | **44/44 passed** |
| `python test_fuzz.py` | legacy | **4/4 passed** |
| `python test_matcher.py` | legacy IO | **ALL CATEGORIES ROUND-TRIP OK** |
| `python test_chat_detect.py` | legacy | **47/47 passed** |
| `python test_selector_doctor.py` | legacy (jsdom) | **105/105 passed** |
| `python test_session_health.py` | sessions (offline) | **152/152 passed** |
| `python test_browser_identity.py` | identity (offline) | **207/207 passed** |
| `python test_thread_scheduler.py` | runtime (offline) | **93/93 passed** |
| `python test_mock_chitchat.py` | mock site + session import + debug layer | **54/54 passed** |
| `python test_ws_transport.py` | chat WebSocket (Engine.IO v4 + Socket.IO) | **50/50 passed** |
| `python3 tools/e2e_mock_chat.py` | **real browser E2E** (DOM transport) | **PASS** (see v2.7 below) |
| `python3 tools/e2e_mock_chat.py --transport ws` | **real browser E2E** (socket transport) | **PASS** (see v2.8 below) |
| `python tools/live_chat.py` | flow (default) | interactive REPL |

## Project layout

```
entry/      GUI dashboard (main.py), CLI runner, thread orchestration
browser/    Playwright/Camoufox automation, context pool, sessions,
            human behavior, device sign-in
chat/       rule_bot.py (engine switch) + rules.py (legacy engine)
core/       config loader, resource governor, engine bridge, accounts
config/     eva_config.json (intent tuning)
data/       input/ + output/ categories, countries.txt, snap_ids.txt,
            local_db/, config.json (runtime behavior)
docs/       architecture, flow spec, installers (INSTALL_SMOOTH.*)
tools/      live_chat.py REPL, coverage check
eva_flow.py the diagram funnel engine (default)
run_chat.py / demo_flow.py / test_flow.py   flow-engine tools
install.bat / run.vbs / run_console.bat / test.bat   Windows helpers
```

## Behavior rules

- No line repeats within one conversation; never back-to-back
- Reply cap 8 (`EVA_MAX_REPLIES` env) → `closer.txt` → END
- Underage (<18) permanently blocks snap sharing (both engines)
- Snap usernames cycle round-robin via `data/snap_ids.txt`
- `%username%` / `{snap}` / `{country}` placeholders in reply lines
- Multi-line SMS: last line drives the machine

## Stranger-SMS detection (v2.2 — "bot user er sms detect korte parche na")

The browser worker reads the stranger's SMS through `browser/chat_reader.py`.

What was wrong before: extraction depended on ONE hardcoded selector set
(`main ol` → `li.select-text` → `span.font-bold[role=button]` →
`span.emoji-content`). If any class name changed on the site, or if our own
message was not rendered, the bot silently stopped seeing the stranger — no
error, no reply, just "blind" chats.

What it does now:

1. **Selector chain** — several container / row / username / text selector
   candidates are tried in order (the original ones first, so nothing regresses).
2. **Ancestor filtering** — a message row is never counted twice together with
   the text span inside it.
3. **Speaker detection** — own username from the sidebar, a cached hint, row
   alignment classes (`justify-end`, `self-end`, …), and finally "this text is
   something we just sent". Our own bubbles are never answered.
4. **Unknown markup fallback** — a generic `Name: message` line parser still
   finds SMS in a completely different layout.
5. **Fingerprint tracking** (`MessageTracker`) — new messages are found by
   diffing the DOM against the previous poll instead of the old
   `len(dom) > last_count` counter that ran ahead of the DOM whenever the site
   did not render our own message (the real reason replies were "not detected").
6. **Loud diagnostics** — if 0 messages are parsed for several polls the log
   says so, prints which selectors were tried, and with `EVA_DUMP_CHAT_DOM=1`
   saves the page HTML (`logs/chat_dom_dump_*.html`) so the selectors can be
   pointed at the new markup.

Debug a page by hand (no login needed for the saved-page mode):

```bash
# 1) open the chat page, right-click -> Save as -> "Webpage, HTML only"
python tools/chat_detect_debug.py --html path/to/chat_page.html
python tools/chat_detect_debug.py --url https://chitchat.gg/        # live
```

Tests: `python test_chat_detect.py` (47 checks; the DOM tests need
`node` + `npm install jsdom`, otherwise they are skipped with a note).

## Site markup changes — the selector doctor (v2.3)

A selector list is still a list: the next redesign (new classes, `ol/li`
replaced by `div` rows, hashed React/Tailwind names, a virtualised list,
renamed `data-*` attributes) would blind the bot again. So the bot no longer
depends on a list alone.

**1. It heals itself at runtime.** When the built-in selectors match nothing
but the page clearly has chat text, `browser/chat_reader.py` runs
`browser/selector_doctor.py`, which *derives* the selectors from the page
markup (repeated-sibling rows, stable class/attribute names, the message
text node, the username node, alignment hints), retries the read with them,
and remembers them for the following polls. The log says what happened:

```
[Detect] chat markup CHANGED — the bot healed itself and learned the new selectors:
[Detect] container=div.chat-scroll | items_selector=div.chat-row | items=4 | ... | HEALED={...}
[Detect] keep them with:  python tools/chat_detect_debug.py --html <saved chat page.html> --save
```

**2. You can drive it by hand** — save the chat page in the browser
(right-click → *Save as* → "Webpage, HTML only") and run:

```bash
python tools/chat_detect_debug.py --html chat_page.html --suggest   # derive + verify
python tools/chat_detect_debug.py --html chat_page.html --explain   # why these selectors
python tools/chat_detect_debug.py --html chat_page.html --save      # apply (no code edit)
python tools/chat_detect_debug.py --html chat_page.html --offline --suggest   # no Playwright/jsdom
python tools/chat_detect_debug.py --url https://chitchat.gg/ --suggest        # live page
```

`--save` merges the result into `config/chat_selectors.json`; that file is
loaded on every poll and its selectors are tried **before** the built-in
list, so a site change is fixed without touching code (delete the file to go
back). Every heal also drops the evidence in
`logs/selector_suggestion_*.json`, and `EVA_SELECTOR_AUTOSAVE=1` makes the
runtime write `config/chat_selectors.json` itself.

**3. It refuses to guess on non-chat pages.** Landing pages, sidebars and
article lists are rejected by the scoring (link rows, headings, CTA buttons,
marketing words, site chrome) — no phantom SMS for the bot to answer.

Example config: `config/chat_selectors.example.json`.
Tests: `python test_selector_doctor.py` (105 checks; the jsdom pass runs the
real extractor against the new-markup fixtures and is skipped when
`node`/`jsdom` are missing).

## Smooth multi-browser runs + backend session→chat (v2.6)

### Adaptive thread management (PC hang/leg kore na)

```bat
python tools/thread_planner.py            :: what can THIS PC carry right now?
python tools/thread_planner.py --simulate :: the 4 load levels, no browser needed
python tools/thread_planner.py --threads 10
```

| Situation | What the bot does |
|-----------|-------------------|
| CPU < 60% and RAM < 60% and free RAM ≥ budget | **adds** one browser thread (up to `max_threads`) |
| CPU > 85% or RAM > 80% | **removes** the newest thread (it finishes its chat first) |
| CPU/RAM > 90% or free RAM < 700 MB | **pauses every worker** — browsers stay open, work resumes automatically when the load drops |
| start with more threads than the PC can carry | starts at the comfortable number and grows later if the load allows |

Config: `data/config.json` → `thread_scheduler` (`max_threads`, `ram_per_browser_mb`,
`reserve_mb`, `pause_above`, `resume_below`, `rest_between_sessions_seconds`, …).
The Resource Governor (throttle / recycle / redline) stays as the last line of defence,
and the per-account Chrome profiles mean a browser never has to be re-created to free RAM.

### Backend: saved session → connect → detect SMS → reply (headless)

```bat
python tools/session_chat.py --all --plan-only              :: what would run
python tools/session_chat.py --account EMAIL --dry-run      :: connect test
python tools/session_chat.py --account EMAIL --minutes 30   :: run it (headless)
python tools/session_chat.py --all --minutes 15 --visible   :: watch it work
```

Only the **saved session** is used (cookies/localStorage in `storage_state.json`
and/or the account's own Chrome profile) — no password, no login form.  Every step
is tagged in the log: `[Session]` `[Identity]` `[Profile]` `[Fingerprint]`
`[Restore]` `[SMS]` `[REPLY]`.  Incoming messages are read with the multi-selector
reader + fingerprint tracker (`browser/chat_reader.py`), replies come from the same
engine the GUI uses (`chat/rule_bot.py`), and the cookies are re-saved during long
runs so the account stays logged in for the next start.

Full explanation (diagram + file-by-file table): **`docs/RUNTIME_AND_CHAT_FLOW.md`**.

## Real browser end-to-end chat test (v2.7 — "acc er session diye chat kora jay?")

chitchat.gg is not reachable from every machine/CI sandbox, so `tools/e2e_mock_chat.py`
runs the **real pipeline** against a local stand-in of the site
(`tools/mock_chitchat/`): real Chromium, the account's own Chrome profile, the
saved cookies, the real chat reader and the real `ChatRuleBot`.  The bot keeps
using `https://app.chitchat.gg/...` — a Playwright route hook answers those
requests with the stand-in pages, so cookies and URL checks behave exactly like
on the live site.

```bash
python3 tools/e2e_mock_chat.py                     # full conversation (headless, ~45 s)
python3 tools/e2e_mock_chat.py --connect-only      # connect test only: fingerprint + session
python3 tools/e2e_mock_chat.py --session data/account_sessions/account_xxx --visible
python3 tools/e2e_mock_chat.py --log /tmp/e2e.log  # transcript to a file
```

A **second real browser** plays the stranger: it types a message into the same
chat, and the test passes only when the bot's reply is rendered in that user's
window.  Nothing is stubbed — `tools/session_chat.py` drives
`browser/browser_automation.py` unchanged.

| Check (printed at the end of a run) | Meaning |
|-------------------------------------|---------|
| browser launched (real Chromium) | the configured engine + `EVA_CHROMIUM_EXECUTABLE` |
| account identity + own Chrome profile | Phase-14 device/profile reuse |
| device fingerprint applied before the site | anti-detect layer on the context |
| saved session imported (cookies) | `storage_state.json` → profile (`_import_storage_state`) |
| session restored + verified by the site | `[Restore] ✓ saved session verified` |
| the user's message detected | `[SMS] user: …` |
| the bot replied through ChatRuleBot | `[REPLY] bot: …` |
| reply visible in the user's browser | read from the second browser's DOM |

### Debugging a run (`--debug`)

```bash
python3 tools/e2e_mock_chat.py --debug                    # everything traced
python3 tools/e2e_mock_chat.py --debug --artifacts /tmp/art
python3 tools/e2e_mock_chat.py --no-artifacts             # never write files
python3 tools/e2e_mock_chat.py --hard-timeout 120         # absolute safety stop
```

| What `--debug` adds | Where it shows up |
|---------------------|-------------------|
| every API call the pages make (`/api/messages`, `/api/send`, greeting) | `[MockAPI] …` lines in the log |
| DOM snapshot of **both** browsers every 2 s (bubbles, last message, input, connection state) | `[DEBUG] [dom] bot: …` / `user: …` |
| page console, JS errors, failed requests and every network request | `[DEBUG] [bot]/[user] console|pageerror|requestfailed` |
| artifacts kept even when the run passes | `--artifacts` folder (`SUMMARY.txt`, `bot_page.html`, `*_screenshot.png`, `events_*.log`, `network.log`, `dom_trace.log`, `mock_api.log`, `transcript.json`, `cookies.json`) |

A **failing** run always writes artifacts (even without `--debug`) and prints a
`what to look at` section: every failed check gets concrete hints (which file,
which log line, which selector to inspect). The log file keeps the `[DEBUG]`
lines even when the terminal stays quiet, stdout being closed early
(`… | head`) can no longer kill a run, and `--hard-timeout` guarantees an
exit with artifacts instead of a hung browser call.

### Using a Chrome/Chromium you already have

Playwright normally downloads its own Chromium (`python -m playwright install chromium`).
On locked-down machines (or when that download is blocked) point the bot at any
existing binary instead:

```bash
# Windows PowerShell
$env:EVA_CHROMIUM_EXECUTABLE="C:\Program Files\Google\Chrome\Application\chrome.exe"
python entry\main.py
```

`EVA_CHROMIUM_EXTRA_ARGS` appends extra launch flags (e.g. `--no-sandbox` in
containers).  Both are optional; without them the bot behaves as before.

## Chat over the live WebSocket (v2.8 — "imporve web socket")

The chat now works the way the site itself works: after the saved session is
restored, the bot connects to the account's own chat socket and reads/writes the
conversation there — no DOM polling for the messages and no typing for the reply.

```
wss://api.chitchat.gg/socket.io/?EIO=4&transport=websocket
  0{…}            ← engine.io handshake (ping 25 s)
  40{release}     → handshake with the site's release id
  ← 40{sid,pid}      the pid is the account's own profile id
  → 42["presenceSync"]
  ← 42["matchUpdate", {match:{conversation:{…}}}]     the chat to talk in
  ← 42["chatMessage", {message:{author, content}}]    incoming SMS
  → 42["<send event>", {conversationId, content}]     our reply
  ← 42["chatMessage", …]                              the server echo = delivered
```

The full capture (frames, payloads, the closure frame) is written down in
`docs/WS_CHAT_PROTOCOL.md`.

| File | Role |
|------|------|
| `core/socketio.py` | Engine.IO v4 + Socket.IO client, stdlib only (no new dependency for the Windows build), auto-reconnect + heartbeat |
| `core/chat_ws.py` | chat layer: session cookies, `matchUpdate`/`chatMessage`, echo-confirmed sending, bundle probe |
| `tools/ws_chat.py` | CLI to listen/reply/probe an account's socket |
| `tools/session_chat.py --transport ws` | the backend runner: browser holds the session, the socket does the chatting |
| `tools/mock_chitchat/ws_server.py` | offline stand-in that speaks exactly those frames |

```bash
# backend runner over the socket (headless by default)
python tools/session_chat.py --account sadia.6.7@gmail.com --transport ws --minutes 30

# watch a session's socket, or answer with ChatRuleBot
python tools/ws_chat.py --account sadia.6.7@gmail.com --listen
python tools/ws_chat.py --account sadia.6.7@gmail.com --reply
python tools/ws_chat.py --account EMAIL --send "hi"        # one message
python tools/ws_chat.py --account EMAIL --reply --probe    # learn the send event

# protocol report from a HAR capture / from a saved JS bundle
python tools/ws_chat.py --har ws.txt
python tools/ws_chat.py --bundle js-direct-chat.js
```

**The send event name.** The capture does not contain the client frame that sends
a message (the message was typed before the capture started), so the bot resolves
it in this order: `--ws-send-event` / `data/ws_config.json` → `--probe` (reads
the event names out of the site's own chat bundle) → the default candidate list →
and in every case **delivery is only accepted when the server echoes the message
back** (`[WS] ✓ delivery confirmed by the server echo`). If nothing is confirmed,
`run_account_ws` falls back to typing in the page (`--no-dom-fallback` disables
that), so a chat is never lost.

`--transport dom` (the default) is unchanged: the DOM flow from v2.2–v2.7 still
runs exactly as before.

### Proving it offline (`--transport ws`)

```bash
python3 tools/e2e_mock_chat.py --transport ws --minutes 1.0
```

Same real pipeline (real Chromium, the account's profile, the saved cookies, the
stealth layer, `ChatRuleBot`), but the stand-in now also runs the chat socket:
**both** the bot's backend client **and** the two browser pages talk Engine.IO
v4 + Socket.IO to it.  The added checks:

| Check | What it proves |
|-------|----------------|
| engine.io + socket.io handshake completed | real socket, real frames |
| the restored session cookies went out on the socket handshake | the socket is authenticated by `storage_state.json` (cookie/token only) |
| the account identity came from its own session (pid match) | `40{pid}` ↔ `matchUpdate` participants |
| incoming SMS was read from `chatMessage` events | detection is socket-side, not DOM |
| the reply was confirmed by the server echo | the reply really left the bot over the socket |
| no DOM fallback was needed | the reply was not typed into the page |
| the chat closure travelled over the socket | `closure{closed:true}` handling |

The run also writes `ws_transcript.json` (every frame the stand-in sent, the
cookie names on the handshake, the transcript) next to the other artifacts.

On a sandbox where the mock pages are served over `https` but the stand-in
listens on `127.0.0.1`, the harness starts Chromium with
`--unsafely-treat-insecure-origin-as-secure=http://127.0.0.1:<port>
--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests`
and grants the `local-network-access` permission to the context.  That is a
test-harness detail for the local stand-in only — the real bot always dials
`wss://api.chitchat.gg`.

## Separate Chrome profile per account (v2.5 — "প্রতিটা account এর আলাদা profile + fingerprint")

Every account now opens **its own real Chrome profile** with **its own
fingerprint**. Two accounts can never log in from one profile.

| Rule | How it is enforced |
|------|--------------------|
| separate Chrome profile per account | `browser_profiles/<account>/` — real Chromium profile (cookies, history, cache, IndexedDB survive between runs) |
| separate fingerprint per account | `browser/browser_identity.py` derives a permanent device (UA, platform, screen, viewport, DPR, hardware, memory, timezone, locale) from the account key; a registry makes sure no two accounts share a device |
| never two accounts in one profile | each profile stores `owner.json`; if a folder belongs to another account, the bot **refuses it** and gives this account its own folder (log: `[Profile] ⛔ … belongs to another account`) |
| fingerprint verified **before** login | the page is asked what the site can see (UA/platform/screen/viewport/DPR/timezone/WebGL) and every value is compared with the profile — `[Fingerprint] ✓ device verified before login`; a mismatch is listed in full and, with `verify_strict: true`, blocks the login |
| profile kept **after** login | `[Profile] saved the logged-in browser profile …` → `identity.json`, `owner.json` (`logged_in_at`) and `profile_state.json` (observed fingerprint) are written next to the profile |
| same browser next run | the profile + identity are reloaded, so the account reappears as the same device it logged in with |

```bat
python tools/session_doctor.py --profiles          :: who owns which Chrome profile
python tools/session_doctor.py --identities        :: which device each account uses
python tools/session_doctor.py --identity EMAIL    :: one account in detail
python tools/session_doctor.py --new-identity EMAIL --apply   :: fresh device (after a ban)
```

Config (`data/config.json` → `browser_identity`, all optional):

```json
"browser_identity": {
  "enabled": true,
  "profile_mode": "persistent",
  "unique_between_accounts": true,
  "adopt_observed_fingerprint": true,
  "rotate_on_ban": true,
  "owner_guard": true,
  "verify_before_login": true,
  "verify_strict": false,
  "verify_url": "",
  "save_profile_after_login": true,
  "warmup_sites": [],
  "log_identity_on_start": true
}
```

* `profile_mode: "context"` goes back to one shared browser with an isolated
  context per account (lower RAM, but no real profile on disk).
* `verify_url` — optional real fingerprint-checker page opened before the check
  (empty = the page the bot is already on).
* `warmup_sites` — a few URLs visited once inside a brand-new profile so it has
  some history before the account logs in.
* Profiles are **never** committed to git (`.gitignore` → `browser_profiles/`).

## Saved sessions auto-load + blind session repair (v2.4 — "session diye acc auto browser e load")

Every saved session (`account_sessions/`, `data/account_sessions/`) is checked
**before** a browser is launched, so a dead session can no longer waste a launch
or silently fail mid-run.

```bat
:: what do my saved sessions look like right now?  (offline, no browser)
python tools/session_doctor.py --list

:: one account in detail (with the folder + the decision)
python tools/session_doctor.py --check sadia.6.7@gmail.com --paths

:: repair: add "email:password" lines to accounts.txt — the bot then re-logs
:: in automatically whenever that session goes blind
```

| What | How it behaves |
|------|----------------|
| health states | `ok` · `expiring` (<3 days) · `expired` · `empty` · `corrupt` · `missing` — the last four are **blind** |
| blind + credentials | browser opens, `login_with_account()` runs, fresh session is saved → chat continues (log: `BLIND SESSION repaired`) |
| blind + no credentials | browser is **not** launched; log says: `add 'email:password' for this account to accounts.txt` |
| banned session | never repaired, never downloaded — skipped as before |
| restore check | the page is probed (chat UI / username / app text) instead of trusting the URL only |
| long runs | cookies are re-saved after each chat (throttled, `session_refresh_minutes`) so a run does not die mid-way |
| context pool | blind sessions get no context slot; repairable ones are let through so the worker can log in |
| deletions | **never** — `--prune` only *moves* dead sessions to `account_sessions/_dead/` (opt-in) |

Config (top-level `session_management` key in `config.json`, all optional —
defaults are shown in `docs/SESSION_PLAN.md`):

```json
"session_management": {
  "verify_after_restore": true,
  "auto_repair_blind_sessions": true,
  "session_refresh_each_chat": true,
  "session_refresh_minutes": 10,
  "session_expiry_warn_days": 3,
  "prune_dead_sessions": false
}
```

`EVA_SESSIONS_DIR` adds extra session roots (use `;` on Windows, `:` elsewhere),
`EVA_ACCOUNTS_FILE` points at a credentials file other than `accounts.txt`.

## Changes vs the original 22.zip snapshot

1. `data/input` + `data/output` renamed to direct symmetric names
   (`01_greeting.txt` → `greeting.txt`, …) + new categories
   (`middle_chat/` inputs, `share_snap.txt` triggers, `closer.txt`)
2. `chat/rule_bot.py`: engine switch (default flow, `EVA_ENGINE=legacy`)
3. `chat/rules.py`: filename refs updated + two fixes
   (punctuation-lossy trigger guard; minors never get IO snap reveals)
4. `eva_flow.py` + tools added (diagram engine, same API)
5. Legacy test scripts pinned to `EVA_ENGINE=legacy`
6. `data/countries.txt` added (editable country keywords)
7. `data/output/info.txt` — Bangla guide (A to Z)
8. **v2.4 session safety** — `browser/session_health.py`, `tools/session_doctor.py`,
   verified restore, blind→auto-repair, keep-alive cookie refresh, honest
   alive/blind stock counts, session columns in the account manager, and the
   `session_management` config block (see `docs/SESSION_PLAN.md`)
9. **v2.5 per-account browser profile** — `browser/browser_identity.py` (permanent
   device profile per account, owner guard, fingerprint verification before
   login, profile saved after login), persistent Chromium profiles, identity
   passed into pooled contexts, `--profiles/--identities/--new-identity` tools
   and the `browser_identity` config block (see `docs/BROWSER_IDENTITY_PLAN.md`)
10. **v2.6 smooth runtime + backend flow** — `core/thread_scheduler.py` (adaptive
    thread count, freeze protection via the pause gate, capacity planning),
    `entry/thread_manager.py` grow/shrink, worker-side pause between chats,
    `tools/thread_planner.py`, `tools/session_chat.py` (session→chat runner) and
    `docs/RUNTIME_AND_CHAT_FLOW.md`

Everything else (browser/, entry/, core/, docs/, config/, bats) is
unchanged from the original project.
