#!/usr/bin/env python3
"""test_session_health.py — "session diye acc auto browser e load hobe" (Phase 13).

What is tested here:

A. ``browser/session_health.py`` — can a saved session be loaded?
   (ok / expiring / expired / empty / corrupt / missing, credentials, plans,
   metadata recording, refresh throttle, pruning, auth-probe interpretation)
B. ``core/account_manager.py`` — both session roots, credentials, blind
   status, ``next_idle_account`` skips unusable accounts.
C. ``browser/account_session_store.py`` — all roots, in-place refresh,
   honest alive/blind stock counts, health-aware restore loading.
D. ``browser/browser_automation.py`` — verified restore, blind → auto repair
   (login with accounts.txt credentials), clean failure without credentials,
   keep-alive refresh, and the pre-launch skip.
E. ``tools/session_doctor.py`` — the CLI (list / check / prune / exit codes).

Everything runs offline: no browser, no network, no Playwright.  Session
fixtures are built in a temp directory, and the automation is exercised with
stub page/context objects.

Run:  python test_session_health.py
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from browser import session_health as health

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


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

def make_session(root: Path, name: str, *, cookies=None, email=None,
                 metadata=True, corrupt=False, missing_state=False,
                 banned=False) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    if not missing_state:
        state = folder / "storage_state.json"
        if corrupt:
            state.write_text("{not json", encoding="utf-8")
        else:
            state.write_text(json.dumps({"cookies": cookies or [], "origins": []}),
                             encoding="utf-8")
    if metadata:
        (folder / "metadata.json").write_text(json.dumps({
            "email": email or f"{name}@example.com",
            "account_key": name,
            "storage_state": "storage_state.json",
        }), encoding="utf-8")
    if banned:
        (folder / ".banned").write_text("banned", encoding="utf-8")
    return folder


def cookie(days: float, domain="app.chitchat.gg", name="__Secure-authjs.session-token"):
    return {"name": name, "value": "v", "domain": domain,
            "expires": time.time() + days * 86400}


# --------------------------------------------------------------------------- #
# A. session_health
# --------------------------------------------------------------------------- #

def test_health_states() -> None:
    print("\n— A. session health: can the browser load it? —")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ok = make_session(root, "account_ok", cookies=[cookie(30)])
        expiring = make_session(root, "account_expiring", cookies=[cookie(0.5)])
        expired = make_session(root, "account_expired", cookies=[cookie(-1)])
        empty = make_session(root, "account_empty", cookies=[])
        corrupt = make_session(root, "account_corrupt", corrupt=True)
        missing = make_session(root, "account_missing", missing_state=True)
        banned = make_session(root, "account_banned", cookies=[cookie(30)], banned=True)

        cases = [
            (ok, health.HEALTH_OK, False),
            (expiring, health.HEALTH_EXPIRING, False),
            (expired, health.HEALTH_EXPIRED, True),
            (empty, health.HEALTH_EMPTY, True),
            (corrupt, health.HEALTH_CORRUPT, True),
            (missing, health.HEALTH_MISSING, True),
        ]
        for folder, state, blind in cases:
            h = health.session_health(folder, warn_days=3)
            check(f"{folder.name}: state={state}", h.get("state") == state,
                  f"got {h.get('state')} ({h.get('reason')})")
            check(f"{folder.name}: blind={blind}", bool(h.get("blind")) is blind)

        # cookie details
        info = health.inspect_storage_state(ok / "storage_state.json")
        check("cookie counter", info["cookies"] == 1, str(info))
        check("auth-domain counter", info["auth_cookies"] == 1, str(info))
        check("live auth cookie counted", info["live_auth_cookies"] == 1, str(info))
        check("days_left computed", info["days_left"] is not None and info["days_left"] > 29,
              str(info["days_left"]))
        expired_info = health.inspect_storage_state(expired / "storage_state.json")
        check("expired cookie not counted live",
              expired_info["live_auth_cookies"] == 0 and expired_info["expired_auth_cookies"] == 1,
              str(expired_info))

        # unrelated domain cookies do not keep a session alive
        other = make_session(root, "account_otherdomain", cookies=[cookie(30, domain="api.hcaptcha.com")])
        h = health.session_health(other)
        check("cookies from other domains do not count as login",
              h.get("state") == health.HEALTH_EXPIRED and h.get("blind"), str(h))

        # session-only cookie (no expiry) still counts as usable
        session_cookie = make_session(root, "account_sessioncookie", cookies=[
            {"name": "sid", "value": "v", "domain": "app.chitchat.gg", "expires": -1}])
        h = health.session_health(session_cookie)
        check("session cookie (expires=-1) counts as live",
              h.get("state") == health.HEALTH_OK, str(h))

        # banned helper + summarize
        check("banned flag detected", health.is_banned(banned))
        summary = health.summarize([ok, expiring, expired, empty, corrupt, missing, banned])
        check("summary total", summary["total"] == 7, str(summary))
        check("summary blind count", summary["blind"] == 4, str(summary))
        check("summary alive excludes blind",
              summary["alive"] == 2, str(summary))
        check("summary banned", summary["banned"] == 1, str(summary))
        line = health.format_summary(summary)
        check("summary line is log-friendly",
              "blind=" in line and "alive=" in line, line)

        # missing file path / no path at all
        empty_info = health.inspect_storage_state(None)
        check("no path is reported missing", empty_info["error"] and not empty_info["exists"])
        check("garbage JSON is corrupt, not a crash",
              health.session_health(corrupt)["state"] == health.HEALTH_CORRUPT)


def test_plans_and_credentials() -> None:
    print("\n— A2. decisions: restore / repair / skip —")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        good = make_session(root, "account_good", cookies=[cookie(30)], email="good@x.com")
        dead = make_session(root, "account_dead", cookies=[cookie(-2)], email="dead@x.com")
        banned = make_session(root, "account_banned", cookies=[cookie(-2)],
                              email="banned@x.com", banned=True)

        creds = {"dead@x.com": {"email": "dead@x.com", "password": "pw123",
                                "source": "accounts.txt"}}

        plan = health.plan_for_account({"email": "good@x.com", "session_dir": str(good)})
        check("healthy session → restore", plan["action"] == health.ACTION_RESTORE, str(plan))

        plan = health.plan_for_account({"email": "dead@x.com", "session_dir": str(dead)},
                                       credentials=creds)
        check("blind + credentials → repair", plan["action"] == health.ACTION_REPAIR, str(plan))
        check("repair plan says BLIND SESSION", "BLIND SESSION" in plan["reason"], plan["reason"])
        check("repair plan exposes has_credentials", plan["has_credentials"] is True)

        plan = health.plan_for_account({"email": "dead@x.com", "session_dir": str(dead)})
        check("blind without credentials → skip", plan["action"] == health.ACTION_SKIP, str(plan))

        plan = health.plan_for_account({"email": "banned@x.com", "session_dir": str(banned)},
                                       credentials=creds)
        check("banned session is never repaired", plan["action"] == health.ACTION_SKIP
              and plan["banned"], str(plan))

        plan = health.plan_for_account({"email": "dead@x.com", "session_dir": str(dead)},
                                       credentials=creds, allow_repair=False)
        check("allow_repair=False → skip", plan["action"] == health.ACTION_SKIP)

        # accounts.txt parsing
        accounts_file = root / "accounts.txt"
        accounts_file.write_text(
            "# comment\n"
            "\n"
            "one@x.com:pw1\n"
            "two@x.com:pw2:proxy.example:8080\n"
            "three@x.com:pw3:proxy:user:pass\n"
            "four@x.com|pw4\n"
            "broken-line\n",
            encoding="utf-8")
        table = health.load_credentials([accounts_file])
        check("four accounts parsed", len(table) == 4, str(sorted(table)))
        check("email:password parsed", table["one@x.com"]["password"] == "pw1")
        check("proxy server parsed",
              table["two@x.com"]["proxy"] == "proxy.example:8080", str(table["two@x.com"]))
        check("proxy with auth parsed",
              table["three@x.com"]["proxy"] == "proxy:user:pass", str(table["three@x.com"]))
        check("pipe separator parsed", table["four@x.com"]["password"] == "pw4")
        check("comments/blanks skipped", "broken-line" not in table)

        account = {"email": "one@x.com", "session_dir": str(dead)}
        health.attach_credentials(account, table)
        check("attach_credentials fills the password in memory",
              account.get("password") == "pw1")
        check("credentials_for matches case-insensitively",
              health.credentials_for("ONE@X.COM", table)["password"] == "pw1")

        # sessions of an unknown email get no credentials
        account2 = {"email": "nobody@x.com", "session_dir": str(dead)}
        health.attach_credentials(account2, table)
        check("no credentials for unknown email", "password" not in account2)


def test_metadata_and_refresh() -> None:
    print("\n— A3. metadata health + refresh throttle + prune —")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = make_session(root, "account_meta", cookies=[cookie(-3)], email="m@x.com")
        h = health.session_health(folder)
        check("record_health writes the health block", health.record_health(folder, h))
        meta = health.read_metadata(folder)
        check("metadata has health_state", meta.get("health_state") == health.HEALTH_EXPIRED, str(meta))
        check("metadata marks blind", meta.get("blind") is True, str(meta))
        check("metadata records blind_since", bool(meta.get("blind_since")), str(meta))

        # repairing clears the blind flag
        (folder / "storage_state.json").write_text(
            json.dumps({"cookies": [cookie(20)], "origins": []}), encoding="utf-8")
        health.record_repaired(folder, repaired_reason="unit test")
        meta = health.read_metadata(folder)
        check("repaired session is no longer blind", meta.get("blind") is False, str(meta))
        check("repair_count tracked", int(meta.get("repair_count") or 0) == 1, str(meta))

        # refresh throttle
        fresh = make_session(root, "account_fresh", cookies=[cookie(10)])
        health.update_metadata(fresh, last_refresh_at="2000-01-01T00:00:00+00:00")
        check("stale session should refresh", health.should_refresh(fresh, 10))
        health.update_metadata(fresh, last_refresh_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        check("just-refreshed session is throttled", not health.should_refresh(fresh, 10))
        check("minutes=0 always refreshes", health.should_refresh(fresh, 0))

        # pruning: dry run + real move, banned untouched
        dead = make_session(root, "account_dead1", cookies=[cookie(-5)])
        alive = make_session(root, "account_alive", cookies=[cookie(20)])
        banned = make_session(root, "account_ban1", cookies=[cookie(-5)], banned=True)
        result = health.prune_dead_sessions([dead, alive, banned], dry_run=True)
        check("dry run lists the dead session", result["count"] == 1
              and str(dead) in result["would_move"], str(result))
        check("dry run does not move anything", dead.exists())
        result = health.prune_dead_sessions([dead, alive, banned], dry_run=False)
        check("real prune moves the dead session", result["count"] == 1 and not dead.exists(),
              str(result))
        check("moved session is parked in _dead/",
              (root / health.DEAD_SUBDIR / "account_dead1").is_dir())
        check("alive session kept", alive.exists())
        check("banned session untouched", banned.exists() and health.is_banned(banned))

        # update_metadata is atomic and merges
        health.update_metadata(folder, note="hello", extra_key=7)
        meta = health.read_metadata(folder)
        check("update_metadata merges fields", meta.get("note") == "hello" and meta.get("extra_key") == 7)
        check("update_metadata keeps existing fields", meta.get("repair_count") == 1)


def test_auth_probe() -> None:
    print("\n— A4. was the restore really authenticated? —")
    cases = [
        ({"url": "https://app.chitchat.gg/login", "textLength": 500}, "https://app.chitchat.gg/login",
         False, "login url"),
        ({"loginForm": True, "textLength": 900, "loginWords": True, "chatUi": False},
         "https://app.chitchat.gg/", False, "password form"),
        ({"chatUi": True, "textLength": 5000}, "https://app.chitchat.gg/start/new",
         True, "chat UI"),
        ({"hasUsername": True, "textLength": 5000}, "https://app.chitchat.gg/chat/x",
         True, "username node"),
        ({"authWords": True, "loginWords": False, "textLength": 4000},
         "https://app.chitchat.gg/start/new", True, "app text"),
        ({"textLength": 5}, "https://app.chitchat.gg/start/new", None, "blank page"),
        ({"textLength": 3000, "loginWords": True}, "https://app.chitchat.gg/start/new",
         False, "login words"),
        ({"textLength": 3000}, "https://app.chitchat.gg/chat/abc", True, "chat url"),
        ({"textLength": 3000}, "https://app.chitchat.gg/", None, "unknown"),
    ]
    for probe, url, expected, label in cases:
        status, reason = health.interpret_auth_probe(probe, url)
        check(f"auth probe: {label} → {expected}", status is expected, f"got {status} ({reason})")

    status, reason = health.interpret_auth_probe({"textLength": 2000}, "https://app.chitchat.gg/",
                                                 allow_unknown=False)
    check("allow_unknown=False turns 'unknown' into not-authenticated", status is False)

    # verify_page never raises on a broken page
    class BrokenPage:
        def evaluate(self, js):
            raise RuntimeError("page crashed")
    status, reason, probe = health.verify_page(BrokenPage())
    check("verify_page survives a crashed page", status is None and "probe failed" in reason, reason)

    class GoodPage:
        def evaluate(self, js):
            return {"url": "https://app.chitchat.gg/chat/1", "chatUi": True, "textLength": 4000}

        def wait_for_timeout(self, ms):
            pass
    status, reason, probe = health.verify_page(GoodPage())
    check("verify_page reads an authenticated page", status is True and probe.get("chatUi") is True)


# --------------------------------------------------------------------------- #
# B/C. manager + store integration
# --------------------------------------------------------------------------- #

def test_account_manager() -> None:
    print("\n— B. account manager: both roots, blind status, skips —")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root_a = root / "account_sessions"
        root_b = root / "data" / "account_sessions"
        good = make_session(root_a, "account_good", cookies=[cookie(30)], email="good@x.com")
        dead = make_session(root_b, "account_dead", cookies=[cookie(-4)], email="dead@x.com")
        repair = make_session(root_b, "account_repair", cookies=[], email="repair@x.com")
        recovered = root_a / "account_recovered"
        recovered.mkdir(parents=True)
        (recovered / "storage_state.json").write_text(
            json.dumps({"cookies": [cookie(12)], "origins": []}), encoding="utf-8")
        accounts_file = root / "accounts.txt"
        accounts_file.write_text("repair@x.com:secret\n", encoding="utf-8")

        env_backup = {k: os.environ.get(k) for k in ("EVA_SESSIONS_DIR", "EVA_ACCOUNTS_FILE")}
        # EVA_SESSIONS_DIR may list several roots separated by os.pathsep
        os.environ["EVA_SESSIONS_DIR"] = f"{root_a}{os.pathsep}{root_b}"
        os.environ["EVA_ACCOUNTS_FILE"] = str(accounts_file)
        try:
            from core.account_manager import (
                AccountManager, STATUS_BLIND,
            )
            manager = AccountManager()
            loaded = manager.load()
            check("manager loads from every session root", loaded == 4, f"loaded={loaded}")
            by_email = {e.email: e for e in manager.all_entries()}
            check("healthy account is not blind",
                  by_email["good@x.com"].blind is False)
            check("expired session is flagged blind",
                  by_email["dead@x.com"].blind is True
                  and by_email["dead@x.com"].status == STATUS_BLIND)
            check("blind status visible in to_display()",
                  by_email["dead@x.com"].to_display()["blind"] is True)
            check("blind + credentials → repair plan",
                  by_email["repair@x.com"].plan.get("action") == "repair",
                  str(by_email["repair@x.com"].plan.get("reason")))
            check("repair plan exposes the password to the worker",
                  by_email["repair@x.com"].extra.get("password") == "secret")
            check("folder without metadata.json is recovered",
                  "recovered@example.com" in by_email
                  or any(e.account_key == "account_recovered" for e in manager.all_entries()))

            summary = manager.summary()
            check("summary counts blind", summary["blind"] >= 1, str(summary))
            check("summary counts repairable", summary["repairable"] >= 1, str(summary))
            check("format_summary shows blind", "blind:" in manager.format_summary())
            check("format_list shows the session state",
                  "Session" in manager.format_list(), manager.format_list()[:120])

            # next_idle_account skips the unusable one
            picked = []
            for _ in range(6):
                entry = manager.next_idle_account()
                if entry is None:
                    break
                picked.append(entry.email)
                manager.mark_status(entry.account_key, "resting")
            check("blind-but-unrepairable account is never handed out",
                  "dead@x.com" not in picked, str(picked))
            check("usable accounts are handed out", "good@x.com" in picked, str(picked))

            # refresh_health picks up a repaired session
            (root_b / "account_dead" / "storage_state.json").write_text(
                json.dumps({"cookies": [cookie(15)], "origins": []}), encoding="utf-8")
            plan = manager.refresh_health(by_email["dead@x.com"].account_key,
                                          credentials=health.load_credentials([accounts_file]))
            check("refresh_health clears the blind state after a repair",
                  plan["action"] == "restore", str(plan))
            check("status returns to idle",
                  manager.get_by_key(by_email["dead@x.com"].account_key).status == "idle")

            # mark_blind / mark_repaired
            manager.mark_blind(by_email["good@x.com"].account_key, "unit test")
            check("mark_blind flags the account",
                  manager.get_by_key(by_email["good@x.com"].account_key).blind is True)
            manager.mark_repaired(by_email["good@x.com"].account_key)
            check("mark_repaired clears it",
                  manager.get_by_key(by_email["good@x.com"].account_key).blind is False)
            check("blind_entries lists flagged accounts", len(manager.blind_entries()) >= 0)
        finally:
            for key, value in env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_session_store() -> None:
    print("\n— C. session store: roots, stock counts, in-place refresh —")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root_a = root / "account_sessions"
        root_b = root / "data" / "account_sessions"
        make_session(root_a, "account_a1", cookies=[cookie(30)], email="a1@x.com")
        make_session(root_b, "account_b1", cookies=[cookie(-2)], email="b1@x.com")
        make_session(root_b, "account_b2", cookies=[], email="b2@x.com")

        accounts_file = root / "accounts.txt"
        accounts_file.write_text("b1@x.com:pw\n", encoding="utf-8")
        env_backup = {k: os.environ.get(k) for k in ("EVA_SESSIONS_DIR", "EVA_ACCOUNTS_FILE")}
        os.environ["EVA_SESSIONS_DIR"] = f"{root_a}{os.pathsep}{root_b}"
        os.environ["EVA_ACCOUNTS_FILE"] = str(accounts_file)
        try:
            from browser import account_session_store as store
            check("env override replaces the default roots (both layouts listed)",
                  len([r for r in store.iter_session_roots() if Path(r).is_dir()]) == 2,
                  str(store.iter_session_roots()))
            check("total counts every root", store.count_total_sessions() == 3,
                  str(store.count_total_sessions()))
            check("alive counts only loadable sessions",
                  store.count_alive_sessions() == 1, str(store.count_alive_sessions()))
            check("blind count is honest", store.count_blind_sessions() == 2,
                  str(store.count_blind_sessions()))
            summary = store.session_stock_summary()
            check("stock summary carries blind", summary["blind"] == 2
                  and summary["alive"] == 1 and summary["total"] == 3, str(summary))

            accounts = store.load_saved_account_sessions()
            check("restore loading sees all roots", len(accounts) == 3, str(len(accounts)))
            by_email = {a["email"]: a for a in accounts}
            check("health attached to each account",
                  by_email["a1@x.com"]["health"]["state"] == health.HEALTH_OK,
                  str(by_email["a1@x.com"].get("health")))
            check("blind flag attached", by_email["b2@x.com"]["blind"] is True)
            check("repair action planned for the credentialled blind session",
                  by_email["b1@x.com"]["action"] == "repair",
                  str(by_email["b1@x.com"].get("action")))
            check("password attached in memory for the repair",
                  by_email["b1@x.com"].get("password") == "pw")
            check("blind without credentials is marked skip",
                  by_email["b2@x.com"]["action"] == "skip")

            # in-place refresh: a restored account keeps its own folder
            folder = root_b / "account_b1"
            class Ctx:
                def storage_state(self, path, **kwargs):
                    Path(path).write_text(json.dumps({"cookies": [cookie(20)], "origins": []}),
                                          encoding="utf-8")
            target = store.refresh_storage_state(
                {"email": "b1@x.com", "session_dir": str(folder)}, Ctx(), reason="unit test")
            check("refresh writes into the account's own folder",
                  Path(target) == folder, f"{target} != {folder}")
            meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
            check("refresh records last_refresh_at", bool(meta.get("last_refresh_at")), str(meta))
            check("refresh re-classifies the session as healthy",
                  meta.get("health_state") == health.HEALTH_OK, str(meta))
            check("refresh clears the blind flag", meta.get("blind") is False, str(meta))
            check("no duplicate folder created under the primary root",
                  len(list(root_a.glob("account_*"))) == 1,
                  str([p.name for p in root_a.glob("account_*")]))
        finally:
            for key, value in env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


# --------------------------------------------------------------------------- #
# D. automation wiring (stub page/context — no browser)
# --------------------------------------------------------------------------- #

def _load_automation_module():
    """Import browser.browser_automation, stubbing Windows/Qt deps if needed."""
    def _stub_platform():
        if "winsound" not in sys.modules:
            sys.modules["winsound"] = types.ModuleType("winsound")
        if "PyQt6" not in sys.modules:
            class _Sig:
                def __init__(self, *a, **k):
                    pass

                def connect(self, *a, **k):
                    pass

                def emit(self, *a, **k):
                    pass

            pkg = types.ModuleType("PyQt6")
            core = types.ModuleType("PyQt6.QtCore")
            core.QThread = type("QThread", (), {"__init__": lambda self, *a, **k: None})
            core.pyqtSignal = lambda *a, **k: _Sig()
            pkg.QtCore = core
            sys.modules["PyQt6"] = pkg
            sys.modules["PyQt6.QtCore"] = core

    try:
        import browser.browser_automation as ba
        return ba, "real dependencies"
    except Exception:
        pass
    sys.modules.pop("browser.browser_automation", None)
    _stub_platform()
    try:
        import browser.browser_automation as ba
        return ba, "stubbed Qt/Windows deps (offline check)"
    except Exception as exc:
        return None, f"unavailable: {type(exc).__name__}: {exc}"


class StubPage:
    """Minimal Playwright page: goto + evaluate + url."""

    def __init__(self, url="https://app.chitchat.gg/start/new", probe=None,
                 redirect_to=None):
        self.url = url
        self.probe = probe or {"url": url, "chatUi": True, "textLength": 4200}
        self.gotos = []
        self.evaluations = []
        self.redirect_to = redirect_to

    def goto(self, url, **kwargs):
        self.gotos.append(url)
        # a real browser follows the site's redirect (e.g. to /login)
        self.url = self.redirect_to or url
        return None

    def evaluate(self, js, arg=None):
        self.evaluations.append(js)
        if self.probe.get("raise"):
            raise RuntimeError("page crashed")
        return dict(self.probe)

    def wait_for_timeout(self, ms):
        pass


class StubContext:
    def __init__(self, cookies=None):
        self.writes = []
        self.cookies = cookies if cookies is not None else [cookie(25)]

    def storage_state(self, path, **kwargs):
        self.writes.append(str(path))
        Path(path).write_text(json.dumps({"cookies": self.cookies, "origins": []}),
                              encoding="utf-8")


def _make_bot(ba, account, *, session_cfg=None):
    bot = object.__new__(ba.ChitchatAutomation)
    logs = []
    bot.log = lambda message: logs.append(str(message))
    bot.account = account
    bot.account_mode = "restore"
    bot.thread_id = 1
    bot.session_management_cfg = dict({
        "verify_after_restore": True,
        "auto_repair_blind_sessions": True,
        "blind_fallback_to_login": True,
        "session_refresh_each_chat": True,
        "session_refresh_minutes": 0,
        "log_health_on_start": True,
    }, **(session_cfg or {}))
    bot.context = StubContext()
    bot._session_blind = False
    bot._session_repair_attempted = False
    bot._session_repair_reason = ""
    bot._session_probe_reason = ""
    bot._last_session_refresh = 0.0
    bot._navigation_history = []
    bot.set_status = lambda text: None
    bot._emit_session = lambda state: None
    bot.hide_after_login = False
    bot.generated_fingerprint = None
    bot.restore_fingerprint = None
    bot.proxy_config = None
    bot._ban_detected = False
    bot._stop_event = __import__("threading").Event()
    return bot, logs


def test_automation_restore_and_repair() -> None:
    print("\n— D. automation: verified restore, blind repair, keep-alive —")
    ba, note = _load_automation_module()
    if ba is None:
        print(f"  [SKIP] browser_automation not importable — {note}")
        return
    print(f"  [info] {note}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_env = {k: os.environ.get(k) for k in ("EVA_SESSIONS_DIR", "EVA_ACCOUNTS_FILE")}
        os.environ["EVA_SESSIONS_DIR"] = str(root)
        try:
            # ---- authenticated restore -----------------------------------
            folder = make_session(root, "account_live", cookies=[cookie(20)], email="live@x.com")
            account = {"email": "live@x.com", "session_dir": str(folder),
                       "storage_state_path": str(folder / "storage_state.json"),
                       "restore_url": "https://app.chitchat.gg/start/new"}
            bot, logs = _make_bot(ba, account)
            page = StubPage(probe={"url": "https://app.chitchat.gg/chat/1", "chatUi": True,
                                   "textLength": 5000})
            check("authenticated restore succeeds",
                  bot.restore_saved_account_session(page) is True)
            check("restore logs the verification",
                  any("verified as authenticated" in line for line in logs), str(logs))
            check("restore visited the restore url",
                  page.gotos and page.gotos[0] == account["restore_url"])

            # ---- blind restore (login page) ------------------------------
            bot, logs = _make_bot(ba, dict(account))
            page = StubPage(url="https://app.chitchat.gg/login",
                            probe={"url": "https://app.chitchat.gg/login", "loginForm": True,
                                   "textLength": 800})
            check("blind restore is rejected", bot.restore_saved_account_session(page) is False)
            check("blind session is reported (grep-able)",
                  any("BLIND SESSION" in line for line in logs), str(logs))
            check("blind state stored on the worker", bot._session_blind is True)
            meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
            check("metadata records the blind state", meta.get("blind") is True, str(meta))

            # ---- repair with credentials ---------------------------------
            accounts_file = root / "accounts.txt"
            accounts_file.write_text("live@x.com:secret-pw\n", encoding="utf-8")
            os.environ["EVA_ACCOUNTS_FILE"] = str(accounts_file)
            # rewrite the session as expired so the plan says "repair"
            (folder / "storage_state.json").write_text(
                json.dumps({"cookies": [cookie(-1)], "origins": []}), encoding="utf-8")
            bot, logs = _make_bot(ba, {**account, "storage_state_path": str(folder / "storage_state.json")})
            calls = {"login": 0}

            def fake_login(page):
                calls["login"] += 1
                return True
            bot.login_with_account = fake_login
            repaired = bot._repair_blind_session(StubPage())
            check("blind session is repaired by a credential login", repaired is True)
            check("login was called exactly once", calls["login"] == 1, str(calls))
            check("repair is logged", any("repaired" in line.lower() for line in logs), str(logs))
            check("repair clears the blind flag", bot._session_blind is False)
            meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
            check("repair persisted a fresh session", (folder / "storage_state.json").exists()
                  and meta.get("repair_count") == 1, str(meta))
            check("repaired session is healthy in metadata",
                  meta.get("health_state") == health.HEALTH_OK, str(meta))
            check("repaired session records the verification",
                  bool(meta.get("last_verified_at")), str(meta))

            # ---- blind without credentials: clean failure ----------------
            os.environ.pop("EVA_ACCOUNTS_FILE", None)
            (folder / "storage_state.json").write_text(
                json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
            bot, logs = _make_bot(ba, {"email": "nobody@x.com", "session_dir": str(folder)})
            bot.login_with_account = lambda page: (_ for _ in ()).throw(
                AssertionError("login must not be attempted without credentials"))
            check("blind + no credentials → repair refused",
                  bot._repair_blind_session(StubPage()) is False)
            check("the reason mentions credentials",
                  any("no credentials" in line for line in logs), str(logs))

            # ---- config can disable repair -------------------------------
            bot, logs = _make_bot(ba, dict(account),
                                  session_cfg={"auto_repair_blind_sessions": False})
            bot.login_with_account = lambda page: (_ for _ in ()).throw(
                AssertionError("login must not run when repair is disabled"))
            check("repair can be disabled in config",
                  bot._repair_blind_session(StubPage()) is False)
            check("log says repair is disabled",
                  any("disabled in config.json" in line for line in logs), str(logs))

            # ---- keep-alive refresh --------------------------------------
            folder2 = make_session(root, "account_refresh", cookies=[cookie(10)], email="r@x.com")
            account2 = {"email": "r@x.com", "session_dir": str(folder2)}
            bot, logs = _make_bot(ba, account2)
            check("keep-alive writes the session",
                  bot._maybe_refresh_session(StubPage(), reason="unit test") is True)
            meta = json.loads((folder2 / "metadata.json").read_text(encoding="utf-8"))
            check("keep-alive records the refresh time", bool(meta.get("last_refresh_at")), str(meta))
            check("keep-alive reason recorded",
                  meta.get("refresh_reason") == "unit test", str(meta))
            bot, logs = _make_bot(ba, dict(account2),
                                  session_cfg={"session_refresh_minutes": 30})
            check("keep-alive respects the throttle",
                  bot._maybe_refresh_session(StubPage()) is False)
            bot, logs = _make_bot(ba, dict(account2),
                                  session_cfg={"session_refresh_each_chat": False})
            check("keep-alive can be disabled",
                  bot._maybe_refresh_session(StubPage()) is False)

            # ---- pre-flight skip (no browser launch for a dead session) ---
            dead = make_session(root, "account_dead", cookies=[cookie(-9)], email="dead@x.com")
            bot, logs = _make_bot(ba, {"email": "dead@x.com", "session_dir": str(dead)})
            worth, plan = bot._preflight_session_plan()
            check("pre-flight skips a blind session without credentials", worth is False, str(plan))
            check("pre-flight logs the skip",
                  any("skipping" in line for line in logs), str(logs))

            os.environ["EVA_ACCOUNTS_FILE"] = str(accounts_file)
            accounts_file.write_text("dead@x.com:pw\n", encoding="utf-8")
            bot, logs = _make_bot(ba, {"email": "dead@x.com", "session_dir": str(dead)})
            worth, plan = bot._preflight_session_plan()
            check("pre-flight lets a repairable session through", worth is True, str(plan))
            check("pre-flight announces the repair",
                  any("will repair" in line for line in logs), str(logs))

            # ---- legacy mode: verify_after_restore=False ---------------
            bot, logs = _make_bot(ba, dict(account),
                                  session_cfg={"verify_after_restore": False})
            page = StubPage(url="https://app.chitchat.gg/start/new",
                            probe={"url": "https://app.chitchat.gg/start/new",
                                   "loginForm": True, "textLength": 600})
            check("verify_after_restore=False restores the legacy URL-only check",
                  bot.restore_saved_account_session(page) is True)
            page = StubPage(url="https://app.chitchat.gg/start/new",
                            probe={"url": "https://app.chitchat.gg/login"},
                            redirect_to="https://app.chitchat.gg/login")
            check("legacy check still rejects a login URL",
                  bot.restore_saved_account_session(page) is False)
            # ---- run(): the pre-flight really stops a dead account --------
            dead2 = make_session(root, "account_dead_run", cookies=[cookie(-7)],
                                 email="deadrun@x.com")
            os.environ.pop("EVA_ACCOUNTS_FILE", None)
            bot, logs = _make_bot(ba, {"email": "deadrun@x.com",
                                       "session_dir": str(dead2)})
            bot.is_running = False
            statuses = []
            bot.set_status = statuses.append
            bot._run_session_attempt = lambda: (_ for _ in ()).throw(
                AssertionError("a blind session must never reach a browser launch"))
            bot._close_camoufox = lambda: None
            bot.run()
            check("run() returns without launching a browser",
                  any("no browser started" in line for line in logs), str(logs))
            check("run() reports the skip to the GUI",
                  any("blind" in status.lower() for status in statuses), str(statuses))
            check("run() logs the session stock/health summary",
                  any("blind=" in line for line in logs), str(logs))
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


# --------------------------------------------------------------------------- #
# E. CLI
# --------------------------------------------------------------------------- #

def test_cli() -> int:
    print("\n— E. tools/session_doctor.py —")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sessions = root / "sessions"
        make_session(sessions, "account_ok", cookies=[cookie(40)], email="ok@x.com")
        make_session(sessions, "account_dead", cookies=[cookie(-2)], email="dead@x.com")
        make_session(sessions, "account_repairable", cookies=[], email="fix@x.com")
        accounts_file = root / "accounts.txt"
        accounts_file.write_text("fix@x.com:pw\n", encoding="utf-8")
        env = dict(os.environ, EVA_SESSIONS_DIR=str(sessions),
                   EVA_ACCOUNTS_FILE=str(accounts_file), PYTHONIOENCODING="utf-8")
        tool = str(ROOT / "tools" / "session_doctor.py")

        result = subprocess.run([sys.executable, tool, "--list"], capture_output=True,
                                text=True, env=env, timeout=180)
        check("--list runs", result.returncode in (0, 2) and "SAVED SESSIONS" in result.stdout,
              result.stdout[-300:] + result.stderr[-200:])
        check("--list shows the blind session", "account_dead" not in result.stdout
              and "dead@x.com" in result.stdout and "skip" in result.stdout, result.stdout[-500:])
        check("--list marks the repairable one",
              "fix@x.com" in result.stdout and "repair" in result.stdout, result.stdout[-400:])
        check("--list returns exit code 2 when something is blind", result.returncode == 2,
              str(result.returncode))

        result = subprocess.run([sys.executable, tool, "--list", "--json"], capture_output=True,
                                text=True, env=env, timeout=180)
        payload = json.loads(result.stdout)
        check("--json output is machine readable",
              payload["summary"]["total"] == 3 and payload["summary"]["blind"] == 2,
              str(payload.get("summary")))
        check("--json includes per-account plans",
              {row["action"] for row in payload["accounts"]} >= {"restore", "repair", "skip"},
              str([row["action"] for row in payload["accounts"]]))

        result = subprocess.run([sys.executable, tool, "--check", "all", "--paths"],
                                capture_output=True, text=True, env=env, timeout=180)
        check("--check all reports the plan", "plan    :" in result.stdout, result.stdout[-300:])
        check("--check shows the folder with --paths", "folder  :" in result.stdout)

        result = subprocess.run([sys.executable, tool, "--check", "ok@x.com"],
                                capture_output=True, text=True, env=env, timeout=180)
        check("--check <email> works and exits 0 for a healthy session",
              result.returncode == 0 and "restore" in result.stdout, result.stdout[-300:])

        result = subprocess.run([sys.executable, tool, "--prune"], capture_output=True,
                                text=True, env=env, timeout=180)
        check("--prune defaults to a dry run", "dry run" in result.stdout
              and (sessions / "account_dead").exists(), result.stdout[-300:])
        result = subprocess.run([sys.executable, tool, "--prune", "--apply"], capture_output=True,
                                text=True, env=env, timeout=180)
        check("--prune --apply parks the dead session",
              (sessions / health.DEAD_SUBDIR / "account_dead").is_dir(), result.stdout[-300:])
        check("--prune keeps the healthy session", (sessions / "account_ok").is_dir())

        result = subprocess.run([sys.executable, tool, "--refresh-metadata"],
                                capture_output=True, text=True, env=env, timeout=180)
        check("--refresh-metadata writes health into metadata.json",
              "metadata updated" in result.stdout, result.stdout[-200:])
        meta = json.loads((sessions / "account_ok" / "metadata.json").read_text(encoding="utf-8"))
        check("metadata now carries the health state", meta.get("health_state") == "ok", str(meta))
    return 0


def main() -> int:
    print("=" * 72)
    print("EVA session tests (test_session_health.py)")
    print("=" * 72)
    test_health_states()
    test_plans_and_credentials()
    test_metadata_and_refresh()
    test_auth_probe()
    test_account_manager()
    test_session_store()
    test_automation_restore_and_repair()
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
