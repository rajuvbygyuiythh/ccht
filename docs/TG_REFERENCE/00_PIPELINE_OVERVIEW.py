# TG Bot Pipeline — How user SMS → match → reply
# Copied logic from C:\Users\papi\Desktop\ev b
# This file explains the flow for C:\Users\papi\Desktop\1\22

"""
FLOW (from main.py: handle_message):

1. UPsert user + save_message(uid, "user", text)
   → db/database.py: upsert_user() + message_count increment + users table (age, country, horny_score etc)

2. PROFILE EXTRACTION
   → detection/profiler.py: profiler.extract(text, last_q)
   → updates users.age / country / profile_name, mark_age_exchanged()

3. SIGNAL DETECTION
   → detection/horny_detector.py: detect_horny(text, recent_user_texts) -> {is_horny, confidence}
   → detection/vibe_engine.py: detect_vibe() -> vibe/readiness/action
   → detection/emotion_engine.py: detect_emotion() -> primary/confidence
   → detection/user_interest_tracker.py: track_interest() -> total_interest / recommended_action
   → detection/adaptive_signal_council.py: council_vote() -> should_send / archetype / momentum

4. FUNNEL EVALUATION
   → core/funnel.py: evaluate_funnel(user, msg_count, vibe, link_sent...) -> stage/link_ready

5. REPLY ROUTING (core/message_brain.py + candidate_engine + signal_scorer)

   a) brain = analyze_message(text, last_question, ...) 
      → intent detection via local_nlu_engine + intent_knowledge.json (50+ intents, priority 50-160)
   
   b) candidates = generate_candidates(text, ..., analysis=brain, vibe_info, allow_adult)
      → collects replies from:
         - ID reactions (greeting, how_are_you, wyd etc.)
         - Curated DB (fast_matcher.py: 10M pairs indexed)
         - Adult consensual (70_adult_consensual_curated.txt if allowed)
         - Learned pairs (learned_pairs table)
         Each candidate = Candidate(reply, source, score, intent)

   c) scored = score_candidates(candidates, brain, vibe_info, emotion, horny_score, recent_assistant, user)
      → signal_scorer.py: weighs:
         - intent confidence (+2.0 if brain.confidence>0.8)
         - anti-repeat (-5.0 if reply in recent_assistant)
         - vibe match (+1.5)
         - emotion match (+1.0)
         - horny relevance

   d) best = scored[0]  → reply = best.reply, source = f"pipeline:{best.source}:s={best.total_score}"

6. CLEAN + ANTI-REPEAT
   → utils.clean_reply() + truncate_words(MAX_REPLY_WORDS)
   → if reply in recent_assistant (8): replace with ["yeah","fair","same"]

7. SEND + SAVE
   → save_message(uid, "assistant", reply)
   → update_field(last_bot_reply, last_question)
   → save_memory(uid, text, reply)

HOW IT MAPS TO 1\22/conversation_engine.py:
------------------------------------------------
TG: analyze_message -> brain.intent/confidence
1\22: _is_greeting(), _extract_gender(), _is_asking_horny(), etc. (your GREETING/GENDER/HORNY keywords)

TG: generate_candidates from 10M indexed DB
1\22: _match_db() on reply_database.json (110 keys) + _match_keyword_db() on keyword_pairs.json (93k) — same idea but simpler threshold 0.65/0.92

TG: score_candidates with signals
1\22: _priority_router_reply order: greeting(1) → gender(2) → country(3) → horny(4) → what_doing(5) → snap_gated(6) → combined(7) → keyword_pairs(8) → basic(9) → flirty(10) → random.txt(11)

TG: DB users table with message_count, horny_score, age_exchanged
1\22: ConversationState.gender_known/age_shared/country_shared + message_history + get_exchange_status()

To make 1\22 behave 100% like TG: copy 01_database.py → use SQLite instead of in-memory dict, call upsert_user() per message.
"""

# QUICK TEST — same method as TG but using 1\22 DB
import sys
sys.path.insert(0, r"C:\Users\papi\Desktop\1\22")
from conversation_engine import ConversationState, load_reply_database

db = load_reply_database()
state = ConversationState(db)

tests = [
    "hi",                      # TG: brain intent=greeting → pipeline:greeting
    "m 21",                    # TG: profiler.extract age=21 → mark_age_exchanged
    "where are you from",      # TG: intent=ask_location → curated reply
    "wyd",                     # TG: intent=wyd → curated reply
    "are you horny",           # TG: horny_detector confidence 0.9 + adult_mood_check intent
    "my snap eva_chill",       # TG: gated by age_exchanged before promo
    "blabla random test",      # TG: fallback to keyword_pairs → random.txt
]

for t in tests:
    print(f"USER: {t!r:25} -> BOT: {state.next_reply(t)!r}")
    print(f"  flags: {state.get_exchange_status()}")
