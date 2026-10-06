# AGENTS.md — Working Rules for This Project

These rules govern how to safely modify the EVA Bot codebase. They assume the
project-wide "Production Coding Rules" already in the global AGENTS.md.

---

## 1. Core Discipline

Follow the ordered workflow on every task:

UNDERSTAND → INSPECT → TRACE → EVIDENCE → ROOT CAUSE → PLAN → TARGETED CHANGE
→ TEST → DIFF REVIEW → VERIFY

Never jump straight into editing. Separate findings into **CONFIRMED**,
**LIKELY**, and **UNKNOWN**. Do not guess when source evidence exists.

## 2. Surgical, Minimal Edits

- Make the **smallest correct change**. Do not rewrite working architecture,
  delete working code, reformat wholesale, or touch unrelated files.
- Preserve public APIs and backward compatibility. Feature flags must degrade
  gracefully (every optional subsystem — Go engine, ContextPool, Camoufox —
  has a pure-Python fallback; keep that property).
- Do **not** add comments unless they document non-obvious intent.

## 3. Planning

- Before editing, record: root cause, target files, target functions, exact
  change, regression risks, validation strategy.
- For non-trivial work, state the plan before writing code.

## 4. Testing

- There is **no automated test framework yet** (the old ad-hoc `test_*.py` /
  `full_test.py` scripts were removed in the cleanup). Do not claim a test
  passed unless it actually ran.
- Static check: `python -m py_compile entry\main.py entry\paths.py entry\thread_manager.py browser\browser_automation.py core\config_loader.py`
  (matches `RUN_SMOOTH.bat`). Import check: `python -c "import chat"`.
- If introducing a real test suite, prefer `pytest`.
- Clearly distinguish **static verification** from **runtime verification**.

## 5. Critical / Fragile Areas (touch with care)

- **`browser/browser_automation.py` (~3600 lines)** — core lifecycle (login,
  chat loop, ban detection). Single-thread-per-worker; do not assume thread
  safety.
- **`entry/thread_manager.py` (~1250 lines)** — orchestration + Qt signals.
- **`entry/main.py` (~1610 lines)** — GUI entry; device-signin gate.
- **`chat/rules.py` (~1320 lines)** — the 6-flow conversation engine plus the
  priority-1 `InputOutputEngine` TXT matcher (`data/input/`/`data/output/`
  folders). All external data (config.json, urls.txt, .ico, sessions) lives in
  `data/`; resolve it via `entry/paths.py` (`project_root()`/`data_dir()`), never
  hardcode paths.
- The legacy `conversation_engine.py` was **removed** — only `chat/` exists now.

## 6. Data & Security (REQUIRED)

- **Never commit or log secrets**: account credentials, proxy credentials,
  cookies, tokens, device IDs, `account_sessions/`.
- Do not hardcode new absolute paths into source. If you must touch DB paths,
  route them through `config.json` — see TASKS.md B1 (current hardcoded paths
  in `chat/chat_db.py:19` and `chat/database/loader.py:26-35` are a known risk).
- Validate external file input (accounts/proxies/templates) and bound resources.

## 7. Networking

- Use bounded retries with backoff; no infinite loops.
- Add proper timeouts to network calls (e.g. `device_signin.py`).
- Prefer deterministic waits on observable state over arbitrary sleeps.

## 8. Resume / Handoff Protocol

- Before a session: read `docs/PROJECT_STATE.md` and `docs/TASKS.md` to pick up
  context.
- Keep `TASKS.md` current: mark an item `[x]` only when actually done and
  verified; otherwise leave it `[ ]` and record the blocker.
- On completion of a task, update `TASKS.md`/`PROJECT_STATE.md` only if it
  reflects real state changes.

## 9. Final Response Format

Report: **Result**, **Root Cause**, **Changes** (files + lines), **Verification**
(only commands actually run), **Remaining Risk**. Never claim "100% working"
without evidence.

## 10. Git

This directory is **not a git repo**. Only initialize/commit when explicitly
asked. If/when a repo is created, follow `TASKS.md` A1 (gitignore) and global git
rules (only commit what is requested; never commit secrets; concise messages).

## 11. COMPLETED TASKS (DO NOT TOUCH)

The following tasks have been completed and verified. Do NOT modify these files
unless explicitly asked by the user:

