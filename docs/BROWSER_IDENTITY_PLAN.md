# BROWSER_IDENTITY_PLAN.md — one account = one permanent "device profile" (Phase 14)

User's requirement, in one line:

> **A saved session must reopen in the *same* browser it was created in
> (same UA / fingerprint / history), and when many accounts run at once every
> browser must be a *different* device — otherwise several accounts inside one
> browser identity is a problem.**

## 1. What the code does today (verified)

| # | Current behaviour | Where | Why it hurts |
|---|-------------------|-------|--------------|
| 1 | The Chromium UA / viewport / hw / memory are picked **at random on every launch** | `browser_engine._stealth_fingerprint()` + `chromium_context_kwargs()` | the *same account* looks like a brand-new device on every run |
| 2 | The stealth init script re-randomizes **screen size, DPR and window metrics** on every launch | `browser_engine._build_stealth_init_script()` | same account reports `1920x1080` today and `1366x768` tomorrow |
| 3 | Nothing stops two accounts from drawing the **same** UA + viewport | same pools, random picks | many accounts share one device identity — exactly the pattern sites flag |
| 4 | `fingerprint.json` stores `generated: null` (never set) — only *observed* values | `account_session_store.save_account_session` | the fingerprint of the login is captured but never re-applied |
| 5 | Saved fingerprint is only reused by **Camoufox**; Chromium ignores it | `browser_engine.launch_browser(fingerprint=…)` → ignored for chromium | the recommended engine has no identity continuity |
| 6 | Cookies/localStorage are restored, but IndexedDB / cache / history are not | `new_context(storage_state=…)` | not the *same* profile, only the same cookies |
| 7 | After a ban the next login reuses the same device | `_handle_ban` | a banned account logs back in from the "same device" (no clean slate) |

## 2. Design

### 2.1 Identity = deterministic, persisted, collision-free

```
account_key (email / folder name)
        │  sha256(account_key + salt + version)  ── deterministic, NOT random()
        ▼
   device identity:  UA · platform · viewport · screen · DPR · window metrics
                     hardwareConcurrency · deviceMemory · timezone · locale
        │
        ├─► <session_dir>/identity.json          (portable, travels with the session)
        └─► <primary_root>/_identities.json      (registry: guarantees uniqueness)
```

* **Same account → same identity, forever.** Derived from a hash of the account
  key, then persisted. No `random()` at launch time.
* **Different accounts → different identity.** The registry stores the signature
  (`UA|platform|viewport|hw|mem|tz`) of every account; when a new account is
  registered the seed is salted until the signature is unused (up to 200 tries,
  then log a warning).
* **Adopt what the login already used.** If `fingerprint.json` has an *observed*
  UA/platform from the run that created the session, that UA is adopted as the
  identity (keeps the device the account was logged in with), as long as it does
  not collide with another account.
* Cross-thread/cross-process safe: a lockfile (`_identities.lock`) + an in-process
  lock protect the read→derive→write cycle; writes are atomic (`os.replace`).

### 2.2 What "same browser" means in each mode

| | `profile_mode: "context"` (default) | `profile_mode: "persistent"` |
|---|---|---|
| browser | one shared browser, one context per account | one browser process per account |
| identity (UA/viewport/screen/tz/hw/mem) | ✅ stable per account | ✅ stable per account |
| cookies + localStorage | ✅ `storage_state.json` | ✅ real profile (cookies live in the profile) |
| IndexedDB / service workers / cache | ❌ | ✅ persists in `browser_profiles/<account>/` |
| real browsing history | ❌ (session-scoped) | ✅ Chromium's own `History` DB, grows every run |
| RAM cost | low (1 browser for N accounts) | 1 browser per account |
| when to use | 5–50 parallel accounts | few accounts, maximum realism |

Both modes keep identities **unique between accounts** (the registry covers all
modes). Persistent profiles are physically separate directories, so two accounts
can never share cookies or history.

### 2.3 Ban → rotate the device

`rotate_on_ban: true` (default) — when `_handle_ban` fires, the account's
identity is re-derived with a new salt (`rotations + 1`), so a re-login after a
ban happens from a *different* device. The registry is updated in place.

## 3. Config (`data/config.json` → top-level `browser_identity`)

