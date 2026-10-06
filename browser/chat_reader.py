"""Robust detection of the stranger's SMS on the Chitchat.gg chat page.

Why this module exists
----------------------
The bot used to read incoming SMS with ONE hardcoded selector set
(``main ol`` -> ``li.select-text`` -> ``span.font-bold[role=button]`` ->
``span.emoji-content``).  The moment any of those class names changes on the
live site, extraction returns ``[]`` and the bot silently "never sees" what
the stranger wrote — i.e. the bot cannot detect the user's sms any more.

This module makes detection defensive instead of brittle:

1. **Multi-selector extraction** (:data:`CHAT_EXTRACT_JS`) tries a list of
   container / item / username / message selectors, drops nested duplicates,
   then falls back to alignment classes (``justify-end``, ``self-end``,
   ``ml-auto``, ...), to "this text is one we just sent", and finally to a
   generic ``Name: message`` line parser for unknown layouts.
   It always returns a *diagnostic* dict, so a failure is explainable.
2. **Legacy fallback** — :func:`legacy_extract` keeps the original selector
   set, so behaviour can never regress below what the bot had before.
3. **Incremental new-message detection** — :class:`MessageTracker` compares
   the current DOM snapshot with the previous one (multiset fingerprint)
   instead of relying on ``len(dom) > last_count`` arithmetic.  That old
   arithmetic breaks the moment the site does not render our own message:
   the counter runs ahead of the DOM forever and every later stranger SMS is
   skipped.  It also breaks when extra multi-line "pending" replies are sent.

Everything here is import-safe without a browser: JS is only executed when a
Playwright/Camoufox ``page`` is passed in.
"""
from __future__ import annotations

import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def norm_text(text: Any) -> str:
    """Collapse whitespace (same rule as the JS extractor)."""
    return _WS_RE.sub(" ", str(text or "")).strip()


def _norm_key(text: Any) -> str:
    """Normalization used for "is this a text we sent?" comparisons."""
    return norm_text(text).lower().rstrip("?.!, ")


def _js_array(values: Sequence[str]) -> str:
    """Render a Python string list as a JS array literal."""
    parts = []
    for value in values:
        escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
        parts.append("'" + escaped + "'")
    return "[" + ", ".join(parts) + "]"


# --------------------------------------------------------------------------
# Selector groups (first group that matches anything wins)
# --------------------------------------------------------------------------

# Group 1 = the original site markup, kept first so nothing regresses.
_CONTAINER_SELECTORS = [
    "main ol",
    "main ol.select-text",
    "ol.select-text",
    "main [role='log']",
    "[role='log']",
    "main ul",
    ".chat-messages",
    "[class*='chat-messages']",
    "[class*='message-list']",
    "main",
]

_ITEM_SELECTORS = [
    "li.select-text",
    "li[class*='select-text']",
    "li",
    "[role='listitem']",
    "[data-testid*='message']",
    "[data-message-id]",
    "[class*='message']",
]

_USERNAME_SELECTORS = [
    "span.font-bold[role='button']",
    "span[role='button'].font-bold",
    "[class*='font-bold']",
    "[class*='font-semibold']",
    "[data-testid*='username']",
    "span[username]",
]

_TEXT_SELECTORS = [
    "span.emoji-content",
    "[class*='emoji-content']",
    "[data-testid='message-text']",
    "[data-testid*='text']",
    "p",
    "div > span",
]

_MY_USERNAME_SELECTORS = [
    "button .truncate.text-sm.font-bold",
    "button span.truncate.text-sm",
    ".bg-panel button span.truncate",
    "button span.truncate",
    "[data-testid='my-username']",
    "span[alt][username]",
    "span[username]",
]

_OWN_CLASS_HINTS = [
    "justify-end", "self-end", "ml-auto", "text-right", "items-end",
    "flex-row-reverse", "bg-primary", "bg-blue", "bg-indigo", "own",
    "outgoing", "is-me", "from-me",
]
_OTHER_CLASS_HINTS = [
    "justify-start", "self-start", "mr-auto", "text-left", "items-start",
    "bg-panel", "bg-gray", "incoming", "other", "from-them",
]

