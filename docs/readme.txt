================================================================================
EVA CHATBOT - CHITCHAT.GG
Complete System Documentation
TABLE OF CONTENTS
-----------------
1. Overview
2. Bot Identity
3. The 6 Unique Flow Types
4. Chat Flow Steps (Common to All Flows)
5. Gender & Age Detection
6. Country Detection & Rotation
7. Horny/Flirty Flow
8. Deflection System (What Bot Rejects)
9. Snap System (Username Cycling + Formats)
10. Reply Databases (13 Sources)
11. Multi-Line Message Handling
12. File Structure
13. Configuration
EVA is a rule-based chatbot for Chitchat.gg. It acts as a female user (EVA)
chatting with male users. The primary goal is to redirect users to Snapchat
through indirect hints and playful conversation.
KEY FEATURE: Each conversation randomly picks one of 6 unique flow types,
making every chat feel different and natural.
Name:       EVA
Age:        18-26 (random per session)
Gender:     Female (F)
Personality: Flirty, playful, casual SMS style
Language:   English (short messages, slang, abbreviations)
All replies are SHORT SMS style (under 10 words) like:
- "hey", "sup", "f 22", "im germany hbu?"
- "u read or watch shows?", "snap me lets go"
Each conversation randomly picks ONE flow type. This makes every chat unique.
FLOW 1: FRIENDLY (30% chance)
─────────────────────────────
Vibe: Warm, casual, getting to know each other
Step 3: "oh nice {country}!" / "{country} is dope!"
Step 4: "wbu do for fun?" / "what u into?" / "u single?"
Step 5: "oh nice! tell me more" / "thats actually cool"
Snap:   "ur fun ngl" → snap
FLOW 2: NIGHT (20% chance)
──────────────────────────
Vibe: Late night vibe, flirty, alone together
Step 3: "{country} vibes 🌙" / "its late there tho"
Step 4: "u alone rn?" / "cant sleep?" / "what keeps u up?"
Step 5: "haha maybe a little 😉" / "a little, why? 👀"
Snap:   "ur cute ngl 👀" → snap
FLOW 3: DEEP (15% chance)
─────────────────────────
Vibe: Emotional, meaningful conversation
Step 3: "oh {country} beautiful" / "i like {country} vibes"
Step 4: "u have any dreams?" / "whats ur biggest fear?"
Step 5: "omg same! we match 💕" / "i feel the same 🥰"
Snap:   "i feel connected to u" → snap
FLOW 4: FUN (15% chance)
────────────────────────
Vibe: Playful, teasing, lighthearted
Step 3: "{country}?? ur joking right 😂" / "no way {country} lolol"
Step 4: "whats ur most embarrassing story?" / "u believe in aliens?"
Step 5: "ngl ur fun to talk to" / "ur actually fun lol"
Snap:   "ur hilarious omg" → snap
FLOW 5: DIRECT (10% chance)
───────────────────────────
Vibe: Quick, confident, less small talk
Step 3: "{country} cool" / "oh {country} nice"
Step 4: "whats ur snap?" / "what u looking for here?"
Step 5: "ok ur cute" / "we should snap"
Snap:   "snap me" / "lets snap"
FLOW 6: ROMANCE (10% chance)
────────────────────────────
Vibe: Sweet, romantic, gentle
Step 3: "{country} is romantic 🥰" / "i love {country} vibes 💕"
Step 4: "whats ur idea of a perfect date?" / "u believe in fate?"
Step 5: "ngl ur adorable" / "ur literally so cute"
Snap:   "i wanna talk to u more 💕" → snap
FLOW SELECTION WEIGHTS:
friendly: 30% (most common)
night:    20%
deep:     15%
fun:      15%
direct:   10%
romance:  10%
Every flow follows the same 6-step structure, but steps 3/4/5 vary:
STEP 0: GREETING (always same)
───────────────────────────────
User says:    hi, hey, hello, yo, sup, etc.
Bot replies:  hey / heyy / hi / sup / yo / hii
Action:       Advances to Step 1
STEP 1: GENDER + AGE (always same)
───────────────────────────────────
User says:    m, m 21, f 19, im 39, 25, etc.
Bot replies:  f 18-26 (random female age)
Action:       Advances to Step 2
STEP 2: COUNTRY (always same)
──────────────────────────────
User says:    anything (bot asks)
Bot replies:  Flow-specific country response
Action:       Advances to Step 3
STEP 3: FLOW-SPECIFIC QUESTION (VARIES BY FLOW)
────────────────────────────────────────────────
Friendly:  "what u into?" / "u single?"
Night:     "u alone rn?" / "cant sleep?"
Deep:      "u have any dreams?" / "whats ur biggest fear?"
Fun:       "whats ur most embarrassing story?"
Direct:    "whats ur snap?"
Romance:   "whats ur idea of a perfect date?"
Action:    Advances to Step 4
STEP 4: FLOW-SPECIFIC FOLLOW-UP (VARIES BY FLOW)
──────────────────────────────────────────────────
Friendly:  "oh nice! tell me more" / "thats actually cool"
Night:     "haha maybe a little 😉" / "a little, why? 👀"
Deep:      "omg same! we match 💕" / "i feel the same 🥰"
Fun:       "ngl ur fun to talk to" / "ur actually fun lol"
Direct:    "ok ur cute" / "we should snap"
Romance:   "ngl ur adorable" / "ur literally so cute"
Action:    Advances to Step 5
STEP 5: SNAP GATE (always same structure)
──────────────────────────────────────────
30% chance: Flow-specific snap intro (e.g., "ur fun ngl")
70% chance: Random snap format with username
Action:     Advances to Step 6 (snap_pivoted = True)
POST-SEQUENCE (Step 6+):
After snap is shared, bot continues chatting naturally using
pipe_db, evoflow, and other databases.
SEQUENCE DIAGRAM (Friendly Flow Example):
User: hi           --> Bot: hey           (Step 0->1)
User: m 21         --> Bot: f 22          (Step 1->2)
User: im from usa  --> Bot: wait usa? thats cool (Step 2->3)
User: nice         --> Bot: what u like to do? (Step 3->4)
User: lol          --> Bot: thats actually cool (Step 4->5)
User: cool         --> Bot: snap me lets go (Step 5->6)
The bot detects gender and age from user messages:
PATTERN                          EXAMPLES              DETECTION
─────────────────────────────────────────────────────────────────────
Gender + Age (short)             m, M 21, f 19        (male/female, age)
Gender only                      m, M, f, F           (male/female, no age)
Age with "im"                    im 39, i'm 25        (no gender, age)
Age with "i am"                  i am 22              (no gender, age)
Age with "age"                   age 30               (no gender, age)
Standalone number                25, 39               (no gender, age)
DETAILED EXAMPLES:
──────────────────
"m"        -> ("male", None)      -- gender only
"M 21"     -> ("male", 21)        -- gender + age
"f"        -> ("female", None)    -- gender only
"F 19"     -> ("female", 19)      -- gender + age
"im 39"    -> (None, 39)          -- age only
"25"       -> (None, 25)          -- standalone age
NOTE: Bot always responds with "f [18-26]" (female age) regardless
of user's gender. The detection is used to store user info, not to
change bot behavior.
COUNTRY DETECTION:
The bot detects countries from user messages using keyword matching.
USER SAYS              DETECTED COUNTRY
────────────────────────────────────────
"im from usa"          usa
"im american"          usa
"im from india"        india
"im bangladeshi"       bangladesh
"im from uk"           uk
"im british"           uk
"germany"              germany
"im from canada"       canada
"australia"            australia
SUPPORTED COUNTRIES (40+):
North America:  usa, canada, mexico
Europe:         uk, germany, france, spain, italy, netherlands, etc.
Asia:           india, pakistan, bangladesh, japan, korea, etc.
Oceania:        australia, new zealand
Middle East:    uae, saudi arabia, israel, etc.
Latin America:  brazil, argentina, colombia, etc.
Africa:         nigeria, south africa, kenya, etc.
BOT COUNTRY ROTATION:
The bot can be from ANY country. It uses a rotation system:
1. If user shares their country -> bot may match it (builds rapport)
2. If user doesn't share -> bot picks random country
3. Bot NEVER contradicts user's country
COUNTRY RESPONSES (VARY BY FLOW):
Friendly:  "oh nice usa!" / "usa is dope!"
Night:     "usa vibes 🌙" / "its late there tho"
Deep:      "oh usa beautiful" / "i like usa vibes"
Fun:       "usa?? ur joking right 😂" / "no way usa lolol"
Direct:    "usa cool" / "oh usa nice"
Romance:   "usa is romantic 🥰" / "i love usa vibes 💕"
The bot detects horny/flirty messages and responds appropriately:
HORNY KEYWORDS: horny, sexting, nudes, pics, etc.
FLIRTY KEYWORDS: cute, hot, sexy, beautiful, etc.
FLOW:
1. User says something horny
2. Bot deflects ONCE with: "lets just chat normally" / "haha calm down"
3. horny_detected = True (won't deflect again)
4. Bot CONTINUES the 6-step sequence normally
EXAMPLE:
User: hi           -> Bot: hey           (Step 0->1)
User: im horny     -> Bot: lets just chat normally (horny deflect)
User: m 22         -> Bot: f 21          (Step 1->2, continues)
User: im from usa  -> Bot: im germany hbu? (Step 2->3)
IMPORTANT: Horny deflection does NOT reset the sequence.
Bot continues from where it left off.
HORNY REPLY CATEGORIES:
Soft:     "haha maybe a little"
Medium:   "ok maybe sometimes"
Strong:   "calm down bro"
Snap:     "snap me lets go"
The bot automatically rejects certain requests:
REQUEST TYPE        TRIGGER WORDS              BOT REPLY
────────────────────────────────────────────────────────────────
Sexting             horny, sexting, nudes      "haha nah lol"
pics, dirty                "nah thats not me"
"lets just chat normally"
Social Media        instagram, ig, fb          "nah too personal"
twitter, tiktok            "i dont share socials yet"
Content Request     pic, photo, selfie         "lol why?"
video, face reveal         "nah why?"
Objection           no, stop, dont             "haha ok fair"
quit, enough               "lol my bad"
SNAP REQUEST        snap, sc, add me           Bot shares snap directly
what's your snap           (bypasses sequence)
SNAP USERNAMES (from snap.txt):
eva_chill, eva_21, eva_69, eva_xx, eva_love,
eva_dark, eva_sweet, eva_hot, eva_real, eva_cool
USERNAME CYCLING:
- Usernames cycle in order: eva_chill -> eva_21 -> eva_69 -> ...
- After last username, wraps back to first
- Each conversation gets next username in rotation
SNAP MESSAGE FORMATS (random selection):
"my snp : {username}"
"my sc : {username}"
"sn-p : {username}"
"s,n.ap : {username}"
"sn..p : {username}"
"snap : {username}"
"sc : {username}"
"{username}"
"add {username}"
"my snap : {username}"
"im {username}"
SNAP GATE BEHAVIOR:
The bot loads replies from 13 database files:
FILE                          KEYWORDS    PURPOSE
─────────────────────────────────────────────────────────────────
01_conversation_flow.txt       3,584       General chat
02_identity_profile.txt       200         Bot identity
03_flirty_playful.txt         885         Flirty responses
04_sexual_adult.txt           1,135       Adult content
05_links_objections.txt       82          Link handling
06_emotional_deep.txt         1,232       Emotional chat
07_pop_culture_casual.txt     4,077       Pop culture
08_sexual_direct.txt          1,731       Direct sexual
09_multilang.txt              588         Multilingual
10_million_pairs.txt          5,000*      Mega pairs (*sampled)
pipe_db                       26,378      Pipe database
evoflow                       26,060      EvoFlow rules
horny_flirty                  25 x 8      Horny/flirty DB
TOTAL: ~11,369 unique keywords, ~12,924+ replies
DATABASE PRIORITY:
1. Sequence steps (always first)
2. Deflection rules (sexting, social media, etc.)
3. Horny detection
4. Pipe DB (fuzzy match)
5. EvoFlow rules
6. Zone fallback (random reply)
If user sends multiple lines at once:
User sends:
"hi"
"m 22"
"im from usa"
Bot processes EACH line separately:
Line 1: "hi"       -> "hey"        (Step 0->1)
Line 2: "m 22"     -> "f 21"       (Step 1->2)
Line 3: "im from usa" -> flow-specific response (Step 2->3)
Bot returns the LAST meaningful reply (skips generic greetings).
NOTE: State advances with each line, so multi-line messages
progress through multiple steps in one response.
chitchat-bot/
browser_automation.py     Main browser automation (Playwright)
main.py                   GUI with live stats
thread_manager.py         Thread management
config.json               Performance settings
snap.txt                  Snap usernames (10)
test_chat.py              Smoke test
chat/
__init__.py             Package init
rules.py                6-step sequence + 6 flow types
common_scan.py          Regex message classifier
chat_db.py              Database loader (13 sources)
geo_handler.py          Country detection + rotation
horny_flirty_db.py      Horny/flirty keyword DB
content_filter.py       Explicit content filter
rule_bot.py             ChatRuleBot orchestrator
style_analyzer.py       Style mirroring
database/
__init__.py
loader.py             Pipe DB + EvoFlow loader
local_db/                 External database files (13 .txt files)
PERFORMANCE SETTINGS (config.json):
Contexts:       2 (browser tabs)
Workers:        2 (parallel processing)
Poll:           1.0s (message check interval)
Background:     OFF (saves resources)
Resource block: OFF (allows all resources)
LOG SETTINGS:
Auto-clear:     Every 3 seconds
Keep last:      30 lines
Stats label:    "Users: X | Snaps: Y"
FLOW WEIGHTS:
friendly: 30%
END OF DOCUMENTATION
