"""Advanced human-behavior utilities for the Context-Pool edition.

Provides helpers that make browser interactions look statistically human:

* **Bezier-curve mouse movement** — instead of moving the cursor in a straight
  line (which is an instant bot tell), the mouse follows a smooth quadratic
  Bezier curve with random control points and per-step jitter.
* **Human typing** — ``type_human()`` presses each key with a 50–200 ms
  Gaussian-distributed delay (not ``fill()`` which pastes instantly).
* **Randomized viewport** — already handled by ``context_pool.py`` but the
  helper is here too for convenience.
* **Random dwell / scroll** — realistic browsing pauses.

All helpers accept a Playwright/Camoufox ``page`` or ``locator`` object and
degrade gracefully if an action fails (no crash on closed pages).
"""

from __future__ import annotations

import math
import random
import time
from typing import Optional


# --------------------------------------------------------------------------- #
# Bezier-curve mouse movement
# --------------------------------------------------------------------------- #

def _bezier_points(
    start: tuple,
    end: tuple,
    steps: int = 25,
    curvature: float = 0.5,
) -> list:
    """Generate intermediate points along a quadratic Bezier curve.

    The control point is offset perpendicular to the start→end line by a
    random amount, giving a natural arc rather than a straight line.
    """
    sx, sy = start
    ex, ey = end
    mx, my = (sx + ex) / 2, (sy + ey) / 2
    dx, dy = ex - sx, ey - sy
    dist = math.hypot(dx, dy) or 1.0
    # Perpendicular offset for the control point.
    perp_x, perp_y = -dy / dist, dx / dist
    offset = random.uniform(-curvature, curvature) * dist * 0.4
    cx, cy = mx + perp_x * offset, my + perp_y * offset

    points = []
    for i in range(steps + 1):
        t = i / steps
        # Quadratic Bezier: B(t) = (1-t)²P₀ + 2(1-t)tP₁ + t²P₂
        x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * ex
        y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * ey
        # Small jitter per step.
        x += random.gauss(0, 0.8)
        y += random.gauss(0, 0.8)
        points.append((int(x), int(y)))
    return points


def human_mouse_move(page, x: int, y: int, steps: int = 25) -> None:
    """Move the mouse to (x, y) along a Bezier curve with per-step pauses."""
    try:
        # Get current mouse position (best-effort; default to a corner).
        current = (0, 0)
        try:
            # Playwright doesn't expose current mouse pos; use a random start
            # near the target to make the curve visible.
            current = (
                x + random.randint(-200, 200),
                y + random.randint(-150, 150),
            )
            current = (max(0, current[0]), max(0, current[1]))
        except Exception:
            pass

        points = _bezier_points(current, (x, y), steps=steps)
        for px, py in points:
            try:
                page.mouse.move(px, py)
            except Exception:
                return
            time.sleep(random.uniform(0.005, 0.020))
    except Exception:
        pass


def human_click(page, x: int, y: int, delay_before: float = 0.0) -> None:
    """Move to (x, y) along a Bezier curve, pause, then click."""
    if delay_before > 0:
        time.sleep(delay_before)
    human_mouse_move(page, x, y)
    time.sleep(random.uniform(0.05, 0.15))
    try:
        page.mouse.click(x, y)
    except Exception:
        pass


def human_click_locator(locator, delay_before: float = 0.0) -> None:
    """Click a Playwright locator with Bezier mouse movement.

    Falls back to ``locator.click()`` if bounding-box retrieval fails.
    """
    if delay_before > 0:
        time.sleep(delay_before)
    try:
        box = locator.bounding_box()
        if box:
            cx = box["x"] + box["width"] / 2 + random.uniform(-3, 3)
            cy = box["y"] + box["height"] / 2 + random.uniform(-3, 3)
            page = locator.page
            human_mouse_move(page, int(cx), int(cy))
            time.sleep(random.uniform(0.05, 0.15))
            page.mouse.click(int(cx), int(cy))
            return
    except Exception:
        pass
    # Fallback: regular click.
    try:
        locator.click()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Human typing
# --------------------------------------------------------------------------- #

