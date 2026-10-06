# RUNTIME_AND_CHAT_FLOW.md — thread management + session→chat backend (Phase 15)

দুইটা প্রশ্নের উত্তর এই ডকুমেন্টে:

1. **multiple browser/session চালালেও PC hang/leg হবে না — কীভাবে?**
   (adaptive thread management, pause gate, capacity planning, governor)
2. **backend থেকে শুধু cookie/token/session দিয়ে user-এর সাথে connect করে incoming
   SMS detect করে reply — পুরো flow কী?** (কোথায় কোন ফাইল, কোন ধাপ)

---

## Part 1 — Advanced thread management (PC never freezes)

### 1.1 চারটা স্তর (কম কঠিন → বেশি কঠিন)

```
        ┌───────────────────────────────────────────────────────────┐
        │ 1. ThreadScheduler   core/thread_scheduler.py             │
        │    • CPU/RAM দেখে thread বাড়ায়/কমায় (hysteresis+cooldown)│
        │    • ramp: প্রথমে অল্প thread, তারপর ধীরে ধীরে বাড়ে        │
        │    • red load → সব worker PAUSE (browser খোলা থাকে)        │
        └───────────────┬───────────────────────────────────────────┘
                        │ tick() প্রতি ~১ সেকেন্ডে (GUI timer)
        ┌───────────────▼───────────────────────────────────────────┐
        │ 2. PauseGate  (workers সম্মতিতে থামে)                      │
        │    worker গুলো প্রতি chat-এর মাঝে                        │
        │    pause_gate().wait_if_paused() call করে → কিছুই mismatch │
        │    হয় না, browser বন্ধ হয় না, cookie নষ্ট হয় না           │
        └───────────────┬───────────────────────────────────────────┘
                        │
        ┌───────────────▼───────────────────────────────────────────┐
        │ 3. ResourceGovernor  core/resource_governor.py            │
        │    Tier1 THROTTLE (নতুন launch বন্ধ)                       │
        │    Tier2 RECYCLE (অতিরিক্ত heavy context close+rebuild)     │
        │    Tier3 REDLINE (সবচেয়ে ভারী browser process kill)        │
        └───────────────┬───────────────────────────────────────────┘
                        │
        ┌───────────────▼───────────────────────────────────────────┐
        │ 4. Context pool / launch gate                       │
        │    max_contexts semaphore, staggered starts,              │
        │    Firefox memory prefs (Chromium: block_resource_types)   │
        └───────────────────────────────────────────────────────────┘
```

### 1.2 কখন কী হয় (decision table)

| অবস্থা (measured) | Scheduler যা করে | Log |
|---|---|---|
| CPU < 60% **এবং** RAM < 60% **এবং** free RAM ≥ `reserve_mb + ram_per_browser_mb × (n+1)` | thread +1 (ceiling `max_threads` পর্যন্ত) | `[Scheduler] ↑ threads 2 → 3 (load allows one more thread)` |
| CPU > 85% **বা** RAM > 80% | সবচেয়ে নতুন thread বন্ধ (restart ছাড়াই, ধীরে ধীরে শেষ হয়) | `[Scheduler] ↓ threads 3 → 2 (load is high …)` |
| CPU/RAM > 90% **বা** free RAM < `pause_when_free_below_mb` | **সব worker PAUSE** (browser window/প্রোফাইল খোলা, শুধু কাজ বন্ধ) + একটা thread shed | `[Scheduler] ⛔ paused all workers — CPU 95% …` |
| load আবার < `resume_below` | resume | `[Scheduler] ✓ resumed — CPU 40%` |
| শুরুতে requested > capacity | clamp করে কম thread দিয়ে start, তারপর load দেখে বাড়ে | `[Scheduler] 10 thread(s) requested but this PC is comfortable with 4 …` |

### 1.3 Capacity planning — "আমার PC কতটা নেবে?"

```bat
python tools/thread_planner.py                :: এখনকার অবস্থা + সুপারিশ
python tools/thread_planner.py --simulate     :: চার load স্তরের সিদ্ধান্ত (browser ছাড়া)
python tools/thread_planner.py --threads 10   :: ১০ thread দিলে কী হবে?
python tools/thread_planner.py --json
```

সূত্র: `recommended = min( (free_RAM − reserve) / ram_per_browser_mb , cores × 0.75 , max_threads )`
(`ram_per_browser_mb` ডিফল্ট 450 MB, `reserve_mb` ডিফল্ট 2048 MB — Windows + আপনার apps এর জন্য)

### 1.4 Config (`data/config.json` → `thread_scheduler`)

