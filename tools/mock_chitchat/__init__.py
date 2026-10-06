"""Local stand-in for chitchat.gg used by the offline end-to-end test.

chitchat.gg is not reachable from the test sandbox, so the E2E run serves a
tiny look-alike app instead:

* :mod:`tools.mock_chitchat.site` — the pages + a real HTTP chat backend.
* :mod:`tools.mock_chitchat.routes` — Playwright route hook that answers
  ``https://app.chitchat.gg/...`` document requests with those pages, so the
  bot code itself is never modified and still believes it talks to the site.

The point of the harness is to exercise the *real* pipeline
(launch → fingerprint → session restore → chat connect → incoming message →
reply) instead of a stubbed one.
"""

__all__ = ["site", "routes"]
