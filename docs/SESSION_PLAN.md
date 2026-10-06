# SESSION_PLAN.md — Session auto-load, blind session detection & recovery (Phase 13)

Problem in one line: **a saved session is only useful if the browser can pick it
up automatically, and a dead (“blind”) session must be detected *before* a
browser launch and repaired instead of silently failing.**

## 1. What is broken today (verified in the code)

| # | Symptom | Where | Why it hurts |
|---|---------|-------|--------------|
| 1 | “Restore” is judged by `"login" in page.url` only | `browser_automation.restore_saved_account_session` | SPA soft-redirects / challenge pages pass as “restored”, real expiry is missed |
| 2 | Nothing looks inside `storage_state.json` before launching | — | a 0-byte / corrupt / fully expired session still costs a full browser launch |
| 3 | A dead session is a dead end: no fallback to `accounts.txt` credentials | `start_chitchat_session` (restore path returns False) | account is marked failed even though we could just log in again |
| 4 | Two session roots: `account_sessions/` and `data/account_sessions/` | `account_session_store.get_account_sessions_dir` vs `thread_manager._start_context_pool` | sessions saved by the bot are invisible to the context pool → “acc browser e load” silently does nothing |
| 5 | Pool mode requires `storage_state_path`, never repairs | `thread_manager._acquire_pool_slot` | blind session → worker falls back to a fresh browser or fails |
| 6 | Cookies are saved once at session start, never refreshed | `_save_successful_account_session` | long runs die mid-way when cookies expire; health is never recorded |
| 7 | “Alive accounts” counts a dead session as alive | `account_session_store.count_alive_sessions` | stock numbers lie |

## 2. Design

```
                     ┌──────────────────────────────────────────────┐
  accounts.txt  ───► │ browser/session_health.py  (new, stdlib only) │
  account_sessions/  │  inspect storage_state → classify health      │
  data/*/…           │  credentials map · plan_for_account()         │
                     │  prune dead · auth page probe + interpreter   │
                     └───────────────┬──────────────────────────────┘
                                     │
        ┌────────────────────────────┼─────────────────────────────┐
        ▼                            ▼                             ▼
 account_session_store        account_manager              browser_automation
 (metadata health block,      (both roots, creds,         (verify after restore,
  refresh storage_state)       blind status, skip)          auto-login repair,
                                                           keep-alive refresh)
                                     │
                                     ▼
        thread_manager (pool: never create a context from a blind session)
        tools/session_doctor.py (CLI: --list/--check/--prune/--live-check)
```

### Session states

`ok` · `expiring` (live but < 3 days) · `expired` · `empty` (no cookies) ·
`corrupt` (unreadable JSON) · `missing` (no file/metadata).
`blind = missing | empty | corrupt | expired` → the browser must not be
launched from it unless a repair (credential login) is possible.

### Decision table used by every caller (`plan_for_account`)

| health | credentials in accounts.txt | action |
|--------|-----------------------------|--------|
| ok / expiring | any | `restore` — load the session into the browser |
| blind | yes | `repair` — login with the stored credentials, then re-save the session |
| blind | no | `skip` — log a clear “BLIND SESSION” line, do not waste a launch |
| (banned flag) | — | `skip` (never re-offer) |

### Runtime behaviour after the change

1. `restore` → open `restore_url`, run the **auth probe** (`PAGE_AUTH_JS`:
   login form / chat UI / username node / url rules). Not authenticated →
   treat as blind.
2. blind + credentials → `login_with_account()` (the *same* proven flow), then
   the session is saved again (fresh cookies) and marked `repaired`.
3. blind + no credentials → one clear log, session metadata gets
   `health.blind=true`, `blind_since=…`; with `prune_dead_sessions` the folder
   is moved to `account_sessions/_dead/` (never deleted, bans untouched).
4. Every chat end (throttled, `session_refresh_minutes`) → storage_state is
   written again + metadata `health`/`last_verified_at`/`last_refresh_at`
   updated → cookies stay warm for the next run.
5. Context pool → only creates contexts for `restore`/`repair` plans; blind
   sessions are skipped with a reason instead of creating an empty context.

### Config (`config.json` → `session_management`)

