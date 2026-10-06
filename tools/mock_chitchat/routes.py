"""Playwright route hook that answers ``app.chitchat.gg`` with the mock site.

The bot code keeps using the real hostname (``https://app.chitchat.gg/...``),
so its cookies, URL checks and flow are untouched — only the document body is
served locally.  API traffic never goes through this hook: the mock pages talk
to the local HTTP backend (see :mod:`tools.mock_chitchat.site`) with CORS, so
page polling keeps working even while the Python thread is inside
``time.sleep``/``page.evaluate``.
"""

from __future__ import annotations

import json
import random
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from tools.mock_chitchat import site

HOST = "app.chitchat.gg"


def install(context: Any,
            backend: site.ChatBackend,
            *,
            me: str = "EvaUser",
            api_base: str = "",
            url_glob: Optional[str] = None,
            on_page: Optional[Callable[[str, str], None]] = None,
            debug: bool = False,
            ws_url: str = "",
            transport: str = "dom",
            connect_socket: bool = True,
            blind_others: bool = False,
            log: Callable[[str], None] = print) -> Any:
    """Serve every document request for the mock host from :mod:`site`.

    Returns the handler so callers can count calls or remove the route.
    ``?as=Name`` in the URL overrides the page identity (used by the
    "stranger" browser so it renders itself as a different user).

    ``blind_others`` hides every other participant's message from *this*
    context's ``/api/messages`` (the page never renders them). The blind-page
    WebSocket detection test uses it so a reply can only have been triggered by
    a backend socket event; the reply itself still uses the app's HTTP send path.
    """
    stats = {"calls": 0, "cookies": 0}

    def _trace(caller, text):
        """Debug trace of every API call the pages make (thread-safe)."""
        line = f"{caller} {text}"
        try:
            backend.note_api(line)
        except Exception:
            pass
        if debug:
            log(f"[MockAPI] {line}")

    def _caller(request, parsed):
        override = (parse_qs(parsed.query).get("as") or [None])[0]
        if override:
            return str(override)
        payload = {}
        if request.method == "POST":
            try:
                payload = request.post_data_json or {}
            except Exception:
                payload = {}
        return str(payload.get("author") or me)

    def _api(route, request, parsed):
        """Answer the page's own /api/* calls (same origin, no CORS games)."""
        query = parse_qs(parsed.query)
        who = _caller(request, parsed)
        if parsed.path == "/api/conversations/send" and backend.api_send_template() is None:
            # First send the *stand-in* sees (i.e. the one the page itself made):
            # that is the request shape the backend has to learn.
            try:
                body = request.post_data_json or {}
            except Exception:
                body = {}
            from core.chat_ws import build_send_template
            template = build_send_template("POST", request.url, body,
                                           _request_headers(request),
                                           text=str(body.get("content") or ""))
            backend.set_api_send_template(template)
            _trace(who, "the mock app's own send request is now known to the backend")
        if parsed.path == "/api/messages":
            since = int((query.get("since") or ["0"])[0] or 0)
            messages, next_index = backend.since(since)
            if blind_others:
                # Deliberate handicap for the WS-only test: somebody else's words
                # are never handed to this page, so the bot cannot read them.
                messages = [m for m in messages if str(m.get("author")) == who]
            _trace(who, f"GET /api/messages since={since} → {len(messages)} new")
            payload = {"messages": messages, "next": next_index}
            if backend.take_dump_request(who):
                payload["dump"] = True
                _trace(who, "→ asked the page for its DOM (dump)")
            return route.fulfill(status=200, json=payload)
        if parsed.path == "/api/state":
            payload = request.post_data_json or {}
            backend.note_state(who, payload)
            _trace("dom", f"{who} bubbles={payload.get('bubbles')} "
                          f"last={str(payload.get('last'))[:60]!r} "
                          f"input={str(payload.get('input'))[:40]!r} "
                          f"connected={str(payload.get('connected'))[:40]!r} "
                          f"ended={payload.get('ended')}")
            return route.fulfill(status=200, json={"ok": True})
        if parsed.path == "/api/dump":
            payload = request.post_data_json or {}
            backend.note_dump(who, payload.get("html") or "")
            _trace(who, f"POST /api/dump → {len(str(payload.get('html') or ''))} chars of DOM")
            return route.fulfill(status=200, json={"ok": True})
        if parsed.path == "/api/conversation":
            return route.fulfill(status=200, json={"conversationId": backend.conversation()})
        if parsed.path == "/api/all":
            return route.fulfill(status=200, json={"messages": backend.all()})
        if parsed.path == "/api/hello" and request.method == "POST":
            payload = request.post_data_json or {}
            backend.hello(payload.get("author"))
            _trace(payload.get("author"), "POST /api/hello (page opened)")
            return route.fulfill(status=200, json={"ok": True})
        if parsed.path == "/api/leave" and request.method == "POST":
            payload = request.post_data_json or {}
            backend.leave(payload.get("author"))
            return route.fulfill(status=200, json={"ok": True})
        if parsed.path == "/api/conversations/send" and request.method == "POST":
            # The real site does not send chat text over the socket: it writes the
            # message out of band (HTTP) and only *listens* on the socket for the
            # echo.  This endpoint is that out-of-band write, so the bot can be
            # tested against the real mechanism (it learns this request from the
            # page and then replays it from the backend).
            payload = request.post_data_json or {}
            author = backend.sender_for_token(payload.get("token"),
                                              fallback=str(payload.get("author") or who))
            message = backend.add(author, payload.get("content") or payload.get("text"),
                                  nonce=payload.get("nonce"))
            _trace(author, f"POST /api/conversations/send nonce={str(payload.get('nonce'))[:12]!r} "
                           f"text={str(payload.get('content'))[:60]!r}")
            return route.fulfill(status=200, json={"ok": True, "message": message})
        if parsed.path == "/api/send" and request.method == "POST":
            payload = request.post_data_json or {}
            message = backend.add(payload.get("author"), payload.get("text"))
            _trace(payload.get("author"), f"POST /api/send text={str(payload.get('text'))[:80]!r}")
            return route.fulfill(status=200, json={"ok": True, "message": message})
        return route.fulfill(status=404, json={"ok": False, "error": "not found"})

    def _request_headers(request):
        try:
            return dict(request.headers or {})
        except Exception:
            return {}

    def _handler(route, request):
        parsed = urlparse(request.url)
        if parsed.path.startswith("/api/"):
            return _api(route, request, parsed)
        override = (parse_qs(parsed.query).get("as") or [None])[0]
        who = str(override or me)
        cookie_header = request.headers.get("cookie", "") or ""
        stats["calls"] += 1
        if cookie_header:
            stats["cookies"] = len([c for c in cookie_header.split(";") if c.strip()])
            backend.note_cookies(parsed.path, cookie_header)
            if stats["calls"] <= 2:
                names = ", ".join(sorted({c.split("=", 1)[0].strip()
                                          for c in cookie_header.split(";") if c.strip()}))
                log(f"[MockSite] {parsed.path} ← {stats['cookies']} session cookies "
                    f"received ({names[:160]})")
        else:
            log(f"[MockSite] {parsed.path} ← no cookies (anonymous visitor)")
        if on_page is not None:
            try:
                on_page(parsed.path, who)
            except Exception:
                pass
        html = site.page_html(parsed.path, me=who, api_base=api_base, debug=debug,
                              ws_url=ws_url, transport=transport,
                              connect_socket=connect_socket)
        # A tiny first-party cookie, like a real site sets on its own domain, so
        # the pipeline's "save the session again" step has something to keep.
        cookie = f"mock_cc_session={random.randint(100000, 999999)}; Path=/; Max-Age=3600"
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html,
                      headers={"Set-Cookie": cookie})

    glob = url_glob or f"**{HOST}/**"
    context.route(glob, _handler)

    return _handler
