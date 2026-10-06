# TASKS.md — Task Tracking for EVA Bot

---

## Active Tasks

None currently assigned.

---

## Completed Tasks

### Phase 1 — Critical Bug Fixes ✅
- [x] GUI loads snap_ids.txt + flirty_questions.txt
- [x] Create mode error handling
- [x] Account marking fix
- [x] Resource cleanup fix
- [x] {snap} placeholder resolution
- [x] DB paths configurable

### Phase 2 — High Priority Fixes ✅
- [x] Style mirroring enabled
- [x] Multi-line message handling
- [x] Log filtering fix
- [x] Camoufox engine param
- [x] Proxy URL fix

### Phase 3 — Medium Priority Fixes ✅
- [x] Word-boundary matching
- [x] Non-blocking thread stagger
- [x] Stats counting fix
- [x] CPU monitoring fix
- [x] Exception handling fix

### Phase 4 — Dead Code Cleanup ✅
- [x] Removed 352 lines of unused pools
- [x] Restored FLOWS dict
- [x] Removed no-op replace
- [x] Removed unused function

### Phase 5 — Smarter Chat Rules ✅
- [x] Flow-type influence (70/30 mix)
- [x] Memory-aware follow-ups
- [x] Retry bound reduction

### Phase 6 — Test Fixes ✅
- [x] Phase 9 pattern detector fix
- [x] All files pass py_compile
- [x] All imports verified
- [x] 51/51 test_live passed
- [x] 4/4 test_fuzz passed

### Phase 7 — File Cleanup & Debug ✅
- [x] Removed 7 dead txt files
- [x] Merged 127 unique lines
- [x] Added line number tracking
- [x] Debug shows file paths

### Phase 8 — Country Collection Fix ✅
- [x] Country collection → flirty_questions.txt

### Phase 11 — Stranger-SMS detection ✅
- [x] `browser/chat_reader.py` created (multi-selector reader + MessageTracker)
- [x] `extract_chat_from_page()` no longer depends on one hardcoded selector set
- [x] Counter-drift bug removed (own message not rendered -> SMS went unseen)
- [x] Loud "0 messages parsed" warning + `EVA_DUMP_CHAT_DOM=1` page dump
- [x] `tools/chat_detect_debug.py` page inspector
- [x] `test_chat_detect.py` 47/47 + all existing suites still green

### Phase 9 — Hardcoded Pools to TXT ✅
- [x] Created 10 new txt files
- [x] All pools now from txt files
- [x] Debug shows file + line number

---

## How to Add a New Task

1. Describe the task clearly
2. Identify which files need modification
3. Check AGENTS.md for completed work
4. Verify the change doesn't break completed work
5. Run tests after implementation
6. Mark as [x] when done and verified
