"""Selector doctor — find the chat selectors on a page whose markup changed.

Why this module exists
----------------------
``browser/chat_reader.py`` reads the stranger's SMS with a *list* of known
selectors.  That list is defensive, but it is still a list: when the site is
redesigned (new classes, ``ol/li`` replaced by ``div`` grids, Tailwind hashed
class names, a virtualised list, ``data-testid`` renames, ...) every known
selector misses and the bot goes blind again.

This module looks at a page and *derives* the selectors from the markup
itself:

    message list  -> the repeated-sibling group that looks like chat
    row           -> the CSS selector that matches exactly those rows
    message text  -> the child that carries the message
    username      -> the short leading child that differs per speaker
    speaker hints -> alignment/own/incoming class hints

It runs on a plain DOM tree, not on a live browser, so the very same code
works in three situations:

* live page      — :func:`discover_page` (Playwright ``page.evaluate``),
* saved HTML     — :func:`discover_html` (``stdlib`` ``html.parser``, no
                   dependency at all: "Save as HTML only" is enough),
* tests          — :func:`discover_tree` on a hand-built tree.

The result is a *report*: the suggested selectors, why they were chosen, how
confident the doctor is, and a verification run (the suggestion is applied
back to the page with :func:`extract_messages`; if it cannot read the SMS,
confidence is cut and the tool says so).

``tools/chat_detect_debug.py --suggest`` prints a report;
``--save`` merges it into ``config/chat_selectors.json``, which
``browser/chat_reader.py`` loads on every poll — so a site change can be
fixed without touching any code, and with ``EVA_SELECTOR_AUTOSAVE=1`` the bot
even heals itself at runtime.

Everything here is offline and dependency-free by design.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Tree format (identical from html.parser and from the browser serializer)
#
#   {"tag": "li", "attrs": {"class": "a b"}, "text": "direct text",
#    "children": [ ... ]}
#
# Python adds, while indexing: ``parent`` and ``n`` (path id).  Never
# serialized to JSON.
# --------------------------------------------------------------------------

_SKIP_TAGS = {
    "script", "style", "noscript", "template", "svg", "head", "meta", "link",
    "title", "iframe", "canvas", "path", "g", "defs", "br", "hr",
}

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

MAX_NODES = 80000
CHAT_CLASS_HINTS = ("message", "messages", "msg", "chat", "conversation",
                    "thread", "log", "feed", "bubble", "timeline", "list")
ROW_CLASS_HINTS = ("message", "msg", "row", "item", "bubble", "chat", "entry",
                   "line", "comment", "text")
SCROLL_CLASS_HINTS = ("overflow", "scroll", "scrollable", "virtual")
OWN_CLASS_HINTS = ("justify-end", "self-end", "ml-auto", "text-right",
                   "items-end", "flex-row-reverse", "bg-primary", "bg-blue",
                   "bg-indigo", "own", "outgoing", "is-me", "from-me", "sent",
                   "me-", "-me", "right")
OTHER_CLASS_HINTS = ("justify-start", "self-start", "mr-auto", "text-left",
                     "items-start", "bg-panel", "bg-gray", "incoming", "other",
                     "from-them", "received", "left")
BOLD_CLASS_HINTS = ("bold", "semibold", "font-medium", "username", "user",
                    "name", "author", "nick")
MY_NAME_ATTRS = ("username", "data-username", "data-user", "data-name",
                 "alt", "title")
#: Page areas that are never the message list (marketing pages, sidebars...)
NON_CHAT_HINTS = ("feature", "pricing", "plan", "testimonial", "service",
                  "product", "faq", "blog", "article", "news", "docs", "guide",
                  "menu", "gallery", "review", "step", "card", "benefit",
                  "team", "contact", "hero", "banner", "sidebar", "widget")

#: Printed by the tools; also stored in the config file.
NEXT_STEPS = (
    "python tools/chat_detect_debug.py --html <saved_page.html> --suggest\n"
    "python tools/chat_detect_debug.py --html <saved_page.html> --save\n"
    "python tools/chat_detect_debug.py --url https://chitchat.gg/ --suggest"
)


# --------------------------------------------------------------------------
# Tree building — 1. saved HTML (stdlib only)
# --------------------------------------------------------------------------

class _TreeBuilder:
    def __init__(self) -> None:
        self.root: Dict[str, Any] = {"tag": "#document", "attrs": {}, "text": "",
                                     "children": []}
        self._stack: List[Dict[str, Any]] = [self.root]
        self._skip_depth = 0
        self.count = 0
        self.truncated = False

    def open(self, tag: str, attrs: Dict[str, str]) -> None:
        node = {"tag": tag, "attrs": attrs, "text": "", "children": []}
        self._stack[-1]["children"].append(node)
        self.count += 1
        if self.count > MAX_NODES:
            self.truncated = True
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            node["#skip"] = True
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def close(self, tag: str) -> None:
        # pop back to the matching open tag, tolerate sloppy markup
        for i in range(len(self._stack) - 1, 0, -1):
            node = self._stack[i]
            if node.get("tag") == tag:
                for j in range(len(self._stack) - 1, i - 1, -1):
                    if self._stack[j].pop("#skip", None) is not None:
                        self._skip_depth = max(0, self._skip_depth - 1)
                del self._stack[i:]
                return
        # unmatched close tag: ignore

    def text(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._stack and data.strip():
            # kept as an ordered child so text/child order survives
            self._stack[-1]["children"].append(
                {"tag": "#text", "attrs": {}, "text": data, "children": []})

    def finish(self) -> Dict[str, Any]:
        return self.root


def parse_html(html: str) -> Dict[str, Any]:
    """Build the doctor tree from a saved page (stdlib ``html.parser``)."""
    from html.parser import HTMLParser

    builder = _TreeBuilder()

    class _Parser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            builder.open(tag.lower(), {k.lower(): (v or "") for k, v in attrs})

        def handle_startendtag(self, tag, attrs):
            builder.open(tag.lower(), {k.lower(): (v or "") for k, v in attrs})
            if tag.lower() not in _VOID_TAGS:
                builder.close(tag.lower())

        def handle_endtag(self, tag):
            builder.close(tag.lower())

        def handle_data(self, data):
            builder.text(data)

    parser = _Parser(convert_charrefs=True)
    parser.feed(html)
    parser.close()
    root = builder.finish()
    root["#truncated"] = builder.truncated
    return root


# --------------------------------------------------------------------------
# Tree building — 2. live page (Playwright) — same shape as parse_html
# --------------------------------------------------------------------------

SERIALIZE_JS = r"""
() => {
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','HEAD',
                        'META','LINK','TITLE','IFRAME','CANVAS','PATH','G']);
  const VOID = new Set(['AREA','BASE','BR','COL','EMBED','HR','IMG','INPUT',
                        'LINK','META','PARAM','SOURCE','TRACK','WBR']);
  const MAX = %(max_nodes)d;
  let count = 0, truncated = false;
  const walk = (el) => {
    if (++count > MAX) { truncated = true; return null; }
    const attrs = {};
    try { for (const a of el.attributes) attrs[a.name] = a.value; } catch (e) {}
    const node = {tag: el.tagName.toLowerCase(), attrs: attrs, children: []};
    if (SKIP.has(el.tagName)) { node.skip = true; return node; }
    const pushText = (value) => {
      const t = String(value || '');
      if (t.trim()) node.children.push({tag: '#text', attrs: {}, text: t, children: []});
    };
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      try { pushText(el.value); } catch (e) {}
    }
    for (const child of el.childNodes) {
      if (child.nodeType === 3) pushText(child.nodeValue);
      else if (child.nodeType === 1 && !VOID.has(child.tagName)) {
        const c = walk(child);
        if (c) node.children.push(c);
      }
    }
    return node;
  };
  const root = walk(document.documentElement);
  return {tag: '#document', attrs: {}, text: '',
          children: root ? [root] : [], '#truncated': truncated};
}
""" % {"max_nodes": MAX_NODES}


def serialize_page(page) -> Dict[str, Any]:
    """Serialise a live Playwright page into the doctor tree."""
    return page.evaluate(SERIALIZE_JS)


# --------------------------------------------------------------------------
# Tree indexing helpers
# --------------------------------------------------------------------------

def index_tree(root: Dict[str, Any]) -> Dict[str, Any]:
    """Add ``parent`` links and path ids; return the same root."""
    counter = [0]

    def walk(node: Dict[str, Any], parent: Optional[Dict[str, Any]]) -> None:
        node["parent"] = parent
        node["n"] = f"{counter[0]}"
        counter[0] += 1
        for child in node.get("children") or []:
            walk(child, node)

    walk(root, None)
    return root


def iter_nodes(root: Dict[str, Any], *, skip_hidden: bool = True) -> Iterable[Dict[str, Any]]:
    stack = [root]
    while stack:
        node = stack.pop()
        tag = node.get("tag") or ""
        if tag == "#document":
            stack.extend(reversed(node.get("children") or []))
            continue
        if node.get("skip") or tag in _SKIP_TAGS or tag == "#text":
            continue
        yield node
        stack.extend(reversed(node.get("children") or []))


def classes_of(node: Dict[str, Any]) -> List[str]:
    raw = (node.get("attrs") or {}).get("class") or ""
    return [c for c in str(raw).split() if c]


def own_text(node: Dict[str, Any]) -> str:
    """Text directly inside the node (no children), document order."""
    return norm_space(" ".join(
        str(c.get("text") or "") for c in (node.get("children") or [])
        if c.get("tag") == "#text"))


def node_text(node: Dict[str, Any]) -> str:
    """All text inside the node, in document order (like ``innerText``)."""
    parts: List[str] = []

    def walk(n: Dict[str, Any]) -> None:
        legacy = n.get("text")
        if legacy and n.get("tag") not in ("#text",):
            parts.append(str(legacy))
        for child in n.get("children") or []:
            if child.get("skip"):
                continue
            if child.get("tag") == "#text":
                parts.append(str(child.get("text") or ""))
            else:
                walk(child)

    walk(node)
    return norm_space(" ".join(p for p in parts if p and p.strip()))


def norm_space(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def node_depth(node: Dict[str, Any]) -> int:
    depth = 0
    parent = node.get("parent")
    while parent is not None:
        depth += 1
        parent = parent.get("parent")
    return depth


def children_of(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Element children (``#text`` runs are not elements)."""
    return [c for c in (node.get("children") or [])
            if not c.get("skip") and c.get("tag") != "#text"]