| key | default | meaning |
|-----|---------|---------|
| `enabled` | `true` | master switch (false = old random-per-launch behaviour) |
| `profile_mode` | `"persistent"` | `persistent` = own real Chrome profile per account (default) · `context` = shared browser |
| `profiles_dir_name` | `"browser_profiles"` | where persistent profiles live (beside the session roots) |
| `unique_between_accounts` | `true` | registry-backed collision avoidance |
| `adopt_observed_fingerprint` | `true` | reuse the UA the account logged in with |
| `rotate_on_ban` | `true` | new device after a ban |
| `warmup_sites` | `[]` | URLs visited once inside a *new* persistent profile (history seed) |
| `log_identity_on_start` | `true` | log one line per account per launch |
| `owner_guard` | `true` | refuse a profile folder owned by another account |
| `verify_before_login` | `true` | check the live browser against the device profile before typing credentials |
| `verify_strict` | `false` | `true` = refuse to log in when the check fails |
| `verify_url` | `""` | optional real fingerprint-checker page used for the check |
| `save_profile_after_login` | `true` | write identity + owner + observed fingerprint after a successful login |

Env override: `EVA_BROWSER_PROFILE_MODE=persistent`.

## 4. Files

| file | change |
|------|--------|
| `browser/browser_identity.py` | **new** — derivation, registry, persistence, kwargs, rotate, audit |
| `browser/browser_engine.py` | stable screen/DPR/window metrics in the stealth script, persistent-context launch |
| `browser/browser_automation.py` | resolve identity before the context, use it for `new_context` + stealth, save it, rotate on ban, persistent mode |
| `browser/context_pool.py` | pooled contexts get the account identity + the stealth init script |
| `entry/thread_manager.py` | pass the identity into pool contexts, log it |
| `core/config_loader.py` | `DEFAULT_BROWSER_IDENTITY` + `load_browser_identity()` |
| `data/config.json` | new `browser_identity` block |
| `tools/session_doctor.py` | `--identities`, `--identity EMAIL`, `--new-identity EMAIL [--apply]` |
| `test_browser_identity.py` | **new** — offline suite |
| `README.md` | usage + mode table |

## 4b. Status (2026-10-06) — **BUILT & GREEN**

Everything above is implemented and `test_browser_identity.py` is green
(**207/207**).  Highlights verified offline:

* 60 accounts → 60 distinct devices (no collisions), stable across restarts,
* owner guard: a second account pointing at an existing profile folder is moved
  to its own folder and told why; colliding folder names are separated,
* fingerprint check: a matching browser passes, every mismatching value is
  listed, `verify_strict` blocks the login, a crashed page never blocks it,
* after login: `identity.json` + `owner.json(logged_in_at)` + `profile_state.json`
  are written, and the profile folder is reused on the next run,
* persistent launch opens the account's own profile directory (stub Playwright),
* context pool passes each account's identity + anti-detect init script; the
  pool is skipped entirely when `profile_mode` is `persistent`,
* `--profiles` reports ownership for every saved account.

## 5. Tests (offline, no browser)

* derivation is deterministic (same key → same identity, different process too),
* identity survives a restart (registry-only lookup) and is written into the
  session folder,
* 60 accounts → 0 signature collisions; salted re-derivation never loops forever,
* observed UA from `fingerprint.json` is adopted when free, skipped when taken,
* `context_kwargs()` / `stealth_fingerprint()` are coherent (platform matches UA,
  screen ≥ viewport, tz/locale pair consistent) and feed
  `chromium_context_kwargs(fingerprint=…)` + `apply_chromium_stealth()`,
* the stealth init script uses the identity's metrics instead of random ones,
* ban → rotation changes the signature, keeps the key, bumps `rotations`,
* pool options helper returns the same identity for the same account,
* config loader defaults + overrides, `EVA_BROWSER_PROFILE_MODE`,
* CLI: `--identities` (table + duplicate check), `--new-identity` dry-run vs `--apply`,
  `--profiles` (ownership table + exit code 2 when a profile is claimed by
  another account),
* profile guard: claim → owner recorded → intruder refused → separate folder,
* verification: match / mismatch / strict / disabled / checker page / crashed page,
* post-login save: identity.json + owner.json + profile_state.json written.
