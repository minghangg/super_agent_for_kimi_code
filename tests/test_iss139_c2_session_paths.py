"""#139: the C2 leg must read the session files the CLIs actually write.

The live failure (ruling ``7207170455``): ``paths.state_dir``
(``…/state``) and ``paths.engine_state_dir`` (``…/new_workbench/data``) are
deliberately different roots, and the launcher passed only ``state_root`` to
``DecideWatchConfig``. ``decide_watch._path()`` therefore fell back to the
engine artifact root, where ``ledger.jsonl`` exists but is empty (0 bytes,
never written) and ``sessions.json`` / ``issue_sessions.json`` do not exist at
all. The scan reported ``offset=0 pending=0`` for hours while the real ledger
grew, and no PM was ever resolved.

Three tests, matching the ruling's §3.2 - each pins a different half:

1. **the paths point at the session root**, taken from
   ``session_events.resolve_session_root`` - the same function ``issue_cli``
   and ``ctx_gate`` write through - and not at ``state_root``;
2. **end to end**: a fixture ledger row plus ``sessions.json`` /
   ``issue_sessions.json`` under the session root makes one ``delivery_tick``
   resolve the project's PM and really send ("已发");
3. **the counter-example**: point the three paths back at ``state_root`` and
   the end-to-end case must go red. Without this one, test 2 would also pass in
   a world where the paths never mattered.

Run: ``python -m pytest tests/test_iss139_c2_session_paths.py -q``
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from super_agent import decide_watch as dw
from super_agent.launcher_parts import c2, seams

REPO = "owner/lwa"
ISSUE = 138

TITLE = "[wk-iss139-fixture]-[完工]-[pm-lwa]-[lwa]-[7000000001]"


class _Bus:
    """The bus seam ``c2.config`` uses: a port and a post sink."""

    PORT = 58800

    def __init__(self) -> None:
        self.posts: list[tuple] = []

    def bus_post(self, path, body, port=0):
        self.posts.append((path, body))
        return {}

    def guardian_thresholds(self) -> dict:
        return {"delivery_watch": ("heartbeat/delivery.json", 900.0, "x"),
                "decide_watch": ("heartbeat/decide.json", 900.0, "y"),
                "supervisor_notice": ("heartbeat/supervisor_notice.json", 900.0, "z")}


class _Store:
    """Only ``root`` is read by ``c2.config``."""

    def __init__(self, root: Path) -> None:
        self.root = root


class _Fixture(unittest.TestCase):
    """Two roots that differ, exactly like the deployment in #139."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="iss139_")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(lambda: dw.configure(dw.DecideWatchConfig()))
        self.addCleanup(self._pop_env)
        self.base = Path(tmp.name)
        #: session side: what the CLIs write. On the live box this is
        #: ``<workbench>/state``, reached through ``engine.session_state_dir``
        #: (which ``paths.state_dir`` supplies).
        self.session_root = self.base / "state"
        #: engine side: the leg's own artifacts, and also the value of
        #: ``state_dir``. On the live box ``paths.engine_state_dir`` overwrites
        #: ``state_dir`` with ``<workbench>/new_workbench/data`` while
        #: ``paths.state_dir`` keeps feeding ``session_state_dir`` -- so the two
        #: keys genuinely differ (#139 fact ③). The fixture mirrors that split;
        #: giving them the same value is what let a "compose it from
        #: ``state_dir``" implementation pass the whole file (QA P1-1).
        self.engine_root = self.base / "new_workbench" / "data"
        self.session_root.mkdir(parents=True)
        self.engine_root.mkdir(parents=True)
        self.cfg = {"state_dir": str(self.engine_root),
                    "engine": {"session_state_dir": str(self.session_root),
                               "decide_watch_wait_s": 0.0},
                    "workspace_root": str(self.base)}
        self.bus = _Bus()
        self.store = _Store(self.engine_root)

    def _pop_env(self) -> None:
        for name in ("DELIVERY_FIXTURE", "DELIVERY_SEND_SINK", "DELIVERY_ALLOW_SEND"):
            os.environ.pop(name, None)
        dw.DELIVERY_SINK = None

    # ------------------------------------------------------------- fixtures
    def write_session_files(self) -> None:
        """A PM in ``sessions.json`` and a worker binding; ledger left empty."""
        (self.session_root / "sessions.json").write_text(json.dumps({
            "pm-lwa-20261125-aaaaaaa1": {"id": "session_pm_lwa_fixture"},
            "supervisor": {"id": "session_supervisor_fixture"},
        }, ensure_ascii=False), encoding="utf-8")
        (self.session_root / "issue_sessions.json").write_text(json.dumps({
            f"lwa#{ISSUE}": {"worker": {"name": "wk-iss139-fixture",
                                        "session": "session_worker_fixture"}},
        }, ensure_ascii=False), encoding="utf-8")
        (self.session_root / "ledger.jsonl").write_text("", encoding="utf-8")

    def append_delivery_row(self) -> None:
        """One ``[WK]`` 完工 row, in the shape ``issue_cli`` writes."""
        row = {"t": "2026-11-27T09:00:00", "role": "worker", "act": "comment",
               "repo": REPO, "issue": ISSUE, "ok": True,
               "note": f"cid:7000000001;title:{TITLE}"}
        with (self.session_root / "ledger.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def configure_from_c2(self) -> None:
        config_obj, _waits, _port = c2.config(self.cfg, self.store, bus=self.bus)
        dw.configure(config_obj)

    def run_one_tick(self, now: float = 1_800_000_000.0) -> dict:
        dw.DELIVERY_SINK = []
        return dw.delivery_tick(dry=False, now=now)

    def establish_baseline(self) -> dict:
        """Round 1 only records the offset; ``scan_deliveries`` returns early.

        ``decide_watch.py:2478-2492``: a state file without
        ``baseline_established`` rebuilds at the current size and consumes **no**
        row. That is production's first-run behaviour, so the delivery row is
        appended *after* this round - which is also how a live worker arrives.
        """
        return self.run_one_tick(now=1_800_000_000.0)


# ---------------------------------------------------------------- test 1
class PathsComeFromTheSessionRoot(_Fixture):
    """Ruling §3.2 ①: the three files resolve under ``session_root``."""

    def test_three_paths_are_under_the_resolved_session_root(self):
        from super_agent import session_events

        config_obj, _waits, _port = c2.config(self.cfg, self.store, bus=self.bus)
        root = session_events.resolve_session_root(self.cfg)

        self.assertEqual(root, self.session_root.resolve())
        self.assertEqual(config_obj.sessions_path, root / "sessions.json")
        self.assertEqual(config_obj.bind_path, root / "issue_sessions.json")
        self.assertEqual(config_obj.ledger_path, root / "ledger.jsonl")
        self.assertEqual(config_obj.state_root, self.engine_root.resolve())

    def test_no_session_path_falls_back_to_the_engine_root(self):
        config_obj, _waits, _port = c2.config(self.cfg, self.store, bus=self.bus)
        for name in ("sessions_path", "bind_path", "ledger_path"):
            path = Path(getattr(config_obj, name))
            self.assertNotEqual(path.parent, Path(config_obj.state_root), name)
            self.assertEqual(path.parent, self.session_root.resolve(), name)

    def test_the_leg_own_artifacts_stay_on_the_engine_root(self):
        """The other half of the split: the leg writes nothing into the CLIs' root."""
        config_obj, _waits, _port = c2.config(self.cfg, self.store, bus=self.bus)
        dw.configure(config_obj)
        engine = Path(config_obj.state_root)
        self.assertEqual(Path(config_obj.lock_path).parent, engine)
        self.assertEqual(Path(config_obj.liveness_state_path).parent, engine)
        self.assertEqual(dw._delivery_paths()["state"].parent, engine)
        self.assertEqual(dw._delivery_paths()["log"].parent, engine)

    def test_resolution_follows_the_config_not_a_literal(self):
        """A different ``state_dir`` moves the three paths with it."""
        other = self.base / "other-state"
        other.mkdir()
        cfg = {"state_dir": str(other),
               "engine": {"session_state_dir": str(other)},
               "workspace_root": str(self.base)}
        config_obj, _waits, _port = c2.config(cfg, self.store, bus=self.bus)
        self.assertEqual(Path(config_obj.ledger_path).parent, other.resolve())

    def test_session_paths_follow_session_state_dir_not_state_dir(self):
        """**The discriminating case** (QA P1-1, ruling §3.1 "不许自己拼路径").

        ``state_dir`` and ``engine.session_state_dir`` differ here, exactly as
        they do on the live box. A "compose it from ``cfg['state_dir']``"
        implementation resolves to the engine root and fails this test; only
        routing through ``session_events.resolve_session_root`` passes.

        Without the split this file could not tell the two implementations
        apart - measured: a self-composed version passed all nine tests.
        """
        config_obj, _waits, _port = c2.config(self.cfg, self.store, bus=self.bus)

        self.assertNotEqual(Path(self.cfg["state_dir"]),
                            Path(self.cfg["engine"]["session_state_dir"]),
                            "夹具把两个 root 设成同值 ⇒ 本用例失去判别力")
        for name in ("ledger_path", "sessions_path", "bind_path"):
            self.assertEqual(Path(getattr(config_obj, name)).parent,
                             self.session_root.resolve(), name)
            self.assertNotEqual(Path(getattr(config_obj, name)).parent,
                                Path(self.cfg["state_dir"]).resolve(), name)

    def test_a_selfcomposed_root_is_refused_by_this_file(self):
        """The counter-example for the case above: composing ``state_dir`` goes red.

        This is the same patch QA applied to prove the gap existed. It is kept
        here so the guard cannot be removed by accident: if someone replaces the
        resolution chain with a composition, both this and the case above fail.
        """
        original = seams.session_files

        def selfcomposed(cfg: dict) -> tuple[dict, str]:
            root = Path(str(cfg.get("state_dir")))     # bypasses the chain
            return {"ledger": root / "ledger.jsonl",
                    "sessions": root / "sessions.json",
                    "bind": root / "issue_sessions.json"}, ""

        seams.session_files = selfcomposed
        self.addCleanup(setattr, seams, "session_files", original)

        config_obj, _waits, _port = c2.config(self.cfg, self.store, bus=self.bus)
        self.assertEqual(Path(config_obj.ledger_path).parent,
                         self.engine_root.resolve(),
                         "自拼实现没有落到 engine root ⇒ 上面的判别用例不成立")


# ---------------------------------------------------------------- P0 regressions
class AssemblyNeverRaises(_Fixture):
    """QA P0-1: an unconfigured session stream must not kill ``build_runner``."""

    def test_unconfigured_session_root_does_not_raise(self):
        from super_agent import session_events

        cfg = {"engine": {"launcher_stop_budget_s": 5}, "workspace_root": str(self.base)}
        with self.assertRaises(session_events.SessionStreamNotConfigured):
            session_events.resolve_session_root(cfg)     # the raw call still raises

        config_obj, _waits, _port = c2.config(cfg, self.store, bus=self.bus)
        engine = self.engine_root.resolve()
        self.assertEqual(Path(config_obj.ledger_path), engine / "ledger.jsonl")
        self.assertEqual(Path(config_obj.sessions_path), engine / "sessions.json")
        self.assertEqual(Path(config_obj.bind_path), engine / "issue_sessions.json")

    def test_the_degradation_is_named_not_silent(self):
        cfg = {"engine": {"launcher_stop_budget_s": 5}, "workspace_root": str(self.base)}
        problem = seams.session_problem(cfg)
        self.assertIn("no session stream configured", problem)
        self.assertEqual(seams.session_problem(self.cfg), "",
                         "配置齐全时不应报缺口")

    def test_the_health_row_carries_the_reason_and_stays_healthy(self):
        """The row must show the gap **without** claiming the leg is broken."""
        from super_agent import launcher
        from super_agent.store import Store

        store = Store(self.engine_root)
        cfg = {"engine": {"launcher_stop_budget_s": 5},
               "github": {"poll_interval_s": 3600}, "readapi": {"port": 0},
               "workspace_root": str(self.base)}
        rn = launcher.build_runner(cfg, store, engine=None, enable_wss=False,
                                   enable_decide_watch=True, enable_at=False,
                                   enable_reporter=False, enable_readapi=False,
                                   keep={})
        row = rn.component("decide_watch").health.as_dict()
        self.assertTrue(row["enabled"], "降级不该把腿关掉")
        self.assertEqual(row["last_error"], "", "腿是健康的，不许报失败")
        self.assertIn("no session stream configured", row["session_problem"])

    def test_a_configured_run_carries_no_problem_field(self):
        """Configured ⇒ the field is absent, not empty: other rows must not gain it."""
        from super_agent import launcher
        from super_agent.store import Store

        self.assertEqual(seams.session_problem(self.cfg), "")
        store = Store(self.engine_root)
        rn = launcher.build_runner(self.cfg, store, engine=None, enable_wss=False,
                                   enable_decide_watch=True, enable_at=False,
                                   enable_reporter=False, enable_readapi=False,
                                   keep={})
        row = rn.component("decide_watch").health.as_dict()
        self.assertNotIn("session_problem", row,
                         "配置齐全时仍输出该字段 ⇒ 每行都会假装有这个缺口")


class StateDirIsolation(_Fixture):
    """QA P0-2: ``--state-dir`` must isolate the session root too."""

    def test_state_dir_moves_the_session_root(self):
        from super_agent import cli

        iso = self.base / "isolated" / "data"
        iso.parent.mkdir(parents=True, exist_ok=True)
        cfg, store = cli._build(_Args(state_dir=str(iso)))

        self.assertEqual(store.root.resolve(), iso.resolve())
        self.assertEqual(Path(cfg["engine"]["session_state_dir"]).resolve(),
                         iso.resolve(),
                         "--state-dir 没带着 session 根一起走 ⇒ 隔离跑会读生产")

    def test_without_the_flag_the_session_root_is_untouched(self):
        """The flag is the only thing that moves it: no flag ⇒ the chain decides.

        Deliberately does **not** assert that the two keys differ - that is a
        property of the deployment file, and the suite runs with
        ``SAFKC_AGENT_CONFIG`` pointed at a path that does not exist. What must
        hold everywhere is the weaker, real invariant: without the flag the
        session root keeps whatever the config chain answered.
        """
        from super_agent import agent_config, cli, session_events
        from super_agent import config as config_mod

        before = session_events.resolve_session_root(
            config_mod.load(overrides=agent_config.overrides()))
        cfg, _store = cli._build(_Args(state_dir=None))
        self.assertEqual(Path(cfg["engine"]["session_state_dir"]).resolve(),
                         Path(before).resolve(),
                         "没给 --state-dir 却改动了 session 根")


class _Args:
    """The flags ``cli._build`` reads (mirrors ``launcher_cli.Args``)."""

    def __init__(self, *, state_dir=None):
        self.config = None
        self.state_dir = state_dir
        self.mode = None
        self.role = None
        self.source = "none"
        self.allowlist = None
        self.session_api = None
        self.accept_empty_index = False
        self.allow_empty_cursor = False


# ---------------------------------------------------------------- test 2
class OneTickReachesTheProjectPm(_Fixture):
    """Ruling §3.2 ②: fixture session files + one tick ⇒ a real "已发"."""

    def setUp(self) -> None:
        super().setUp()
        self.write_session_files()

    def test_tick_resolves_the_pm_and_sends(self):
        self.configure_from_c2()
        self.establish_baseline()                # round 1: offset only
        self.append_delivery_row()               # a worker delivers
        out = self.run_one_tick(now=1_800_000_100.0)

        self.assertEqual(dw.resolve_recipients(REPO),
                         [("pm-lwa-20261125-aaaaaaa1", "session_pm_lwa_fixture")],
                         "收单腿解析不出项目 PM")
        self.assertEqual(out["sent"], 1, f"本轮没有发出交付：{out}")
        self.assertEqual([rec["sid"] for rec in dw.DELIVERY_SINK],
                         ["session_pm_lwa_fixture"], "收件人不是项目 PM")
        self.assertIn("【收单】", dw.DELIVERY_SINK[0]["text"])

    def test_the_scan_offset_advances_past_the_row(self):
        self.configure_from_c2()
        self.establish_baseline()
        self.append_delivery_row()
        out = self.run_one_tick(now=1_800_000_100.0)
        size = (self.session_root / "ledger.jsonl").stat().st_size
        self.assertEqual(out["offset"], size, "offset 没走到台账末尾")
        self.assertGreater(out["offset"], 0, "offset 仍为 0 ⇒ 扫的是空台账")

    def test_the_send_is_logged_as_sent(self):
        """The "已发" line only exists on the real send branch.

        ``delivery_notify`` short-circuits when the in-memory sink is set
        (``decide_watch.py:2031-2035``), so a sink-based test can never see the
        log line. Here the sink is left off and ``_send`` is injected, which is
        the same path a production send takes minus the subprocess.
        """
        import subprocess
        from unittest import mock

        self.configure_from_c2()
        self.establish_baseline()
        self.append_delivery_row()
        sent: list[tuple[str, str]] = []

        def fake_send(sid, text, timeout=60.0):
            sent.append((sid, text))
            return subprocess.CompletedProcess([], 0, "", "")

        dw.DELIVERY_SINK = None            # not run_one_tick: it installs the sink
        with mock.patch.object(dw, "_send", side_effect=fake_send):
            out = dw.delivery_tick(dry=False, now=1_800_000_100.0)
        dw.DELIVERY_SINK = None

        self.assertEqual(out["sent"], 1, f"没有走真实发送分支：{out}")
        self.assertEqual([sid for sid, _ in sent], ["session_pm_lwa_fixture"])
        log = self.engine_root / "delivery_watch.log"
        text = log.read_text(encoding="utf-8") if log.is_file() else ""
        self.assertIn("已发", text, f"日志里没有「已发」：{text[-400:]}")


# ---------------------------------------------------------------- test 3
class PointingBackAtTheEngineRootMustGoRed(_Fixture):
    """Ruling §3.2 ③: re-introduce the defect and this file goes red.

    This is the test that keeps test 2 honest. It patches
    :func:`seams.session_files` back to the broken shape - every path under
    ``state_root`` - and asserts the end-to-end case fails the same way the live
    launcher did: the ledger it scans is the empty one, and no PM is found.
    """

    def setUp(self) -> None:
        super().setUp()
        self.write_session_files()

    def _broken(self, cfg: dict) -> tuple[dict, str]:
        root = self.engine_root
        return {"ledger": root / "ledger.jsonl",
                "sessions": root / "sessions.json",
                "bind": root / "issue_sessions.json"}, ""

    def test_broken_paths_send_nothing_and_resolve_no_pm(self):
        original = seams.session_files
        seams.session_files = self._broken
        self.addCleanup(setattr, seams, "session_files", original)

        self.configure_from_c2()
        self.establish_baseline()
        self.append_delivery_row()
        out = self.run_one_tick(now=1_800_000_100.0)

        self.assertEqual(dw.resolve_recipients(REPO), [],
                         "旧路径居然解析出了 PM——测试没锁住缺陷")
        self.assertEqual(out["sent"], 0, f"旧路径仍发出了交付：{out}")
        self.assertEqual(dw.DELIVERY_SINK, [], "旧路径仍投递了消息")

    def test_the_broken_shape_is_what_the_leg_used_to_get(self):
        """The fallback really is ``state_root/filename`` - pinned, not assumed."""
        config_obj = dw.DecideWatchConfig(state_root=self.engine_root)
        dw.configure(config_obj)
        paths = dw._delivery_paths()
        self.assertEqual(paths["ledger"], self.engine_root / "ledger.jsonl")
        self.assertEqual(paths["sessions"], self.engine_root / "sessions.json")
        self.assertEqual(paths["bind"], self.engine_root / "issue_sessions.json")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
