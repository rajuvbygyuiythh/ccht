"""Fuzz harness for the EVA RuleEngine / Section 5.8 context-aware fallback.

Fires N random stranger messages (seeded, reproducible) through
``RuleEngine.decide_reply`` exactly like a worker thread and checks invariants:

  1. every message is answered (non-empty reply, no exceptions)
  2. ``flirty_state`` always stays in the allowed machine set
  3. age / gender / country are each asked at most ONCE per conversation
     (the ``_asked``-flag guards are permanent)
  4. a no-info stranger (greetings / reactions / agreements / noise only) is
     NEVER asked for age/gender/country once the 2-turn warm window closes —
     the reported A32 bug ("me too" -> "f 20 u? age?") is a regression here
  5. agreement streaks escalate (A34): a "me too" x5 run breaks into new-topic
     questions and never repeats a line
  6. endpoint-state histogram (SANITY print only — the new spec deliberately
     allows a convo to stay in GREETING when the stranger never gives info)

Run:  python test_fuzz.py [N]      (N = messages per run, default 100)
      Standard batch: test_matcher.bat runs it (and test.bat runs the
      interactive REPL after the automated checks).
Exit: 0 on success, 1 on any failure.
"""

import os

# Fuzz checks run against the LEGACY engine (chat/rules.py).
os.environ.setdefault("EVA_ENGINE", "legacy")

import random
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from chat.rules import RuleEngine

ALLOWED_STATES = {"GREETING", "FLIRTY_ASK", "MIDDLE", "ASKSC", "SHARESC", "END"}

# Real identity asks are ONLY emitted by the GREETING collection step (the
# _asked flags are permanent), so count asks from the decision trace, not from
# reply substrings ("mess age?" would otherwise false-positive on "age?").
_COLLECT_RE = re.compile(
    r"GREETING: collect (age|gender|country) \((?:new info|early)\)")
_COLLECT_IDX = {"age": 0, "gender": 1, "country": 2}

GREET = ["hi", "hey", "hello", "yo", "sup", "wassup", "hii", "hlo", "hey u"]
REACT = ["lol", "lmao", "haha", "hehe", "kk", "ok", "okay", "cool", "nice",
         "damn", "wow", "aww", "hmm", "oh", "no way", "rip"]
AGREE = ["me too", "me 2", "same", "same here", "same lol", "me too lol",
         "haha same", "ikr", "yes", "same tbh", "yeah me too", "same ngl",
         "indeed", "true", "so true", "facts", "for real", "same fr"]
QUEST = ["do u have a bf", "wanna snap", "u busy rn", "how tall are u",
         "whats ur name", "u from usa", "y u so quiet", "fr?", "who r u",
         "u up?", "wyd"]
INFO = ["im 20", "im f", "m 22", "i am 19", "21 m", "im 20 from usa",
        "im a girl", "hi im 18"]
STATEMENTS = ["i like music", "its raining here", "just laying in bed",
              "my phone dieing", "i ate food", "nothing much", "im bored",
              "gotta go soon", "this convo is fun", "i watch cricket",
              "the weather is nice today"]
NOISE = ["sdkfjg", "asdf", "123", "zzz", "...", "mere laf", "xxxxxxxx",
         "hmm hm", "maybe", "ok but why"]
HORNY = ["im horny", "wanna sext", "send nudes", "u r cute and sexy",
         "i got wet", "horny"]
SNAP = ["wanna snap", "add me on snap", "whats ur snap"]

NOINFO_POOL = GREET + REACT + AGREE + NOISE
MIXED_POOL = GREET + REACT + AGREE + QUEST + STATEMENTS + INFO + HORNY + SNAP


def _count_trace_asks(trace):
    """(age, gender, country) identity-asks evidenced in a decision trace."""
    asks = [0, 0, 0]
    for t in trace:
        m = _COLLECT_RE.search(t)
        if m:
            asks[_COLLECT_IDX[m.group(1)]] += 1
    return tuple(asks)


