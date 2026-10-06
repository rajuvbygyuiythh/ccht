#!/usr/bin/env python3
"""test_flow.py — verifies the symmetric-category rules + logic.

Checks:
  A. category symmetry: every input category has an output counterpart
  B. '#' comment lines in input files are ignored
  C. stage routing (diagram): greeting -> age_gender -> country -> flirty
  D. middle chat: horny / flirty / warm / busy / small-talk categories
  E. positive replies (yes / ur sc? / whats ur snap / yeop) -> share + END
  F. safety: underage block, multi-line, reply cap
  G. custom category pair (input/foo.txt + output/foo.txt) works auto
  H. fuzz: 300 random messages never crash / never empty / no repeats
"""
import os
import sys
import shutil
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

import random
from eva_flow import (EvaFlowBot, TriggerMatcher, _load_triggers,
                      INPUT_DIR, OUTPUT_DIR, MAX_BOT_REPLIES,
                      CATEGORY_PRIORITY, TERMINAL_CATEGORIES)

PASS, FAIL = 0, 0
FAILURES = []


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK ] {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def fresh(bot):
    return bot.new_conversation()


def state_pending(state):
    """Currently queued 2nd-line pending replies (empty = none)."""
    return state.get("pending_replies", [])


def to_middle(bot, s):
    """Walk a state to MIDDLE the diagram way."""
    bot.reply("hi", s)
    bot.reply("m21", s)
    bot.reply("from?", s)
    bot.reply("usa", s)


def main() -> int:
    random.seed(42)
    bot = EvaFlowBot()

    print("— A. category symmetry (input <-> output same names) —")
    for cat in bot.matchers:
        check(f"output counterpart for input/{cat}",
              bool(bot.pools.get(cat)), f"missing output/{cat}")
    for cat in ("flirty_questions.txt", "age_ask.txt", "closer.txt"):
        check(f"bot-initiated pool present: {cat}",
              bool(bot.pools.get(cat)))
    check("info.txt not loaded as a pool",
          "info.txt" not in bot.pools)

    print("— B. input file handling —")
    check("'#' comment lines ignored by trigger loader",
          "# comment" not in _load_triggers(
              os.path.join(INPUT_DIR, "greeting.txt")))
    m = TriggerMatcher(["# hello there", "sup boi"])
    check("matcher ignores comment triggers",
          m.match("sup boi") == "sup boi" and m.match("hello there") is None)
    check("greeting triggers loaded", len(
        _load_triggers(os.path.join(INPUT_DIR, "greeting.txt"))) > 100)

    print("— C. stage routing (the diagram) —")
    s = fresh(bot)
    r = bot.reply("hi", s)
    check("first SMS -> greeting.txt",
          bot.last_file == "greeting.txt" and r.strip(),
          f"got {bot.last_file!r} {r!r}")
    check("stage WAIT_AGE_GENDER after greeting",
          s["stage"] == "WAIT_AGE_GENDER")

    r = bot.reply("m21", s)
    check("'m21' -> age_gender.txt",
          bot.last_file == "age_gender.txt" and r.strip(),
          f"got {bot.last_file!r} {r!r}")
    check("age/gender captured", s["user_gender"] == "m"
          and s["user_age"] == 21)

    r = bot.reply("from?", s)
    check("'from?' -> country.txt",
          bot.last_file == "country.txt" and r.strip(),
          f"got {bot.last_file!r} {r!r}")

    r = bot.reply("usa", s)
    check("'usa' -> flirty_questions.txt",
          bot.last_file == "flirty_questions.txt" and r.strip(),
          f"got {bot.last_file!r} {r!r}")
    check("stage MIDDLE after country", s["stage"] == "MIDDLE")
    check("country captured", s["user_country"] == "usa")

    # first-SMS variants
    s2 = fresh(bot)
    r = bot.reply("m21", s2)
    check("first SMS 'm21' -> age_gender.txt directly",
          bot.last_file == "age_gender.txt", f"got {bot.last_file!r}")
    s2 = fresh(bot)
    r = bot.reply("where u from", s2)
    check("first SMS 'where u from' -> country.txt",
          bot.last_file == "country.txt", f"got {bot.last_file!r}")

    print("— D. middle chat categories —")
    s = fresh(bot)
    to_middle(bot, s)
    r = bot.reply("u horny?", s)
    check("'u horny?' QUESTION -> horny_answer.txt (line 1)",
          bot.last_file == "horny_answer.txt" and r.strip(),
          f"got {bot.last_file!r} {r!r}")
    pend = bot.get_pending_replies(s)
    check("line 2 of the answer queued from horny.txt (pending)",
          len(pend) == 1 and pend[0].strip() and s["ask_next"],
          f"pending={pend!r}")
    # STRICT chain: whatever the user says after the horny reply -> ask
    r = bot.reply("send nudes", s)
    check("whatever after horny -> ask_snap.txt (strict chain)",
          bot.last_file == "ask_snap.txt" and s["asked_snap"],
          f"got {bot.last_file!r}")

    # leet/obfuscated horny detection (advanced layer)
    s = fresh(bot)
    to_middle(bot, s)
    r = bot.reply("h0rny?", s)
    check("leet 'h0rny?' QUESTION -> horny_answer.txt (2-line)",
          bot.last_file == "horny_answer.txt",
          f"got {bot.last_file!r}")
    bot.get_pending_replies(s)
    s = fresh(bot)
    to_middle(bot, s)
    r = bot.reply("s3x?", s)
    check("leet 's3x?' (not a horny question) -> horny.txt",
          bot.last_file == "horny.txt",
          f"got {bot.last_file!r}")

    # fresh conversation (horny/flirty replies leave a pending snap ask)
    s = fresh(bot)
    to_middle(bot, s)
    r = bot.reply("ur cute", s)
    check("'ur cute' -> middle_chat/flirty_reply.txt",
          bot.last_file == "middle_chat/flirty_reply.txt" and r.strip(),
          f"got {bot.last_file!r} {r!r}")

    # fresh conversation (flirty_reply leaves a pending snap ask)
    s = fresh(bot)
    to_middle(bot, s)
    r = bot.reply("how are u", s)
    check("'how are u' -> how_are_you.txt",
          bot.last_file == "how_are_you.txt" and r.strip(),
          f"got {bot.last_file!r} {r!r}")

    # fresh conversation for the rest (avoid the 8-reply cap)
    s = fresh(bot)
    to_middle(bot, s)
    r = bot.reply("how old are u", s)
    check("'how old are u' -> your_age.txt",
          bot.last_file == "your_age.txt" and r.strip(),
          f"got {bot.last_file!r} {r!r}")
    r = bot.reply("lol", s)
    check("'lol' -> middle_chat/warm_reply.txt",
          bot.last_file == "middle_chat/warm_reply.txt" and r.strip(),
          f"got {bot.last_file!r} {r!r}")
    r = bot.reply("gtg bye", s)
    check("'gtg bye' -> middle_chat/busy_later.txt",
          bot.last_file == "middle_chat/busy_later.txt" and r.strip(),
          f"got {bot.last_file!r} {r!r}")
    r = bot.reply("asdfgh qwerty", s)
    check("no match -> flirty_questions.txt",
          bot.last_file == "flirty_questions.txt" and r.strip(),
          f"got {bot.last_file!r} {r!r}")

    print("— E. snap share gating (ask first, then share) —")
    # user asks for the snap directly -> share immediately
    for msg in ("ur sc?", "whats ur snap", "ur @?", "wanna snap",
                "my snap is dumpqu"):
        s = fresh(bot)
        to_middle(bot, s)
        r = bot.reply(msg, s)
        check(f"direct ask {msg!r} -> share pool + END",
              bot.last_file in ("share_snap.txt", "snapchat.txt")
              and s["stage"] == "END" and r.strip(),
              f"got {bot.last_file!r} stage={s['stage']}")

    # bare yes-words WITHOUT the bot's ask -> NO share (chat continues)
    for msg in ("yes", "yeop", "yeah", "yes sure", "sure"):
        s = fresh(bot)
        to_middle(bot, s)
        r = bot.reply(msg, s)
        check(f"yes-word {msg!r} without ask -> NO share",
              s["stage"] == "MIDDLE" and not s["snap_pivoted"]
              and r.strip(),
              f"stage={s['stage']} pivoted={s.get('snap_pivoted')} "
              f"file={bot.last_file!r}")

    print("— E2. ask_snap funnel (horny / flirty paths) —")
    # horny path: question -> 2-line answer -> ask_snap -> yes -> share
    s = fresh(bot)
    to_middle(bot, s)
    r = bot.reply("u horny?", s)
    check("'u horny?' -> horny_answer.txt + pending horny line",
          bot.last_file == "horny_answer.txt" and s["ask_next"]
          and len(bot.get_pending_replies(s)) == 1,
          f"got {bot.last_file!r} ask_next={s.get('ask_next')}")
    r = bot.reply("haha", s)
    check("next turn after horny -> ask_snap.txt",
          bot.last_file == "ask_snap.txt" and s["asked_snap"]
          and s["stage"] == "MIDDLE",
          f"got {bot.last_file!r} asked={s.get('asked_snap')}")
    r = bot.reply("yeop", s)
    check("'yeop' after ask -> share_snap.txt + END",
          bot.last_file == "share_snap.txt" and s["stage"] == "END"
          and s["snap_pivoted"] and r.strip(),
          f"got {bot.last_file!r} stage={s['stage']}")

    # flirty path: flirty question -> user flirty -> flirty_reply.txt
    #              -> ask_snap -> yes -> share -> END
    s = fresh(bot)
    to_middle(bot, s)
    r = bot.reply("ur cute", s)
    check("flirty after flirty_question -> middle_chat/flirty_reply.txt",
          bot.last_file == "middle_chat/flirty_reply.txt" and s["ask_next"],
          f"got {bot.last_file!r}")
    r = bot.reply("haha thanks", s)
    check("next turn after flirty_reply -> ask_snap.txt",
          bot.last_file == "ask_snap.txt" and s["asked_snap"],
          f"got {bot.last_file!r}")
    r = bot.reply("yes", s)
    check("'yes' after ask -> share_snap.txt + END",
          bot.last_file == "share_snap.txt" and s["stage"] == "END",
          f"got {bot.last_file!r} stage={s['stage']}")

    # refusal after the ask -> one retry nudge, then yes -> share
    s = fresh(bot)
    to_middle(bot, s)
    bot.reply("u horny?", s)
    bot.reply("haha", s)
    r = bot.reply("no i dont have snap", s)
    check("refusal after ask -> retry_nudge.txt",
          bot.last_file == "retry_nudge.txt" and s["stage"] == "MIDDLE",
          f"got {bot.last_file!r}")
    r = bot.reply("ok ok yes", s)
    check("'yes' after nudge -> share_snap.txt + END",
          bot.last_file == "share_snap.txt" and s["stage"] == "END",
          f"got {bot.last_file!r}")

    # share line must contain a snap username from snap_ids.txt
    s = fresh(bot)
    to_middle(bot, s)
    bot.reply("u horny?", s)
    bot.reply("haha", s)
    r = bot.reply("yes", s)
    snap_pool = bot.snap_ids._items or bot.snap_ids.fallback
    check("share line contains a snap username",
          any(u.lower() in r.lower() for u in snap_pool), f"reply={r!r}")

    # positive works even in EARLY stages (user asks right after greeting)
    s = fresh(bot)
    bot.reply("hi", s)
    r = bot.reply("whats ur snap", s)
    check("early 'whats ur snap' -> share + END",
          s["stage"] == "END"
          and bot.last_file in ("share_snap.txt", "snapchat.txt"),
          f"got {bot.last_file!r} stage={s['stage']}")

    # after END: closer
    r = bot.reply("hey", s)
    check("after END -> closer.txt", bot.last_file == "closer.txt"
          and s["stage"] == "END")

    print("— F. safety & edge cases —")
    s = fresh(bot)
    bot.reply("hi", s)
    bot.reply("m15", s)
    bot.reply("from?", s)
    bot.reply("usa", s)
    r = bot.reply("whats ur snap", s)
    check("underage 'm15' -> no snap share, chat ends",
          s["stage"] == "END" and bot.last_file != "share_snap.txt",
          f"file={bot.last_file!r} stage={s['stage']}")
    check("underage flagged", s.get("blocked") is True)

    s = fresh(bot)
    r = bot.reply("hi\nm21", s)
    check("multi-line -> last line processed",
          bot.last_file == "age_gender.txt" and s["user_age"] == 21,
          f"got {bot.last_file!r}")

    # reply cap -> closer -> END
    s = fresh(bot)
    to_middle(bot, s)
    for i in range(MAX_BOT_REPLIES + 3):
        r = bot.reply(f"random msg {i}", s)
        if s["stage"] == "END":
            break
    check(f"reply cap ({MAX_BOT_REPLIES}) -> closer.txt -> END",
          s["stage"] == "END" and bot.last_file == "closer.txt",
          f"file={bot.last_file!r} stage={s['stage']}")

    # stage progression: 2 age-asks then country exchange
    s = fresh(bot)
    bot.reply("hlw", s)
    r1 = bot.reply("asdf qwerty", s)
    r2 = bot.reply("zxcv mnbs", s)
    check("WAIT_AGE_GENDER: 2 misses -> progresses to country",
          s["stage"] == "WAIT_COUNTRY" and bot.last_file == "country.txt",
          f"stage={s['stage']} file={bot.last_file!r}")

    # country keyword from data/countries.txt
    s = fresh(bot)
    bot.reply("hi", s)
    bot.reply("m22", s)
    bot.reply("from?", s)
    r = bot.reply("bharat", s)
    check("countries.txt custom keyword 'bharat'=india works",
          s["user_country"] == "india"
          and bot.last_file == "flirty_questions.txt",
          f"country={s.get('user_country')} file={bot.last_file!r}")

    print("— I2. NO-REPEAT: every output pool fires once per user —")
    # greeting only once
    s = fresh(bot)
    bot.reply("hi", s)
    check("1st 'hi' -> greeting.txt", bot.last_file == "greeting.txt")
    bot.reply("m21", s)
    bot.reply("from?", s)
    bot.reply("usa", s)
    bot.reply("hi", s)
    check("2nd 'hi' -> NOT greeting.txt (repeat off)",
          bot.last_file != "greeting.txt",
          f"got {bot.last_file!r}")

    # how_are_you only once
    bot.reply("how are u", s)
    check("1st 'how are u' -> how_are_you.txt",
          bot.last_file == "how_are_you.txt")
    bot.reply("how are u", s)
    check("2nd 'how are u' -> NOT how_are_you.txt",
          bot.last_file != "how_are_you.txt", f"got {bot.last_file!r}")

    # warm_reply only once
    s6 = fresh(bot)
    to_middle(bot, s6)
    bot.reply("lol", s6)
    check("1st 'lol' -> middle_chat/warm_reply.txt",
          bot.last_file == "middle_chat/warm_reply.txt")
    bot.reply("haha ok", s6)
    check("2nd ack -> NOT warm_reply.txt",
          bot.last_file != "middle_chat/warm_reply.txt",
          f"got {bot.last_file!r}")

    # age_gender only once (re-sent age never re-fires the pool)
    s7 = fresh(bot)
    bot.reply("hi", s7)
    bot.reply("m21", s7)
    check("1st 'm21' -> age_gender.txt",
          bot.last_file == "age_gender.txt")
    bot.reply("from?", s7)
    bot.reply("m 30", s7)   # age again, pool spent
    check("re-sent age -> NOT age_gender.txt",
          bot.last_file != "age_gender.txt", f"got {bot.last_file!r}")

    # busy twice -> polite END (closer)
    s8 = fresh(bot)
    to_middle(bot, s8)
    bot.reply("gtg bye", s8)
    check("1st 'gtg bye' -> busy_later.txt",
          bot.last_file == "middle_chat/busy_later.txt")
    bot.reply("gtg", s8)
    check("2nd 'gtg' -> closer.txt + END",
          bot.last_file == "closer.txt" and s8["stage"] == "END",
          f"got {bot.last_file!r} stage={s8['stage']}")

    # flirty_questions MAY repeat (funnel driver) with different lines
    s9 = fresh(bot)
    to_middle(bot, s9)
    r1 = bot.reply("asdf qwerty", s9)
    f1 = bot.last_file
    r2 = bot.reply("zxcv bnmm", s9)
    check("flirty_questions repeatable, never same line",
          f1 == "flirty_questions.txt"
          and bot.last_file == "flirty_questions.txt" and r1 != r2,
          f"{f1!r}->{bot.last_file!r} {r1!r}/{r2!r}")

    print("— I3. MEMORY: line-by-line transcript —")
    s10 = fresh(bot)
    bot.reply("hi", s10)
    bot.reply("m21", s10)
    tr = s10["transcript"]
    check("transcript recorded line-by-line",
          len(tr) == 4 and [e["who"] for e in tr] == ["user", "bot",
                                                      "user", "bot"],
          f"len={len(tr)}")
    check("transcript numbered 1..n",
          all(e["n"] == i + 1 for i, e in enumerate(tr)),
          f"{[e['n'] for e in tr]}")
    check("user + bot messages saved",
          len(s10["user_messages"]) == 2
          and len(s10["bot_messages"]) == 2)
    check("pools_used tracked",
          "greeting.txt" in s10["pools_used"]
          and "age_gender.txt" in s10["pools_used"],
          f"{s10['pools_used']}")
    check("last_bot_pool tracked",
          s10["last_bot_pool"] == "age_gender.txt",
          f"{s10['last_bot_pool']!r}")

    print("— I4. expanded positives after ask + strict chain —")
    # premature 'yes' (ask not made yet) -> ask, NOT share
    s11 = fresh(bot)
    to_middle(bot, s11)
    bot.reply("u horny?", s11)
    r = bot.reply("yes", s11)
    check("premature 'yes' after horny -> ask_snap (not share)",
          bot.last_file == "ask_snap.txt" and s11["stage"] == "MIDDLE"
          and not s11["snap_pivoted"],
          f"got {bot.last_file!r} stage={s11['stage']}")
    # expanded positives
    for pos in ("ok", "k", "@", "share me", "yes i got", "whats ur"):
        s12 = fresh(bot)
        for m in ("hi", "m21", "from?", "usa", "u horny?", "haha"):
            bot.reply(m, s12)
        r = bot.reply(pos, s12)
        check(f"post-ask {pos!r} -> share_snap.txt + END",
              bot.last_file in ("share_snap.txt", "snapchat.txt")
              and s12["stage"] == "END",
              f"got {bot.last_file!r} stage={s12['stage']}")
    # double refusal -> polite END
    s13 = fresh(bot)
    for m in ("hi", "m21", "from?", "usa", "u horny?", "haha"):
        bot.reply(m, s13)
    bot.reply("no", s13)
    check("1st refusal -> retry_nudge.txt",
          bot.last_file == "retry_nudge.txt")
    bot.reply("no way", s13)
    check("2nd refusal -> closer.txt + END",
          bot.last_file == "closer.txt" and s13["stage"] == "END",
          f"got {bot.last_file!r} stage={s13['stage']}")

    print("— I5. repeated flirty/horny: re-reply + re-ask loop —")
    # 1st flirty after the flirty question -> flirty_reply.txt
    s14 = fresh(bot)
    to_middle(bot, s14)
    r = bot.reply("ur cute", s14)
    check("1st flirty after flirty_question -> flirty_reply.txt",
          bot.last_file == "middle_chat/flirty_reply.txt",
          f"got {bot.last_file!r}")
    first_flirty_line = r
    bot.reply("hehe", s14)               # -> ask_snap (strict chain)
    check("next turn -> ask_snap.txt",
          bot.last_file == "ask_snap.txt", f"got {bot.last_file!r}")

    # post-ask flirty (not positive) -> flirty_reply.txt AGAIN (new line)
    # + the ask chain re-fires on the following turn
    r = bot.reply("ur so cute babe", s14)
    check("post-ask flirty AGAIN -> flirty_reply.txt (repeatable)",
          bot.last_file == "middle_chat/flirty_reply.txt",
          f"got {bot.last_file!r}")
    check("repeated flirty uses a DIFFERENT line",
          r != first_flirty_line, f"{r!r} == {first_flirty_line!r}")
    r = bot.reply("hmm tell me", s14)
    check("after repeated flirty -> ask_snap.txt re-fires",
          bot.last_file == "ask_snap.txt", f"got {bot.last_file!r}")

    # post-ask horny -> horny.txt again + re-ask
    s15 = fresh(bot)
    to_middle(bot, s15)
    bot.reply("u horny?", s15)
    check("1st horny question -> horny_answer.txt",
          bot.last_file == "horny_answer.txt")
    bot.get_pending_replies(s15)
    bot.reply("hmm", s15)                # -> ask_snap
    r = bot.reply("im horny again", s15)
    check("post-ask horny AGAIN -> horny.txt (repeatable)",
          bot.last_file == "horny.txt", f"got {bot.last_file!r}")
    r = bot.reply("lol", s15)
    check("after repeated horny -> ask_snap.txt re-fires",
          bot.last_file == "ask_snap.txt", f"got {bot.last_file!r}")
    # and the loop still converts to a share
    r = bot.reply("ok", s15)
    check("re-ask loop still converts: 'ok' -> share + END",
          bot.last_file == "share_snap.txt" and s15["stage"] == "END",
          f"got {bot.last_file!r} stage={s15['stage']}")

    print("— I6. horny QUESTION -> 2-line reply (answer + horny.txt) —")
    # middle chat question: line 1 = answer, line 2 = horny.txt (pending)
    s16 = fresh(bot)
    to_middle(bot, s16)
    r = bot.reply("are u horny?", s16)
    check("'are u horny?' -> horny_answer.txt (line 1)",
          bot.last_file == "horny_answer.txt" and r.strip(),
          f"got {bot.last_file!r} {r!r}")
    pend = bot.get_pending_replies(s16)
    check("line 2 queued from horny.txt (pending)",
          len(pend) == 1 and pend[0].strip(), f"pending={pend!r}")
    check("ask chain armed after the 2-line reply", s16["ask_next"])
    r = bot.reply("anything at all", s16)
    check("next msg (whatever) -> ask_snap.txt",
          bot.last_file == "ask_snap.txt", f"got {bot.last_file!r}")
    r = bot.reply("ok", s16)
    check("positive -> share_snap.txt + END",
          bot.last_file == "share_snap.txt" and s16["stage"] == "END",
          f"got {bot.last_file!r} stage={s16['stage']}")

    # early chat: question on the FIRST line
    s17 = fresh(bot)
    r = bot.reply("horny?", s17)
    check("first SMS 'horny?' -> horny_answer.txt (2-line)",
          bot.last_file == "horny_answer.txt", f"got {bot.last_file!r}")
    check("first SMS question also queues the horny.txt line",
          len(bot.get_pending_replies(s17)) == 1)
    r = bot.reply("hm", s17)
    check("then whatever -> ask_snap.txt", bot.last_file == "ask_snap.txt",
          f"got {bot.last_file!r}")

    # question form variants (typo / rn / leet)
    for q in ("hrny?", "rn horny?", "u horny rn?", "h0rny?", "u dtf?"):
        s18 = fresh(bot)
        to_middle(bot, s18)
        bot.reply(q, s18)
        check(f"{q!r} -> horny_answer.txt (question form)",
              bot.last_file == "horny_answer.txt",
              f"got {bot.last_file!r}")

    # STATEMENTS keep the single horny.txt reply (unchanged behavior)
    s19 = fresh(bot)
    to_middle(bot, s19)
    r = bot.reply("im horny", s19)
    check("statement 'im horny' -> horny.txt (single line, unchanged)",
          bot.last_file == "horny.txt"
          and not state_pending(s19),
          f"got {bot.last_file!r}")
    s19b = fresh(bot)
    to_middle(bot, s19b)
    r = bot.reply("send nudes", s19b)
    check("statement 'send nudes' -> horny.txt (single line, unchanged)",
          bot.last_file == "horny.txt" and not state_pending(s19b),
          f"got {bot.last_file!r}")

    # transcript records BOTH lines of the 2-line reply
    s20 = fresh(bot)
    to_middle(bot, s20)
    bot.reply("are u horny?", s20)
    bot_lines = [e for e in s20["transcript"] if e["who"] == "bot"]
    check("transcript records BOTH lines (answer + horny)",
          len(bot_lines) >= 2, f"bot lines={len(bot_lines)}")

    # repeated question -> new answer line + new horny line (repeatable)
    s21 = fresh(bot)
    to_middle(bot, s21)
    first = bot.reply("are u horny?", s21)
    bot.get_pending_replies(s21)
    bot.reply("hmm", s21)                       # -> ask
    r = bot.reply("are u horny?", s21)          # question AGAIN
    check("repeated question -> horny_answer.txt again (new line)",
          bot.last_file == "horny_answer.txt" and r != first,
          f"got {bot.last_file!r} {r!r} vs {first!r}")
    check("repeated question queues a new horny.txt line",
          len(bot.get_pending_replies(s21)) == 1)

    print("— G. custom category pair works automatically —")
    tmp = tempfile.mkdtemp(prefix="eva_cat_")
    try:
        os.makedirs(os.path.join(tmp, "data", "input"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "data", "output"), exist_ok=True)
        with open(os.path.join(tmp, "data", "input", "music.txt"),
                  "w", encoding="utf-8") as f:
            f.write("# music talk\nfav song?\nmusic?\nwhat music u like\n")
        with open(os.path.join(tmp, "data", "output", "music.txt"),
                  "w", encoding="utf-8") as f:
            f.write("i love pop u?\nEDM all the way\n")
        bot2 = EvaFlowBot(root=tmp)
        s = bot2.new_conversation()
        bot2.reply("hi", s)               # greeting (pool missing -> fallback)
        r = bot2.reply("fav song?", s)    # WAIT_AGE_GENDER scan -> music
        check("custom category music.txt replies from output/music.txt",
              bot2.last_file == "music.txt" and r in
              ("i love pop u?", "EDM all the way"),
              f"got {bot2.last_file!r} {r!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("— H. fuzz: random messages never crash / never empty —")
    import string
    random.seed(7)
    crashed, empty = 0, 0
    for i in range(300):
        s = fresh(bot)
        for j in range(10):
            msg = "".join(random.choices(
                string.ascii_lowercase + " 0123456789?!@",
                k=random.randint(1, 25)))
            if not msg.strip():
                continue
            try:
                r = bot.reply(msg, s)
                if not r.strip():
                    empty += 1
            except Exception as exc:  # noqa: BLE001
                crashed += 1
                print(f"    crash on {msg!r}: {exc}")
            if s["stage"] == "END":
                break
    check("fuzz: no crashes", crashed == 0, f"crashes={crashed}")
    check("fuzz: no empty replies", empty == 0, f"empty={empty}")

    random.seed(99)
    repeats = 0
    for i in range(50):
        s = fresh(bot)
        last_pick = None
        for j in range(8):
            r = bot.reply(random.choice(
                ["hey", "lol", "nice", "ok", "hmm", "really", "idk", "true"]),
                s)
            if r == last_pick:
                repeats += 1
            last_pick = r
            if s["stage"] == "END":
                break
    check("no back-to-back identical replies", repeats == 0,
          f"repeats={repeats}")

    print()
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("failures:")
        for f in FAILURES:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