### Phase 1 — Critical Bug Fixes ✅
- [x] `entry/main.py`: GUI loads snap_ids.txt + flirty_questions.txt (were hardcoded `[]`)
- [x] `entry/main.py`: Create mode shows error and returns (was launching with zero accounts)
- [x] `entry/thread_manager.py`: Accounts only marked "failed" on actual errors (was burning every account)
- [x] `entry/thread_manager.py`: Force-cleanup calls `_stop_context_pool()` (was leaking resources)
- [x] `chat/rules.py`: `{snap}` placeholder resolved via `_resolve_snap_reply()`
- [x] `chat/database/loader.py`: DB paths configurable via `EVA_BOT_DB_ROOT` env var

### Phase 2 — High Priority Fixes ✅
- [x] `chat/rules.py`: Style mirroring enabled after 5 messages
- [x] `chat/rules.py`: Multi-line messages — only last line processed through full machine
- [x] `entry/main.py`: Log filtering clears on log clear
- [x] `browser/browser_automation.py`: `_kill_orphan_camoufox()` accepts engine param
- [x] `browser/browser_automation.py`: Proxy URL double-scheme fixed

### Phase 3 — Medium Priority Fixes ✅
- [x] `chat/horny_flirty_db.py`: Word-boundary matching in `match_horny()` and `match_flirty()`
- [x] `entry/thread_manager.py`: Non-blocking thread stagger using `QCoreApplication.processEvents()`
- [x] `entry/main.py`: Stats counting uses `message.startswith("Stranger: ")`
- [x] `core/resource_governor.py`: `psutil.cpu_percent(interval=None)` non-blocking
- [x] `browser/device_signin.py`: Bare `except:` → `except Exception:`

### Phase 4 — Dead Code Cleanup ✅
- [x] `chat/rules.py`: Removed 352 lines of unused pool definitions
- [x] `chat/rules.py`: Restored accidentally removed `FLOWS` dict and `SNAP_GATE_REPLIES`
- [x] `chat/style_analyzer.py`: Removed no-op `replace(" gonna ", " gonna ")`
- [x] `chat/horny_flirty_db.py`: Removed unused `get_horny_snap_hint()` function

### Phase 5 — Smarter Chat Rules ✅
- [x] `chat/rules.py`: Flow-type influence — 70% main pool / 30% flow-specific step4 pool
- [x] `chat/rules.py`: Memory-aware follow-ups using known facts
- [x] `chat/rules.py`: Retry bound reduced from 60→20 in `_session_unused_line()`

### Phase 6 — Test Fixes & Verification ✅
- [x] `test_live.py`: Phase 9 pattern detector assertion fixed
- [x] All 9 modified files pass `py_compile`
- [x] All imports verified
- [x] `test_live.py`: 51/51 passed
- [x] `test_fuzz.py`: 4/4 passed

### Phase 7 — File Cleanup & Debug Output ✅
- [x] Removed 7 dead txt files (01_greeting.txt, 02_age_gender.txt, 03_country.txt, asksc.txt, flirty.txt, normal.txt, sharesc.txt)
- [x] Merged 127 unique lines into active files
- [x] Added line number tracking for debug output
- [x] `tools/live_chat.py`: Debug shows exact file path + line number

### Phase 8 — Country Collection Fix ✅
- [x] `chat/rules.py`: Country collection now goes directly to flirty_questions.txt (was replying from country.txt)

### Phase 9 — Hardcoded Pools to TXT Files ✅
- [x] Created 10 new txt files for previously hardcoded pools
- [x] All pools now load from editable txt files
- [x] Debug output shows exact file path + line number

### Phase 11 — Stranger-SMS detection ("bot user er sms detect korte parche na") ✅
- [x] NEW `browser/chat_reader.py` — multi-selector DOM reader + diagnostics
      (container / item / username / text selector chains, ancestor filtering,
      alignment-class and "text we just sent" speaker fallbacks, and a generic
      `Name: message` line parser for unknown markup)
- [x] `browser/browser_automation.py`: `extract_chat_from_page()` now uses the
      new reader, keeps the ORIGINAL selector set as `_extract_chat_from_page_legacy`
      fallback, caches own username, and logs a loud warning (once) when the page
      cannot be parsed; `EVA_DUMP_CHAT_DOM=1` saves the page HTML for fixing selectors
- [x] `browser/browser_automation.py`: replaced the `len(dom) > last_message_count`
      arithmetic (which went blind whenever the site did not render our own message)
      with `chat_reader.MessageTracker` (fingerprint diff per poll)
- [x] NEW `test_chat_detect.py` — 47 checks (tracker, real DOM extraction through
      jsdom fixtures, wiring, browser-layer integration): **47/47 PASS**
- [x] NEW `tools/chat_detect_debug.py` — shows exactly what the bot can read
      (`--html saved_page.html` or `--url …`)
