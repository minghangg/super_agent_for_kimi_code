"""L1 tests for the three leftovers of #132's review that #136 picks up.

Each class names the fault it measures, so green means "the guard exists" rather
than "the code happens to run":

1. ``GET /issues`` published ``key=""``/``issue=0`` for a key with no ``#N`` -
   the same silent-zero as ``[]`` meaning "no issues", which P1-3 refused;
2. ``launcher run --supervisor`` had no production injection point, so the
   rescue parameter reached a test and nothing else.

Both are measured through the production assembly point (``build_runner`` /
``_issue_reader``) and, for the override, on a **real** ``/healthz`` response.

Deliberately a separate file: ``test_iss132_components.py`` is already over the
300-line line and must not grow (``worker.md`` §3.1), and these tests belong to
#136 rather than to #132's counter-example set.

Run: ``python -m pytest tests/test_iss136_leftovers.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from super_agent.launcher_parts import readapi
from super_agent.store import Store

from test_iss132_components import free_port


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iss136_")
        self.root = Path(self.tmp.name)
        self.state = self.root / "data"
        self.state.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.state)
        self.addCleanup(self.tmp.cleanup)


class TestEmptyIssueKeyIsNeverPublished(Base):
    """ISS-136 §二: a key with no issue number must be named, not published empty.

    ``{"owner/demo": {}}`` used to come back as ``{"key": "", "issue": 0}``, which
    reads as "there is a ticket, numbered zero". Skipping it silently would be the
    other half of the same fault, so the skip is reported in ``reason`` and the
    good keys in the same index are untouched.
    """

    def test_a_key_without_an_issue_number_is_named_not_published_empty(self):
        (self.state / "index.json").write_text(json.dumps({
            "issues": {"Owner/Demo#7": {}, "owner/demo": {}, "not-a-key": {}}}),
            encoding="utf-8")
        from super_agent.launcher_parts import host

        refs, reason = host._issue_reader({"github": {"repo": "x/y"}}, self.store)
        self.assertEqual([ref.key for ref in refs], ["owner/demo#7"],
                         "坏键不许挡住好键，也不许被发布成空 key")
        self.assertIn("owner/demo", reason, f"跳过没有说原因：{reason!r}")
        self.assertIn("not-a-key", reason, f"跳过没有说原因：{reason!r}")
        self.assertTrue(all(ref.key and ref.issue for ref in refs),
                        "空 key 或 0 号单被发布了")

    def test_an_index_of_only_bad_keys_reads_as_a_reason(self):
        """The all-bad case must still be a reason, never an empty success list."""
        (self.state / "index.json").write_text(json.dumps({
            "issues": {"owner/demo": {}}}), encoding="utf-8")
        from super_agent.launcher_parts import host

        refs, reason = host._issue_reader({"github": {"repo": "x/y"}}, self.store)
        self.assertEqual(refs, [])
        self.assertIn("owner/demo", reason)

    def test_the_route_over_real_http_carries_the_reason(self):
        """The reason must survive the HTTP hop, not only the function call."""
        (self.state / "index.json").write_text(json.dumps({
            "issues": {"owner/demo#7": {}, "owner/demo": {}}}), encoding="utf-8")
        from super_agent.launcher_parts import host

        refs, reason = host._issue_reader({"github": {"repo": "x/y"}}, self.store)
        port = free_port()
        server, thread = readapi.serve(
            host="127.0.0.1", port=port,
            snapshot=lambda: {"running": True, "components": {}},
            issues=lambda: (refs, reason))
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/issues",
                                        timeout=5) as response:
                payload = json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual([row["key"] for row in payload["issues"]], ["owner/demo#7"])
        self.assertIn("owner/demo", payload["reason"])


class TestSupervisorOverrideHasAProductionInjectionPoint(Base):
    """ISS-136 §三 甲: ``launcher run --supervisor`` must reach the leg.

    Before this, ``build_runner`` called ``register_c2`` without ``supervisor=``
    and ``launcher run`` had no such flag, so the leg was permanently ``""`` and
    the rescue parameter existed only in tests. Measured here through
    ``build_runner`` (the production assembly point) and then on a real ``/healthz``
    response, not by reading the attribute back.
    """

    class Dummy:
        def run_once(self):
            return {"ok": True}

    def build(self, **kwargs):
        from super_agent import launcher

        cfg = {"engine": {"launcher_stop_budget_s": 5},
               "github": {"poll_interval_s": 3600},
               "readapi": {"port": 0}}
        return launcher.build_runner(cfg, self.store, engine=self.Dummy(),
                                     enable_wss=False, enable_at=False,
                                     enable_reporter=False, enable_readapi=False,
                                     enable_decide_watch=False, keep={}, **kwargs)

    def test_dash_dash_supervisor_reaches_the_leg(self):
        from super_agent.launcher_parts import host

        built = {}
        original = host.register_c2

        def spy(cfg, store, rn, **kwargs):
            built["supervisor"] = kwargs.get("supervisor")
            return original(cfg, store, rn, **kwargs)

        host.register_c2 = spy
        try:
            rn = self.build(supervisor="session_rescue")
        finally:
            host.register_c2 = original
        self.assertEqual(built.get("supervisor"), "session_rescue",
                         "build_runner 没有把 supervisor 传给 register_c2")
        self.assertEqual(rn.component("decide_watch").health.supervisor,
                         "session_rescue")

    def test_without_the_flag_the_row_says_config_chain(self):
        """``""`` must not read as "no recipient": the chain supplies one."""
        rn = self.build()
        self.assertEqual(rn.component("decide_watch").health.supervisor,
                         "config-chain")

    def test_the_flag_parses_and_is_passed_by_cmd_run(self):
        """``run --supervisor <sid>`` must parse **and** reach ``build_runner``.

        Parsing alone was the original gap: the flag existed on ``decide_watch``
        but not on ``launcher run``, so nothing on the production path could ever
        set it. ``build_runner`` is replaced with a spy that raises before the
        loop starts, so the assertion is about the wiring, not a live server.
        """
        from super_agent import launcher

        parser = launcher.build_parser()
        args = parser.parse_args(["run", "--supervisor", "session_rescue"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.supervisor, "session_rescue")
        self.assertIsNone(parser.parse_args(["run"]).supervisor)

        seen = {}

        def spy(cfg, store, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop before the loop")

        argv = ["--state-dir", str(self.state), "--source", "none",
                "run", "--no-wss", "--supervisor", "session_rescue"]
        original = launcher.build_runner
        launcher.build_runner = spy
        try:
            rc = launcher.cmd_run(parser.parse_args(argv))
        finally:
            launcher.build_runner = original
        self.assertEqual(seen.get("supervisor"), "session_rescue",
                         "cmd_run 没有把 --supervisor 传给 build_runner")
        self.assertEqual(rc, 1, "启动失败必须是可见的非零退出")

    def test_healthz_publishes_the_override(self):
        """``/healthz`` is the only way an operator sees which recipient is live."""
        rn = self.build(supervisor="session_rescue")
        port = free_port()
        server, thread = readapi.serve(host="127.0.0.1", port=port,
                                       snapshot=rn.health)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz",
                                        timeout=5) as response:
                payload = json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(payload["components"]["decide_watch"]["supervisor"],
                         "session_rescue")
        self.assertNotIn("supervisor", payload["components"]["engine"],
                         "别的腿没有收件人，不该被发布一个空字段")


if __name__ == "__main__":
    unittest.main()
