"""L1 tests for the injectable WSS event bus daemon (ISS-25 item 8).

The workbench daemon was coupled to a private repo root, private role names, a
private runtime file set and a private alarm module. The package port must take
all of those as explicit injections, must never silently reach production, and
must keep three hard requirements verbatim:

* the guardian read side keeps **three** states apart (missing / unknown /
  stale), with ``PermissionError`` ⇒ ``unknown`` and no alert;
* issue-space keys are **fully qualified** (``owner/repo#issue``) so two
  repositories with the same name can never crosstalk;
* the legacy short-key spellings stay readable **read-only**, and only when the
  lookup cannot be ambiguous.

Run: ``python -m pytest tests/test_wss_bus.py -q``
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from super_agent import wss_bus as wb

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ("re" + "ed", "lai" + "ke", "yuan" + "hai",
           "ming" + "hangg", "Users/m" + "ingh", "Users\\m" + "ingh")


class InjectionFixture(unittest.TestCase):
    """Every test gets a throw-away workspace, state root and fake production root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iss25_8_")
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.prod = self.root / "prod"
        self.ws = self.root / "ws"
        for directory in (self.state, self.prod, self.ws):
            directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.tmp.cleanup)
        sessions = self.state / "sessions.json"
        sessions.write_text("{}", encoding="utf-8")
        self.cfg = wb.configure(
            workspace_root=self.ws,
            state_root=self.state,
            production_state_root=self.prod,
            sessions_path=sessions,
            probe_session=lambda sid: (False, {}),
        )
        self.addCleanup(self._restore)
        self._saved = (wb.PATHS, wb.STATE.seq, wb.STATE.journal,
                       dict(wb.DEFAULT_CONFIG.__dict__))

    def _restore(self):
        wb.PATHS = self._saved[0]
        wb.STATE.seq = self._saved[1]
        wb.STATE.journal = self._saved[2]
        wb.DEFAULT_CONFIG = wb.WssBusConfig(**self._saved[3])


class TestNoPrivateIdentifiers(InjectionFixture):
    """The new address must contain no private name, path or project identifier."""

    def test_source_has_no_private_tokens(self):
        source = (ROOT / "super_agent" / "wss_bus.py").read_text(encoding="utf-8")
        lowered = source.lower()
        for token in PRIVATE:
            self.assertNotIn(token.lower(), lowered, f"私有词命中：{token}")

    def test_import_brings_no_workbench_module(self):
        import importlib
        import sys

        legacy_names = {"wss_bus", "kimi_direct", "kimi_paths", "kimi_api",
                        "retired_sids", "sess",
                        ("re" + "ed_alarm"), ("re" + "ed_health")}
        # The assertion is about what *this module* pulls in, so compare before
        # and after: another test in the same session may already have imported a
        # workbench module for its own reasons.
        before = {name for name in sys.modules if name in legacy_names}
        importlib.reload(wb)
        after = {name for name in sys.modules if name in legacy_names}
        self.assertEqual(sorted(after - before), [],
                         f"导入把老址模块带进了 sys.modules：{sorted(after - before)}")


class TestInjectionGate(InjectionFixture):
    """An uninjected boundary is an explicit error, never a production guess."""

    def test_missing_injections_are_named(self):
        missing = wb.missing_injections(wb.WssBusConfig())
        for name in ("workspace_root", "state_root", "production_state_root",
                     "sessions_path", "probe_session", "retired_module"):
            self.assertIn(name, missing)

    def test_uninjected_root_refuses_to_guess(self):
        wb.configure(wb.WssBusConfig())
        with self.assertRaises(RuntimeError):
            wb._workspace_root()
        with self.assertRaises(RuntimeError):
            wb._production_root()

    def test_self_check_reports_parameter_error(self):
        self.assertEqual(wb.self_check(), 2)

    def test_isolated_state_detected_by_env(self):
        wb.PATHS = wb.Paths(self.prod)
        self.assertFalse(wb.is_isolated_state())
        with mock.patch.dict(os.environ, {"WSS_BUS_STATE_DIR": str(self.state)}):
            self.assertTrue(wb.is_isolated_state())