# NOTE: this must stay a *function* expression — it is handed straight to
# page.evaluate(expr, arg).
CHAT_EXTRACT_JS = r"""
(args) => {
  const recentSent = ((args && args.recentSent) || []).map(
    (t) => String(t || '').replace(/\s+/g, ' ').trim().toLowerCase().replace(/[?.!,]+$/, '')
  );
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const key = (s) => norm(s).toLowerCase().replace(/[?.!,]+$/, '');

  const CONTAINERS = %(containers)s;
  const ITEMS = %(items)s;
  const USERNAMES = %(usernames)s;
  const TEXTS = %(texts)s;
  const MY_NAMES = %(my_names)s;
  const OWN_HINTS = %(own_hints)s;
  const OTHER_HINTS = %(other_hints)s;

  const diag = {
    container: null, containerTag: '', itemSelector: null, items: 0,
    myUsername: null, myUsernameSelector: null,
    speakerSources: {}, sampleClasses: [], rawTextLength: 0,
    rawLines: 0, errors: []
  };

  const pickAll = (root, sels) => {
    for (const sel of sels) {
      try {
        const els = root.querySelectorAll(sel);
        if (els && els.length) return {sel: sel, els: Array.from(els)};
      } catch (e) { diag.errors.push(String(e).slice(0, 80)); }
    }
    return {sel: null, els: []};
  };
  // keep only the outermost matches (a row must not be counted twice with
  // the text span inside it)
  const outermost = (els) => els.filter(
    (el) => !els.some((other) => other !== el && other.contains(el))
  );

  // ---- 1. our own username --------------------------------------------
  // a username already discovered on an earlier poll (Python caches it)
  if (args && args.myUsernameHint) {
    const hinted = norm(args.myUsernameHint);
    if (hinted) { diag.myUsername = hinted; diag.myUsernameSelector = 'hint'; }
  }
  for (const sel of MY_NAMES) {
    if (diag.myUsername) break;
    try {
      const el = document.querySelector(sel);
      if (el) {
        const t = norm(el.getAttribute('username') || el.textContent || '');
        if (t && t.length <= 40) { diag.myUsername = t; diag.myUsernameSelector = sel; break; }
      }
    } catch (e) { diag.errors.push(String(e).slice(0, 80)); }
  }

  // ---- 2. find the message list ---------------------------------------
  let container = null;
  for (const sel of CONTAINERS) {
    try {
      const el = document.querySelector(sel);
      if (el) { container = el; diag.container = sel; break; }
    } catch (e) { diag.errors.push(String(e).slice(0, 80)); }
  }
  // No known container at all: fall back to <body> but ONLY for the generic
  // "Name: message" line parser below (never parse random UI nodes as SMS).
  let bodyFallback = false;
  if (!container) {
    try { container = document.querySelector('main') || document.body; } catch (e) { container = null; }
    if (!container) return {messages: [], diag};
    bodyFallback = true;
    diag.container = (container.tagName || 'body').toLowerCase() + '(fallback)';
  }
  diag.containerTag = container.tagName || '';
  const containerText = norm(container.innerText || container.textContent || '');
  diag.rawTextLength = containerText.length;

  const items = bodyFallback ? {sel: null, els: []} : pickAll(container, ITEMS);
  const itemEls = outermost(items.els);
  diag.itemSelector = items.sel;
  diag.items = itemEls.length;

  const classify = (el) => {
    let cls = '';
    try { cls = (el.className || '') + ' ' + ((el.parentElement && el.parentElement.className) || ''); } catch (e) {}
    cls = String(cls).toLowerCase();
    if (OWN_HINTS.some((h) => cls.includes(h))) return 'You';
    if (OTHER_HINTS.some((h) => cls.includes(h))) return 'Stranger';
    return null;
  };
  const bump = (source) => { diag.speakerSources[source] = (diag.speakerSources[source] || 0) + 1; };

  const messages = [];
  for (const item of itemEls) {
    let textEl = null;
    for (const sel of TEXTS) {
      try { const el = item.querySelector(sel); if (el) { textEl = el; break; } } catch (e) {}
    }

    let username = '';
    for (const sel of USERNAMES) {
      try {
        const el = item.querySelector(sel);
        if (el) {
          const t = norm(el.getAttribute('username') || el.textContent || '');
          if (t && t.length <= 40) { username = t; break; }
        }
      } catch (e) {}
    }

    let text = '';
    if (textEl) {
      text = norm(textEl.textContent);
      if (username && key(text).indexOf(key(username)) === 0) {
        text = norm(text.slice(username.length).replace(/^[:\-\s]+/, ''));
      }
    }
    if (!text) {
      let full = norm(item.innerText || item.textContent || '');
      if (username && key(full).indexOf(key(username)) === 0) {
        full = norm(full.slice(username.length).replace(/^[:\-\s]+/, ''));
      }
      text = full;
    }
    if (!text) continue;

    // heuristic username when no dedicated node matched: short leading token
    if (!username) {
      const heads = item.querySelectorAll('span, b, strong, small, div');
      for (const h of heads) {
        const t = norm(h.textContent);
        if (t && t.length <= 24 && key(text).indexOf(key(t)) === 0 && key(t) !== key(text)) {
          username = t; break;
        }
      }
    }

    let speaker = 'Stranger';
    let source = 'default';
    let explicit = null;
    try { explicit = item.getAttribute('data-speaker') || item.getAttribute('data-own'); } catch (e) {}
    if (explicit && /^(you|me|self|own|true)$/i.test(String(explicit))) {
      speaker = 'You'; source = 'align';
    } else if (username && diag.myUsername) {
      speaker = (key(username) === key(diag.myUsername)) ? 'You' : 'Stranger';
      source = 'username';
    } else {
      const byAlign = classify(item);
      if (byAlign) { speaker = byAlign; source = 'align'; }
      else if (recentSent.includes(key(text))) { speaker = 'You'; source = 'sent'; }
    }
    bump(source);

    let mid = null;
    try {
      mid = item.getAttribute('data-message-id') || item.getAttribute('data-id') || item.getAttribute('id') || null;
    } catch (e) {}

    messages.push({
      speaker: speaker, message: text, mid: mid,
      username: username || null, via: source, own: speaker === 'You'
    });
    if (diag.sampleClasses.length < 5) {
      let cls = '';
      try { cls = String(item.className || '').slice(0, 120); } catch (e) {}
      diag.sampleClasses.push((item.tagName || '?') + '.' + cls);
    }
  }

  // ---- 3. last resort: "Name: message" rows in an unknown layout ------
  if (!messages.length) {
    let scope = container;
    try { scope = bodyFallback ? container : (document.querySelector('main') || container); } catch (e) {}
    let cands = [];
    try {
      cands = Array.from(scope.querySelectorAll('div, li, p, span')).filter((el) => {
        const t = norm(el.textContent);
        return t.length > 2 && t.length <= 300 && /^[^:\n]{1,24}:\s*\S/.test(t);
      });
    } catch (e) {}
    cands = outermost(cands);
    const byParent = new Map();
    for (const el of cands) {
      const p = el.parentElement;
      if (!p) continue;
      if (!byParent.has(p)) byParent.set(p, []);
      byParent.get(p).push(el);
    }
    let best = [];
    for (const list of byParent.values()) if (list.length > best.length) best = list;
    if (best.length >= 2) {
      diag.itemSelector = 'text-line-fallback';
      for (const el of best) {
        const t = norm(el.textContent);
        const i = t.indexOf(':');
        const username = norm(t.slice(0, i));
        const text = norm(t.slice(i + 1));
        if (!text) continue;
        let speaker = 'Stranger';
        let source = 'textline';
        if (diag.myUsername && key(username) === key(diag.myUsername)) speaker = 'You';
        else if (recentSent.includes(key(text))) { speaker = 'You'; source = 'sent'; }
        bump(source);
        messages.push({speaker: speaker, message: text, mid: null,
                       username: username || null, via: source, own: speaker === 'You'});
      }
      diag.items = messages.length;
    }
    diag.rawLines = (containerText.match(/\n/g) || []).length + (containerText ? 1 : 0);
  }

  return {messages: messages, diag: diag};
}
""" % {
    "containers": _js_array(_CONTAINER_SELECTORS),
    "items": _js_array(_ITEM_SELECTORS),
    "usernames": _js_array(_USERNAME_SELECTORS),
    "texts": _js_array(_TEXT_SELECTORS),
    "my_names": _js_array(_MY_USERNAME_SELECTORS),
    "own_hints": _js_array(_OWN_CLASS_HINTS),
    "other_hints": _js_array(_OTHER_CLASS_HINTS),
}


