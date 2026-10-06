"""Flow Reference — Complete chat flow with file locations.

This file shows the complete flow of EVA bot with exact file locations
for each step. Use this to understand where replies come from.
"""

import sys

# ============================================================================
# COMPLETE FLOW REFERENCE
# ============================================================================

FLOW_REFERENCE = """
╔══════════════════════════════════════════════════════════════════════════════╗
║                    EVA BOT — COMPLETE FLOW REFERENCE                       ║
╚══════════════════════════════════════════════════════════════════════════════╝

┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 1: FIRST SMS (Greeting)                                              │
├─────────────────────────────────────────────────────────────────────────────┤
│  User: "hi" / "hey" / "hello"                                              │
│                                                                             │
│  FILE: data/output/greeting.txt                                             │
│  CODE: chat/rules.py → _first_sms_reply() → FIRST_GREETING                 │
│  REPLY: "hii" / "hey" / "hello" (random from greeting.txt)                 │
│                                                                             │
│  NEXT: STATE = WAIT_AGE_GENDER                                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 2: AGE/GENDER DETECTION                                               │
├─────────────────────────────────────────────────────────────────────────────┤
│  User: "m21" / "f20" / "hi m21"                                            │
│                                                                             │
│  FILE: data/output/age_gender.txt                                           │
│  CODE: chat/rules.py → _first_sms_reply() → FIRST_AGE_GENDER               │
│  REPLY: "f 23" / "f.21" / "f 20" (random from age_gender.txt)              │
│                                                                             │
│  STATE STORED: gender=male, age=21                                          │
│  NEXT: STATE = WAIT_COUNTRY                                                 │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 3: COUNTRY DETECTION                                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│  User: "usa" / "uk" / "from india"                                         │
│                                                                             │
│  FILE: data/output/country.txt                                              │
│  CODE: chat/rules.py → _first_sms_reply() → FIRST_COUNTRY                  │
│  REPLY: "usa you?" / "uk wbu?" (random from country.txt)                   │
│                                                                             │
│  STATE STORED: country=usa                                                  │
│  NEXT: STATE = MIDDLE                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 4: FLIRTY QUESTION                                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│  User: (any message after country)                                         │
│                                                                             │
│  FILE: data/output/flirty_questions.txt (1,004 questions)                   │
│  CODE: chat/rules.py → _flirty_lines() → MIDDLE                            │
│  REPLY: "whats ur" / "u single?" / "whats ur type?"                        │
│                                                                             │
│  NEXT: STATE = MIDDLE                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 5: MIDDLE CHAT (Adaptive based on user type)                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  USER TYPE DETECTION: chat/adaptive_flow.py → classify()                    │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ Horny keywords   → data/output/horny.txt → ASKSC (fast)            │    │
│  │ Flirty keywords  → data/output/middle_chat/flirty_reply.txt        │    │
│  │ Romantic keywords → sweet replies (code-based)                      │    │
│  │ Bored keywords   → fun/engaging replies (code-based)               │    │
│  │ Default          → data/output/middle_chat/warm_reply.txt          │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  CODE: chat/rules.py → _middle_reply_horny() / _middle_reply_flirty() etc  │
│  REPLY: Various based on user type                                          │
│                                                                             │
│  NEXT: STATE = ASKSC (when snap conditions met)                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 6: SNAP HINT                                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  User: (any message when ASKSC state)                                      │
│                                                                             │
│  FILE: db/snap/hint.txt (290 hints)                                         │
│  CODE: chat/rules.py → ASKSC state → _pool_choice()                        │
│  REPLY: "u on snp?" / "u on snap?" / "snp?"                               │
│                                                                             │
│  NEXT: STATE = SHARESC                                                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 7: SNAP SHARE                                                         │
├─────────────────────────────────────────────────────────────────────────────┤
│  User: (any message when SHARESC state)                                    │
│                                                                             │
│  FILE: data/output/snapchat.txt (8 replies)                                 │
│  CODE: chat/rules.py → SHARESC state → next_scshare_line()                 │
│  REPLY: "add me: Dumpqu" / "k: duMpqu"                                    │
│                                                                             │
│  STATE: snap_pivoted=True                                                   │
│  NEXT: STATE = END                                                          │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 8: END                                                                │
├─────────────────────────────────────────────────────────────────────────────┤
│  User: (any message when END state)                                        │
│                                                                             │
│  FILE: chat/rules.py (hardcoded closer)                                    │
│  REPLY: "ttyl, snap me"                                                     │
│                                                                             │
│  CHAT OVER                                                                  │
└─────────────────────────────────────────────────────────────────────────────┘

╔══════════════════════════════════════════════════════════════════════════════╗
║                         USER TYPE DETECTION                                 ║
╚══════════════════════════════════════════════════════════════════════════════╝

  FILE: chat/adaptive_flow.py
  
  KEYWORDS:
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ Horny:   horny, sexy, dtf, nude, send pic, hookup, fuck, phone sex    │
  │ Flirty:  cute, adorable, hot, beautiful, u single, ur type, kiss      │
  │ Romantic: love u, miss u, together, relationship, soulmate, forever    │
  │ Bored:   bored, boring, nothing to do, kill time, pass time           │
  │ Direct:  wyd, what u doing, u down, u free, lets go                   │
  │ Curious: (high question ratio > 50%)                                   │
  │ Shy:     (short messages < 10 chars)                                   │
  │ Default: FRIENDLY                                                      │
  └─────────────────────────────────────────────────────────────────────────┘

  FLOW SPEED:
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ Horny:   3-4 replies (fastest) - skip country/age                     │
  │ Flirty:  5-6 replies (fast)                                           │
  │ Romantic: 5-6 replies (fast)                                          │
  │ Bored:   6-7 replies (medium)                                         │
  │ Direct:  6-7 replies (medium)                                         │
  │ Curious: 7-8 replies (slow)                                           │
  │ Shy:     8-9 replies (slowest) - skip country/age                     │
  │ Friendly: 8-9 replies (slowest)                                       │
  └─────────────────────────────────────────────────────────────────────────┘

╔══════════════════════════════════════════════════════════════════════════════╗
║                         SNAP CONDITIONS                                     ║
╚══════════════════════════════════════════════════════════════════════════════╝

  FILE: chat/rules.py → _can_share_snap()
  
  NORMAL MODE (gender + age + country required):
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ has_gender = True                                                      │
  │ has_age = True                                                         │
  │ has_country = True                                                     │
  │ not_blocked = True (age >= 18)                                         │
  │ not_pivoted = True (snap not shared yet)                               │
  └─────────────────────────────────────────────────────────────────────────┘
  
  HORNY MODE (only gender required):
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ has_gender = True                                                      │
  │ Horny_minimal = True                                                   │
  │ not_blocked = True                                                     │
  │ not_pivoted = True                                                     │
  └─────────────────────────────────────────────────────────────────────────┘

╔══════════════════════════════════════════════════════════════════════════════╗
║                         FILE LOCATIONS                                      ║
╚══════════════════════════════════════════════════════════════════════════════╝

  INPUT FILES (user messages → bot replies):
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ data/output/greeting.txt           (96 replies)                        │
  │ data/output/age_gender.txt         (48 replies)                        │
  │ data/output/country.txt            (966 replies)                       │
  │ data/output/flirty_questions.txt   (1,004 replies)                     │
  │ data/output/horny.txt              (123 replies)                       │
  │ data/output/reaction.txt           (240 replies)                       │
  │ data/output/goodbye.txt            (49 replies)                        │
  │ data/output/how_are_you.txt        (203 replies)                       │
  │ data/output/feelings.txt           (177 replies)                       │
  │ data/output/hobbies.txt            (50 replies)                        │
  └─────────────────────────────────────────────────────────────────────────┘
  
  SNAP FILES:
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ db/snap/hint.txt                 (290 hints)                           │
  │ data/output/snapchat.txt         (8 share replies)                     │
  │ data/output/snap_ids.txt         (usernames)                           │
  └─────────────────────────────────────────────────────────────────────────┘
  
  MIDDLE CHAT FILES:
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ data/output/middle_chat/flirty_reply.txt                               │
  │ data/output/middle_chat/warm_reply.txt                                 │
  │ data/output/middle_chat/horny_reply.txt                                │
  │ data/output/middle_chat/new_topic.txt                                  │
  │ data/output/middle_chat/busy_later.txt                                 │
  └─────────────────────────────────────────────────────────────────────────┘

  CODE FILES:
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ chat/rules.py                  (state machine, 2,900+ lines)           │
  │ chat/adaptive_flow.py          (user type detection)                   │
  │ chat/first_message.py          (first SMS classification)              │
  │ chat/style_analyzer.py         (style mirroring)                       │
  │ chat/persona.py                (persona answers)                       │
  │ chat/horny_flirty_db.py        (keyword matching)                      │
  └─────────────────────────────────────────────────────────────────────────┘
"""

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    print(FLOW_REFERENCE)
