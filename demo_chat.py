"""Real-user chat demo — two scripted strangers through the full funnel.

Same code path as production (ChatRuleBot -> RuleEngine.decide_reply):
  first SMS -> greeting/age_gender/country -> flirty_questions
  -> 3-type answer detect -> ask_snap -> share_snap -> END.

Exit 0 only if every reply is non-empty and both chats reach END.
Run via test.bat step [4/4], or standalone: python demo_chat.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from chat.rule_bot import ChatRuleBot

STRANGERS = [
    ("polite", ["hi", "m21", "usa", "haha thanks", "ur cute", "yeah sure", "yes"]),
    ("bold", ["hi f20", "uk", "lol", "im horny", "ok", "sure"]),
]


def main() -> int:
    bot = ChatRuleBot()
    fails = 0
    for name, script in STRANGERS:
        state = bot.new_conversation()
        print(f"===== stranger: {name} =====")
        for msg in script:
            reply = bot.reply(msg, state)
            print(f"  you> {msg}")
            print(f"  eva> {reply}")
            if not (reply or "").strip():
                print(f"  [FAIL] empty reply for {msg!r}")
                fails += 1
        ended = state.get("flirty_state") == "END"
        print(f"  -- end: state={state.get('flirty_state')} "
              f"pivoted={state.get('snap_pivoted')} --")
        if not ended:
            print(f"  [FAIL] {name} did not reach END")
            fails += 1
        print()
    print("DEMO: ALL CHATS REACHED END" if not fails
          else f"DEMO: {fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