class TestGuardianThreeStates(InjectionFixture):
    """Hard requirement ①: missing / unknown / stale stay apart, verbatim."""

    def setUp(self):
        super().setUp()
        wb.PATHS = wb.Paths(self.state)
        self.hb = self.state / wb.GUARDIAN_THRESHOLDS["monitor"][0]
        self.hb.parent.mkdir(parents=True, exist_ok=True)

    def test_absent_heartbeat_is_missing(self):
        self.assertIsNone(wb.read_heartbeat("monitor"))
        self.assertEqual(wb._guardian_summary()["monitor"]["state"], "missing")

    def test_permission_error_is_unknown_not_missing(self):
        self.hb.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
        original = Path.read_text

        def boom(path, *args, **kwargs):
            if str(path).endswith("monitor.json"):
                raise PermissionError(13, "concurrent replace")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", boom):
            raw = wb.read_heartbeat("monitor")
            self.assertEqual(raw, {"_read_error": "PermissionError"})
            summary = wb._guardian_summary()
        self.assertEqual(summary["monitor"]["state"], "unknown")
        self.assertEqual(summary["monitor"]["read_error"], "PermissionError")
        self.assertNotEqual(summary["monitor"]["state"], "missing")

    def test_bad_json_is_still_missing(self):
        self.hb.write_text("{ NOT JSON", encoding="utf-8")
        self.assertIsNone(wb.read_heartbeat("monitor"))
        summary = wb._guardian_summary()
        self.assertEqual(summary["monitor"]["state"], "missing")
        self.assertNotIn("read_error", summary["monitor"])

    def test_unknown_is_not_delivered_and_takes_no_slot(self):
        state = {"monitor": {"state": "stale", "ts": 0.0}}
        due = wb._guardian_alert_due(
            {"monitor": {"state": "unknown", "age_s": None, "threshold_s": 300,
                         "heartbeat": "hb/x.json", "writer": ""}}, state)
        self.assertEqual(due, [])
        self.assertNotIn("monitor", state)

    def test_dedup_window_and_recovery_reset(self):
        state = {}
        broken = {"a": {"state": "stale", "age_s": 999, "threshold_s": 1,
                        "heartbeat": "x", "writer": ""}}
        self.assertEqual(len(wb._guardian_alert_due(broken, state)), 1)
        self.assertEqual(wb._guardian_alert_due(broken, state), [])
        recovered = {"a": {**broken["a"], "state": "ok"}}
        self.assertEqual(wb._guardian_alert_due(recovered, state), [])
        self.assertNotIn("a", state)


class TestGuardianExtra(InjectionFixture):
    """External registration: four shapes, built-in wins, thresholds untouched."""

    def setUp(self):
        super().setUp()
        wb.PATHS = wb.Paths(self.state)
        self.extra = self.state / wb.DEFAULT_CONFIG.guardian_extra_file

    def test_absent_file_returns_builtins_only(self):
        self.assertEqual(set(wb.guardian_thresholds()), set(wb.GUARDIAN_THRESHOLDS))

    def test_bad_json_keeps_builtins(self):
        self.extra.write_text("{ NOT JSON", encoding="utf-8")
        self.assertEqual(set(wb.guardian_thresholds()), set(wb.GUARDIAN_THRESHOLDS))

    def test_non_dict_top_level_keeps_builtins(self):
        self.extra.write_text(json.dumps([1, 2]), encoding="utf-8")
        self.assertEqual(set(wb.guardian_thresholds()), set(wb.GUARDIAN_THRESHOLDS))

    def test_malformed_entry_is_skipped(self):
        self.extra.write_text(json.dumps({"io": {"heartbeat": "", "threshold_s": 10}}),
                              encoding="utf-8")
        self.assertNotIn("io", wb.guardian_thresholds())

    def test_name_clash_keeps_builtin(self):
        self.extra.write_text(json.dumps({"monitor": {"heartbeat": "hb/x.json",
                                                     "threshold_s": 1}}),
                              encoding="utf-8")
        self.assertEqual(wb.guardian_thresholds()["monitor"],
                         wb.GUARDIAN_THRESHOLDS["monitor"])

    def test_valid_registration_applies(self):
        self.extra.write_text(json.dumps({"reporter": {"heartbeat": "hb/rep.json",
                                                      "threshold_s": 120}}),
                              encoding="utf-8")
        self.assertEqual(wb.guardian_thresholds()["reporter"][:2],
                         ("hb/rep.json", 120.0))

    def test_builtin_thresholds_unchanged(self):
        self.assertEqual(wb.GUARDIAN_THRESHOLDS["delivery_watch"][1], 900.0)
        self.assertEqual(wb.GUARDIAN_THRESHOLDS["decide_watch"][1], 900.0)
        self.assertEqual(wb.GUARDIAN_THRESHOLDS["monitor"][1], 300.0)