def ancestors(node: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    parent = node.get("parent")
    while parent is not None:
        yield parent
        parent = parent.get("parent")


def descendants(node: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Element descendants, in document order (``#text`` runs skipped)."""
    for child in node.get("children") or []:
        if child.get("skip") or child.get("tag") == "#text":
            continue
        yield child
        yield from descendants(child)


# --------------------------------------------------------------------------
# Tiny CSS subset matcher (only what the doctor emits)
#
# supported:  tag   *   .class   #id   [attr]  [attr=v]  [attr*=v]
#             [attr^=v]  [attr$=v]  [attr~=v]  :scope
# combinators: descendant (space) and child (>), comma groups
# unsupported pseudo classes are ignored (treated as no constraint)
# --------------------------------------------------------------------------

_ATTR_RE = re.compile(r"\[\s*([\w:-]+)\s*(?:([*^$~|]?=)\s*(\"[^\"]*\"|'[^']*'|[^\]]*?)\s*)?\]")
_TOKEN_RE = re.compile(r"([.#]?[\w-]+|\[[^\]]*\]|:scope|::?[\w-]+\(\s*[^)]*\)|::?[\w-]+|\*)")


class _Compound(dict):
    """{"tag": str|None, "classes": [...], "id": str|None, "attrs": [...],
        "scope": bool}"""


def _parse_compound(text: str) -> _Compound:
    comp = _Compound(tag=None, classes=[], id=None, attrs=[], scope=False)
    for token in _TOKEN_RE.findall(text):
        if token == "*":
            continue
        if token == ":scope":
            comp["scope"] = True
        elif token.startswith(":"):
            continue  # unsupported pseudo -> ignore
        elif token.startswith("."):
            comp["classes"].append(token[1:])
        elif token.startswith("#"):
            comp["id"] = token[1:]
        elif token.startswith("["):
            m = _ATTR_RE.match(token)
            if not m:
                continue
            name, op, value = m.group(1), m.group(2), m.group(3)
            if value is not None:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
            comp["attrs"].append((name, op or None, value))
        else:
            comp["tag"] = token.lower()
    return comp


def _split_groups(selector: str) -> List[List[Tuple[Optional[str], _Compound]]]:
    """Split ``a > b, c d`` into parsed parts per comma group."""
    groups: List[List[Tuple[Optional[str], _Compound]]] = []
    for group in selector.split(","):
        group = group.strip()
        if not group:
            continue
        # tokenise into compound strings + combinators
        parts: List[Tuple[Optional[str], _Compound]] = []
        buf = ""
        combinator: Optional[str] = None
        depth = 0
        quote = ""
        for ch in group:
            if quote:
                buf += ch
                if ch == quote:
                    quote = ""
                continue
            if ch in "\"'":
                quote = ch
                buf += ch
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth = max(0, depth - 1)
            if depth == 0 and ch in " \t\n>+~":
                if buf.strip():
                    parts.append((combinator, _parse_compound(buf.strip())))
                    buf = ""
                    combinator = " " if ch in " \t\n" else ch
                elif ch in ">+~":
                    combinator = ch
                continue
            buf += ch
        if buf.strip():
            parts.append((combinator, _parse_compound(buf.strip())))
        if parts:
            groups.append(parts)
    return groups


def _attr_matches(node: Dict[str, Any], item: Tuple[str, Optional[str], Optional[str]]) -> bool:
    name, op, value = item
    attrs = node.get("attrs") or {}
    actual = attrs.get(name)
    if actual is None and name == "class":
        actual = (node.get("attrs") or {}).get("class")
    if actual is None:
        return False
    actual = str(actual)
    if op is None:
        return True
    if value is None:
        return False
    if op == "=":
        return actual == value
    if op == "*=":
        return value.lower() in actual.lower()
    if op == "^=":
        return actual.lower().startswith(value.lower())
    if op == "$=":
        return actual.lower().endswith(value.lower())
    if op == "~=":
        return value.lower() in [w.lower() for w in actual.split()]
    return False


def _compound_matches(comp: _Compound, node: Dict[str, Any], scope: Dict[str, Any]) -> bool:
    if comp.get("scope"):
        return node is scope
    tag = comp.get("tag")
    if tag and (node.get("tag") or "").lower() != tag:
        return False
    classes = classes_of(node)
    for cls in comp["classes"]:
        if cls not in classes:
            return False
    if comp.get("id") and (node.get("attrs") or {}).get("id") != comp["id"]:
        return False
    for item in comp["attrs"]:
        if not _attr_matches(node, item):
            return False
    return True


def _match_from(parts: List[Tuple[Optional[str], _Compound]], index: int,
                node: Dict[str, Any], scope: Dict[str, Any]) -> bool:
    combinator, comp = parts[index]
    if not _compound_matches(comp, node, scope):
        return False
    if index == 0:
        return True
    linker = combinator
    if linker == ">":
        parent = node.get("parent")
        return parent is not None and _match_from(parts, index - 1, parent, scope)
    if linker in ("+", "~"):
        siblings = (node.get("parent") or {}).get("children") or []
        idx = next((i for i, s in enumerate(siblings) if s is node), None)
        if idx is None:
            return False
        if linker == "+":
            return idx > 0 and _match_from(parts, index - 1, siblings[idx - 1], scope)
        for prev in reversed(siblings[:idx]):
            if _match_from(parts, index - 1, prev, scope):
                return True
        return False
    # descendant
    parent = node.get("parent")
    while parent is not None:
        if _match_from(parts, index - 1, parent, scope):
            return True
        parent = parent.get("parent")
    return False


def selector_matches(selector: str, node: Dict[str, Any],
                     scope: Optional[Dict[str, Any]] = None) -> bool:
    """True when *node* matches *selector* (CSS subset, see module docstring)."""
    scope = scope if scope is not None else node
    for parts in _split_groups(selector):
        if _match_from(parts, len(parts) - 1, node, scope):
            return True
    return False


def query_all(root: Dict[str, Any], selector: str,
              scope: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """``root.querySelectorAll(selector)`` for the supported subset."""
    if "n" not in root:          # freshly parsed tree: needs parent links
        index_tree(root)
    try:
        groups = _split_groups(selector)
    except Exception:
        return []
    if not groups:
        return []
    scope_el = scope if scope is not None else root
    out: List[Dict[str, Any]] = []
    for node in descendants(root):
        for parts in groups:
            if _match_from(parts, len(parts) - 1, node, scope_el):
                out.append(node)
                break
    return out


def outermost(nodes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop matches contained in another match (mirrors the JS extractor)."""
    node_set = list(nodes)
    out: List[Dict[str, Any]] = []
    for node in node_set:
        if any(other is not node and _contains(other, node) for other in node_set):
            continue
        out.append(node)
    return out


def _contains(outer: Dict[str, Any], inner: Dict[str, Any]) -> bool:
    parent = inner.get("parent")
    while parent is not None:
        if parent is outer:
            return True
        parent = parent.get("parent")
    return False


# --------------------------------------------------------------------------
# Selector building helpers
# --------------------------------------------------------------------------

_HASHED_CLASS_RE = re.compile(
    r"^(?:css|sc|jsx|emotion|tw|_)-?[a-z0-9]{4,}$|^[a-z]{1,3}[0-9]{3,}$|[0-9a-f]{6,}",
    re.IGNORECASE)
_UNSTABLE_ATTRS = {"class", "style", "id", "role", "aria-label", "href", "src",
                   "value", "placeholder", "draggable", "tabindex", "width",
                   "height", "target", "rel", "type", "name"}


def is_stable_class(token: str) -> bool:
    """Heuristic: is this class name hand-written (stable) or build-hashed?"""
    if not token or len(token) > 40:
        return False
    if _HASHED_CLASS_RE.match(token):
        return False
    digits = sum(c.isdigit() for c in token)
    letters = sum(c.isalpha() for c in token)
    if digits and letters and len(token) >= 8 and digits >= 3:
        return False          # random-looking mix, e.g. "a1b2c3d4"
    if digits and not letters:
        return False
    return True


def common_classes(nodes: Sequence[Dict[str, Any]], *, ratio: float = 0.8,
                   stable_only: bool = True) -> List[str]:
    if not nodes:
        return []
    need = max(1, int(len(nodes) * ratio))
    counts: Dict[str, int] = {}
    for node in nodes:
        for cls in set(classes_of(node)):
            counts[cls] = counts.get(cls, 0) + 1
    keep = [(c, n) for c, n in counts.items() if n >= need]
    if stable_only:
        stable = [(c, n) for c, n in keep if is_stable_class(c)]
        if stable:
            keep = stable
    return [c for c, _ in sorted(keep, key=lambda kv: (-kv[1], kv[0]))]


def class_selector_hint(tokens: Sequence[str]) -> Optional[str]:
    """Prefer a token that *says* what the node is (message/row/bubble...)."""
    if not tokens:
        return None
    for hint in ROW_CLASS_HINTS:
        for token in tokens:
            if hint in token.lower():
                return token
    return tokens[0]


def common_attrs(nodes: Sequence[Dict[str, Any]], *, ratio: float = 0.8
                 ) -> List[Tuple[str, str]]:
    """Attribute names present on most rows -> (name, 'const:<v>|prefix:<p>|'')."""
    if not nodes:
        return []
    need = max(1, int(len(nodes) * ratio))
    values: Dict[str, List[str]] = {}
    for node in nodes:
        for name, value in (node.get("attrs") or {}).items():
            if name.lower() in _UNSTABLE_ATTRS:
                continue
            if name.lower().startswith("on"):
                continue
            values.setdefault(name.lower(), []).append(str(value))
    out: List[Tuple[str, str]] = []
    for name, vals in values.items():
        if len(vals) < need:
            continue
        uniq = set(vals)
        if len(uniq) == 1:
            out.append((name, "const:" + vals[0]))
            continue
        prefixes = {re.sub(r"\d+$", "", v) for v in uniq}
        if len(prefixes) == 1:
            prefix = prefixes.pop()
            if prefix and len(prefix) >= 3:
                out.append((name, "prefix:" + prefix))
                continue
        out.append((name, ""))
    order = {"data-testid": 0, "data-message-id": 1, "data-id": 2, "data-index": 3}
    return sorted(out, key=lambda kv: (order.get(kv[0], 9), kv[0]))


def css_attr_selector(name: str, kind: str) -> str:
    if kind.startswith("const:"):
        return "[%s='%s']" % (name, kind[len("const:"):].replace("'", "\\'"))
    if kind.startswith("prefix:"):
        return "[%s^='%s']" % (name, kind[len("prefix:"):].replace("'", "\\'"))
    return "[%s]" % name


def text_looks_like_chat(text: str) -> bool:
    if not text:
        return False
    if len(text) > 400:
        return False
    return any(ch.isalnum() for ch in text)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

class Report(dict):
    """Discovery result (a dict with attribute access for convenience)."""

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))

    @property
    def selectors(self) -> Dict[str, List[str]]:
        return self.get("selectors") or {}

    @property
    def confidence(self) -> float:
        return float(self.get("confidence") or 0.0)

    def to_config(self, note: str = "") -> Dict[str, Any]:
        cfg: Dict[str, Any] = {
            "_note": note or ("Auto-discovered chat selectors (selector doctor). "
                              "Delete this file to fall back to the built-in list."),
            "learned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": self.get("source") or "selector_doctor",
            "confidence": round(self.confidence, 3),
        }
        cfg.update(self.selectors)
        return cfg

    def summary(self) -> str:
        if not self.ok:
            return f"no chat markup found ({'; '.join(self.get('notes') or ['unknown'])})"
        s = self.selectors
        return (f"confidence={self.confidence:.2f} "
                f"rows={self.get('rowCount', 0)} "
                f"item={','.join(s.get('items') or []) or '-'} "
                f"text={','.join(s.get('texts') or []) or '(row text)'} "
                f"user={','.join(s.get('usernames') or []) or '-'} "
                f"verified={self.get('verifiedMessages', 0)}")


def _row_signature(kids: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    texts = [node_text(k) for k in kids]
    lengths = [len(t) for t in texts]
    nonempty = [t for t in texts if t]
    links = sum(1 for k in kids
                if k.get("tag") == "a" or any(d.get("tag") == "a" for d in descendants(k)))
    small = sum(1 for t in nonempty if t_looks(t, 1, 120))
    return {
        "count": len(kids),
        "texts": texts,
        "avgLen": (sum(lengths) / len(lengths)) if lengths else 0,
        "variety": len(set(nonempty)),
        "links": links,
        "small": small,
        "childrenPerRow": (sum(len(children_of(k)) for k in kids) / len(kids)) if kids else 0,
    }


def t_looks(text: str, lo: int, hi: int) -> bool:
    return lo <= len(text) <= hi


def _score_container(node: Dict[str, Any], sig: Dict[str, Any],
                     kids: Sequence[Dict[str, Any]]) -> Tuple[float, List[str]]:
    parts: List[str] = []
    score = 0.0
    count = sig["count"]
    repeat = min(count, 30) / 30.0
    score += 0.30 * repeat
    parts.append(f"+{0.30 * repeat:.2f} repetition({count})")

    variety = (sig["variety"] / count) if count else 0.0
    score += 0.15 * variety
    parts.append(f"+{0.15 * variety:.2f} text-variety({sig['variety']})")

    shortness = (sig["small"] / sig["count"]) if sig["count"] else 0.0
    score += 0.20 * shortness
    parts.append(f"+{0.20 * shortness:.2f} short-rows")

    tag = (node.get("tag") or "").lower()
    if tag in ("ol", "ul"):
        score += 0.06
        parts.append("+0.06 list-tag")
    role = str((node.get("attrs") or {}).get("role") or "").lower()
    if role in ("log", "list", "feed", "messages"):
        score += 0.10
        parts.append(f"+0.10 role={role}")
    tokens = " ".join(classes_of(node)).lower()
    if any(h in tokens for h in CHAT_CLASS_HINTS):
        score += 0.08
        parts.append("+0.08 chat-class")
    if any(h in tokens for h in SCROLL_CLASS_HINTS) or "overflow" in str((node.get("attrs") or {}).get("style") or ""):
        score += 0.06
        parts.append("+0.06 scrollable")

    ancestors_list = list(ancestors(node))[:3]
    near_tags = {a.get("tag") for a in ancestors_list} | {tag}
    if {"nav", "header", "footer"} & near_tags:
        score -= 0.25
        parts.append("-0.25 site-chrome")

    # scrollable containers / ancestors are chat-like ("chat" class nearby too)
    near_classes = " ".join(
        " ".join(classes_of(a)) + " " + str((a.get("attrs") or {}).get("id") or "")
        for a in ancestors_list).lower()
    if "overflow" in str((node.get("attrs") or {}).get("style") or "").lower():
        pass
    elif any("overflow" in str((a.get("attrs") or {}).get("style") or "").lower()
             or any(h in " ".join(classes_of(a)).lower() for h in SCROLL_CLASS_HINTS)
             for a in ancestors_list):
        score += 0.06
        parts.append("+0.06 scrollable-ancestor")
    if any(h in near_classes for h in CHAT_CLASS_HINTS):
        score += 0.05
        parts.append("+0.05 chat-class-ancestor")

    link_rows = sig["links"]
    if sig["count"] and (link_rows / sig["count"]) > 0.5:
        score -= 0.30
        parts.append("-0.30 link-rows")

    # headings/buttons inside the rows: marketing sections, not chat
    heading_rows = sum(1 for k in kids
                       if any(d.get("tag") in ("h1", "h2", "h3", "h4", "h5", "h6")
                              for d in descendants(k)))
    button_rows = sum(1 for k in kids
                      if any(d.get("tag") == "button" for d in descendants(k)))
    if heading_rows / sig["count"] > 0.5:
        score -= 0.30
        parts.append("-0.30 heading-rows")
    if button_rows / sig["count"] > 0.5:
        score -= 0.25
        parts.append("-0.25 cta-rows")
    if any(k.get("tag") in ("h1", "h2", "h3", "h4", "h5", "h6")
           for k in children_of(node)):
        score -= 0.15
        parts.append("-0.15 heading-sibling")

    tokens = (" ".join(classes_of(node)) + " "
              + str((node.get("attrs") or {}).get("id") or "") + " "
              + " ".join(t for k in kids for t in classes_of(k))).lower()
    if any(h in tokens for h in NON_CHAT_HINTS):
        score -= 0.35
        parts.append("-0.35 non-chat-words")

    if sig["avgLen"] > 300:
        score -= 0.30
        parts.append("-0.30 long-rows")
    if 0 < sig["childrenPerRow"] <= 4:
        score += 0.05
        parts.append("+0.05 few-children")
    if not any(ch.isalnum() for ch in "".join(sig["texts"])):
        score = 0.0
        parts.append("zeroed: no text")
    return max(0.0, min(1.0, score)), parts


def _pick_text_selector(rows: Sequence[Dict[str, Any]],
                        username_nodes: Sequence[Optional[Dict[str, Any]]]
                        ) -> Tuple[List[str], List[str]]:
    """Child selector that carries the message text (empty -> use row text)."""
    notes: List[str] = []
    picks: List[Dict[str, Any]] = []
    for row, uname in zip(rows, username_nodes):
        best = None
        best_len = 0
        for child in descendants(row):
            if uname is not None and (child is uname or _contains(uname, child)):
                continue
            text = node_text(child)
            if len(text) > best_len and not any(d.get("tag") == "a" for d in descendants(child)):
                best, best_len = child, len(text)
        if best is not None and best_len >= 1:
            picks.append(best)
    if len(picks) < max(1, int(len(rows) * 0.6)):
        notes.append("message text is in the row itself (no dedicated text node)")
        return [], notes

    attrs = common_attrs(picks)
    for name, kind in attrs:
        return [css_attr_selector(name, kind)], notes
    tokens = common_classes(picks)
    token = class_selector_hint(tokens)
    if token:
        tag = (picks[0].get("tag") or "").lower()
        return [f"{tag}.{token}" if tag else f".{token}"], notes
    tag = (picks[0].get("tag") or "").lower()
    if tag and all((p.get("tag") or "").lower() == tag for p in picks):
        return [tag], notes
    return [], notes


def _pick_username_selector(rows: Sequence[Dict[str, Any]]) -> Tuple[List[str], List[Optional[Dict[str, Any]]], List[str]]:
    """Short leading child that repeats per row (the speaker name)."""
    notes: List[str] = []
    per_row: List[Optional[Dict[str, Any]]] = []
    for row in rows:
        row_text = node_text(row)
        cand = None
        for child in descendants(row):
            text = node_text(child)
            if not text or len(text) > 24:
                continue
            if text and row_text.startswith(text) and len(text) < len(row_text):
                attrs = child.get("attrs") or {}
                tokens = " ".join(classes_of(child)).lower()
                style = str(attrs.get("style") or "").lower()
                score = 0
                if any(a in attrs for a in ("username", "data-username", "data-name")):
                    score += 3
                if any(h in tokens for h in BOLD_CLASS_HINTS):
                    score += 2
                if attrs.get("role") == "button":
                    score += 1
                if "font-weight" in style and not re.search(r"font-weight:\s*(normal|[1-5]00)", style):
                    score += 1
                if cand is None or score > cand[0]:
                    cand = (score, child)
        per_row.append(cand[1] if cand else None)
    found = [c for c in per_row if c is not None]
    if len(found) < max(1, int(len(rows) * 0.6)):
        notes.append("no per-row username node found (speaker from alignment/sent text)")
        return [], per_row, notes

    attrs = common_attrs(found)
    for name, kind in attrs:
        return [css_attr_selector(name, kind)], per_row, notes
    tokens = common_classes(found)
    token = class_selector_hint(tokens) or next(
        (t for t in tokens if any(h in t.lower() for h in BOLD_CLASS_HINTS)), None)
    if token:
        tag = (found[0].get("tag") or "").lower()
        return [f"{tag}.{token}" if tag else f".{token}"], per_row, notes
    tag = (found[0].get("tag") or "").lower()
    if tag and all((f.get("tag") or "").lower() == tag for f in found):
        return [tag], per_row, notes
    return [], per_row, notes


def _add_hint(bucket: List[str], prop: str, value: str) -> None:
    """Hints are matched against a row's class list + style signature."""
    for token in (f"{prop}:{value}", value if value.startswith("flex-") else ""):
        if token and token not in bucket:
            bucket.append(token)


def _speaker_hints(rows: Sequence[Dict[str, Any]]) -> Tuple[List[str], List[str], List[str]]:
    """Derive own/other class hints from the row markup itself."""
    notes: List[str] = []
    own: List[str] = []
    other: List[str] = []
    tokens_by_row = [set(classes_of(r)) for r in rows]
    union = sorted(set().union(*tokens_by_row)) if tokens_by_row else []
    for token in union:
        low = token.lower()
        if any(h == low or h in low for h in OWN_CLASS_HINTS):
            own.append(token)
        elif any(h == low or h in low for h in OTHER_CLASS_HINTS):
            other.append(token)

    # no alignment classes: read the inline alignment styles instead.
    # the hint tokens below are matched by the extractor against the *style
    # signature* of a row (inline style + computed values), e.g. "flex-end"
    # or "margin-left:auto".
    if not own and not other:
        right_pairs = (
            ("margin-left", "auto"), ("align-self", "flex-end"),
            ("align-items", "flex-end"), ("justify-content", "flex-end"),
            ("justify-items", "flex-end"), ("text-align", "right"),
        )
        left_pairs = (
            ("margin-right", "auto"), ("align-self", "flex-start"),
            ("align-items", "flex-start"), ("justify-content", "flex-start"),
            ("justify-items", "flex-start"), ("text-align", "left"),
        )
        own_values, other_values = [], []
        for row in rows:
            style = norm_space((row.get("attrs") or {}).get("style") or "").lower()
            style = re.sub(r"\s*:\s*", ":", style)
            for prop, value in right_pairs:
                if f"{prop}:{value}" in style:
                    _add_hint(own_values, prop, value)
            for prop, value in left_pairs:
                if f"{prop}:{value}" in style:
                    _add_hint(other_values, prop, value)
        if own_values and other_values:
            own = own_values[:2]
            other = other_values[:2]
            notes.append("speaker hints derived from inline alignment styles")
    if own or other:
        notes.append("speaker hints: own=%s other=%s" % (own or "-", other or "-"))
    return own, other, notes


def _inside_rows(name: str, rows: Sequence[Dict[str, Any]]) -> bool:
    for row in rows:
        if row.get("n") == name:
            return True
        for node in descendants(row):
            if node.get("n") == name:
                return True
    return False


def _my_username_selectors(tree: Dict[str, Any], rows: Sequence[Dict[str, Any]]) -> List[str]:
    """Guesses for the sidebar/header element that shows *our* username."""
    out: List[str] = []

    # 1. explicit username-ish attributes outside the message list
    for node in iter_nodes(tree):
        if _inside_rows(node.get("n"), rows):
            continue
        attrs = node.get("attrs") or {}
        for name in MY_NAME_ATTRS:
            if name not in attrs:
                continue
            value = str(attrs.get(name) or "").strip()
            if not value or len(value) > 40 or (" " in value and len(value) > 24):
                continue
            sel = f"[{name}='{value}']" if name in ("username", "data-username", "data-name") else f"[{name}]"
            if sel not in out:
                out.append(sel)
    if out:
        return out[:3]

    # 2. short text inside a button/aside/header/nav (the account widget)
    scored: List[Tuple[int, str]] = []
    for node in iter_nodes(tree):
        tag = (node.get("tag") or "").lower()
        if tag in ("html", "body", "main", "ol", "ul"):
            continue
        if _inside_rows(node.get("n"), rows):
            continue
        text = node_text(node)
        if not text or len(text) > 40 or len(children_of(node)) > 3:
            continue
        parents = {a.get("tag") for a in ancestors(node)}
        host = {"button", "aside", "header", "nav", "footer"} & (parents | {tag})
        if not host:
            continue
        score = 0
        if "button" in (parents | {tag}):
            score += 2
        if {"aside", "header", "nav"} & parents:
            score += 2
        tokens = [t.lower() for t in classes_of(node)]
        if any(h in t for t in tokens for h in BOLD_CLASS_HINTS):
            score += 1
        if any("truncate" in t or "username" in t for t in tokens):
            score += 1
        if not score:
            continue
        best_token = next((t for t in classes_of(node)
                           if any(h in t.lower() for h in BOLD_CLASS_HINTS)
                           or "truncate" in t.lower()), None)
        if best_token:
            sel = f"{tag}.{best_token}"
            if "button" in parents:
                scored.append((score + 1, f"button {sel}"))
        elif tag in ("span", "b", "strong", "p", "div"):
            sel = tag
        else:
            continue
        scored.append((score, sel))
    scored.sort(key=lambda kv: -kv[0])
    for _, sel in scored:
        if sel not in out:
            out.append(sel)
    return out[:3]


_VARIANT_CLASS_HINTS = OWN_CLASS_HINTS + OTHER_CLASS_HINTS + (
    "active", "selected", "first", "last", "odd", "even", "new", "unread",
    "seen", "read", "unseen", "hidden", "open", "current",
)
_NAME_COLON_RE = re.compile(r"^[^:\n]{1,24}:\s*\S")


def is_variant_class(token: str) -> bool:
    """Class that changes per row state (``justify-end``, ``unread``, ...)."""
    low = token.lower()
    return any(h == low or h in low for h in _VARIANT_CLASS_HINTS)


def _row_kind_similar(a: Sequence[str], b: Sequence[str]) -> bool:
    """Are these two rows the same kind of row (same base classes)?"""
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return True
    common = set_a & set_b
    if not common:
        return False
    smaller = min(len(set_a), len(set_b))
    if len(common) / smaller >= 0.5:
        return True
    return all(is_variant_class(t) for t in (set_a ^ set_b))


def _cluster_children(kids: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group same-tag siblings by class similarity (not exact equality).

    Chat rows often differ in one alignment class (``justify-start`` vs
    ``justify-end``) — an exact signature would split them into groups too
    small to be recognised as a message list.
    """
    clusters: List[Dict[str, Any]] = []
    order: List[List[Dict[str, Any]]] = []
    for kid in kids:
        tag = (kid.get("tag") or "").lower()
        classes = [c for c in classes_of(kid) if is_stable_class(c)]
        placed = False
        for rep in clusters:
            if rep["tag"] == tag and _row_kind_similar(rep["classes"], classes):
                order[rep["index"]].append(kid)
                placed = True
                break
        if not placed:
            clusters.append({"tag": tag, "classes": classes, "index": len(order)})
            order.append([kid])
    return order


def _group_looks_like_chat(node: Dict[str, Any], group: Sequence[Dict[str, Any]]) -> bool:
    """A 2-row group is only trusted when the markup *says* it is a chat."""
    role = str((node.get("attrs") or {}).get("role") or "").lower()
    tokens = " ".join(classes_of(node)).lower()
    if role in ("log", "list", "feed", "messages"):
        return True
    if any(h in tokens for h in CHAT_CLASS_HINTS):
        return True
    named = sum(1 for row in group if _NAME_COLON_RE.match(node_text(row)))
    return named >= max(2, int(len(group) * 0.8))


def discover_tree(root: Dict[str, Any], *, source: str = "html",
                  top: int = 5) -> Report:
    """Find the most likely chat markup in an already-built tree."""
    index_tree(root)
    notes: List[str] = []
    candidates: List[Dict[str, Any]] = []

    for node in iter_nodes(root):
        kids = children_of(node)
        if len(kids) < 2 or len(kids) > 400:
            continue
        for group in _cluster_children(kids):
            if len(group) < 2:
                continue
            if len(group) == 2 and not _group_looks_like_chat(node, group):
                continue
            stats = _row_signature(group)
            if not stats["variety"]:
                continue
            score, parts = _score_container(node, stats, group)
            if score < 0.40:
                continue
            candidates.append({
                "score": round(score, 3), "parts": parts, "node": node,
                "rows": group, "stats": stats,
                "key": f"{(group[0].get('tag') or '').lower()}|{','.join(sorted(classes_of(group[0])))}",
                "depth": node_depth(node),
            })

    if not candidates:
        return Report(ok=False, source=source, confidence=0.0, candidates=[],
                      notes=["no repeated-sibling block that looks like a message list"],
                      selectors={})

    candidates.sort(key=lambda c: (-c["score"], c["depth"]))
    # drop candidates that live inside a better candidate (same rows twice)
    top_candidates = candidates[:max(top * 3, top)]
    best: Optional[Dict[str, Any]] = None
    for cand in top_candidates:
        if any(_contains(other["node"], cand["node"]) and other is not cand
               for other in candidates if other["score"] > cand["score"] + 0.05):
            continue
        best = cand
        break
    best = best or candidates[0]

    container = best["node"]
    rows = best["rows"]

    # ---- container selector -------------------------------------------
    containers: List[str] = []
    for name, kind in common_attrs([container]):
        containers.append(css_attr_selector(name, kind))
    c_tokens = common_classes([container])
    c_token = class_selector_hint(c_tokens)
    c_tag = (container.get("tag") or "").lower()
    if c_tag == "body":
        containers = []
    elif c_token:
        containers.insert(0, f"{c_tag}.{c_token}" if c_tag else f".{c_token}")
        hint = next((h for h in CHAT_CLASS_HINTS
                     if any(h in t.lower() for t in c_tokens)), None)
        if hint:
            containers.append(f"[class*='{hint}']")
    if c_tag in ("ol", "ul", "main", "section"):
        containers.append(c_tag)
    role = str((container.get("attrs") or {}).get("role") or "").lower()
    if role:
        containers.insert(0, f"[role='{role}']")

    # ---- row selector --------------------------------------------------
    items: List[str] = []
    for name, kind in common_attrs(rows):
        items.append(css_attr_selector(name, kind))
    r_tokens = common_classes(rows)
    r_token = class_selector_hint(r_tokens)
    r_tag = (rows[0].get("tag") or "").lower()
    if r_token:
        items.insert(0, f"{r_tag}.{r_token}" if r_tag else f".{r_token}")
    if r_tag:
        items.append(r_tag)
    if container is not (rows[0].get("parent") or {}):
        notes.append("rows are not direct children of the detected list")
    else:
        items.append(f":scope > {r_tag}" if r_tag else ":scope > *")

    # ---- text / username / speaker ------------------------------------
    own, other, hint_notes = _speaker_hints(rows)
    notes.extend(hint_notes)
    uname_sel, uname_nodes, uname_notes = _pick_username_selector(rows)
    notes.extend(uname_notes)
    text_sel, text_notes = _pick_text_selector(rows, uname_nodes)
    notes.extend(text_notes)
    my_names = _my_username_selectors(root, rows)

    selectors = {
        "containers": _dedupe(containers),
        "items": _dedupe(items),
        "usernames": _dedupe(uname_sel),
        "texts": _dedupe(text_sel),
        "my_usernames": _dedupe(my_names),
        "own_hints": _dedupe(own),
        "other_hints": _dedupe(other),
    }

    report = Report(
        ok=True, source=source, confidence=round(best["score"], 3),
        selectors=selectors, notes=notes,
        rowCount=len(rows),
        containerSelector=selectors["containers"][:1],
        scoreParts=best["parts"],
        candidates=[{
            "score": c["score"], "rows": c["stats"]["count"],
            "tag": c["node"].get("tag"),
            "classes": classes_of(c["node"])[:4],
            "depth": c["depth"], "parts": c["parts"],
        } for c in candidates[:top]],
        rowSamples=[node_text(r)[:90] for r in rows[:5]],
    )

    # ---- verify: can these selectors actually read the messages? -------
    try:
        guessed_me = None
        for sel in selectors["my_usernames"]:
            found = query_all(root, sel)
            if found:
                text = node_text(found[0])
                if text and len(text) <= 40 and not _inside_rows(found[0].get("n"), rows):
                    guessed_me = text
                    break
        messages = extract_messages(root, selectors, my_username=guessed_me)
        report["verifiedMessages"] = len(messages)
        report["verifiedSample"] = [f"{m['speaker']}: {m['message'][:60]}" for m in messages[:5]]
        if len(messages) >= min(2, len(rows)):
            report["confidence"] = round(min(0.99, best["score"] + 0.12), 3)
            notes.append(f"verified: {len(messages)} message(s) read back with these selectors")
        else:
            report["confidence"] = round(max(0.0, best["score"] * 0.5), 3)
            notes.append("could not read the messages back with these selectors — "
                         "step through --explain and pick a candidate by hand")
    except Exception as exc:  # never fail because verification failed
        report["verifiedMessages"] = 0
        notes.append(f"verification error: {type(exc).__name__}: {exc}")

    return report


def _dedupe(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


# --------------------------------------------------------------------------
# Verification extractor (mirrors browser/chat_reader.py's JS rules)
# --------------------------------------------------------------------------

def extract_messages(root: Dict[str, Any], selectors: Dict[str, List[str]],
                     recent_sent: Sequence[str] = (),
                     my_username: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read messages from a tree with the given selector sets.

    This mirrors the important rules of ``chat_reader.CHAT_EXTRACT_JS``:
    first container that matches, first item selector that matches, outermost
    rows only, text from the text-selector chain else the row text, speaker
    from username/attr/alignment/sent-text.
    """
    index_tree(root)
    containers = selectors.get("containers") or []
    container = None
    for sel in containers:
        found = query_all(root, sel)
        if found:
            container, container_sel = found[0], sel
            break
    if container is None:
        found = query_all(root, "main") or query_all(root, "body")
        container, container_sel = (found[0] if found else root), "fallback"

    item_sel_used = None
    rows: List[Dict[str, Any]] = []
    for sel in selectors.get("items") or []:
        found = outermost(query_all(container, sel, scope=container))
        if found:
            rows, item_sel_used = found, sel
            break

    usernames = selectors.get("usernames") or []
    texts = selectors.get("texts") or []
    own_hints = [h.lower() for h in (selectors.get("own_hints") or [])]
    other_hints = [h.lower() for h in (selectors.get("other_hints") or [])]
    sent = {norm_space(t).lower() for t in recent_sent}

    my = norm_space(my_username or "")
    messages: List[Dict[str, Any]] = []
    for row in rows:
        text_el = None
        for sel in texts:
            found = query_all(row, sel, scope=row)
            if found:
                text_el = found[0]
                break
        username = ""
        for sel in usernames:
            found = query_all(row, sel, scope=row)
            if found:
                username = norm_space(node_text(found[0]))
                if username and len(username) <= 40:
                    break
                username = ""
        text = norm_space(node_text(text_el)) if text_el is not None else ""
        if not text:
            text = node_text(row)
        if username and text.lower().startswith(username.lower()):
            text = norm_space(text[len(username):].lstrip(" :-"))
        if not text:
            continue

        parent = row.get("parent")
        cls = " ".join(classes_of(row)).lower()
        if parent is not None:
            cls += " " + " ".join(classes_of(parent)).lower()
        style = norm_space((row.get("attrs") or {}).get("style") or "").lower()
        if parent is not None:
            style += " " + norm_space((parent.get("attrs") or {}).get("style") or "").lower()
        cls += " " + re.sub(r"\s*:\s*", ":", style)
        attrs = row.get("attrs") or {}
        explicit = str(attrs.get("data-speaker") or attrs.get("data-own") or "")
        if re.match(r"^(you|me|self|own|true)$", explicit, re.I):
            speaker, via = "You", "align"
        elif username and my:
            speaker = "You" if username.lower() == my.lower() else "Stranger"
            via = "username"
        elif own_hints and any(h in cls for h in own_hints):
            speaker, via = "You", "align"
        elif other_hints and any(h in cls for h in other_hints):
            speaker, via = "Stranger", "align"
        elif text.lower() in sent:
            speaker, via = "You", "sent"
        else:
            speaker, via = "Stranger", "default"
        messages.append({"speaker": speaker, "message": text, "username": username or None,
                         "via": via, "mid": attrs.get("data-message-id") or attrs.get("data-id")})
    return messages


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------

def discover_html(html: str, *, my_username: Optional[str] = None) -> Report:
    """Discover selectors from saved page HTML (no browser needed)."""
    tree = parse_html(html)
    report = discover_tree(tree, source="html.parser")
    if my_username and report.ok:
        report.setdefault("notes", []).append(f"my_username given: {my_username}")
    return report


def discover_tree_file(path: Path) -> Report:
    return discover_html(Path(path).read_text(encoding="utf-8", errors="replace"))


def discover_page(page) -> Report:
    """Discover selectors from a live Playwright page."""
    tree = serialize_page(page)
    return discover_tree(tree, source="playwright")


def merge_config(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a new discovery into an existing config (new first, deduped)."""
    out = dict(new)
    for key, values in (old or {}).items():
        if key.startswith("_") or key in ("learned_at", "source", "confidence"):
            if key not in out or key.startswith("_"):
                out[key] = values          # a note the user wrote is kept
            continue
        if isinstance(values, list):
            merged = list(out.get(key) or [])
            for value in values:
                if value not in merged:
                    merged.append(value)
            out[key] = merged
    return out


def save_config(report: Report, path: Path, *, note: str = "") -> Path:
    """Write/merge the discovered selectors into ``path`` (keeps a backup)."""
    path = Path(path)
    old: Dict[str, Any] = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            old = {}
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            backup.write_text(json.dumps(old, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
        except OSError:
            pass
    cfg = merge_config(old, report.to_config(note))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def format_report(report: Report, *, explain: bool = False) -> str:
    """Human-readable report for the tool output."""
    lines: List[str] = []
    if not report.ok:
        lines.append("selector doctor: NO message list found on this page")
        for note in report.get("notes") or []:
            lines.append(f"  - {note}")
        lines.append("  (is this the chat page? open the chat, wait for messages, then save it)")
        return "\n".join(lines)

    s = report.selectors
    lines.append(f"selector doctor: chat list found (confidence {report.confidence:.2f})")
    lines.append(f"  rows detected      : {report.get('rowCount', 0)}")
    lines.append(f"  container selector : {', '.join(s.get('containers') or []) or '-'}")
    lines.append(f"  row selector       : {', '.join(s.get('items') or []) or '-'}")
    lines.append(f"  text selector      : {', '.join(s.get('texts') or []) or '(row text)'}")
    lines.append(f"  username selector  : {', '.join(s.get('usernames') or []) or '-'}")
    lines.append(f"  my-username guess  : {', '.join(s.get('my_usernames') or []) or '-'}")
    lines.append(f"  own hints          : {', '.join(s.get('own_hints') or []) or '-'}")
    lines.append(f"  other hints        : {', '.join(s.get('other_hints') or []) or '-'}")
    lines.append(f"  verified messages  : {report.get('verifiedMessages', 0)}")
    for sample in report.get("verifiedSample") or []:
        lines.append(f"      {sample}")
    for note in report.get("notes") or []:
        lines.append(f"  note: {note}")
    if explain:
        lines.append("  candidates:")
        for cand in report.get("candidates") or []:
            lines.append(f"    score={cand['score']:.2f} rows={cand['rows']} "
                         f"depth={cand['depth']} <{cand['tag']}> {cand['classes']}")
            lines.append(f"        {'; '.join(cand.get('parts') or [])}")
    lines.append("  save it with:  --save   (config/chat_selectors.json)")
    return "\n".join(lines)


__all__ = [
    "Report", "discover_html", "discover_page", "discover_tree",
    "discover_tree_file", "extract_messages", "format_report", "index_tree",
    "merge_config", "parse_html", "query_all", "save_config",
    "selector_matches", "serialize_page", "SERIALIZE_JS",
]
