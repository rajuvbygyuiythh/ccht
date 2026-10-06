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

Everything else (browser/, entry/, core/, docs/, config/, bats) is
unchanged from the original project.
