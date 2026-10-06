import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from chat.rules import InputOutputEngine

SAMPLE_MSGS = [
    ("hi", "greeting.txt"),
    ("how old are u", "your_age.txt"),
    ("age?", "your_age.txt"),
    ("m 21", "age_gender.txt"),
    ("m21", "age_gender.txt"),
    ("where u from", "country.txt"),
    ("ur from where", "country.txt"),
    ("from germany", "country.txt"),
    ("how r u", "how_are_you.txt"),
    ("are u horny baby", "horny.txt"),
    ("wanna snap", "snapchat.txt"),
    ("this is a random no-match test sentence", None),
]


def main() -> int:
    e = InputOutputEngine(ROOT)
    e.reload_files()

    cats = sorted(set(e.input_rules) | set(e.output_replies))
    print("=== Category check (input triggers -> output replies) ===")
    warnings = 0
    for cat in cats:
        inn = len(e.input_rules.get(cat, []))
        out = len(e.output_replies.get(cat, []))
        flag = "OK"
        round_trip = "-"
        if cat in e.input_rules and cat in e.output_replies:
            round_trip = False
            for trig in e.input_rules[cat][:400]:
                m = e.find_match(trig)
                if m and m[1] == cat:
                    round_trip = True
                    flag = "OK"
                    break
            if not round_trip:
                flag = "WARN"
                warnings += 1
        elif inn and not out:
            flag = "NO-OUTPUT"
            warnings += 1
        elif out and not inn:
            flag = "bot-pool"
        print(f"  {cat:22} triggers={inn:5} replies={out:6} roundtrip={round_trip!s:5} {flag}")

    print()
    print("=== Sample routing (user SMS -> category -> reply) ===")
    for msg, want in SAMPLE_MSGS:
        m = e.find_match(msg)
        if m:
            reply, cat = m
            note = f"   <-- expected {want}" if (want is not None and cat != want) else ""
            line = f"  {msg!r:40} -> {cat:18} {reply!r}{note}"
        else:
            note = f"   <-- expected {want}" if want is not None else ""
            line = f"  {msg!r:40} -> <funnel/other flow>{note}"
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "backslashreplace").decode("ascii"))

    print()
    print("ALL CATEGORIES ROUND-TRIP OK" if not warnings
          else f"Category issues: {warnings} (WARN/NO-OUTPUT rows above).")
    return 1 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())