- [x] Regression suites unchanged: test_flow 124/124, test_live 44/44, test_fuzz 4/4,
      test_matcher round-trip OK, demo_flow/demo_chat PASS

### Phase 10 — Simplified Logic ✅
- [x] `chat/rules.py`: Removed complex state machine from `_decide_reply()`
- [x] Replaced with simple flow: IOEngine scan → horny/flirty/normal detection → asksc → sharesc → END
- [x] `chat/horny_flirty_db.py`: Moved compliment keywords from HORNY to FLIRTY (PATH A vs PATH B)
- [x] `data/input/02_age_gender.txt`: Added female triggers (f20, f 21, etc.)
- [x] `test_live.py`: Updated Phase 5 (removed persona assertions), Phase 8 (love = flirty), Phase 10 (ok/brb = normal)
- [x] All 44/44 test_live passed, 4/4 test_fuzz passed, 21/21 test_matcher passed

## 12. ACTIVE TASK TRACKING

Current state: **ALL TASKS COMPLETED**

When a new task is assigned:
1. Read this AGENTS.md first to understand what's done
2. Check `docs/TASKS.md` and `docs/PROJECT_STATE.md` for context
3. Identify which files need to be modified
4. Verify the modification doesn't break completed work
5. Run tests: `python test_live.py && python test_fuzz.py`
6. Update this AGENTS.md with the new completed task

## 13. FILE STRUCTURE REFERENCE

### Data Files (ALL EDITABLE BY USER)
```
data/output/
├── greeting.txt          (64 lines) — GREETING state replies
├── age_gender.txt        (52 lines) — AGE/GENDER capture replies
├── country.txt           (976 lines) — COUNTRY capture replies
├── how_are_you.txt       (203 lines) — HOW ARE U replies
├── horny.txt             (36 lines) — HORNY replies
├── flirty_questions.txt  (1004 lines) — FLIRTY_ASK questions
├── ask_snap.txt          (298 lines) — ASKSC (ask snap)
├── share_snap.txt        (23 lines) — SHARESC (share snap)
├── snapchat.txt          (8 lines) — SNAPCHAT request
├── your_age.txt          (17 lines) — AGE question
├── english_only.txt      (4 lines) — ENGLISH ONLY
├── age_ask.txt           (6 lines) — Age collection ask
├── country_ask.txt       (5 lines) — Country collection ask
├── gender_ask.txt        (4 lines) — Gender collection ask
├── country_dodge.txt     (5 lines) — Country dodge response
├── retry_nudge.txt       (4 lines) — Snap retry nudge
├── answer_ack.txt        (10 lines) — Answer acknowledgement
├── agreement_ack.txt     (8 lines) — Agreement acknowledgement
├── agreement_breaker.txt (6 lines) — Agreement streak breaker
├── info_ack.txt          (8 lines) — Info acknowledgement
├── answer_followup.txt   (8 lines) — Answer follow-up
├── middle_chat/
│   ├── horny_reply.txt   (10 lines) — MIDDLE horny path
│   ├── flirty_reply.txt  (40 lines) — MIDDLE flirty path
│   ├── warm_reply.txt    (20 lines) — MIDDLE warm ack
│   ├── new_topic.txt     (6 lines) — Topic shift
│   └── busy_later.txt    (6 lines) — Busy/leaving
```

### Code Files (DO NOT MODIFY UNLESS ASKED)
```
chat/rules.py          — Main conversation engine (2300+ lines)
chat/rule_bot.py       — ChatRuleBot wrapper
chat/common_scan.py    — Regex classifier
chat/persona.py        — Persona layer
chat/tg_brain.py       — TgBrain fallback
chat/style_analyzer.py — Style mirroring
chat/horny_flirty_db.py — Horny/flirty detection
chat/database/loader.py — DB loader
entry/main.py          — GUI dashboard
entry/thread_manager.py — Thread orchestration
browser/browser_automation.py — Browser worker
browser/chat_reader.py — Stranger-SMS detection (multi-selector + tracker)
test_chat_detect.py    — Detection test-suite (47 checks)
tools/chat_detect_debug.py — "what can the bot read?" page inspector
```

## 14. CRITICAL RULES FOR FUTURE AGENTS

1. **NEVER modify a file unless the user explicitly asks for that specific file**
2. **ALWAYS run `python test_live.py && python test_fuzz.py` after any change**
3. **ALWAYS read AGENTS.md first to understand what's completed**
4. **Make the SMALLEST correct change — no rewriting, no reformatting**
5. **If a task seems complex, ask the user for clarification first**
6. **Document what you changed and why in the final response**
7. **Never claim "100% working" without running tests**