```json
"thread_scheduler": {
  "enabled": true,
  "adaptive": true,
  "min_threads": 1,
  "max_threads": 6,
  "start_threads": 2,
  "ram_per_browser_mb": 450,
  "reserve_mb": 2048,
  "cpu_scale_up": 0.60, "mem_scale_up": 0.60,
  "cpu_scale_down": 0.85, "mem_scale_down": 0.80,
  "pause_above": 0.90, "resume_below": 0.70,
  "pause_when_free_below_mb": 700,
  "scale_cooldown_seconds": 45,
  "scale_up_step": 1, "scale_down_step": 1,
  "ramp_seconds": 20,
  "sample_interval": 3,
  "rest_between_sessions_seconds": 0,
  "log_interval_seconds": 60
}
```

* `max_threads` — আপনার PC-এর জন্য hard ceiling। দুর্বল PC-তে 2–3, 16 GB-তে 4–6, 32 GB-তে 8+।
* `adaptive: false` — শুধু pause protection থাকবে, thread count আপনার হাতেই।
* `rest_between_sessions_seconds` — প্রতি chat-এর পরে বাধ্যতামূলক idle (আরও নরম দেখতে + কম load)।
* `pause_above` / `pause_when_free_below_mb` — এই দুটোই "hang হবে না" এর আসল গ্যারান্টি।

### 1.5 কোথায় কোড

| কাজ | ফাইল |
|---|---|
| সিদ্ধান্ত + capacity + pause gate | `core/thread_scheduler.py` |
| worker বাড়ানো/কমানো, clamp, wind-down | `entry/thread_manager.py` (`_apply_thread_target`, `recommended_thread_count`, `_on_thread_finished`) |
| worker সম্মতিতে থামা | `browser/browser_automation.py` (`_wait_for_scheduler`, `_inter_chat_rest`) |
| শেষ রক্ষা (throttle/recycle/redline) | `core/resource_governor.py` |
| capacity CLI | `tools/thread_planner.py` |

---

## Part 2 — Backend: cookie/token/session → connect → SMS detect → reply

### 2.1 পুরো pipeline (এক নজরে)

```
 accounts.txt / storage_state.json / browser_profiles/<account>/
        │
        │ ① session_health.py   → cookie গুলো কাজ করবে? (ok/expiring/expired/…)
        ▼
 [Session] health + plan ─────────────► blind? credentials থাকলে repair (login),
        │                                না থাকলে এই account skip (browser খুলবেই না)
        │ ② browser_identity.py → permanent device profile (UA/screen/tz/...)
        ▼
 [Identity] device ────────────────────► একই account = একই browser, অন্য account = অন্য device
        │ ③ claim_profile()     → account-এর নিজের Chrome profile (owner.json guard)
        ▼
 [Profile] browser_profiles/<account>/ ─► cookie, IndexedDB, cache, history এখানেই থাকে
        │ ④ verify_identity_on_page() → site যা দেখে সেটার সাথে মিলিয়ে দেখা (login-এর আগে)
        ▼
 [Fingerprint] ✓ device verified ──────► mismatch হলে (verify_strict) login আটকে যায়
        │ ⑤ context.storage_state / persistent profile → app.chitchat.gg
        ▼
 [Restore] session restored ───────────► restore_saved_account_session() page probe দিয়ে verify
        │ ⑥ START CHAT → http://app.chitchat.gg/start/new → popup → chat
        ▼
 [Chat] waiting for stranger … 
        │ ⑦ প্রতি poll (poll_interval_seconds ≈ 1s):
        │      chat_reader.extract_with_diag(page)   → DOM থেকে message list
        │      MessageTracker.sync(...)               → শুধু *নতুন* message
        ▼
 [SMS]   Stranger: hi …  ───────────────► count + live dashboard (chat_signal)
        │ ⑧ human_behavior: read pause → typing simulator → send
        ▼
 [REPLY] ChatRuleBot.reply(text, state) → data/output/*.txt থেকে reply → send_chat_message()
        │ ⑨ keep-alive: refresh_storage_state() throttled (session_refresh_minutes)
        ▼
 [Session] cookies refreshed ──────────► পরের run-এও account logged-in থাকবে
```

### 2.2 কোন ধাপে কোন code

| ধাপ | ফাইল / ফাংশন |
|---|---|
| ① health + plan | `browser/session_health.py` → `session_health()`, `plan_for_account()` |
| ② device profile | `browser/browser_identity.py` → `identity_for()` |
| ③ profile ownership | `browser_identity.claim_profile()` + `owner.json` |
| ④ fingerprint verify | `browser_identity.verify_identity_on_page()` (JS: `PAGE_FINGERPRINT_JS`) |
| ⑤ session restore | `browser/browser_automation.py` → `restore_saved_account_session()` |
| ⑥ chat open | `start_chitchat_session()`, `start_new_chat()` |
| ⑦ SMS read | `extract_chat_from_page()` + `browser/chat_reader.py` (`extract_with_diag`), `MessageTracker.sync()` |
| ⑧ human typing | `browser/human_behavior.py` (`typing_sim`, read pauses) |
| ⑨ reply | `chat/rule_bot.py` (`ChatRuleBot.reply`) → `send_chat_message()` |
| ⑩ keep-alive | `account_session_store.refresh_storage_state()` (session-এ লেখে) |

