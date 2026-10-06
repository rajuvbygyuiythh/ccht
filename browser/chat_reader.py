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

import json
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

  // Custom selector groups (config file / selector doctor / self-healing)
  // are prepended, so a site change can be handled without touching code.
  const extra = (args && args.sel) || {};
  const cat = (over, base) => (Array.isArray(over) ? over.concat(base) : base);
  const CONTAINERS = cat(extra.containers, %(containers)s);
  const ITEMS = cat(extra.items, %(items)s);
  const USERNAMES = cat(extra.usernames, %(usernames)s);
  const TEXTS = cat(extra.texts, %(texts)s);
  const MY_NAMES = cat(extra.myUsernames, %(my_names)s);
  const OWN_HINTS = cat(extra.ownHints, %(own_hints)s);
  const OTHER_HINTS = cat(extra.otherHints, %(other_hints)s);
  const custom = new Set([].concat(
    Array.isArray(extra.containers) ? extra.containers : [],
    Array.isArray(extra.items) ? extra.items : [],
    Array.isArray(extra.usernames) ? extra.usernames : [],
    Array.isArray(extra.texts) ? extra.texts : []));

  const diag = {
    container: null, containerTag: '', itemSelector: null, items: 0,
    myUsername: null, myUsernameSelector: null,
    speakerSources: {}, sampleClasses: [], rawTextLength: 0,
    rawLines: 0, errors: [], customSelectors: custom.size,
    usedCustom: false
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
      if (el) {
        container = el; diag.container = sel;
        if (custom.has(sel)) diag.usedCustom = true;
        break;
      }
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
  if (items.sel && custom.has(items.sel)) diag.usedCustom = true;

  // A row's "signature" for speaker hints: class names (row + parent) plus
  // the alignment style (inline style AND computed values, so both
  // class-based ("justify-end") and style-based ("flex-end",
  // "margin-left:auto") hints work — the selector doctor emits both kinds.
  const styleSig = (el) => {
    let sig = '';
    const add = (value) => { if (value) sig += ' ' + String(value); };
    try { add(el.getAttribute('style')); } catch (e) {}
    try { if (el.parentElement) add(el.parentElement.getAttribute('style')); } catch (e) {}
    try {
      const cs = (typeof window !== 'undefined' && window.getComputedStyle)
        ? window.getComputedStyle(el) : null;
      if (cs) {
        add(cs.justifyContent); add(cs.alignItems); add(cs.alignSelf);
        add(cs.textAlign); add(cs.marginLeft); add(cs.marginRight);
      }
      const ps = (el.parentElement && typeof window !== 'undefined' && window.getComputedStyle)
        ? window.getComputedStyle(el.parentElement) : null;
      if (ps) { add(ps.justifyContent); add(ps.alignItems); }
    } catch (e) {}
    return sig.replace(/\s*:\s*/g, ':').toLowerCase();
  };
  const classify = (el) => {
    let cls = '';
    try { cls = (el.className || '') + ' ' + ((el.parentElement && el.parentElement.className) || ''); } catch (e) {}
    cls = (String(cls) + ' ' + styleSig(el)).toLowerCase();
    if (OWN_HINTS.some((h) => cls.includes(h))) return 'You';
    if (OTHER_HINTS.some((h) => cls.includes(h))) return 'Stranger';
    return null;
  };
  const bump = (source) => { diag.speakerSources[source] = (diag.speakerSources[source] || 0) + 1; };

  const messages = [];
  for (const item of itemEls) {
    let textEl = null;
    for (const sel of TEXTS) {
      try { const el = item.querySelector(sel); if (el) { textEl = el; if (custom.has(sel)) diag.usedCustom = true; break; } } catch (e) {}
    }

    let username = '';
    for (const sel of USERNAMES) {
      try {
        const el = item.querySelector(sel);
        if (el) {
          const t = norm(el.getAttribute('username') || el.textContent || '');
          if (t && t.length <= 40) { username = t; if (custom.has(sel)) diag.usedCustom = true; break; }
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
# Selector config + learned selectors
#
# The site can be redesigned at any time.  Instead of editing this file, a
# discovered selector set can live in ``config/chat_selectors.json`` (written
# by ``tools/chat_detect_debug.py --save``), and the reader can even heal
# itself at runtime (see :func:`extract_with_diag`).  Custom selectors are
# always tried BEFORE the built-in ones, so nothing regresses.
# --------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
SELECTOR_CONFIG_ENV = "EVA_CHAT_SELECTORS"
DEFAULT_SELECTOR_CONFIG = ROOT_DIR / "config" / "chat_selectors.json"
AUTOSAVE_ENV = "EVA_SELECTOR_AUTOSAVE"
DISCOVERY_COOLDOWN = 30.0

#: config-file keys -> the key names used in the extractor JS arguments
_SELECTOR_KEY_MAP = {
    "containers": "containers",
    "items": "items",
    "usernames": "usernames",
    "texts": "texts",
    "my_usernames": "myUsernames",
    "myusernames": "myUsernames",
    "own_hints": "ownHints",
    "ownhints": "ownHints",
    "other_hints": "otherHints",
    "otherhints": "otherHints",
}

_CONFIG_CACHE: Dict[str, Any] = {"path": None, "mtime": None, "data": {}}
_LEARNED: Dict[str, List[str]] = {}
_LAST_DISCOVERY_TS = 0.0


def selector_config_path() -> Path:
    """Where the custom selectors are read from (env override supported)."""
    override = os.environ.get(SELECTOR_CONFIG_ENV)
    return Path(override).expanduser() if override else DEFAULT_SELECTOR_CONFIG


def _clean_selector_list(values: Any) -> List[str]:
    out: List[str] = []
    if isinstance(values, str):
        values = [values]
    for value in values or []:
        sel = str(value or "").strip()
        if not sel or len(sel) > 400:
            continue
        # cheap sanity check: selectors are passed to querySelector, which
        # throws on garbage — we prefer to skip it here (diag stays quiet)
        if sel.count("[") != sel.count("]") or sel.count("(") != sel.count(")"):
            continue
        if sel not in out:
            out.append(sel)
    return out


def coerce_selectors(raw: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Normalise any selector dict (snake_case, camelCase) to JS arg keys."""
    out: Dict[str, List[str]] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        target = _SELECTOR_KEY_MAP.get(str(key).lower().replace("-", "_"))
        if not target:
            continue
        cleaned = _clean_selector_list(value)
        if cleaned:
            out[target] = cleaned
    return out


def load_selector_config(*, force: bool = False) -> Dict[str, List[str]]:
    """Load ``config/chat_selectors.json`` (cached by mtime).

    A broken file never breaks the bot: it is ignored (and reported in the
    diagnostics of the next extraction).
    """
    path = selector_config_path()
    try:
        stat = path.stat()
    except OSError:
        _CONFIG_CACHE.update(path=None, mtime=None, data={})
        return {}
    if (not force and _CONFIG_CACHE["path"] == str(path)
            and _CONFIG_CACHE["mtime"] == stat.st_mtime):
        return dict(_CONFIG_CACHE["data"])
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) or {}
        data = coerce_selectors(raw)
    except Exception:
        data = {}
    _CONFIG_CACHE.update(path=str(path), mtime=stat.st_mtime, data=data)
    return dict(data)


def learned_selectors() -> Dict[str, List[str]]:
    """Selectors discovered at runtime for this process (self-healing)."""
    return {k: list(v) for k, v in _LEARNED.items()}


def apply_selectors(selectors: Optional[Dict[str, Any]], *, persist: bool = False,
                    note: str = "") -> Dict[str, List[str]]:
    """Remember a selector set for every later poll (used by healing/tools)."""
    clean = coerce_selectors(selectors)
    for key, values in clean.items():
        merged = list(_LEARNED.get(key) or [])
        for value in values:
            if value not in merged:
                merged.append(value)
        _LEARNED[key] = merged
    if persist and clean:
        try:
            from browser import selector_doctor
            report = selector_doctor.Report(ok=True, selectors=selectors, confidence=1.0,
                                            source="apply_selectors")
            selector_doctor.save_config(report, selector_config_path(), note=note)
        except Exception:
            pass
    return clean


def reset_learned() -> None:
    """Forget runtime-learned selectors (tests / manual retry)."""
    _LEARNED.clear()


def merge_selector_dicts(*dicts: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Merge selector dicts (earlier wins, deduped, JS arg keys)."""
    out: Dict[str, List[str]] = {}
    for raw in dicts:
        for key, values in coerce_selectors(raw).items():
            merged = out.setdefault(key, [])
            for value in values:
                if value not in merged:
                    merged.append(value)
    return out


def custom_selectors(extra: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
    """All non-default selectors: config file + runtime-learned + explicit."""
    return merge_selector_dicts(load_selector_config(), _LEARNED, extra)


def suggestion_dir() -> Path:
    return ROOT_DIR / "logs"


def save_suggestion(report: Any, *, autosave: Optional[bool] = None) -> Optional[str]:
    """Persist a discovery result.

    Always writes ``logs/selector_suggestion_*.json`` (so the evidence stays
    for a human), and additionally updates the real config file when
    ``EVA_SELECTOR_AUTOSAVE=1``.
    """
    try:
        from browser import selector_doctor
        out_dir = suggestion_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"selector_suggestion_{stamp}.json"
        path.write_text(json.dumps(report.to_config("auto-discovered by chat_reader"),
                                   indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        if autosave is None:
            autosave = str(os.environ.get(AUTOSAVE_ENV, "")).strip() not in ("", "0", "false", "False")
        if autosave:
            selector_doctor.save_config(report, selector_config_path(),
                                        note="auto-healed by chat_reader at runtime")
        return str(path)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Page-facing API
# --------------------------------------------------------------------------

def _evaluate(page, args: Dict[str, Any]) -> Tuple[Any, List[str]]:
    """Run the extractor JS; returns (result, errors)."""
    errors: List[str] = []
    try:
        return page.evaluate(CHAT_EXTRACT_JS, args), errors
    except Exception as exc:  # page closed / evaluate failed
        errors.append(f"evaluate: {type(exc).__name__}: {exc}")
        try:
            return page.evaluate(CHAT_EXTRACT_JS, args), errors
        except Exception as exc2:
            errors.append(f"evaluate retry: {type(exc2).__name__}: {exc2}")
            return None, errors


def _self_heal(page, base_args: Dict[str, Any], diag: Dict[str, Any],
               recent: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Markup changed? derive the selectors from the page and retry once."""
    global _LAST_DISCOVERY_TS
    messages: List[Dict[str, Any]] = []
    now = time.time()
    if now - _LAST_DISCOVERY_TS < DISCOVERY_COOLDOWN:
        diag["discoverySkipped"] = "cooldown"
        return messages, []
    _LAST_DISCOVERY_TS = now
    try:
        from browser import selector_doctor
    except Exception as exc:
        diag["errors"].append(f"selector doctor unavailable: {type(exc).__name__}: {exc}")
        return messages, []
    try:
        report = selector_doctor.discover_page(page)
    except Exception as exc:
        diag["errors"].append(f"discovery: {type(exc).__name__}: {exc}")
        return messages, []

    diag["discoveryRuns"] = int(diag.get("discoveryRuns") or 0) + 1
    diag["discovery"] = report.summary()
    if not report.ok:
        return messages, []
    discovered = coerce_selectors(report.selectors)
    if not discovered.get("containers") or not discovered.get("items"):
        diag["errors"].append("discovery found no usable container/row selector")
        return messages, []

    args = dict(base_args)
    args["sel"] = merge_selector_dicts(base_args.get("sel"), discovered)
    result, errors = _evaluate(page, args)
    if errors:
        diag["errors"].extend(errors)
        return messages, []
    if isinstance(result, dict):
        diag.update({k: v for k, v in (result.get("diag") or {}).items()
                     if k not in ("errors",)})
        messages = _post_process(result.get("messages") or [], recent)
    elif isinstance(result, list):
        messages = _post_process(result, recent)

    path = save_suggestion(report)
    if path:
        diag["suggestionFile"] = path
    if messages:
        apply_selectors(discovered)
        diag["healed"] = True
        diag["healedSelectors"] = discovered
    return messages, []


def extract_with_diag(page, recent_sent: Optional[Iterable[str]] = None,
                      my_username: Optional[str] = None,
                      extra_selectors: Optional[Dict[str, Any]] = None,
                      autodiscover: bool = True,
                      ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read the chat list from *page*.

    Returns ``(messages, diag)`` where every message is
    ``{'speaker': 'You'|'Stranger', 'message': str, ...}``.

    Selector sources, in priority order: *extra_selectors* (caller) →
    ``config/chat_selectors.json`` → runtime-learned (self-healing) → the
    built-in list.

    When nothing is detected and *autodiscover* is on, the page markup is
    analysed once (``browser/selector_doctor``) and the extraction is retried
    with the discovered selectors — that is how a redesigned site is handled
    without editing code.  ``diag`` reports ``healed``/``healedSelectors``/
    ``suggestionFile`` when that happens.

    Never raises for site-side problems: JS errors land in ``diag['errors']``
    so the chat loop keeps running.
    """
    diag: Dict[str, Any] = {"extractor": "chat_reader", "errors": []}
    recent = [norm_text(t) for t in (recent_sent or []) if norm_text(t)]
    args: Dict[str, Any] = {"recentSent": recent,
                            "myUsernameHint": norm_text(my_username) or None}
    custom = custom_selectors(extra_selectors)
    if custom:
        args["sel"] = custom

    result, errors = _evaluate(page, args)
    if errors and result is None:
        diag["errors"].extend(errors)
        return [], diag

    if isinstance(result, list):  # defensive: old-style return
        return _post_process(result, recent), diag
    if not isinstance(result, dict):
        diag["errors"].append(f"unexpected result type: {type(result).__name__}")
        return [], diag

    diag.update(result.get("diag") or {})
    messages = _post_process(result.get("messages") or [], recent)
    if messages or not autodiscover:
        return messages, diag
    # nothing parsed and the page shows no chat text at all -> there is
    # nothing to discover (blank page / not the chat view yet)
    if int(diag.get("rawTextLength") or 0) < 1:
        diag["discoverySkipped"] = "no text on the page"
        return messages, diag
    healed, more_errors = _self_heal(page, args, diag, recent)
    diag["errors"].extend(more_errors)
    return healed, diag


def extract(page, recent_sent: Optional[Iterable[str]] = None,
            my_username: Optional[str] = None,
            extra_selectors: Optional[Dict[str, Any]] = None,
            autodiscover: bool = True) -> List[Dict[str, Any]]:
    """Same as :func:`extract_with_diag` but returns only the messages."""
    return extract_with_diag(page, recent_sent=recent_sent, my_username=my_username,
                             extra_selectors=extra_selectors,
                             autodiscover=autodiscover)[0]


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
    line = (
        f"container={diag.get('container') or 'NOT FOUND'}"
        f" | items_selector={diag.get('itemSelector') or 'none'}"
        f" | items={diag.get('items', 0)}"
        f" | my_username={diag.get('myUsername') or 'unknown'}"
        f" | speaker_via={src_txt}"
        f" | text_chars={diag.get('rawTextLength', 0)}"
    )
    if diag.get("customSelectors"):
        line += (f" | custom_selectors={diag['customSelectors']}"
                 f"({'used' if diag.get('usedCustom') else 'unused'})")
    if diag.get("healed"):
        line += f" | HEALED={diag.get('healedSelectors')}"
    elif diag.get("discovery"):
        line += f" | discovery={diag['discovery']}"
    if diag.get("errors"):
        line += f" | errors={diag['errors'][:2]}"
    return line


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
    "CHAT_EXTRACT_JS", "LEGACY_EXTRACT_JS", "MessageTracker", "apply_selectors",
    "coerce_selectors", "custom_selectors", "describe_diag", "dump_dom",
    "extract", "extract_with_diag", "learned_selectors", "legacy_extract",
    "load_selector_config", "merge_selector_dicts", "norm_text",
    "reset_learned", "save_suggestion", "selector_config_path",
]