# The original, single-selector extractor — kept as a guaranteed fallback.
LEGACY_EXTRACT_JS = r"""
() => {
    const messages = [];
    let myUsername = null;
    const userButton = document.querySelector('button .truncate.text-sm.font-bold');
    if (userButton) myUsername = userButton.textContent.trim();
    if (!myUsername) {
        const userSection = document.querySelector('.bg-panel button span.truncate');
        if (userSection) myUsername = userSection.textContent.trim();
    }
    if (!myUsername) {
        const profileSpan = document.querySelector('span[alt][username]');
        if (profileSpan && profileSpan.closest('.bg-panel')) myUsername = profileSpan.getAttribute('username');
    }
    const chatContainer = document.querySelector('main ol');
    if (!chatContainer) return messages;
    const messageItems = chatContainer.querySelectorAll('li.select-text');
    for (let li of messageItems) {
        const usernameSpan = li.querySelector('span.font-bold[role="button"]');
        if (!usernameSpan) continue;
        const username = usernameSpan.textContent.trim();
        const messageSpan = li.querySelector('span.emoji-content');
        if (!messageSpan) continue;
        const messageText = messageSpan.textContent.trim();
        if (!messageText.length) continue;
        messages.push({
            speaker: (myUsername && username === myUsername) ? 'You' : 'Stranger',
            message: messageText
        });
    }
    return messages;
}
"""


