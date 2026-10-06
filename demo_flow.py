#!/usr/bin/env python3
"""demo_flow.py — scripted strangers walking the EXACT funnel.

Funnel (with the ask-snap step before the share):
    greeting -> age/gender -> country -> flirty question
    -> (flirty keyword) flirty_reply -> ask_snap
    -> (yes) share_snap -> END CHAT
    Horny replies also pivot to ask_snap on the next turn.
    A refusal after the ask draws one retry_nudge line, then "yes" shares.

Chat 1 (horny path):
    "hi"    -> greeting.txt
    "m21"   -> age_gender.txt
    "from?" -> country.txt
    "usa"   -> flirty_questions.txt
    "lol"   -> middle_chat/warm_reply.txt
    "u horny?" -> horny.txt            (ask pending)
    "haha"  -> ask_snap.txt            (bot asks for snap)
    "yeop"  -> share_snap.txt -> END CHAT

Chat 2 (flirty path + refusal):
    "hi"    -> greeting.txt
    "m19"   -> age_gender.txt
    "where u from" -> country.txt
    "canada"-> flirty_questions.txt
    "ur cute" -> middle_chat/flirty_reply.txt   (ask pending)
    "hehe ok" -> ask_snap.txt
    "nope"  -> retry_nudge.txt          (one nudge)
    "ok yes"-> share_snap.txt -> END CHAT

Exit 0 only if every reply is non-empty, comes from the expected file,
and both chats reach END.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from eva_flow import EvaFlowBot

CHATS = [
    ("stranger 1 (horny question -> 2-line answer -> ask -> share)", [
        ("hi", "greeting.txt"),
        ("m21", "age_gender.txt"),
        ("from?", "country.txt"),
        ("usa", "flirty_questions.txt"),
        ("lol", "middle_chat/warm_reply.txt"),
        ("are u horny?", "horny_answer.txt"),
        ("haha", "ask_snap.txt"),
        ("yeop", "share_snap.txt"),
    ]),
    ("stranger 2 (flirty path + refusal -> nudge -> share)", [
        ("hi", "greeting.txt"),
        ("m19", "age_gender.txt"),
        ("where u from", "country.txt"),
        ("canada", "flirty_questions.txt"),
        ("ur cute", "middle_chat/flirty_reply.txt"),
        ("hehe ok", "ask_snap.txt"),
        ("nope", "retry_nudge.txt"),
        ("ok yes", "share_snap.txt"),
    ]),
]


def run() -> int:
    bot = EvaFlowBot()
    all_ok = True

    for title, script in CHATS:
        print(f"=== {title} ===")
        state = bot.new_conversation()
        for user_msg, expected_file in script:
            reply = bot.reply(user_msg, state)
            pending = bot.get_pending_replies(state)
            ok = (reply.strip() != ""
                  and bot.last_file == expected_file
                  and state["stage"] != "ERROR")
            if expected_file == "horny_answer.txt":
                # 2-line reply: answer + queued horny.txt line
                ok = ok and len(pending) == 1 and pending[0].strip()
            if expected_file == "share_snap.txt":
                ok = ok and state["stage"] == "END"
            all_ok = all_ok and ok
            mark = "OK  " if ok else "FAIL"
            print(f"  [{mark}] you> {user_msg}")
            print(f"        eva> {reply}")
            print(f"        # {bot.last_file} line {bot.last_line} "
                  f"({bot.last_reason})")
            for extra in pending:
                print(f"        eva> {extra}   (+ horny.txt 2nd line)")
        ended = state["stage"] == "END"
        all_ok = all_ok and ended
        print(f"  final stage: {state['stage']} "
              f"{'(END CHAT)' if ended else '(NOT ENDED!)'}\n")

    print("DEMO", "PASSED" if all_ok else "FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
