#!/usr/bin/env python3
"""run_chat.py — talk to the EVA flow bot yourself (interactive REPL).

Every message goes through the exact funnel logic (eva_flow.EvaFlowBot),
and every reply is picked from a data/output/*.txt file. The debug line
under each reply shows exactly which file + line it came from.

Commands:
  /new      start a fresh conversation
  /state    dump the conversation state
  /debug    toggle the reason-trace line
  /quit     exit
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from eva_flow import EvaFlowBot

HELP = (
    "You are the stranger. Type like a real user -> the bot replies exactly\n"
    "as it would on chitchat.gg (replies come from data/output/*.txt).\n"
    "  /new    fresh conversation   /state  dump state\n"
    "  /debug  toggle trace         /quit   exit\n"
)

def main() -> int:
    bot = EvaFlowBot()
    state = bot.new_conversation()
    show_debug = True
    print(HELP)

    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user:
            continue

        cmd = user.lower()
        if cmd in ("/quit", "/exit", "/q"):
            return 0
        if cmd == "/new":
            state = bot.new_conversation()
            print("--- new conversation ---\n")
            continue
        if cmd == "/state":
            for key, value in state.items():
                print(f"  {key}: {value}")
            print()
            continue
        if cmd == "/debug":
            show_debug = not show_debug
            print(f"debug trace: {'ON' if show_debug else 'OFF'}\n")
            continue
        if cmd in ("/help", "/?"):
            print(HELP)
            continue

        reply = bot.reply(user, state)
        print(f"eva> {reply}")
        if show_debug:
            print(f"     # {bot.last_file} line {bot.last_line}  "
                  f"[stage={state['stage']}] {bot.last_reason}")
        # 2-line replies (horny question: answer + horny.txt line)
        for extra in bot.get_pending_replies(state):
            print(f"eva> {extra}")
            if show_debug:
                print(f"     # horny.txt (2nd line of the answer)")
        if state["stage"] == "END":
            print("--- chat ended, type /new to start another ---")
        print()

if __name__ == "__main__":
    raise SystemExit(main())