class TestIsolationPenetration(InjectionFixture):
    """An isolated instance never writes production, and says so on /healthz."""

    def test_isolated_run_does_not_touch_production(self):
        wb.PATHS = wb.Paths(self.prod)
        prod_file = self.prod / wb.DEFAULT_CONFIG.guardian_state_file
        prod_file.write_text('{"probe": {"state": "stale", "ts": 1}}', encoding="utf-8")
        os.utime(prod_file, (1_700_000_000, 1_700_000_000))
        before = prod_file.stat().st_mtime
        with mock.patch.dict(os.environ, {"WSS_BUS_STATE_DIR": str(self.state)}):
            wb.STATE.guardian_state = {}
            result = wb.guardian_check(do_notify=False)
            ctx = wb.guardian_context()
        self.assertEqual(prod_file.stat().st_mtime, before)
        self.assertEqual(result["channel"], "isolated")
        self.assertEqual(result["notified"], [])
        self.assertEqual(ctx["state_dir"], str(self.state.resolve()))
        self.assertTrue((self.state / wb.DEFAULT_CONFIG.guardian_state_file).is_file())


class TestQualifiedIssueKeys(InjectionFixture):
    """Hard requirement ②: ``owner/repo#issue`` everywhere, no crosstalk."""

    def test_key_format(self):
        self.assertEqual(wb.issue_key("Owner/Demo", 12), "owner/demo#12")
        self.assertEqual(wb.issue_key("demo", 12), "")
        self.assertEqual(wb.delivery_key("o/demo", 12, "abc"), "o/demo#12#abc")
        self.assertEqual(wb.delivery_key("o/demo", 12), "o/demo#12")

    def test_same_name_different_owner_never_matches(self):
        db = {
            "ownerA/demo#12": {"worker": {"name": "A", "session": "session_a"}},
            "ownerB/demo#12": {"worker": {"name": "B", "session": "session_b"}},
        }
        first = wb.lookup_issue_record(db, "ownerA/demo", 12)
        second = wb.lookup_issue_record(db, "ownerB/demo", 12)
        self.assertEqual(first["worker"]["name"], "A")
        self.assertEqual(second["worker"]["name"], "B")
        self.assertIsNone(wb.lookup_issue_record(db, "ownerC/demo", 12))

    def test_short_key_readable_only_when_unambiguous(self):
        only_short = {"demo#12": {"worker": {"name": "S", "session": "session_s"}}}
        self.assertEqual(
            (wb.lookup_issue_record(only_short, "ownerA/demo", 12) or {})["worker"]["name"],
            "S")
        ambiguous = dict(only_short)
        ambiguous["ownerB/demo#12"] = {"worker": {"name": "B", "session": "session_b"}}
        self.assertIsNone(wb.lookup_issue_record(ambiguous, "ownerC/demo", 12))

    def test_bind_worker_uses_qualified_lookup(self):
        bind = self.state / wb.DEFAULT_CONFIG.bind_file
        bind.write_text(json.dumps({
            "ownerA/demo#12": {"worker": {"name": "A", "session": "session_a"}},
            "ownerB/demo#12": {"worker": {"name": "B", "session": "session_b"}},
        }), encoding="utf-8")
        self.assertEqual(wb.bind_worker("ownerA/demo", 12, bind)[0], "A")
        self.assertEqual(wb.bind_worker("ownerB/demo", 12, bind)[0], "B")
        self.assertEqual(wb.bind_worker("ownerC/demo", 12, bind)[0], "unknown")

    def test_event_payload_keeps_project_and_adds_repo_full(self):
        one = wb.qualify_event_payload({"project": "demo", "issue": 12}, repo="OwnerA/Demo")
        two = wb.qualify_event_payload({"project": "demo", "issue": 12}, repo="OwnerB/Demo")
        self.assertEqual(one["project"], "demo")
        self.assertEqual(one["repo_full"], "ownera/demo")
        self.assertEqual(one["key"], "ownera/demo#12")
        self.assertNotEqual(one["key"], two["key"])


