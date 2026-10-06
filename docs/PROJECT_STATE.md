# PROJECT_STATE.md — EVA Bot Current State

Last updated: Phase 12 — Site markup changes handled automatically (selector doctor)

---

## Current Status: ALL TASKS COMPLETED ✅

### Test Results
- `test_chat_detect.py`: 47/47 passed (SMS detection)
- `test_selector_doctor.py`: 105/105 passed (markup change handling; 96/96 without jsdom)
- `test_flow.py`: 124/124 passed
- `test_live.py`: 44/44 passed
- `test_fuzz.py`: 4/4 passed
- `test_matcher.py`: ALL CATEGORIES ROUND-TRIP OK
- `demo_chat.py`: ALL CHATS REACHED END

### What Works
1. **Chat Engine** — 12 flow types, horny deflection, IO TXT matcher, regex classifier
2. **File-Based Replies** — All replies from editable txt files (32 files total)
3. **Debug Output** — Shows exact file path + line number for each reply
4. **Style Mirroring** — Enabled after 5 messages
5. **Memory-Aware** — Uses known facts for follow-ups
6. **Flow Mixing** — 70% main pool / 30% flow-specific variety
7. **Country Collection** — Directly to flirty_questions.txt after country capture
8. **All Pools Editable** — 10 previously hardcoded pools now in txt files
9. **Auto-Healing Selectors** — `browser/selector_doctor.py` derives the
   selectors from the page when the site is redesigned; `config/chat_selectors.json`
   applies them without code changes; the bot also learns them at runtime
   (`diag.healed`) and never guesses on non-chat pages

### File Structure
- `data/output/` — 22 txt files (ALL EDITABLE BY USER)
- `data/output/middle_chat/` — 5 txt files (ALL EDITABLE BY USER)
- `data/input/` — 8 txt files (trigger patterns)

### How to Test
```bash
# Run all automated tests
test.bat

# Or manually:
python test_live.py      # 51 checks
python test_fuzz.py      # 4 fuzz checks
python test_matcher.py   # Pool roundtrip
python demo_chat.py      # Scripted chats

# Interactive REPL:
python tools/live_chat.py
```

### Debug Output Format
```
you> hey
eva> hii stranger
   # greeting.txt line 61

you> m 21
eva> F-25
   # age_gender.txt line 14

you> uk
eva> what u do for fun?
   # flirty_questions.txt line 42
```

---

## Completed Work (DO NOT REPEAT)

See `AGENTS.md` Phase 1-8 for full details of all completed tasks.

### Key Fixes Applied
- `{snap}` placeholder resolved
- Style mirroring enabled
- Multi-line message handling
- Word-boundary matching
- Non-blocking thread stagger
- Dead code cleanup (352 lines removed)
- File cleanup (7 dead files removed, 127 lines merged)
- Country collection fix
- Debug output with file paths + line numbers
- **Stranger-SMS detection (Phase 11)** — see `browser/chat_reader.py`:
  multi-selector reading, fingerprint-based new-message tracking,
  "0 messages parsed" warning, `tools/chat_detect_debug.py` inspector
- **Markup-change handling (Phase 12)** — `browser/selector_doctor.py` +
  `config/chat_selectors.json` + runtime self-healing; fix a changed site
  with `python tools/chat_detect_debug.py --html page.html --save`

---

## Known Issues (None Critical)

All critical bugs have been fixed. The system is stable.

---

## Future Tasks

When assigning a new task:
1. Read `AGENTS.md` first
2. Check this file for current state
3. Identify which files need modification
4. Verify the change doesn't break completed work
5. Run tests: `python test_live.py && python test_fuzz.py`
6. Update this file and `AGENTS.md`