def type_human(
    locator_or_page,
    text: str,
    min_delay_ms: int = 50,
    max_delay_ms: int = 200,
) -> None:
    """Type *text* one character at a time with Gaussian-distributed delays.

    Uses ``page.keyboard.type()`` for each character.  This is far more
    human-like than ``fill()`` which pastes the entire string instantly.
    """
    try:
        # If it's a locator, focus it first.
        if hasattr(locator_or_page, "click") and hasattr(locator_or_page, "fill"):
            try:
                locator_or_page.click()
            except Exception:
                pass
            page = locator_or_page.page
        else:
            page = locator_or_page

        for char in text:
            try:
                page.keyboard.type(char)
            except Exception:
                break
            delay = max(0.001, random.gauss(
                (min_delay_ms + max_delay_ms) / 2000.0,
                (max_delay_ms - min_delay_ms) / 6000.0,
            ))
            delay = max(min_delay_ms / 1000.0, min(max_delay_ms / 1000.0, delay))
            time.sleep(delay)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Randomized viewport helper
# --------------------------------------------------------------------------- #

def random_viewport(base_sizes: Optional[list] = None) -> dict:
    """Return a randomized viewport dict (±10-15px from a base size)."""
    if base_sizes is None:
        base_sizes = [
            (1280, 720), (1366, 768), (1440, 900),
            (1536, 864), (1600, 900), (1920, 1080),
        ]
    bw, bh = random.choice(base_sizes)
    w = bw + random.randint(-15, 15)
    h = bh + random.randint(-10, 10)
    return {"width": max(320, w), "height": max(240, h)}


# --------------------------------------------------------------------------- #
# Random dwell / scroll
# --------------------------------------------------------------------------- #

def random_scroll(page, min_pixels: int = 100, max_pixels: int = 500) -> None:
    """Scroll the page by a random amount with smooth intermediate steps."""
    total = random.randint(min_pixels, max_pixels)
    steps = random.randint(3, 8)
    per_step = total // steps
    for _ in range(steps):
        try:
            page.mouse.wheel(0, per_step + random.randint(-20, 20))
        except Exception:
            return
        time.sleep(random.uniform(0.1, 0.3))


def random_dwell(min_seconds: float = 2.0, max_seconds: float = 8.0) -> float:
    """Return a random dwell time (for browsing pauses)."""
    return random.uniform(min_seconds, max_seconds)


# --------------------------------------------------------------------------- #
# Reaction pause (pre-action human delay)
# --------------------------------------------------------------------------- #

def reaction_pause(min_s: float = 0.25, max_s: float = 0.85) -> float:
    """Sleep for a Gaussian-distributed reaction pause.  Returns the delay."""
    mean = (min_s + max_s) / 2
    std = (max_s - min_s) / 6
    delay = max(min_s, min(max_s, random.gauss(mean, std)))
    time.sleep(delay)
    return delay


# --------------------------------------------------------------------------- #
# Full human-like browsing session on a tab
# --------------------------------------------------------------------------- #

def _safe_eval(page, expression: str):
    """Run page.evaluate() with full error swallowing (never crash a tab)."""
    try:
        return page.evaluate(expression)
    except Exception:
        return None


def _collect_clickable_links(page, max_links: int = 10) -> list:
    """Return up to *max_links* visible, same-origin link hrefs on the page.

    We only pick links that are actually visible in the viewport and point to
    the same site (so we don't accidentally navigate to a completely different
    domain and blow up the tab).  Each entry is a dict with 'href' and 'text'.
    """
    links = _safe_eval(
        page,
        r"""
        () => {
            const result = [];
            const anchors = document.querySelectorAll('a[href]');
            const base = window.location.origin;
            for (const a of anchors) {
                if (result.length >= 30) break;
                try {
                    const href = a.href;
                    if (!href || href.startsWith('javascript:')) continue;
                    if (!href.startsWith(base) && !href.startsWith(window.location.href.split('#')[0])) continue;
                    const rect = a.getBoundingClientRect();
                    if (rect.width < 5 || rect.height < 5) continue;
                    // Must be roughly in the upper viewport
                    if (rect.bottom < 0 || rect.top > 1200) continue;
                    const style = window.getComputedStyle(a);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    result.push({ href: href, text: (a.innerText || a.textContent || '').trim().substring(0, 60) });
                } catch (e) { continue; }
            }
            return result.slice(0, 15);
        }
        """,
    )
    if not links or not isinstance(links, list):
        return []
    # Filter out javascript: / mailto: / tel: links and de-duplicate by href.
    seen = set()
    unique = []
    for link in links:
        href = link.get("href", "")
        if not href:
            continue
        lower = href.lower()
        if lower.startswith("javascript:") or lower.startswith("mailto:") or lower.startswith("tel:"):
            continue
        if href not in seen:
            seen.add(href)
            unique.append(link)
    random.shuffle(unique)
    return unique[:max_links]