### 2.3 চালানো: GUI ছাড়া, শুধু session দিয়ে (headless default)

```bat
:: একটা account, ৩০ মিনিট, কোনো window নেই
python tools/session_chat.py --account sadia.6.7@gmail.com --minutes 30

:: সব usable session, একটার পর একটা
python tools/session_chat.py --all --minutes 15

:: window দেখতে চাইলে
python tools/session_chat.py --account EMAIL --visible

:: শুধু connect test: open → fingerprint verify → restore → close
python tools/session_chat.py --account EMAIL --dry-run

:: কী চালবে দেখে নিন (browser ছাড়া)
python tools/session_chat.py --all --plan-only
```

প্রতিটা ধাপ log-এ tag সহ আসে — grep করলেই হবে:

```
[Session] running sadia.6.7@gmail.com (session-only restore)
[Identity] sadia.6.7@gmail.com → Windows · Chrome/131 · 1898x941 · hw 8 · mem 8GB · en-US/…
[Browser] Opening the account's own browser profile: …\browser_profiles\sadia.6.7@gmail.com
[Fingerprint] ✓ device verified before login: Windows · 1898x941 · America/New_York
[Step 1/3] ✓ Saved account session restored!
[Chat #1] Waiting for stranger's first message
[SMS]   user: hey
[REPLY] bot: hi :)
[Session] cookies refreshed after chat #1
```

### 2.4 "backend" বলতে কী বোঝায় — সার্ভারে চালানোর নিয়ম

* **কোনো password লাগে না** — শুধু `account_sessions/<account>/storage_state.json`
  (cookies + localStorage) আর/বা `browser_profiles/<account>/`।
* **Headless**: `--headless` (ডিফল্ট)। সার্ভারে X না থাকলে Chromium headless-ই চলে।
* **Token vs cookie**: chitchat.gg cookie-based auth (`__Secure-authjs.session-token`)।
  `storage_state.json`-এ ওগুলোই থাকে; session-এর ভিতরের `expires` timestamp দেখে
  `expiring`/`expired` ঠিক হয় — তাই "token dead" আগেই ধরা পড়ে।
* **Keep-alive**: দীর্ঘ run-এ প্রতি `session_refresh_minutes` (ডিফল্ট 10) মিনিটে cookie
  আবার লেখা হয়, তাই পরের বার আবার login লাগে না।
* **এক account এক browser**: একই account দুই জায়গায় (দুই process/দুই PC) একসাথে
  চালাবেন না — একই device-এর দুই session সাইটে suspicious লাগে। এক account = এক worker।

### 2.5 SMS detect কেন ভাঙে না (এবং ভাঙলে কী করবেন)

* `chat_reader.extract_with_diag()` কয়েক সেট selector চেষ্টা করে, এবং chat row-এর
  fingerprint (`mid` বা speaker+text) ব্যবহার করে — তাই "আমার message DOM-এ নেই" হলে
  counter এগিয়ে গিয়ে stranger-এর message মিস হয় না (`MessageTracker`)।
* ৫ poll ধরে ০ message পড়লে: `[Detect] WARNING …` + selector suggestion।
  `EVA_DUMP_CHAT_DOM=1` দিলে পুরো HTML সেভ হবে →
  `python tools/chat_detect_debug.py --html <file> --suggest` (বা `--save` দিয়ে ঠিক করা)।
* `EVA_CHAT_SELECTORS` env দিয়ে নিজের selector set-ও দিতে পারেন (`config/chat_selectors.example.json`)।

---

## 3. Tests

| Suite | কী কভার করে |
|---|---|
| `test_thread_scheduler.py` (93 checks) | capacity plan · scale up/down · hysteresis/cooldown · pause gate (block, resume, Stop) · degradation · manager grow/shrink + wind-down · planner CLI · session_chat selection/dry-run/watchdog/CLI |
| `test_session_health.py` (152) | cookie health, repair, keep-alive, pre-flight skip |
| `test_browser_identity.py` (207) | device profile, owner guard, verify-before-login, profile save |
| `test_flow.py` / `test_live.py` / `test_fuzz.py` / `test_matcher.py` | reply engine |
| `test_chat_detect.py` / `test_selector_doctor.py` | DOM detection + self-heal |

চালান: `python test_thread_scheduler.py` · `python tools/thread_planner.py --simulate`