| key | default | meaning |
|-----|---------|---------|
| `verify_after_restore` | `true` | run the auth probe after opening a restored session |
| `auto_repair_blind_sessions` | `true` | login again when the session is blind and credentials exist |
| `blind_fallback_to_login` | `true` | alias used by the automation for the same repair |
| `session_refresh_each_chat` | `true` | re-save the session during chats |
| `session_refresh_minutes` | `10` | minimum minutes between two refreshes |
| `session_expiry_warn_days` | `3` | “expiring” threshold |
| `prune_dead_sessions` | `false` | move blind/expired sessions to `_dead/` (never delete) |
| `log_health_on_start` | `true` | print the session health summary at startup |
| `sessions_dir_name` | `account_sessions` | primary session root (all roots are scanned) |

### Files

| file | change |
|------|--------|
| `browser/session_health.py` | **new** — classification, plans, credentials, prune, auth probe |
| `browser/account_session_store.py` | roots helper, `refresh_storage_state`, `record_session_health`, health-aware stock counts |
| `core/account_manager.py` | both roots + credentials + `blind` status, skip blind accounts |
| `browser/browser_automation.py` | verified restore, blind→repair, keep-alive refresh, pool-mode verify |
| `entry/thread_manager.py` | pool loads from all roots, skips blind sessions |
| `core/config_loader.py` | new `session_management` keys |
| `tools/session_doctor.py` | **new** — CLI (`--list`, `--check`, `--prune`, `--live-check`) |
| `test_session_health.py` | **new** — offline test suite (152 checks, green) |

### Status (2026-10-06) — **BUILT & GREEN**

All of the above landed; the offline suite is green and the existing suites
were re-run afterwards:

| suite | result |
|-------|--------|
| `test_session_health.py` | **152/152 passed** (this phase) |
| `test_flow.py` / `test_matcher.py` / `test_live.py` / `test_fuzz.py` | green |
| `demo_chat.py` / `demo_flow.py` | green |
| `test_chat_detect.py` | **47/47 passed** |
| `test_selector_doctor.py` | **105/105 passed** (jsdom) |
| `compileall` on the touched files | clean |

Not verifiable inside this sandbox (needs Windows + a real browser):
the actual login on chitchat.gg and the pool-mode run.  Those paths are covered
with stub page/context objects, so the wiring is exercised but the live site
behaviour still needs one real run on the user's machine.

### Tests (offline, no browser needed)

* storage-state classification for ok / expiring / expired / empty / corrupt /
  missing files (hand-built fixtures in a temp dir),
* plan decisions per health × credentials × banned,
* session-root discovery (`account_sessions/`, `data/account_sessions/`,
  `EVA_SESSIONS_DIR`) and dedupe,
* metadata health recording + refresh throttle logic,
* automation: stub page + stub context → verified restore, blind repair
  (login called once), blind without credentials fails cleanly, keep-alive
  refresh writes the state, pool-mode verify skips blind sessions,
* CLI tool: `--list`, `--check`, `--prune --dry-run`, `--prune --apply`,
  `--refresh-metadata`, `--json`, exit codes,
* `run()` pre-flight: a blind account returns *before* any browser launch
  (the launch function raises `AssertionError` if it is ever reached).

### Config example

```json
"session_management": {
  "log_alive_count_on_start": true,
  "log_alive_count_after_each_chat": false,
  "sessions_dir_name": "account_sessions",
  "verify_after_restore": true,
  "auto_repair_blind_sessions": true,
  "blind_fallback_to_login": true,
  "session_refresh_each_chat": true,
  "session_refresh_minutes": 10,
  "session_expiry_warn_days": 3,
  "prune_dead_sessions": false,
  "log_health_on_start": true
}
```

Environment overrides: `EVA_SESSIONS_DIR` (extra roots, `;`/`:` separated),
`EVA_ACCOUNTS_FILE` (credentials file — default `accounts.txt`).

### Where to look in the logs

```
[Session] sessions total=4 alive=4 blind=0 (repairable=0) banned=0 [ok=4 ...]
[Session] BLIND SESSION sadia.6.7@gmail.com — 1 cookie(s) expired
[Session] repairing sadia.6.7@gmail.com by logging in with stored credentials...
[Session] ✓ BLIND SESSION repaired — fresh authenticated session
[Session] account skipped before launch — no browser started
```
