"""Coverage checker for the TXT matcher database.

Reads real stranger SMS lines (one per line) and reports which ones the
InputOutputEngine matches and which fall through. Helps grow data/input/*.txt
triggers toward the messages that actually arrive.

Usage:
    python tools/coverage_check.py path/to/messages.txt
    type messages.txt | python tools/coverage_check.py
    python tools/coverage_check.py            # paste lines on stdin, Ctrl-Z/EOF ends

Output:
    [MATCH] <file> [exact|substring] 'trigger'  <- message  (+ a reply candidate)
    [MISS ] <- message  (with detected tag + gender_age for context)

Run from the project root. Matching uses the exact same InputOutputEngine the
bot uses at runtime (priorities: snap/horny gates may change behaviour in
production, but the matcher itself is identical).
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from chat.common_scan import detect_gender_age, scan_message
from chat.rules import InputOutputEngine


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] != "-":
        with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()

    engine = InputOutputEngine(os.path.abspath("."))
    total = matched = 0
    by_file = Counter()
    misses = []

    for raw in lines:
        msg = raw.strip()
        if not msg:
            continue
        total += 1
        result = engine.find_match(msg)
        primary, _ = scan_message(msg)
        ga = detect_gender_age(msg)
        if result:
            reply, fname = result
            trigger, kind = engine.last_match or ("?", "?")
            matched += 1
            by_file[fname] += 1
            print(f"[MATCH] {fname} [{kind}] trigger={trigger!r}")
            print(f"        <- {msg!r}  reply_candidate={reply!r}")
        else:
            misses.append(msg)
            print(f"[MISS ] <- {msg!r}  tag={primary!r} gender_age={ga}")

    print("\n---- summary ----")
    print(f"total: {total}  matched: {matched}  missed: {total - matched}")
    if total:
        print(f"coverage: {100.0 * matched / max(total, 1):.1f}%")
    if by_file:
        print("by file:")
        for fname, count in by_file.most_common():
            print(f"    {fname}: {count}")
    if misses:
        print("still unmatched:")
        for m in misses:
            print(f"    {m!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())