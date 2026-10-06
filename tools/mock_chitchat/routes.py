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
            log: Callable[[str], None] = print) -> Any:
    """Serve every document request for the mock host from :mod:`site`.

    Returns the handler so callers can count calls or remove the route.
    ``?as=Name`` in the URL overrides the page identity (used by the
    "stranger" browser so it renders itself as a different user).
    """
    stats = {"calls": 0, "cookies": 0}

    def _api(route, request, parsed):
        """Answer the page's own /api/* calls (same origin, no CORS games)."""
        query = parse_qs(parsed.query)
        if parsed.path == "/api/messages":
            since = int((query.get("since") or ["0"])[0] or 0)
            messages, next_index = backend.since(since)
            return route.fulfill(status=200, json={"messages": messages, "next": next_index})
        if parsed.path == "/api/all":
            return route.fulfill(status=200, json={"messages": backend.all()})
        if parsed.path == "/api/hello" and request.method == "POST":
            payload = request.post_data_json or {}
            backend.hello(payload.get("author"))
            return route.fulfill(status=200, json={"ok": True})
        if parsed.path == "/api/leave" and request.method == "POST":
            payload = request.post_data_json or {}
            backend.leave(payload.get("author"))
            return route.fulfill(status=200, json={"ok": True})
        if parsed.path == "/api/send" and request.method == "POST":
            payload = request.post_data_json or {}
            message = backend.add(payload.get("author"), payload.get("text"))
            return route.fulfill(status=200, json={"ok": True, "message": message})
        return route.fulfill(status=404, json={"ok": False, "error": "not found"})

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
        html = site.page_html(parsed.path, me=who, api_base=api_base)
        # A tiny first-party cookie, like a real site sets on its own domain, so
        # the pipeline's "save the session again" step has something to keep.
        cookie = f"mock_cc_session={random.randint(100000, 999999)}; Path=/; Max-Age=3600"
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html,
                      headers={"Set-Cookie": cookie})

    glob = url_glob or f"**{HOST}/**"
    context.route(glob, _handler)
    return _handler
