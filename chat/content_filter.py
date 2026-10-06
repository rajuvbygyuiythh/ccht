"""Content filter to block inappropriate replies from early zones.

The pipe_db contains entries from ALL source files, including adult/sexting
databases.  This filter removes obviously explicit replies before they can
be returned in zone 1 or zone 2.
"""

from __future__ import annotations

import re
from typing import List

# ---------------------------------------------------------------------------
# Patterns that indicate explicit / sexual content
# ---------------------------------------------------------------------------

_EXPLICIT_PATTERNS = re.compile(
    r"\b("
    r"horny|nude|nudes?|sex|sexy|dick|cock|pussy"
    r"|masturbat|orgasm|cum\b|cumming"
    r"|fuck\b|fucking|fucked"
    r"|send\s*(me\s*)?(pic|photo|nude|selfie)"
    r"|rate\s*(me|my)"
    r"|what\s*are\s*u\s*wearing"
    r"|dirty|naughty|wet\b"
    r"|adult|nsfw|18\+|onlyfans|of\b"
    r"|suck|blowjob|handjob"
    r"|bedroom|under\s*the\s*sheet"
    r")\b",
    re.IGNORECASE,
)

# Additional single-word flags
_EXPLICIT_WORDS = {
    "horny", "nude", "nudes", "sexy", "dick", "cock", "pussy",
    "cum", "fuck", "fucking", "wet", "naughty", "dirty",
    "nsfw", "suck", "blowjob",
}


def is_explicit(reply: str) -> bool:
    """Return True if *reply* contains explicit/sexual content."""
    if _EXPLICIT_PATTERNS.search(reply):
        return True
    lower_words = set(reply.lower().split())
    if lower_words & _EXPLICIT_WORDS:
        return True
    return False


def filter_explicit(replies: List[str]) -> List[str]:
    """Return a new list with explicit entries removed."""
    return [r for r in replies if not is_explicit(r)]


def safe_choice(replies: List[str]) -> str:
    """Pick a random non-explicit reply. Falls back to any if all are explicit."""
    safe = filter_explicit(replies)
    if safe:
        import random
        return random.choice(safe)
    import random
    return random.choice(replies) if replies else ""
