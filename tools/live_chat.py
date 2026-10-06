"""Live chat test harness — talk to the EVA RuleEngine as a stranger/user.

Run via test.bat. Every message goes through the exact same code path the
browser worker uses (ChatRuleBot -> RuleEngine.decide_reply), so the replies
are what a real stranger would get.

Commands:
  /new        start a fresh conversation (new random flow)
  /state      dump the full conversation state
  /debug      toggle the reason-trace + compact state line
  /quit       exit
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from chat.rule_bot import ChatRuleBot


HELP = (
    "You are EVA's chat engine. Type like a stranger -> the bot replies "
    "exactly as in production.\n"
    "Commands: /new  /state  /debug  /quit\n"
    "Tip: drop your age/country anywhere (e.g. 'm 22 from usa') "
    "and the bot will capture it.\n"
    "Say 'hi' to start.\n"
)


def fmt_state(state) -> str:
    return (
        f"[step={state['sequence_step']} flow={state['flow_type']} "
        f"age={state.get('user_age')} gender={state.get('user_gender')} "
        f"country={state.get('user_country')} horny={state.get('horny_detected')} "
        f"asked_snap={state.get('asked_snap')} blocked={state.get('blocked')}]"
    )


def main() -> int:
    bot = ChatRuleBot()
    state = bot.new_conversation()
    debug = True

    print(HELP)
    print("-- new conversation -- " + fmt_state(state))

    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return 0
        if not text:
            continue

        low = text.lower()
        if low in ("/quit", "/exit", "quit", "exit"):
            print("bye")
            return 0
        if low in ("/new", "/reset", "reset"):
            state = bot.new_conversation()
            print("-- new conversation -- " + fmt_state(state))
            continue
        if low == "/state":
            print(fmt_state(state))
            for k, v in state.items():
                print(f"   {k}: {v}")
            continue
        if low == "/debug":
            debug = not debug
            print(f"debug line: {'on' if debug else 'off'}")
            continue

        reply = bot.reply(text, state)
        print("eva> " + reply)
        first = True
        for extra in bot.get_pending_replies(state):
            prefix = "eva> " if first else "     "
            print(prefix + extra)
            first = False
        if debug:
            summary = bot.last_debug_summary()
            fname, lineno = bot.last_picked_info()
            if fname and lineno:
                # Show exact file path and line number
                # Convert to relative path from project root
                import os
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                try:
                    rel_path = os.path.relpath(fname, project_root).replace("\\", "/")
                except ValueError:
                    rel_path = fname
                print(f"   # {rel_path} line {lineno}")
            else:
                print("   # " + summary)


if __name__ == "__main__":
    raise SystemExit(main())