def human_browse_tab(
    page,
    dwell_seconds: float = 15.0,
    scroll: bool = True,
    hover_links: bool = True,
    click_links: bool = True,
    is_running_check=None,
) -> bool:
    """Simulate a full human browsing session on a background tab.

    This makes the tab look like a real person is reading it:

    1. **Initial pause** — 1-3 s reaction time (page just loaded).
    2. **Scroll down** in 3-7 smooth steps with pauses between each.
    3. **Hover** over 2-5 random links (mouse movement, no click).
    4. **Maybe click** one link and navigate, then scroll the new page too.
    5. **Dwell** — spend the remaining time reading.
    6. **Scroll back up** occasionally (people scroll back up).

    Parameters
    ----------
    page : Playwright/Camoufox page object (the background tab).
    dwell_seconds : total time to spend on this tab.
    scroll : whether to scroll.
    hover_links : whether to hover over links.
    click_links : whether to sometimes click a link and navigate.
    is_running_check : optional callable returning bool; if it returns False
        we bail out early (so stop() stays responsive).

    Returns True if the browse completed, False if interrupted.
    """
    def still_running():
        if is_running_check is not None:
            try:
                if not is_running_check():
                    return False
            except Exception:
                pass
        return True

    start = time.monotonic()
    deadline = start + dwell_seconds

    # 1. Initial reaction pause — human doesn't instantly scroll a fresh page.
    initial = random.uniform(1.0, 3.0)
    if not _wait_sliced(initial, deadline, still_running):
        return False

    # 2. Scroll down in smooth steps.
    if scroll:
        scroll_steps = random.randint(3, 7)
        for _ in range(scroll_steps):
            if not still_running() or time.monotonic() >= deadline:
                return False
            try:
                page.mouse.wheel(0, random.randint(150, 500))
            except Exception:
                break
            if not _wait_sliced(random.uniform(0.8, 2.5), deadline, still_running):
                return False

    # 3. Hover over random links.
    if hover_links and still_running() and time.monotonic() < deadline:
        links = _collect_clickable_links(page, max_links=8)
        hover_count = min(len(links), random.randint(2, 5))
        for i in range(hover_count):
            if not still_running() or time.monotonic() >= deadline:
                return False
            link = links[i]
            href = link.get("href", "")
            if not href:
                continue
            # Find the anchor element and hover it.
            try:
                locator = page.locator(f'a[href="{_css_escape(href)}"]').first
                if locator.count() > 0:
                    box = locator.bounding_box()
                    if box:
                        cx = int(box["x"] + box["width"] / 2 + random.uniform(-5, 5))
                        cy = int(box["y"] + box["height"] / 2 + random.uniform(-3, 3))
                        human_mouse_move(page, cx, cy)
                        time.sleep(random.uniform(0.3, 1.2))  # hover dwell
            except Exception:
                continue
            if not _wait_sliced(random.uniform(0.5, 1.5), deadline, still_running):
                return False

    # 4. Maybe click a link and navigate to a new page on the same site.
    if click_links and still_running() and time.monotonic() < deadline:
        links = _collect_clickable_links(page, max_links=5)
        if links and random.random() < 0.55:
            link = random.choice(links)
            href = link.get("href", "")
            if href:
                try:
                    # Scroll the link into view first, then click.
                    locator = page.locator(f'a[href="{_css_escape(href)}"]').first
                    if locator.count() > 0:
                        try:
                            locator.scroll_into_view_if_needed(timeout=3000)
                        except Exception:
                            pass
                        human_click_locator(locator, delay_before=random.uniform(0.2, 0.8))
                        # Wait for navigation.
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=10000)
                        except Exception:
                            pass
                        # Dwell on the new page: initial pause + a little scroll.
                        if not _wait_sliced(random.uniform(1.5, 3.0), deadline, still_running):
                            return False
                        if scroll:
                            for _ in range(random.randint(1, 3)):
                                if not still_running() or time.monotonic() >= deadline:
                                    return False
                                try:
                                    page.mouse.wheel(0, random.randint(100, 350))
                                except Exception:
                                    break
                                if not _wait_sliced(random.uniform(1.0, 2.5), deadline, still_running):
                                    return False
                except Exception:
                    pass  # navigation failed, just continue dwelling

    # 5. Spend remaining time dwelling (reading).
    remaining = deadline - time.monotonic()
    if remaining > 0 and still_running():
        if not _wait_sliced(remaining, deadline, still_running):
            return False

    # 6. Occasionally scroll back up (people scroll back up before leaving).
    if scroll and random.random() < 0.35 and still_running():
        try:
            page.mouse.wheel(0, -random.randint(300, 800))
        except Exception:
            pass

    return True