# --------------------------------------------------------------------------
# Page-facing API
# --------------------------------------------------------------------------

def extract_with_diag(page, recent_sent: Optional[Iterable[str]] = None,
                      my_username: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read the chat list from *page*.

    Returns ``(messages, diag)`` where every message is
    ``{'speaker': 'You'|'Stranger', 'message': str, ...}``.

    Never raises for site-side problems: JS errors land in ``diag['errors']``
    so the chat loop keeps running.
    """
    diag: Dict[str, Any] = {"extractor": "chat_reader", "errors": []}
    recent = [norm_text(t) for t in (recent_sent or []) if norm_text(t)]
    args = {"recentSent": recent, "myUsernameHint": norm_text(my_username) or None}
    try:
        result = page.evaluate(CHAT_EXTRACT_JS, args)
    except Exception as exc:  # page closed / evaluate failed
        diag["errors"].append(f"evaluate: {type(exc).__name__}: {exc}")
        try:
            result = page.evaluate(CHAT_EXTRACT_JS, args)
        except Exception as exc2:
            diag["errors"].append(f"evaluate retry: {type(exc2).__name__}: {exc2}")
            return [], diag

    if isinstance(result, list):  # defensive: old-style return
        return _post_process(result, recent), diag
    if not isinstance(result, dict):
        diag["errors"].append(f"unexpected result type: {type(result).__name__}")
        return [], diag

    diag.update(result.get("diag") or {})
    messages = result.get("messages") or []
    return _post_process(messages, recent), diag


def extract(page, recent_sent: Optional[Iterable[str]] = None,
            my_username: Optional[str] = None) -> List[Dict[str, Any]]:
    """Same as :func:`extract_with_diag` but returns only the messages."""
    return extract_with_diag(page, recent_sent=recent_sent, my_username=my_username)[0]


def legacy_extract(page) -> List[Dict[str, Any]]:
    """The original single-selector extraction (never worse than before)."""
    try:
        result = page.evaluate(LEGACY_EXTRACT_JS)
    except Exception:
        return []
    return _post_process(result or [], [])


def _post_process(messages: Sequence[Dict[str, Any]], recent_sent: Sequence[str]) -> List[Dict[str, Any]]:
    """Second safety pass: never let our own echoed text look like an SMS.

    The JS already labels speakers; this pass only re-labels messages that
    came back with an unreliable label (``via`` empty/``default``) *and*
    whose text is identical to something we just sent.
    """
    sent_keys = {_norm_key(t) for t in recent_sent if _norm_key(t)}
    out: List[Dict[str, Any]] = []
    for raw in messages or []:
        if not isinstance(raw, dict):
            continue
        text = norm_text(raw.get("message"))
        if not text:
            continue
        speaker = str(raw.get("speaker") or "Stranger")
        via = str(raw.get("via") or "")
        if speaker == "Stranger" and via in ("", "default") and _norm_key(text) in sent_keys:
            speaker = "You"
            via = "sent"
        out.append({
            "speaker": speaker,
            "message": text,
            "mid": raw.get("mid"),
            "username": raw.get("username"),
            "via": via or "default",
        })
    return out


def dump_dom(page, root_dir: Optional[str] = None) -> Optional[str]:
    """Save the current page HTML for offline debugging (``EVA_DUMP_CHAT_DOM=1``)."""
    try:
        base = Path(root_dir) if root_dir else Path(__file__).resolve().parent.parent
        out_dir = base / "logs"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"chat_dom_dump_{time.strftime('%Y%m%d_%H%M%S')}.html"
        html = page.content()
        path.write_text(html, encoding="utf-8", errors="replace")
        return str(path)
    except Exception:
        return None


def describe_diag(diag: Optional[Dict[str, Any]]) -> str:
    """One-line human summary of a diagnostic dict (used in logs/tools)."""
    if not diag:
        return "no diagnostics"
    sources = diag.get("speakerSources") or {}
    src_txt = ",".join(f"{k}={v}" for k, v in sorted(sources.items())) or "-"
    return (
        f"container={diag.get('container') or 'NOT FOUND'}"
        f" | items_selector={diag.get('itemSelector') or 'none'}"
        f" | items={diag.get('items', 0)}"
        f" | my_username={diag.get('myUsername') or 'unknown'}"
        f" | speaker_via={src_txt}"
        f" | text_chars={diag.get('rawTextLength', 0)}"
        + (f" | errors={diag.get('errors')[:2]}" if diag.get("errors") else "")
    )


# --------------------------------------------------------------------------
# Incremental new-message detection
# --------------------------------------------------------------------------

class MessageTracker:
    """Report only the messages that are *new* since the previous poll.

    The old counter logic (``len(dom) > last_count`` plus
    ``last_count += 1`` after every send) silently breaks when the site does
    not render our own message: the counter runs ahead of the DOM forever and
    the stranger's replies are never detected.  This tracker compares message
    *fingerprints* instead, so it is immune to that whole class of bug:

    * our own message missing from the DOM -> no effect,
    * extra "pending" multi-line sends -> no effect,
    * repeated texts (stranger says "hi" twice) -> counted correctly,
    * the list being reset/trimmed between chats -> no false replies.
    """

    def __init__(self) -> None:
        self._seen: Counter = Counter()
        self._keys: List[str] = []

    @staticmethod
    def key_of(message: Dict[str, Any]) -> str:
        mid = message.get("mid")
        if mid not in (None, "", "None"):
            return f"id:{mid}"
        return f"{message.get('speaker')}|{_norm_key(message.get('message'))}"

    def snapshot(self, messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Seed the tracker without reporting anything (baseline)."""
        self.reset()
        self._keys = [self.key_of(m) for m in messages or []]
        self._seen = Counter(self._keys)
        return []

    def reset(self) -> None:
        self._seen = Counter()
        self._keys = []

    def sync(self, messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return the messages that were not visible in the previous poll."""
        messages = list(messages or [])
        keys = [self.key_of(m) for m in messages]
        remaining = Counter(self._seen)
        new: List[Dict[str, Any]] = []
        for message, key in zip(messages, keys):
            if remaining.get(key, 0) > 0:
                remaining[key] -= 1  # already handled in an earlier poll
            else:
                new.append(message)
        self._keys = keys
        self._seen = Counter(keys)
        return new

    def new_stranger_texts(self, messages: Sequence[Dict[str, Any]]) -> List[str]:
        """Convenience: only the stranger texts among the new messages."""
        return [m["message"] for m in self.sync(messages)
                if str(m.get("speaker")) == "Stranger"]


__all__ = [
    "CHAT_EXTRACT_JS", "LEGACY_EXTRACT_JS", "MessageTracker", "extract",
    "extract_with_diag", "legacy_extract", "dump_dom", "describe_diag",
    "norm_text",
]