def _drive_warm(engine, pool, n, seed, warmup):
    """Drive ``n`` messages (``warmup`` lines prepended, counted toward n).
    Returns (state, asks=(age,gender,country)). Raises on empty reply or an
    out-of-set machine state so the fuzz is a hard failure, not a warning."""
    rng = random.Random(seed)
    msgs = list(warmup) if warmup else []
    for _ in range(n - len(msgs)):
        msgs.append(rng.choice(pool))
    state = engine.init_conversation()
    asks = [0, 0, 0]
    for m in msgs:
        reply = engine.decide_reply(m, state)
        if not (isinstance(reply, str) and reply.strip()):
            raise AssertionError(f"empty reply for {m!r} (seed={seed})")
        if state["flirty_state"] not in ALLOWED_STATES:
            raise AssertionError(
                f"bad state {state['flirty_state']!r} after {m!r} (seed={seed})")
        a, g, c = _count_trace_asks(engine.last_trace)
        asks[0] += a
        asks[1] += g
        asks[2] += c
    return state, tuple(asks)


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    engine = RuleEngine()
    fails = 0
    total = 0

    print(f"=== Fuzz: {n} messages per run, seeded ===\n")

    # 1) no-info stranger -> zero identity asks after the 2-turn warm window
    state, asks = _drive_warm(engine, NOINFO_POOL, n, 101, ["hi", "lol"])
    ok = asks == (0, 0, 0)
    fails += 0 if ok else 1
    total += 1
    print(f"  {'PASS' if ok else 'FAIL'}  no-info stranger: 0 identity asks "
          f"after warm window (got age={asks[0]} gender={asks[1]} country={asks[2]})")

    # 2) mixed stranger: each field asked <= 1 across 100 messages
    worst = (0, 0, 0)
    for seed in (1, 2, 3):
        state, asks = _drive_warm(engine, MIXED_POOL, n, seed, ["hi"])
        worst = tuple(max(x, y) for x, y in zip(worst, asks))
    ok = all(x <= 1 for x in worst)
    fails += 0 if ok else 1
    total += 1
    print(f"  {'PASS' if ok else 'FAIL'}  mixed {n}-msg runs: worst per-field "
          f"asks <= 1 (worst age={worst[0]} gender={worst[1]} country={worst[2]})")

    # 3) A34 agreement streak breaks the "me too" loop
    stream = ["hi m", "how are u?"] + ["me too"] * 5 + ["i love music"]
    state, asks = _drive_warm(engine, [], 0, 5, stream)
    ok = asks == (0, 0, 0)
    fails += 0 if ok else 1
    total += 1
    print(f"  {'PASS' if ok else 'FAIL'}  'me too' x5 after a question: "
          f"no identity ask (asks={asks})")

    # replay the streak script collecting replies directly
    s = engine.init_conversation()
    replies = []
    for m in stream:
        replies.append(engine.decide_reply(m, s))
    ok = (len(set(replies)) == len(replies)
          and s.get("agreement_streak", 0) == 0
          and all(str(r).strip() for r in replies))
    fails += 0 if ok else 1
    total += 1
    print(f"  {'PASS' if ok else 'FAIL'}  agreement run: {len(set(replies))}/"
          f"{len(replies)} distinct replies, streak reset after topic change")

    # 4) endpoint-state histogram across 5 mixed runs (SANITY, informational)
    from collections import Counter
    hist = Counter()
    for seed in range(5):
        state, _ = _drive_warm(engine, MIXED_POOL, n, 1000 + seed, ["hi"])
        hist[state["flirty_state"]] += 1
    print(f"\n  endpoint-state histogram ({n}-msg runs): {dict(hist)}")
    all_non_greeting = any(x != "GREETING" for x in hist)
    print(f"  {'OK' if all_non_greeting else 'WARN'}  at least one run "
          f"reached the flirty/snap funnel (stall in GREETING is spec-valid "
          f"when the stranger never gives info)")

    print(f"\nRESULT: {total - fails}/{total} passed")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())