class TestFileEvents(InjectionFixture):
    """File events are structurally WS events, first round silent, empty ≠ hit."""

    def test_first_round_is_silent_and_new_file_fires(self):
        handoff = self.ws / "asst_handoff"
        handoff.mkdir(parents=True, exist_ok=True)
        cfg = wb.WatchConfig([{"id": "h", "kind": "dir_new_file",
                               "path": "asst_handoff",
                               "event": "event.file.handoff_created"}], 0.2)
        (handoff / "worker_old.md").write_text("x\n", encoding="utf-8")
        watcher = wb.FileWatcher(cfg, self.ws)
        self.assertEqual(watcher.poll_once(), [])
        (handoff / "worker_new.md").write_text("y\n", encoding="utf-8")
        events = watcher.poll_once()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]["role"], "worker")
        self.assertNotIn("body", events[0][1])

    def test_empty_section_is_not_a_hit(self):
        pm = self.ws / "pm"
        pm.mkdir(parents=True, exist_ok=True)
        section = wb.DEFAULT_CONFIG.decide_section
        (pm / "full.md").write_text(f"{section}\n- A\n", encoding="utf-8")
        (pm / "empty.md").write_text(f"{section}\n\n## 其它\n- B\n", encoding="utf-8")
        cfg = wb.WatchConfig([{"id": "pd", "kind": "file_section_md5",
                               "path_glob": str(pm / "*.md"), "section": section,
                               "event": "e.p"}], 0.2)
        watcher = wb.FileWatcher(cfg, self.ws)
        watcher.poll_once()
        self.assertEqual(watcher.matched["pd"], ["full"])
        self.assertEqual(watcher.empty_sections["pd"], ["empty"])

    def test_bad_watch_config_keeps_previous(self):
        cfg_path = self.ws / "watch.json"
        cfg_path.write_text('{"watchers":[{"id":"a","kind":"dir_new_file",'
                            '"path":"h","event":"e.a"}],"poll_interval_s":1}',
                            encoding="utf-8")
        good = wb.WatchConfig.load(cfg_path)
        self.assertFalse(good.error)
        cfg_path.write_text("{ NOT JSON", encoding="utf-8")
        bad = wb.WatchConfig.load(cfg_path, previous=good)
        self.assertTrue(bad.error)
        self.assertEqual(bad.watchers, good.watchers)

    def test_file_event_contract(self):
        event = wb.STATE.add_event(wb.FILE_EVENT_SID, "event.file.test", {"k": 1})
        self.assertEqual(set(event), {"seq", "ts", "sid", "type", "payload", "boot_id"})
        self.assertEqual(event["sid"], "file")


class TestControlThreeState(InjectionFixture):
    """Accepted ⇒ indeterminate (never resend); unreadable journal ⇒ never retry."""

    def setUp(self):
        super().setUp()
        wb.STATE.journal = wb.Journal(self.state / "journal.jsonl")

    def test_accepted_request_is_never_resent(self):
        wb.STATE.journal.put({"request_id": "dup", "verb": "send", "sid": "s",
                              "prompt_id": "p1"})
        status, payload = wb.control({"verb": "send", "sid": "s", "text": "x",
                                      "request_id": "dup"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "indeterminate")
        self.assertIn("禁止重发", payload["err"])

    def test_unreadable_journal_never_retries(self):
        wb.STATE.journal.ok = False
        status, payload = wb.control({"verb": "send", "sid": "s", "text": "x",
                                      "request_id": "fresh"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "indeterminate")

    def test_unsubscribe_survives_broken_journal(self):
        wb.STATE.journal.ok = False
        status, payload = wb.control({"verb": "unsubscribe", "sid": "session_x"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "accepted")


if __name__ == "__main__":
    unittest.main()