def _wait_sliced(total_seconds: float, deadline: float, still_running) -> bool:
    """Sleep for *total_seconds* in 0.5 s slices so we stay responsive to stop.

    Returns False if interrupted (stopped or deadline passed).
    """
    end_sleep = time.monotonic() + total_seconds
    while time.monotonic() < end_sleep:
        if not still_running():
            return False
        now = time.monotonic()
        if now >= deadline:
            return False
        # Slice size: 0.5 s, but never overshoot the sleep end or the deadline.
        slice_time = min(0.5, end_sleep - now, deadline - now)
        if slice_time > 0:
            time.sleep(slice_time)
    return True


def _css_escape(value: str) -> str:
    """Escape a string for use inside a CSS attribute selector [href="..."]."""
    # Escape backslash and double-quote — the two chars that break the selector.
    return value.replace("\\", "\\\\").replace('"', '\\"')


# --------------------------------------------------------------------------- #
# Random site-rotation helper
# --------------------------------------------------------------------------- #

def pick_random_site(url_pool: list, exclude: str = None) -> Optional[str]:
    """Pick a random URL from *url_pool*, optionally excluding *exclude*.

    Returns None if the pool is empty.
    """
    if not url_pool:
        return None
    candidates = [u for u in url_pool if u != exclude] or url_pool
    return random.choice(candidates)


# --------------------------------------------------------------------------- #
# Mouse idle jitter (small random movements while waiting)
# --------------------------------------------------------------------------- #

def mouse_idle_jitter(page, duration: float = 2.0, moves: int = 3) -> None:
    """Make small random mouse movements to simulate idle restlessness."""
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        vw, vh = viewport.get("width", 1280), viewport.get("height", 720)
    except Exception:
        vw, vh = 1280, 720
    interval = duration / max(moves, 1)
    for _ in range(moves):
        try:
            jitter_x = random.randint(int(vw * 0.3), int(vw * 0.7))
            jitter_y = random.randint(int(vh * 0.3), int(vh * 0.7))
            page.mouse.move(jitter_x, jitter_y, steps=random.randint(3, 8))
        except Exception:
            return
        time.sleep(interval * random.uniform(0.5, 1.5))


# --------------------------------------------------------------------------- #
# Tab switch simulation (Ctrl+Tab / Ctrl+W)
# --------------------------------------------------------------------------- #

def simulate_tab_switch(page, action: str = "switch") -> None:
    """Simulate keyboard-driven tab management.

    Actions:
        'switch'  — Ctrl+Tab (next tab)
        'prev'    — Ctrl+Shift+Tab (previous tab)
        'close'   — Ctrl+W (close current tab)
        'new'     — Ctrl+T (new tab)
        'refresh' — F5 (refresh page)
        'back'    — Alt+Left (go back)
    """
    key_map = {
        "switch": "Control+Tab",
        "prev": "Control+Shift+Tab",
        "close": "Control+w",
        "new": "Control+t",
        "refresh": "F5",
        "back": "Alt+ArrowLeft",
    }
    key = key_map.get(action, "Control+Tab")
    try:
        page.keyboard.press(key)
    except Exception:
        pass
    time.sleep(random.uniform(0.3, 0.8))


# --------------------------------------------------------------------------- #
# Viewport resize jitter (subtle size change during session)
# --------------------------------------------------------------------------- #

def viewport_resize_jitter(page) -> None:
    """Slightly resize the viewport to simulate a real user adjusting their window."""
    try:
        current = page.viewport_size or {"width": 1366, "height": 768}
        w = current.get("width", 1366) + random.randint(-20, 20)
        h = current.get("height", 768) + random.randint(-10, 10)
        page.set_viewport_size({"width": max(800, w), "height": max(600, h)})
    except Exception:
        pass
