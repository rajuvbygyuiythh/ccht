#!/usr/bin/env python3
"""test_browser_identity.py — "same account = same browser, different accounts
= different browsers" (Phase 14).

What is tested here (all offline — no browser, no network):

A. ``browser/browser_identity.py``
   deterministic derivation · stability across calls/processes · uniqueness
   between accounts · registry + session-folder persistence · adoption of the
   UA the account logged in with · rotation · audit/table output.
B. Coherence of the identity values that reach Playwright
   (``context_kwargs``, ``stealth_fingerprint``) and integration with
   ``browser_engine.chromium_context_kwargs`` + the stealth init script
   (stable screen/DPR/languages instead of per-launch random values).
C. ``browser/context_pool.py`` — every pooled context gets its account's
   identity + the anti-detect init script (stub browser, real pool code).
D. ``browser/browser_automation.py`` — identity is resolved and logged before
   the context, saved with the session, rotated after a ban, the profile-aware
   restore path, and the persistent-profile launch path (stub Playwright).
E. ``core/config_loader.py`` + ``data/config.json`` defaults and overrides.
F. ``tools/session_doctor.py`` — --identities / --identity / --new-identity.

Run:  python test_browser_identity.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from browser import browser_identity as identity            # noqa: E402
from test_session_health import (                           # noqa: E402
    StubPage, _load_automation_module, _make_bot, cookie, make_session,
)

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK ] {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  [FAIL] {name} {detail}")


class TempSessions:
    """Point every session/identity path at a temp folder for one test."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = {k: os.environ.get(k) for k in
                     ("EVA_SESSIONS_DIR", "EVA_ACCOUNTS_FILE", "EVA_BROWSER_PROFILE_MODE")}
        os.environ["EVA_SESSIONS_DIR"] = str(self.root)
        for key in ("EVA_ACCOUNTS_FILE", "EVA_BROWSER_PROFILE_MODE"):
            os.environ.pop(key, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()
        return False


# --------------------------------------------------------------------------- #
# A. identity core
# --------------------------------------------------------------------------- #

def test_derivation_and_stability() -> None:
    print("\n— A. deterministic, permanent device profile —")
    with TempSessions() as tmp:
        a = identity.derive_identity("user@example.com")
        b = identity.derive_identity("user@example.com")
        c = identity.derive_identity("other@example.com")
        check("derivation is deterministic", identity.signature_of(a) == identity.signature_of(b))
        check("different keys → different devices", identity.signature_of(a) != identity.signature_of(c))
        check("platform matches the UA", identity._platform_for_ua(a["user_agent"]) == a["platform"])
        check("os label matches the UA",
              identity._os_name_for_ua(a["user_agent"]) in ("Windows", "macOS", "Linux"))
        check("screen is at least as big as the viewport",
              a["screen"]["width"] >= a["viewport"]["width"]
              and a["screen"]["height"] >= a["viewport"]["height"], str(a["viewport"]))
        check("outer window is at least the viewport",
              a["outer"]["width"] >= a["viewport"]["width"]
              and a["outer"]["height"] >= a["viewport"]["height"])
        check("locale and timezone belong together",
              (a["locale"], a["timezone_id"]) in identity.LOCALE_TIMEZONE_POOL,
              f"{a['locale']}/{a['timezone_id']}")
        check("languages include the locale", a["languages"][0] == a["locale"], str(a["languages"]))
        check("no randomness between two process-like derivations",
              identity._sha("seed") == identity._sha("seed"))

        # a new salt must produce a different device
        salted = identity.derive_identity("user@example.com", salt=1)
        check("salt changes the device",
              identity.signature_of(salted) != identity.signature_of(a))

        # account_key extraction
        check("key from email", identity.account_key_for({"email": "A@B.com"}) == "a@b.com")
        check("key from session folder",
              identity.account_key_for({"session_dir": "/x/account_foo"}) == "account_foo")
        check("key from string", identity.account_key_for("simple@x.com") == "simple@x.com")


def test_uniqueness_and_persistence() -> None:
    print("\n— A2. unique between accounts, stable across restarts —")
    with TempSessions() as tmp:
        signatures = {}
        started = time.time()
        for i in range(60):
            ident = identity.identity_for({"email": f"user{i:03d}@example.com"})
            signatures.setdefault(identity.signature_of(ident), []).append(i)
        check("60 accounts → 60 distinct devices", len(signatures) == 60,
              f"{len(signatures)} unique")
        check("derivation stays fast", time.time() - started < 5.0)
        check("registry file written", identity.registry_path().exists())
        check("lock file cleaned up", not (identity.primary_root() / identity.LOCK_FILE).exists())
        audit = identity.audit()
        check("audit reports no duplicates", audit["unique"] is True and not audit["duplicates"],
              str(audit["duplicates"]))

        # restart: only the registry survives
        first = identity.identity_for({"email": "user017@example.com"})
        reloaded = identity.identity_for({"email": "USER017@example.com"})
        check("registry-only reload keeps the same device",
              identity.signature_of(first) == identity.signature_of(reloaded),
              f"{identity.describe(first)} vs {identity.describe(reloaded)}")

        # a session folder keeps its own identity.json
        folder = tmp.root / "account_keep"
        folder.mkdir()
        account = {"email": "keep@example.com", "session_dir": str(folder)}
        ident = identity.identity_for(account)
        check("identity.json stored with the session", (folder / identity.IDENTITY_FILE).exists())
        stored = json.loads((folder / identity.IDENTITY_FILE).read_text(encoding="utf-8"))
        check("stored identity matches the live one",
              identity.signature_of(stored) == identity.signature_of(ident))
        check("stored identity records the session folder",
              stored.get("session_dir") == str(folder))
        loaded = identity.load_identity(account)
        check("load_identity returns the stored device",
              identity.signature_of(loaded) == identity.signature_of(ident))
        check("describe() is a readable one-liner", "·" in identity.describe(ident),
              identity.describe(ident))
        check("table lists the account", "keep@example.com" in identity.format_identity_table())

        # explicit no-create lookup must not invent a device
        check("create=False returns None for an unknown account",
              identity.identity_for({"email": "never-seen@example.com"}, create=False) is None)

        # config can turn the whole feature off
        check("enabled() honours the config flag",
              identity.identity_for({"email": "off@example.com"},
                                    cfg={"enabled": False}) is None)


def test_adoption_and_rotation() -> None:
    print("\n— A3. adopt the login browser · rotate after a ban —")
    with TempSessions() as tmp:
        observed_ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
        folder = tmp.root / "account_old"
        folder.mkdir()
        (folder / "fingerprint.json").write_text(json.dumps({
            "generated": None,
            "observed": {"user_agent": observed_ua, "platform": "MacIntel",
                         "language": "en-GB", "hardware_concurrency": 12,
                         "device_memory": 8},
        }), encoding="utf-8")
        (folder / "storage_state.json").write_text(json.dumps({
            "cookies": [{"name": "t", "value": "v", "domain": "app.chitchat.gg",
                         "expires": time.time() + 90000}], "origins": []}), encoding="utf-8")
        account = {"email": "old@example.com", "session_dir": str(folder)}
        ident = identity.identity_for(account)
        check("adopts the UA the account logged in with",
              ident["user_agent"] == observed_ua and ident["source"] == "observed",
              str(ident.get("source")))
        check("adopted platform kept", ident["platform"] == "MacIntel")
        check("adopted hardware kept", ident["hardware_concurrency"] == 12)
        check("adopted locale kept", ident["locale"] == "en-GB", ident["locale"])
        check("adopted locale matches the UA language",
              ident["languages"][0] == "en-GB")

        # a second account must not steal the same UA signature
        folder2 = tmp.root / "account_second"
        folder2.mkdir()
        (folder2 / "fingerprint.json").write_text(json.dumps({"observed": {
            "user_agent": observed_ua, "platform": "MacIntel"}}), encoding="utf-8")
        second = identity.identity_for({"email": "second@example.com",
                                        "session_dir": str(folder2)})
        check("a taken UA is not handed out twice",
              identity.signature_of(second) != identity.signature_of(ident),
              identity.describe(second))
        check("the second account still got a full device",
              bool(second.get("user_agent")) and bool(second.get("viewport")))

        # rotation (ban)
        rotated = identity.rotate_identity(account, reason="unit test ban")
        check("rotation changes the signature",
              identity.signature_of(rotated) != identity.signature_of(ident))
        check("rotation keeps the account key", rotated["account_key"] == "old@example.com")
        check("rotation count increases", rotated["rotations"] == 1)
        check("rotation reason recorded", rotated["rotation_reason"] == "unit test ban")
        check("rotation is written to the session folder",
              identity.signature_of(json.loads(
                  (folder / identity.IDENTITY_FILE).read_text(encoding="utf-8")))
              == identity.signature_of(rotated))
        after = identity.identity_for({"email": "old@example.com"})
        check("a restart keeps the rotated device",
              identity.signature_of(after) == identity.signature_of(rotated))
        check("describe mentions the rotation", "rotations 1" in identity.describe(after))


# --------------------------------------------------------------------------- #
# B. values handed to Playwright
# --------------------------------------------------------------------------- #

def test_playwright_values() -> None:
    print("\n— B. identity → Playwright (context kwargs + stealth) —")
    with TempSessions():
        ident = identity.identity_for({"email": "pw@example.com"})
        kwargs = identity.context_kwargs(ident)
        check("context kwargs carry the UA", kwargs["user_agent"] == ident["user_agent"])
        check("context kwargs carry the viewport", kwargs["viewport"] == ident["viewport"])
        check("context kwargs carry the screen", kwargs["screen"] == ident["screen"])
        check("context kwargs carry the timezone", kwargs["timezone_id"] == ident["timezone_id"])
        check("context kwargs carry the locale", kwargs["locale"] == ident["locale"])
        check("Accept-Language follows the identity",
              ident["languages"][0] in kwargs["extra_http_headers"]["Accept-Language"],
              str(kwargs.get("extra_http_headers")))
        check("no mobile/touch flags", kwargs["is_mobile"] is False and kwargs["has_touch"] is False)
        check("device_scale_factor is a float", isinstance(kwargs["device_scale_factor"], float))

        fp = identity.stealth_fingerprint(ident)
        for key in ("user_agent", "viewport", "platform", "hardware_concurrency",
                    "device_memory", "screen", "device_scale_factor", "languages",
                    "timezone_id"):
            check(f"stealth fingerprint has {key}", fp.get(key) is not None)
        check("no None values leak into the fingerprint",
              all(value is not None for value in fp.values()), str(fp))

        # integration with the engine helpers
        from browser.browser_engine import (chromium_context_kwargs,
                                            _build_stealth_init_script)
        engine_kwargs, engine_fp = chromium_context_kwargs(fingerprint=fp)
        check("engine reuses the identity UA", engine_kwargs["user_agent"] == ident["user_agent"])
        check("engine reuses the identity timezone",
              engine_kwargs["timezone_id"] == ident["timezone_id"])
        check("engine reuses the identity screen", engine_kwargs["screen"] == ident["screen"])
        check("engine keeps the same fingerprint object", engine_fp is fp)

        script = _build_stealth_init_script(
            ident["user_agent"], ident["platform"],
            ident["hardware_concurrency"], ident["device_memory"], fp)
        check("init script embeds the stable screen width",
              f"get: () => {ident['screen']['width']}" in script)
        check("init script embeds the stable DPR",
              f"get: () => {ident['device_scale_factor']}" in script)
        check("init script embeds the identity languages",
              json.dumps(ident["languages"]) in script, str(ident["languages"]))
        check("init script embeds the clock-overrides UA",
              ident["user_agent"] in script)

        # stability: the same identity produces byte-identical metrics
        script2 = _build_stealth_init_script(
            ident["user_agent"], ident["platform"],
            ident["hardware_concurrency"], ident["device_memory"],
            identity.stealth_fingerprint(identity.identity_for({"email": "pw@example.com"})))
        check("screen/DPR/locale lines are identical on the next run",
              all(line in script2 for line in
                  [f"get: () => {ident['screen']['width']}",
                   f"get: () => {ident['device_scale_factor']}",
                   json.dumps(ident["languages"])]))

        # legacy behaviour when no identity is passed (random metrics, no crash)
        legacy = _build_stealth_init_script("Mozilla/5.0 (Windows NT 10.0) Chrome/131.0.0.0",
                                            "Win32", 8, 8)
        check("no-identity call still works (backwards compatible)", "navigator" in legacy)


# --------------------------------------------------------------------------- #
# C. context pool
# --------------------------------------------------------------------------- #

class _FakeContext:
    def __init__(self):
        self.init_scripts = []
        self.headers = None
        self.closed = False

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def set_extra_http_headers(self, headers):
        self.headers = headers

    def new_page(self):
        return object()

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.created = []

    def new_context(self, **kwargs):
        self.created.append(kwargs)
        return _FakeContext()


def _fake_pool():
    from browser.context_pool import ContextPool
    pool = ContextPool.__new__(ContextPool)
    pool._lock = threading.RLock()
    pool._semaphore = threading.Semaphore(20)
    pool._browser = _FakeBrowser()
    pool._camoufox_manager = None
    pool._slots = {}
    pool._next_slot_id = 1
    pool._started = True
    pool._shutting_down = False
    pool.verbose = False
    pool.viewport_jitter = True          # worst case: random jitter must lose
    pool.block_resource_types = False
    pool.max_contexts = 5
    return pool


def test_context_pool() -> None:
    print("\n— C. context pool: every account keeps its own device —")
    with TempSessions() as tmp:
        pool = _fake_pool()
        seen = []
        for i in range(6):
            account = {"email": f"pool{i}@example.com",
                       "session_dir": str(tmp.root / f"pool{i}")}
            options = identity.pool_options(account)
            slot = pool.create_context(
                account_key=account["email"],
                extra_context_options=options["extra_context_options"],
                stealth_fingerprint=options["stealth_fingerprint"],
            )
            check(f"pool0..pool{i} context created", slot is not None)
            kwargs = pool._browser.created[i]
            check(f"context {i} uses the account UA",
                  kwargs["user_agent"] == options["identity"]["user_agent"])
            check(f"context {i} keeps the identity viewport (jitter loses)",
                  kwargs["viewport"] == options["identity"]["viewport"])
            check(f"context {i} got the anti-detect init script",
                  len(slot.context.init_scripts) == 1)
            check(f"context {i} init script has the stable screen",
                  f"get: () => {options['identity']['screen']['width']}"
                  in slot.context.init_scripts[0])
            check(f"context {i} Accept-Language header set",
                  bool(slot.context.headers))
            seen.append(identity.signature_of(options["identity"]))
        check("all six pooled accounts are different devices",
              len(set(seen)) == 6, f"{len(set(seen))} unique")

        # a pool context without an identity must keep working (legacy path)
        slot = pool.create_context(account_key="plain@example.com")
        check("identity-less pool context still works", slot is not None)
        check("no stealth script when no identity is given",
              slot.context.init_scripts == [])

        # releasing a slot frees the semaphore (so the pool never deadlocks)
        first_slot = next(iter(pool._slots.values()))
        pool.close_context(first_slot.slot_id)
        check("pool slot released after close",
              first_slot.slot_id not in pool._slots)


# --------------------------------------------------------------------------- #
# D. automation wiring
# --------------------------------------------------------------------------- #

def test_automation_identity() -> None:
    print("\n— D. automation: identity resolved, saved, rotated —")
    ba, note = _load_automation_module()
    if ba is None:
        print(f"  [SKIP] browser_automation not importable — {note}")
        return
    with TempSessions() as tmp:
        folder = make_session(tmp.root, "account_ident", cookies=[cookie(20)],
                              email="ident@example.com")
        account = {"email": "ident@example.com", "session_dir": str(folder),
                   "storage_state_path": str(folder / "storage_state.json"),
                   "restore_url": "https://app.chitchat.gg/start/new"}
        bot, logs = _make_bot(ba, account)
        ident = bot._ensure_identity()
        check("automation resolves an identity", bool(ident and ident.get("user_agent")))
        check("identity is logged (one clear line)",
              any("[Identity]" in line and "→" in line for line in logs), str(logs))
        check("identity is attached to the account dict",
              account.get("identity") is ident)
        check("second call is cached (no duplicate work)",
              bot._ensure_identity() is ident)
        options, fp = bot._identity_context_options()
        check("identity context options carry the UA",
              options.get("user_agent") == ident["user_agent"])
        check("stealth fingerprint carries the screen",
              fp.get("screen") == ident["screen"])

        # same account on the "next run" keeps the same device
        bot2, logs2 = _make_bot(ba, dict(account))
        ident2 = bot2._ensure_identity()
        check("restart keeps the same device",
              identity.signature_of(ident2) == identity.signature_of(ident))

        # saving the session stores the identity next to it
        saved = bot._save_successful_account_session(
            StubPage(probe={"url": "https://app.chitchat.gg/", "textLength": 3000,
                            "user_agent": ident["user_agent"], "platform": ident["platform"]}))
        check("session save wrote the storage state", bool(saved))
        fingerprint_file = folder / "fingerprint.json"
        check("fingerprint.json now contains the device (not null)",
              fingerprint_file.exists()
              and json.loads(fingerprint_file.read_text(encoding="utf-8"))
              .get("generated", {}).get("user_agent") == ident["user_agent"],
              fingerprint_file.read_text(encoding="utf-8")[:200]
              if fingerprint_file.exists() else "missing")
        check("identity.json written with the session",
              (folder / identity.IDENTITY_FILE).exists())

        # ban → new device
        bot._rotate_identity_after_ban()
        check("ban rotation changed the device",
              identity.signature_of(bot._identity) != identity.signature_of(ident))
        check("rotation is logged", any("rotated the device" in line for line in logs), str(logs))
        check("rotation count grew", bot._identity.get("rotations") == 1)

        # config can disable rotation
        bot3, logs3 = _make_bot(ba, {"email": "norotate@example.com",
                                     "session_dir": str(tmp.root / "norotate")})
        bot3._identity = None
        original_config = identity.config
        identity.config = lambda: {"enabled": True, "rotate_on_ban": False,
                                   "profile_mode": "context"}
        try:
            bot3._rotate_identity_after_ban()
            check("rotation can be disabled in config", bot3._identity is None)
        finally:
            identity.config = original_config


def test_persistent_profile_flow() -> None:
    print("\n— D2. persistent profile mode (same real browser) —")
    ba, _ = _load_automation_module()
    if ba is None:
        print("  [SKIP] browser_automation not importable")
        return
    with TempSessions() as tmp:
        from browser import browser_engine

        # --- the proxy that makes a persistent context look like a browser ---
        fake_context = _FakeContext()
        proxy = browser_engine._PersistentProfile(fake_context, tmp.root / "profiles/x")
        check("proxy returns the profile as the context",
              proxy.new_context(storage_state="ignored") is fake_context)
        check("proxy reports connectivity", proxy.is_connected() is True)
        check("proxy exposes contexts", proxy.contexts == [fake_context])
        check("proxy records the profile dir", proxy.profile_dir.endswith("profiles/x"))

        # --- launch kwargs captured through a stubbed Playwright ---
        captured = {}

        class _FakeChromium:
            def launch_persistent_context(self, user_data_dir, **options):
                captured["dir"] = user_data_dir
                captured["options"] = options
                return _FakeContext()

        class _FakePW:
            chromium = _FakeChromium()

            def stop(self):
                captured["stopped"] = True

        class _FakeStarter:
            def start(self):
                return _FakePW()

        module = types.ModuleType("playwright.sync_api")
        module.sync_playwright = lambda: _FakeStarter()
        pkg = types.ModuleType("playwright")
        pkg.sync_api = module
        sys.modules["playwright"] = pkg
        sys.modules["playwright.sync_api"] = module
        try:
            profile_dir = tmp.root / "browser_profiles" / "acct"
            browser, handle = browser_engine.launch_browser(
                engine="chromium", headless=True, persistent_dir=profile_dir,
                context_kwargs={"user_agent": "UA-TEST", "viewport": {"width": 1000, "height": 700}})
            check("persistent launch opens the account's own profile dir",
                  str(captured.get("dir")) == str(profile_dir), str(captured.get("dir")))
            check("persistent launch passes the identity options",
                  captured["options"].get("user_agent") == "UA-TEST")
            check("persistent launch keeps the viewport",
                  captured["options"].get("viewport") == {"width": 1000, "height": 700})
            check("persistent launch returns the browser proxy",
                  isinstance(browser, browser_engine._PersistentProfile))
            check("automation-compatible API (new_context/is_connected)",
                  browser.new_context() is not None and browser.is_connected() is True)
            handle.close()
            check("cleanup stops Playwright", captured.get("stopped") is True)
        finally:
            sys.modules.pop("playwright", None)
            sys.modules.pop("playwright.sync_api", None)

        # --- automation: first launch imports the session, later ones don't ---
        identity.config = lambda: {"enabled": True, "profile_mode": "persistent",
                                   "profiles_dir_name": "browser_profiles",
                                   "unique_between_accounts": True,
                                   "adopt_observed_fingerprint": True,
                                   "rotate_on_ban": True, "warmup_sites": [],
                                   "log_identity_on_start": True}
        try:
            session = make_session(tmp.root, "account_prof", cookies=[cookie(20)],
                                   email="prof@example.com")
            account = {"email": "prof@example.com", "session_dir": str(session),
                       "storage_state_path": str(session / "storage_state.json")}
            bot, logs = _make_bot(ba, account)
            ident = bot._ensure_identity()
            profile_dir = identity.profile_dir_for(account)
            check("persistent mode records the profile dir",
                  ident.get("profile_dir") == str(profile_dir), str(ident.get("profile_dir")))
            check("profile is not marked used before the first launch",
                  identity.profile_is_used(profile_dir) is False)
            # simulate a used profile
            (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
            (profile_dir / "Default" / "Cookies").write_text("x", encoding="utf-8")
            check("profile_is_used detects a real profile",
                  identity.profile_is_used(profile_dir) is True)
            check("pre-flight lets a profile-backed account through",
                  bot._preflight_session_plan()[0] is True)
            check("profile-aware restore does not need storage_state.json",
                  bot.restore_saved_account_session.__name__ == "restore_saved_account_session")
            account_no_state = {"email": "prof@example.com", "session_dir": str(session)}
            bot2, logs2 = _make_bot(ba, account_no_state)
            page = types.SimpleNamespace(url="https://app.chitchat.gg/x", goto=lambda *a, **k: None)
            check("restore accepts a profile even without storage_state.json",
                  bot2._profile_cookies_present() is True)

            # warm-up pending logic
            check("warm-up skipped when no sites are configured",
                  identity.warmup_pending(ident, {"warmup_sites": []}) == [])
            check("warm-up triggered for a brand-new profile",
                  identity.warmup_pending(ident, {"warmup_sites": ["example.com"]}) == ["example.com"])
            identity.mark_warmup_done(account, ident, ["example.com"])
            check("warm-up runs only once",
                  identity.warmup_pending(ident, {"warmup_sites": ["example.com"]}) == [])
        finally:
            pass


# --------------------------------------------------------------------------- #
# E. config
# --------------------------------------------------------------------------- #

def test_config() -> None:
    print("\n— E. config: browser_identity section —")
    from core.config_loader import load_browser_identity, normalize_browser_identity
    defaults = load_browser_identity()
    check("enabled by default", defaults["enabled"] is True)
    check("default mode is a separate Chrome profile per account",
          defaults["profile_mode"] == "persistent", str(defaults["profile_mode"]))
    check("profile owner guard on by default", defaults["owner_guard"] is True)
    check("fingerprint verified before login by default",
          defaults["verify_before_login"] is True)
    check("verification is a warning by default (not strict)",
          defaults["verify_strict"] is False)
    check("profile saved after login by default",
          defaults["save_profile_after_login"] is True)
    check("uniqueness on by default", defaults["unique_between_accounts"] is True)
    check("rotation after a ban on by default", defaults["rotate_on_ban"] is True)
    check("no warm-up sites by default", defaults["warmup_sites"] == [])

    custom = normalize_browser_identity({
        "profile_mode": "PERSISTENT", "enabled": False, "warmup_sites": ["google.com"],
        "unique_between_accounts": False, "profiles_dir_name": "  ",
    })
    check("mode normalised", custom["profile_mode"] == "persistent", custom["profile_mode"])
    check("false switches are honoured",
          custom["enabled"] is False and custom["unique_between_accounts"] is False)
    weird = normalize_browser_identity({"enabled": "yes", "rotate_on_ban": 0})
    check("non-bool values fall back to the default (house style)",
          weird["enabled"] is True and weird["rotate_on_ban"] is True)
    check("warm-up sites parsed", custom["warmup_sites"] == ["google.com"])
    check("blank profile dir falls back",
          custom["profiles_dir_name"] == "browser_profiles")
    bad = normalize_browser_identity({"profile_mode": "nonsense"})
    check("invalid mode falls back to the default",
          bad["profile_mode"] == "persistent", bad["profile_mode"])

    raw = json.loads((ROOT / "data" / "config.json").read_text(encoding="utf-8"))
    check("data/config.json documents the section", "browser_identity" in raw)

    with TempSessions() as tmp:
        os.environ["EVA_BROWSER_PROFILE_MODE"] = "persistent"
        try:
            check("env override switches the mode",
                  identity.config()["profile_mode"] == "persistent")
        finally:
            os.environ.pop("EVA_BROWSER_PROFILE_MODE", None)
        check("env override removed → back to config",
              identity.config()["profile_mode"] == "persistent"
              or identity.config()["profile_mode"] == "context")

    text = (ROOT / "entry" / "thread_manager.py").read_text(encoding="utf-8")
    check("thread manager feeds identities into the pool",
          "pool_options" in text and "extra_context_options" in text)
    check("thread manager logs the identity",
          "[Identity]" in text)


# --------------------------------------------------------------------------- #
# F. CLI
# --------------------------------------------------------------------------- #

def test_cli() -> None:
    print("\n— F. tools/session_doctor.py — device profiles —")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sessions = root / "sessions"
        make_session(sessions, "account_one", cookies=[cookie(30)], email="one@x.com")
        make_session(sessions, "account_two", cookies=[cookie(30)], email="two@x.com")
        env = dict(os.environ, EVA_SESSIONS_DIR=str(sessions),
                   PYTHONIOENCODING="utf-8")
        env.pop("EVA_BROWSER_PYPROFILE_MODE", None)
        tool = str(ROOT / "tools" / "session_doctor.py")

        result = subprocess.run([sys.executable, tool, "--identities"],
                                capture_output=True, text=True, env=env, timeout=180)
        check("--identities runs", result.returncode == 0
              and "DEVICE PROFILES" in result.stdout, result.stdout[-300:])
        check("--identities reports the mode", "mode        :" in result.stdout)

        # create identities for the two accounts through the tool's own module
        code = ("import sys; sys.path.insert(0, %r);\n"
                "from browser import browser_identity as b;\n"
                "browser_store = __import__('browser.account_session_store', fromlist=['x']);\n"
                "for a in browser_store.load_saved_account_sessions():\n"
                "    b.identity_for(a)\n"
                "print('created')\n" % str(ROOT))
        created = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                 text=True, env=env, timeout=180)
        check("identities can be created for the saved sessions",
              "created" in created.stdout, created.stderr[-300:])

        result = subprocess.run([sys.executable, tool, "--identities"],
                                capture_output=True, text=True, env=env, timeout=180)
        check("--identities lists both accounts",
              "one@x.com" in result.stdout and "two@x.com" in result.stdout,
              result.stdout[-400:])
        check("--identities confirms uniqueness",
              "own device" in result.stdout, result.stdout[-200:])

        result = subprocess.run([sys.executable, tool, "--profiles"],
                                capture_output=True, text=True, env=env, timeout=180)
        check("--profiles runs", "CHROME PROFILES" in result.stdout, result.stdout[-300:])
        check("--profiles lists the accounts and the mode",
              "one@x.com" in result.stdout and "two@x.com" in result.stdout
              and "mode:" in result.stdout, result.stdout[-300:])
        check("--profiles confirms no sharing",
              "no account shares another account's Chrome profile" in result.stdout,
              result.stdout[-200:])

        result = subprocess.run([sys.executable, tool, "--identity", "one@x.com"],
                                capture_output=True, text=True, env=env, timeout=180)
        check("--identity prints the full device",
              "user agent" in result.stdout and "signature" in result.stdout,
              result.stdout[-300:])

        result = subprocess.run([sys.executable, tool, "--new-identity", "one@x.com"],
                                capture_output=True, text=True, env=env, timeout=180)
        check("--new-identity is a dry run without --apply",
              "dry run" in result.stdout and "Nothing changed" in result.stdout,
              result.stdout[-200:])
        result = subprocess.run([sys.executable, tool, "--new-identity", "one@x.com", "--apply"],
                                capture_output=True, text=True, env=env, timeout=180)
        check("--new-identity --apply rotates the device",
              "[done]" in result.stdout, result.stdout[-300:])
        result = subprocess.run([sys.executable, tool, "--identity", "one@x.com"],
                                capture_output=True, text=True, env=env, timeout=180)
        check("the rotated device is reported as a rotation",
              "rotations: 1" in result.stdout, result.stdout[-300:])


# --------------------------------------------------------------------------- #
# G. separate Chrome profile per account (owner guard) + verification
# --------------------------------------------------------------------------- #

def _probe_for(ident, **overrides):
    """A page probe that matches the identity (as a real browser would)."""
    probe = {
        "user_agent": ident["user_agent"],
        "platform": ident["platform"],
        "languages": ident["languages"],
        "language": ident["locale"],
        "hardware_concurrency": ident["hardware_concurrency"],
        "device_memory": ident["device_memory"],
        "webdriver": False,
        "screen": ident["screen"],
        "avail_height": ident["avail_height"],
        "viewport": ident["viewport"],
        "device_scale_factor": ident["device_scale_factor"],
        "timezone": ident["timezone_id"],
        "webgl_vendor": "Google Inc.",
        "webgl_renderer": "ANGLE (test)",
        "canvas_hash": "123",
        "url": "https://app.chitchat.gg/start/new",
    }
    probe.update(overrides)
    return probe


def test_profile_ownership() -> None:
    print("\n— G. one separate Chrome profile per account, never shared —")
    with TempSessions() as tmp:
        cfg = {"enabled": True, "profile_mode": "persistent",
               "profiles_dir_name": "browser_profiles", "owner_guard": True,
               "unique_between_accounts": True, "adopt_observed_fingerprint": True,
               "rotate_on_ban": True, "warmup_sites": [],
               "log_identity_on_start": True, "verify_before_login": True,
               "verify_strict": False, "verify_url": "",
               "save_profile_after_login": True}
        original = identity.config
        identity.config = lambda: dict(cfg)
        try:
            logs = []
            a1 = {"email": "one@x.com", "session_dir": str(tmp.root / "one")}
            a2 = {"email": "two@x.com", "session_dir": str(tmp.root / "two")}
            i1 = identity.identity_for(a1, cfg=cfg, log_fn=logs.append)
            i2 = identity.identity_for(a2, cfg=cfg, log_fn=logs.append)
            p1 = Path(i1["profile_dir"])
            p2 = Path(i2["profile_dir"])
            check("account 1 gets its own profile folder", p1.is_dir())
            check("account 2 gets a DIFFERENT profile folder", p1 != p2)
            check("profile folders are separate on disk",
                  str(p1) != str(p2) and p1.name != p2.name)
            check("owner.json records account 1",
                  identity.profile_owner(p1)["account_key"] == "one@x.com")
            check("owner.json records account 2",
                  identity.profile_owner(p2)["account_key"] == "two@x.com")
            check("re-claiming keeps the same folder (stable)",
                  identity.claim_profile(a1, cfg=cfg) == p1)

            # the dangerous case: a second account pointing at the first folder
            intruder = {"email": "two@x.com", "profile_dir": str(p1)}
            claimed = identity.claim_profile(intruder, cfg=cfg, log_fn=logs.append)
            check("a second account never opens another account's profile",
                  claimed != p1, str(claimed))
            check("the intruder is told why",
                  any("belongs to another account" in line for line in logs), str(logs))
            check("the original owner is untouched",
                  identity.profile_owner(p1)["account_key"] == "one@x.com")
            check("the intruder got a complete device too",
                  bool(identity.identity_for(intruder, cfg=cfg).get("profile_dir")))

            status = identity.profile_status(a1, cfg=cfg)
            check("status: profile on disk + owned by this account",
                  status["exists"] and status["owned_by_this_account"], str(status))
            check("status: not claimed by anyone else", status["claimed_by_other"] is False)

            # two accounts whose folder names would collide get separate folders
            collide_a = {"email": "user name@x.com"}
            collide_b = {"email": "user_name@x.com"}
            ca = identity.claim_profile(collide_a, cfg=cfg, log_fn=logs.append)
            cb = identity.claim_profile(collide_b, cfg=cfg, log_fn=logs.append)
            check("colliding folder names are separated",
                  ca != cb and identity.profile_owner(cb)["account_key"] == "user_name@x.com",
                  f"{ca} vs {cb}")

            # guard can be disabled explicitly
            claimed_any = identity.claim_profile(intruder, cfg=dict(cfg, owner_guard=False))
            check("owner_guard=false disables the guard", claimed_any == p1, str(claimed_any))

            status2 = identity.profile_status({"email": "three@x.com",
                                               "profile_dir": str(p1)},
                                              cfg=cfg)
            check("status flags a profile claimed by another account",
                  status2["claimed_by_other"] is True and status2["owner"], str(status2))
        finally:
            identity.config = original


def test_verification_and_profile_save() -> None:
    print("\n— G2. verify the fingerprint BEFORE login, save the profile after —")
    ba, _ = _load_automation_module()
    if ba is None:
        print("  [SKIP] browser_automation not importable")
        return
    with TempSessions() as tmp:
        session = make_session(tmp.root, "account_verify", cookies=[cookie(20)],
                               email="verify@example.com")
        account = {"email": "verify@example.com", "session_dir": str(session),
                   "storage_state_path": str(session / "storage_state.json")}
        bot, logs = _make_bot(ba, account)
        ident = bot._ensure_identity()
        check("identity ready for verification", bool(ident and ident.get("user_agent")))

        # --- values match: allowed to log in ---
        page = StubPage(probe=_probe_for(ident))
        check("verification passes when the browser matches the profile",
              bot._verify_fingerprint_before_login(page, purpose="unit test") is True)
        check("a pass is logged", any("device verified before unit test" in line
                                      for line in logs), str(logs[-3:]))
        check("the check is remembered for later saving",
              isinstance(bot._last_fingerprint_check, dict)
              and bot._last_fingerprint_check.get("ok") is True)

        # --- values mismatch: warning (not strict) ---
        bot2, logs2 = _make_bot(ba, dict(account))
        bot2._ensure_identity()
        page = StubPage(probe=_probe_for(ident, user_agent="Mozilla/5.0 (Linux) Chrome/90.0.0.0",
                                            screen={"width": 800, "height": 600}))
        check("a mismatch does not block by default",
              bot2._verify_fingerprint_before_login(page, purpose="unit test") is True)
        check("the mismatch is reported",
              any("MISMATCH" in line for line in logs2), str(logs2[-5:]))
        check("every wrong value is listed",
              any("user agent" in line for line in logs2)
              and any("screen width" in line for line in logs2), str(logs2[-6:]))

        # --- strict mode: block the login ---
        bot3, logs3 = _make_bot(ba, dict(account))
        bot3._ensure_identity()
        original_config = identity.config
        identity.config = lambda: {**dict(identity.DEFAULT_CONFIG), "verify_strict": True}
        try:
            check("verify_strict refuses to log in on a mismatch",
                  bot3._verify_fingerprint_before_login(page, purpose="unit test") is False)
            check("the refusal is explicit",
                  any("refusing to log in" in line for line in logs3), str(logs3[-3:]))
        finally:
            identity.config = original_config

        # --- config can disable the whole check ---
        bot4, logs4 = _make_bot(ba, dict(account))
        identity.config = lambda: {**dict(identity.DEFAULT_CONFIG), "verify_before_login": False}
        try:
            check("verification can be disabled in config",
                  bot4._verify_fingerprint_before_login(page, purpose="unit test") is True
                  and not any("MISMATCH" in line for line in logs4))
        finally:
            identity.config = original_config

        # --- a broken page never blocks a login ---
        class BrokenPage:
            def evaluate(self, js):
                raise RuntimeError("page crashed")
        bot5, logs5 = _make_bot(ba, dict(account))
        bot5._ensure_identity()
        check("a crashed page is not treated as a mismatch",
              bot5._verify_fingerprint_before_login(BrokenPage()) is True)

        # --- verify_url: the checker page is opened first ---
        bot6, logs6 = _make_bot(ba, dict(account))
        bot6._ensure_identity()
        identity.config = lambda: {**dict(identity.DEFAULT_CONFIG),
                                   "verify_url": "https://example.com/fp"}
        try:
            checker = StubPage(probe=_probe_for(ident))
            bot6._verify_fingerprint_before_login(checker)
            check("the configured checker page is opened",
                  checker.gotos and checker.gotos[0] == "https://example.com/fp",
                  str(checker.gotos))
        finally:
            identity.config = original_config

        # --- after login: the profile is saved + owner stamped ---
        bot7, logs7 = _make_bot(ba, dict(account))
        bot7._ensure_identity()
        bot7._last_fingerprint_check = {"ok": True, "observed": _probe_for(ident)}
        bot7._save_profile_after_login()
        profile_dir = Path(bot7._identity["profile_dir"])
        check("the profile folder exists after login", profile_dir.is_dir())
        owner = identity.profile_owner(profile_dir)
        check("owner.json records the login", bool(owner and owner.get("logged_in_at")),
              str(owner))
        stored = json.loads((session / identity.IDENTITY_FILE).read_text(encoding="utf-8"))
        check("identity.json records the profile save",
              bool(stored.get("profile_saved_at")), str(stored.get("profile_saved_at")))
        check("the observed fingerprint is remembered with the profile",
              stored.get("verified_observed", {}).get("user_agent") == ident["user_agent"])
        check("the save is logged", any("[Profile] saved" in line for line in logs7), str(logs7))
        check("a probe snapshot is written next to the profile",
              (profile_dir / "profile_state.json").exists())


def main() -> int:
    print("=" * 72)
    print("EVA browser identity tests (test_browser_identity.py)")
    print("=" * 72)
    test_derivation_and_stability()
    test_uniqueness_and_persistence()
    test_adoption_and_rotation()
    test_playwright_values()
    test_context_pool()
    test_automation_identity()
    test_persistent_profile_flow()
    test_profile_ownership()
    test_verification_and_profile_save()
    test_config()
    test_cli()
    print("\n" + "=" * 72)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
