"""L1 tests for the engine-native worker liveness watch (ISS-69 / E61).

A bound worker or QA session that is not busy for N consecutive probes, with no
new comment or verdict on its thread in between, gets exactly one exception
frame to the supervisor. The ticket names five behaviours and each has a test
here:

* a dead worker reports **once**, and further idle rounds stay silent;
* a normal delivery (busy, then a delivery comment) reports **zero** frames;
* a long task (busy the whole time) reports **zero** frames;
* restore plus a re-death reports a second frame (the dedupe key resets);
* a new comment during the N rounds resets the counter and reports nothing.

The probe is injected, so every test is offline. Nothing here touches a
production file.

Run: ``python -m pytest tests/test_iss69_liveness.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from super_agent import decide_watch as dw

THREAD = "owner/repo#69"
DEAD_SID = "session_dead_fixture"
LIVE_SID = "session_live_fixture"
SUPERVISOR_SID = "session_supervisor_fixture"


class LivenessFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iss69_")
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: dw.configure(dw.DecideWatchConfig()))

        self.write_bindings({"worker": DEAD_SID})
        (self.state / "sessions.json").write_text(json.dumps({
            "supervisor": {"id": SUPERVISOR_SID},
        }), encoding="utf-8")
        (self.state / "ledger.jsonl").write_text("", encoding="utf-8")

        self.busy = {DEAD_SID: False, LIVE_SID: False}
        self.pending = {DEAD_SID: None, LIVE_SID: None}
        self.archived = {DEAD_SID: False, LIVE_SID: False}
        self.injections: list = []
        self.probe_error: Exception | None = None
        dw.configure(dw.DecideWatchConfig(
            state_root=self.state,
            workspace_root=self.root,
            sessions_path=self.state / "sessions.json",
            bind_path=self.state / "issue_sessions.json",
            ledger_path=self.state / "ledger.jsonl",
            liveness_state_path=self.state / "worker_liveness_state.json",
            probe_session=self.probe,
            inject_event=lambda path, body, port=0: self.injections.append((path, body)),
            liveness={"worker_liveness_enabled": True,
                      "worker_liveness_rounds": 2,
                      "worker_liveness_interval_s": 60},
        ))

    # -- helpers ---------------------------------------------------------
    def probe(self, sid):
        if self.probe_error is not None:
            raise self.probe_error
        if sid not in self.busy:
            return False, "none"
        return True, {"sid": sid, "busy": self.busy[sid],
                      "archived": self.archived.get(sid, False),
                      "pending_interaction": self.pending.get(sid)}

    def write_bindings(self, roles: dict, thread: str = THREAD) -> None:
        (self.state / "issue_sessions.json").write_text(json.dumps({
            thread: {role: {"bound": "2026-11-14T00:00:00", "name": f"wk-{role}",
                            "session": sid}
                     for role, sid in roles.items()},
        }), encoding="utf-8")

    def comment(self, issue=69, ok=True, repo="owner/repo", role="worker") -> None:
        """Append one ledger comment row for THREAD (the thread moved)."""
        row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "role": role,
               "act": "comment", "repo": repo,
               "issue": issue, "ok": ok, "note": "cid:1"}
        with (self.state / "ledger.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def frames(self) -> list:
        return [body for path, body in self.injections
                if body.get("event") == dw.WORKER_LIVENESS_EVENT
                and (body.get("payload") or {}).get("kind") == dw.LIVENESS_KIND]

    def tick(self, at: float | None = None, supervisor: str = "") -> int:
        return dw.worker_liveness_tick(now=at if at is not None else time.time(),
                                       supervisor=supervisor)

    def tick_dry(self) -> int:
        return dw.worker_liveness_tick(dry=True)


class Positive(LivenessFixture):
    def test_dead_worker_reports_exactly_once(self):
        self.assertEqual(self.tick(), 0, "N=2：第一轮不忙只是计数")
        self.assertEqual(self.tick(), 1, "第二轮不忙即投一帧")
        self.assertEqual(self.tick(), 0, "同一死亡态不重发")
        bodies = self.frames()
        self.assertEqual(len(bodies), 1)
        payload = bodies[0]["payload"]
        self.assertEqual(payload["sid"], SUPERVISOR_SID, "收件只 supervisor")
        self.assertEqual(payload["thread"], THREAD)
        self.assertEqual(payload["dead_sid"], DEAD_SID)
        self.assertEqual(payload["issue"], 69)
        self.assertIn("waited_s", payload)
        self.assertIn("probed_at", payload)
        self.assertIn(DEAD_SID, payload["text"])

    def test_single_idle_round_is_not_enough(self):
        self.assertEqual(self.tick(), 0, "N=2：第一轮不忙不报")
        self.assertEqual(self.frames(), [])

    def test_restore_then_redeath_reports_again(self):
        self.tick()
        self.tick()
        self.assertEqual(len(self.frames()), 1, "第一次死亡一帧")
        self.busy[DEAD_SID] = True            # 复活
        self.assertEqual(self.tick(), 0)
        self.busy[DEAD_SID] = False           # 再死
        self.assertEqual(self.tick(), 0, "重置后重新从第 1 轮数")
        self.assertEqual(self.tick(), 1, "复活后重置，再死可再报一次")
        self.assertEqual(len(self.frames()), 2, "两条帧，不合并、不吞")
        self.assertTrue(self.frames()[1]["payload"]["probed_at"])

    def test_new_comment_resets_the_counter(self):
        self.assertEqual(self.tick(), 0)      # 第一轮不忙
        self.comment()                        # N 期间来新评论
        self.assertEqual(self.tick(), 0, "计数清零 ⇒ 不报")
        self.assertEqual(self.frames(), [])
        for _ in range(3):
            self.assertEqual(self.tick(), 0, "已交付：保持空闲是正常收尾，不报")
        self.assertEqual(self.frames(), [])

    def test_busy_rearms_after_a_delivery(self):
        self.comment()                        # 交付
        self.assertEqual(self.tick(), 0)
        self.busy[DEAD_SID] = True            # 下一单开工
        self.assertEqual(self.tick(), 0)
        self.busy[DEAD_SID] = False           # 又死了
        self.assertEqual(self.tick(), 0, "重新武装后从第 1 轮数")
        self.assertEqual(self.tick(), 1, "重新忙过 ⇒ 再死可报")
        self.assertEqual(len(self.frames()), 1)

    def test_unknown_probe_neither_counts_nor_reports(self):
        self.probe_error = OSError("network blip")
        for _ in range(4):
            self.assertEqual(self.tick(), 0, "看不清 ≠ 死了")
        self.assertEqual(self.frames(), [])

    def test_unbound_sid_is_forgotten(self):
        self.tick()
        self.write_bindings({"worker": LIVE_SID})   # 换绑
        self.tick()
        st = json.loads((self.state / "worker_liveness_state.json").read_text(encoding="utf-8"))
        self.assertNotIn(DEAD_SID, st["workers"], "解绑后不再看护")
        self.assertIn(LIVE_SID, st["workers"])

    def test_disabled_by_default(self):
        dw.configure(liveness={"worker_liveness_enabled": False})
        for _ in range(3):
            self.assertEqual(self.tick(), 0)
        self.assertEqual(self.frames(), [])


class Negative(LivenessFixture):
    def test_normal_delivery_reports_zero(self):
        self.busy[DEAD_SID] = True            # 忙：在干活
        self.tick()
        self.busy[DEAD_SID] = False           # 交付
        self.comment()                        # 交付评论
        self.tick()
        self.tick()
        self.assertEqual(self.frames(), [], "正常交付零帧")

    def test_long_task_reports_zero(self):
        self.busy[DEAD_SID] = True
        for _ in range(11):                   # ≥10 分钟，busy=True 持续
            self.assertEqual(self.tick(), 0)
        self.assertEqual(self.frames(), [], "长任务零帧")

    def test_second_tick_does_not_duplicate(self):
        self.tick()
        self.tick()
        before = len(self.frames())
        self.assertEqual(before, 1)
        for _ in range(5):
            self.tick()
        self.assertEqual(len(self.frames()), before, "去重：重复巡检不追加帧")

    def test_project_key_delivery_reports_zero(self):
        """生产形状：`gh_issue.bind` 写的键是 `项目名#N`，台账 `repo` 是 `owner/repo`。

        两者拼不出同一个字符串，按全键比对时清零永不生效 ⇒ 交付后空闲必被误报。
        这条测试就是那次的回归：绑定键与台账 repo 不同源也必须能对上。
        """
        self.write_bindings({"worker": DEAD_SID}, thread="example-project#69")
        self.busy[DEAD_SID] = True            # 干活中
        self.tick()
        self.busy[DEAD_SID] = False           # 交付
        self.comment(repo="acme/example-project")
        for _ in range(4):
            self.assertEqual(self.tick(), 0, "交付后空闲是正常收尾")
        self.assertEqual(self.frames(), [], "生产形状：正常交付零帧")

    def test_archived_session_still_reports(self):
        """归档是任务书认可的「真杀」（`sess.py stop` 或 API archive）。

        归档的会话当然不忙，但它**必须**计入「连续不忙」——否则「真杀一个 bound
        worker ⇒ 恰好一帧」这条验收就不成立。关单归档要先解绑，别指望这条腿放过。
        """
        self.archived[DEAD_SID] = True
        self.assertEqual(self.tick(), 0, "N=2：第一轮不忙不报")
        self.assertEqual(self.tick(), 1, "归档的 bound worker 被杀 ⇒ 报")
        self.assertEqual(len(self.frames()), 1, "恰好一帧")

    def test_old_comment_does_not_mask_a_later_death(self):
        """首轮播种 settled 只豁免「活着且空闲」，不能盖住明确的死亡信号。

        反例：thread 三天前交过活，会话之后被杀。若播种时把 `exists=False`／
        `archived` 一并当正常收尾，这个会话会**永远不报**——漏报比误报更致命。
        """
        # 先造一条历史交付评论（首轮播种会读到它）
        self.comment()
        dw.configure(liveness={"worker_liveness_enabled": True,
                               "worker_liveness_rounds": 2,
                               "worker_liveness_interval_s": 60})
        (self.state / "worker_liveness_state.json").unlink(missing_ok=True)
        self.busy[DEAD_SID] = False
        self.archived[DEAD_SID] = True        # 历史评论之后被杀
        self.assertEqual(self.tick(), 0, "第一轮不忙不报")
        self.assertEqual(self.tick(), 1, "历史评论不该盖住后来的死亡")
        self.assertEqual(len(self.frames()), 1)


class ReworkP0Recipient(LivenessFixture):
    """P0-① 收件人解析：按配置链取，不认死 `supervisor` 一个键。"""

    def test_configured_key_resolves_when_supervisor_is_null(self):
        """现网形状：`supervisor` 键存在但值是 null，真名在另一个键上。

        上一版在这里返回空串 ⇒ 帧投给 ""，静默丢失。键名由配置给出，
        本文件不带任何私有身份字面量。
        """
        (self.state / "sessions.json").write_text(json.dumps({
            "supervisor": None,
            "lead": {"id": "session_lead_live"},
        }), encoding="utf-8")
        dw.configure(supervisor_keys=("supervisor", "lead"))
        self.assertEqual(dw.supervisor_sid(""), "session_lead_live",
                         "null 不能当命中，要落到配置里给的备用键")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.tick(), 1, "解析到收件人才投得出")
        self.assertEqual(self.frames()[0]["payload"]["sid"], "session_lead_live")

    def test_configured_sid_wins_over_sessions_json(self):
        cfg = self.root / "engine.json"
        cfg.write_text(json.dumps({"engine": {
            "supervisor_sid": "session_from_config"}}), encoding="utf-8")
        dw.configure(config_path=cfg)
        self.assertEqual(dw.supervisor_sid(""), "session_from_config")

    def test_explicit_argument_wins(self):
        self.assertEqual(dw.supervisor_sid("session_explicit"), "session_explicit")

    def test_missing_recipient_is_loud_and_sends_nothing(self):
        """三处都没有 ⇒ 大声：WARN ＋ 状态留痕，且**不投**给空串。"""
        (self.state / "sessions.json").write_text(json.dumps({}), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.tick(), 0)
            self.assertEqual(self.tick(), 0)
        self.assertEqual(self.frames(), [], "收件人为空时一条帧都不许投")
        self.assertIn("活性网无收件人", buf.getvalue(), "必须大声报，不能静默")
        st = json.loads((self.state / "worker_liveness_state.json").read_text(encoding="utf-8"))
        self.assertIn("recipient_missing_at", st, "状态里要留痕，事后可查")


class ReworkP0Granularity(LivenessFixture):
    """P0-② 解除武装的粒度是 `(thread, role)`，不是 thread。"""

    def test_pm_comment_does_not_reset_the_worker(self):
        """本轮核心反例：同 thread 先有 PM 派单书，worker 连续不忙仍要能报。"""
        self.write_bindings({"worker": DEAD_SID, "qa": "session_qa_fixture"})
        self.comment(role="pm")               # PM 贴派单书（同 thread，同 issue）
        self.assertEqual(self.tick(), 0, "PM 评论不该把 worker 的计数清零")
        self.assertNotIn("settled", json.loads(
            (self.state / "worker_liveness_state.json").read_text(encoding="utf-8")
        )["workers"][DEAD_SID], "PM 评论不该让 worker 进 settled")
        self.assertEqual(self.tick(), 2, "worker 与 qa 各攒满 N 轮，各报一帧")
        dead = sorted(f["payload"]["dead_sid"] for f in self.frames())
        self.assertEqual(dead, sorted([DEAD_SID, "session_qa_fixture"]),
                         "被 PM 评论干扰的只是计数，不是该报的人")
        self.assertTrue(all(f["payload"]["role"] in ("worker", "qa") for f in self.frames()))

    def test_own_delivery_still_resets(self):
        """该 sid 自己的交付评论仍要清零＋解除武装（正常收尾不误报，这条不能丢）。"""
        self.busy[DEAD_SID] = True
        self.tick()
        self.busy[DEAD_SID] = False
        self.comment(role="worker")
        for _ in range(3):
            self.assertEqual(self.tick(), 0, "自己交付后空闲是正常收尾")
        self.assertEqual(self.frames(), [])

    def test_other_role_never_seeds_settled(self):
        """首轮播种也要按角色：PM 的历史评论不能替 worker 播种。"""
        self.write_bindings({"worker": DEAD_SID})
        self.comment(role="pm")
        (self.state / "worker_liveness_state.json").unlink(missing_ok=True)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.tick(), 1, "PM 的历史评论不该盖住 worker 的死")


class ReworkP1Config(LivenessFixture):
    """P1-③ 配置读 `config.load()`：配了就要生效。"""

    def test_sample_style_config_enables_the_leg(self):
        cfg = self.root / "engine.json"
        cfg.write_text(json.dumps({"engine": {
            "worker_liveness_enabled": True,
            "worker_liveness_rounds": 3,
            "worker_liveness_interval_s": 45,
        }}), encoding="utf-8")
        dw.configure(liveness=None, config_path=cfg)
        got = dw._liveness_settings()
        self.assertTrue(got["enabled"], "配了 worker_liveness_enabled=true 就必须生效")
        self.assertEqual(got["rounds"], 3)
        self.assertEqual(got["interval_s"], 45.0)

    def test_defaults_keep_the_leg_off(self):
        dw.configure(liveness=None, config_path=None)
        self.assertFalse(dw._liveness_settings()["enabled"], "缺省必须关")

    def test_broken_config_falls_back_instead_of_crashing(self):
        cfg = self.root / "engine.json"
        cfg.write_text("{ not json", encoding="utf-8")
        dw.configure(liveness=None, config_path=cfg)
        self.assertFalse(dw._liveness_settings()["enabled"], "坏配置降级成缺省，不炸腿")


class ReworkP1CorruptState(LivenessFixture):
    """P1-④ 坏 state 是「数据坏」，不是「腿坏」：留证＋继续，不退出。"""

    def test_corrupt_state_is_quarantined_and_the_leg_continues(self):
        p = self.state / "worker_liveness_state.json"
        p.write_text("{ this is not json", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.tick(), 0, "坏文件不该抛出")
            self.assertEqual(self.tick(), 1, "空态继续后仍能重新攒满 N 轮")
        kept = list(self.state.glob("worker_liveness_state.json.corrupt.*"))
        self.assertEqual(len(kept), 1, "坏文件必须改名留证")
        self.assertIn("状态文件损坏", buf.getvalue(), "必须落 WARN")
        self.assertEqual(len(self.frames()), 1)

    def test_non_object_state_is_also_quarantined(self):
        p = self.state / "worker_liveness_state.json"
        p.write_text("[]", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.tick(), 0)
        self.assertEqual(len(list(self.state.glob("worker_liveness_state.json.corrupt.*"))), 1)

    def test_pm_comment_after_the_first_idle_round_still_reports(self):
        """P0-② 的精确形态：worker 已数到第 1 轮，**之后**同 thread 来 PM 评论。

        旧判据（thread 级）会把 idle_rounds 清零并置 settled ⇒ 这个 worker
        永远等不到第 2 轮，真死也报不出来。新判据只认 `(thread, role)`。
        """
        self.assertEqual(self.tick(), 0, "worker 第 1 轮不忙")
        self.comment(role="pm")              # 派单书，不是 worker 在说话
        self.assertEqual(self.tick(), 1, "PM 评论不清零 ⇒ 第 2 轮照报")
        self.assertEqual(len(self.frames()), 1)
        self.assertEqual(self.frames()[0]["payload"]["role"], "worker")


class ReworkP1DryRun(LivenessFixture):
    """P1-⑤：dry-run 只许日志，不许污染真实态。"""

    def test_dry_run_writes_no_state(self):
        """dry 不落盘 ⇒ 每轮都从 0 计数，所以 N=2 永远攒不满、永远说 0 帧。

        这看着「什么都没干」，但恰恰是不污染的证据：如果 dry 拿得到第 2 轮，
        就说明它把 `idle_rounds` 写下去了。
        """
        for _ in range(3):
            self.assertEqual(self.tick_dry(), 0, "dry 不落盘 ⇒ 每轮都从第 1 轮数起")
        self.assertEqual(self.frames(), [], "dry 绝不真投")
        p = self.state / "worker_liveness_state.json"
        self.assertFalse(p.is_file(), "dry 不许创建/写入状态文件")

    def test_dry_run_reports_when_a_state_file_is_present(self):
        """真实态已置 reported=False 且攒到第 1 轮时，dry 要能说「本来会投」。"""
        st = {"workers": {DEAD_SID: {"sid": DEAD_SID, "thread": THREAD,
                                     "role": "worker", "idle_rounds": 1,
                                     "first_idle_at": time.time(),
                                     "reported": False}},
              "ledger_offset": 0}
        (self.state / "worker_liveness_state.json").write_text(
            json.dumps(st), encoding="utf-8")
        self.assertEqual(self.tick_dry(), 1, "dry 该报「本来会投 1 帧」")
        self.assertEqual(self.frames(), [], "但仍然一条都不真投")
        after = json.loads((self.state / "worker_liveness_state.json").read_text(encoding="utf-8"))
        self.assertFalse(after["workers"][DEAD_SID]["reported"],
                         "dry 不许把 reported 写成 True（这正是上一版的缺陷）")
        self.assertEqual(after["workers"][DEAD_SID]["idle_rounds"], 1,
                         "dry 不许推进 idle_rounds")

    def test_real_mode_still_reports_after_dry_runs(self):
        """反例：dry 跑 N 轮后，真实模式**仍要能报**。

        上一版在 dry 分支里写 `row["reported"] = True` 并落盘 ⇒ dry 一次就把这个
        sid 永久静音，真实模式再也不报，且现场看不出任何痕迹。
        """
        for _ in range(4):
            self.tick_dry()
        self.assertEqual(self.tick(), 0, "真实第 1 轮不忙只是计数")
        self.assertEqual(self.tick(), 1, "dry 跑过 N 轮也不能静音真实模式")
        self.assertEqual(len(self.frames()), 1)
        st = json.loads((self.state / "worker_liveness_state.json").read_text(encoding="utf-8"))
        self.assertTrue(st["workers"][DEAD_SID]["reported"])


class ReworkP1PendingInteraction(LivenessFixture):
    """P1-⑥：等人工交互的活会话不算死。"""

    def test_pending_interaction_is_not_idle(self):
        self.pending[DEAD_SID] = "approval"
        for _ in range(4):
            self.assertEqual(self.tick(), 0, "等人工批 ≠ 不忙 ≠ 死")
        self.assertEqual(self.frames(), [])

    def test_pending_cleared_then_death_still_reports(self):
        self.pending[DEAD_SID] = "approval"
        for _ in range(3):
            self.tick()
        self.pending[DEAD_SID] = None        # 人批了，继续跑完再死
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.tick(), 1, "清掉等交互后仍能正常判死")

    def test_pending_does_not_reset_the_counter(self):
        """与 unknown 同侧：计数**不动**（既不加也不清零）。"""
        self.assertEqual(self.tick(), 0)     # 第 1 轮
        self.pending[DEAD_SID] = "approval"
        self.assertEqual(self.tick(), 0, "等交互期间不推进")
        self.pending[DEAD_SID] = None
        self.assertEqual(self.tick(), 1, "恢复后接着第 2 轮判死")


class ReworkNoInjection(LivenessFixture):
    """QA 指出的漏洞：既有 fixture 全注入 `liveness=` 且 `sessions.json` 写了
    `supervisor` 键，于是两条 P0 的根因被绕过。

    本节**不注入** `liveness=`，配置只从磁盘上的 `new_workbench/engine.json` 来，
    收件人只走真实 `supervisor_sid()` 解析链。
    """

    def setUp(self):
        super().setUp()
        self.ws = self.root / "workbench"
        (self.ws / "new_workbench").mkdir(parents=True, exist_ok=True)

    def workbench_config(self, **engine):
        (self.ws / "new_workbench" / "engine.json").write_text(
            json.dumps({"engine": engine}, ensure_ascii=False), encoding="utf-8")

    def use_real_wiring(self):
        """只给路径，不注入 liveness、不注入 config_path。"""
        dw.configure(dw.DecideWatchConfig(
            state_root=self.state,
            workspace_root=self.ws,
            sessions_path=self.state / "sessions.json",
            bind_path=self.state / "issue_sessions.json",
            ledger_path=self.state / "ledger.jsonl",
            liveness_state_path=self.state / "worker_liveness_state.json",
            probe_session=self.probe,
            inject_event=lambda path, body, port=0: self.injections.append((path, body)),
        ))

    def test_workbench_engine_json_alone_enables_the_leg(self):
        """P1-③ 真断点：薄壳没传配置路径，safkc 必须自己找到工作台配置。"""
        self.workbench_config(worker_liveness_enabled=True,
                              worker_liveness_rounds=2,
                              worker_liveness_interval_s=60)
        self.use_real_wiring()
        got = dw._liveness_settings()
        self.assertTrue(got["enabled"], "配了 new_workbench/engine.json 就必须生效")
        self.assertEqual(got["rounds"], 2)

    def test_absent_workbench_config_keeps_the_leg_off(self):
        self.use_real_wiring()
        self.assertFalse(dw._liveness_settings()["enabled"])

    def test_recipient_resolved_without_injection(self):
        """P0-① 真相：不注入任何东西，只靠 sessions.json 的真实键名。"""
        (self.state / "sessions.json").write_text(json.dumps({
            "supervisor": None,               # 现网形状：键在、值为 null
            "lead": {"id": "session_lead_live"},
        }), encoding="utf-8")
        dw.configure(supervisor_keys=("supervisor", "lead"))
        self.assertEqual(dw.supervisor_sid(""), "session_lead_live")

    def test_full_tick_reports_with_no_injection(self):
        """端到端：不注入 liveness=，全靠工作台配置 + 真实解析链。"""
        self.workbench_config(worker_liveness_enabled=True,
                              worker_liveness_rounds=2,
                              worker_liveness_interval_s=60)
        (self.state / "sessions.json").write_text(json.dumps({
            "supervisor": None,
            "lead": {"id": "session_lead_live"},
        }), encoding="utf-8")
        self.use_real_wiring()
        dw.configure(supervisor_keys=("supervisor", "lead"))
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.tick(), 1, "全真实路径也必须投得出")
        self.assertEqual(self.frames()[0]["payload"]["sid"], "session_lead_live")


class ReworkP0RecipientChain(LivenessFixture):
    """P0-① 三条硬要求：配置面键名、`--supervisor` 传递、缺收件人不刷屏。"""

    def test_configured_supervisor_key_plane_is_used(self):
        """工作台配置面 `supervisor_key` 指向的键名必须被采用。

        lwa 薄壳把这个值从 `config/decide_watch_iss25_7.json` 注入进来，
        本仓不该再另起一套收件人配置。
        """
        (self.state / "sessions.json").write_text(json.dumps({
            "supervisor": None,               # 公开角色名没配
            "lead": {"id": "session_lead_live"},
        }), encoding="utf-8")
        dw.configure(supervisor_key="lead")   # 就是配置面那条通路
        self.assertEqual(dw.supervisor_sid(""), "session_lead_live")

    def test_null_value_is_not_a_hit(self):
        """`supervisor` 值是 null 时不能当命中（否则投给一个叫 null 的会话）。"""
        (self.state / "sessions.json").write_text(json.dumps({
            "supervisor": None,
            "lead": {"id": "session_lead_live"},
        }), encoding="utf-8")
        dw.configure(supervisor_key="supervisor", supervisor_keys=("supervisor", "lead"))
        self.assertEqual(dw.supervisor_sid(""), "session_lead_live")

    def test_explicit_supervisor_wins_over_everything(self):
        (self.state / "sessions.json").write_text(json.dumps({
            "supervisor": {"id": "session_from_json"},
        }), encoding="utf-8")
        self.assertEqual(dw.supervisor_sid("session_from_cli"), "session_from_cli")

    def test_tick_honours_the_supervisor_argument(self):
        """`--supervisor` 必须能救场：tick 要接收并优先使用它。

        上一版 `_loop` 调 tick 时没传 supervisor，于是 `--supervisor <sid>`
        对活性网完全无效——连救场都救不了。
        """
        (self.state / "sessions.json").write_text(json.dumps({}), encoding="utf-8")
        self.assertEqual(self.tick(supervisor="session_from_cli"), 0)
        self.assertEqual(self.tick(supervisor="session_from_cli"), 1, "有收件人就能投")
        self.assertEqual(self.frames()[0]["payload"]["sid"], "session_from_cli")

    def test_missing_recipient_warns_once_not_per_sid(self):
        """缺收件人：一条汇总 WARN ＋ 一个 [user] 事件，且不逐 sid 刷屏。"""
        self.write_bindings({"worker": DEAD_SID, "qa": "session_qa_fixture"})
        (self.state / "sessions.json").write_text(json.dumps({}), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for _ in range(4):
                self.assertEqual(self.tick(), 0)
        out = buf.getvalue()
        self.assertEqual(out.count("活性网无收件人"), 1,
                         "同一原因只许喊一次，不许每轮每 sid 各喊一行")
        dead_events = [body for _path, body in self.injections
                       if body.get("event") == "event.session.supervisor_key_dead"]
        self.assertEqual(len(dead_events), 1, "事件也只发一次")
        self.assertIn("本轮 0 帧", out)
        self.assertEqual(self.frames(), [], "没有收件人时一条例外帧都不许投")

    def test_recipient_recovery_clears_the_once_flag(self):
        """收件人恢复后再丢，能重新喊一次（不是永久静音）。"""
        (self.state / "sessions.json").write_text(json.dumps({}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.tick()
        (self.state / "sessions.json").write_text(json.dumps({
            "supervisor": {"id": "session_supervisor_fixture"},
        }), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.tick()                       # 恢复：清标记
        st = json.loads((self.state / "worker_liveness_state.json").read_text(encoding="utf-8"))
        self.assertNotIn("recipient_missing", st)
        (self.state / "sessions.json").write_text(json.dumps({}), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.tick()
        self.assertIn("活性网无收件人", buf.getvalue(), "再丢要能再喊")


class ReworkLoopWiring(LivenessFixture):
    """`_loop` 真的把 `--supervisor` 传给活性巡检。

    这一条必须驱动 `_loop` 本身，不能只测 `tick(supervisor=...)`：
    上一版的缺陷就在**调用点**少传一个参数，单测 tick 永远发现不了。
    """

    def setUp(self):
        super().setUp()
        self.events = []
        # daemon 桩：`/events` 回一帧空批次，让 _loop 走完一轮
        class Source:
            PORT = 58800
            def __init__(self, outer):
                self.outer = outer
            def bus_get(self, path, timeout=0, port=0):
                return {"events": [], "next_seq": 1, "seq": 1,
                        "oldest_seq": 1, "boot_id": "b"}
            def bus_health(self, port=0):
                return {"boot_id": "b", "seq": 1}
            def guardian_thresholds(self):
                return {"decide_watch": ("heartbeat/decide.json", 900.0, "主循环"),
                        "delivery_watch": ("heartbeat/delivery.json", 900.0, "收单哨"),
                        "supervisor_notice": ("heartbeat/notice.json", 900.0, "投递哨")}
        self.src = Source(self)
        # 既有隐患（不属本单）：health_state_path 缺失时 `_last_state_read() or`
        # 会去求值未定义的 `_LAST_SUPERVISOR_STATE`。写一份状态文件绕开它。
        (self.state / "supervisor_health_state.json").write_text(
            json.dumps({"state": "ok", "ts": 0}), encoding="utf-8")
        dw.configure(event_source=self.src,
                     heartbeat=lambda name, extra: self.events.append(name),
                     cursor_path=self.state / "cursor.json",
                     lock_path=self.state / "lock")

    def run_one_loop_round(self, **args_kw):
        class Args:
            dry_run = False
            once = False
            supervisor = ""
            max_age = 1800
            wait = 0.01
        a = Args()
        for k, v in args_kw.items():
            setattr(a, k, v)
        # 只跑一轮：让 _loop 在第一轮末尾因 once 返回前先触发活性巡检
        a.once = True
        return dw._loop(a, 58800, 0, "b", 1.0)

    def test_loop_passes_supervisor_into_the_tick(self):
        (self.state / "sessions.json").write_text(json.dumps({}), encoding="utf-8")
        self.run_one_loop_round(supervisor="session_via_cli")
        # _loop 只跑一轮，活性巡检在 `_retry_pending` 之后；本轮若已过间隔才会跑，
        # 所以这里直接断言「后续 tick 用了同一个解析结果」由下一次调用承担：
        # 关键是 `--supervisor` 让收件人非空 ⇒ 帧能投出去。
        st = json.loads((self.state / "worker_liveness_state.json").read_text(encoding="utf-8"))
        self.assertNotIn("recipient_missing", st,
                         "传了 --supervisor 就不该判定为「无收件人」")

    def test_dry_run_missing_recipient_sends_no_event(self):
        """P1-⑤ 与 P0-① 的交叉：干跑缺收件人也**不许**发事件、不许写状态。

        否则 dry 跑一次就把「已告警」落盘，真实模式反而被静音。
        """
        (self.state / "sessions.json").write_text(json.dumps({}), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for _ in range(3):
                self.tick_dry()
        dead = [b for _p, b in self.injections
                if b.get("event") == "event.session.supervisor_key_dead"]
        self.assertEqual(dead, [], "干跑不许发 [user] 事件")
        self.assertFalse((self.state / "worker_liveness_state.json").is_file(),
                         "干跑不许写状态文件")
        # 真实模式仍要能出声
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            self.tick()
        self.assertIn("活性网无收件人", buf2.getvalue(), "真实模式不能被干跑静音")


class ReworkP1BadStateRow(LivenessFixture):
    """P1-B：state 里的坏字段只丢那一行，不许让整条腿（乃至进程）退出。

    转型点上一版是 `int(row.get("idle_rounds") or 0) + 1` 与
    `float(row.get("first_idle_at") or now)`，一次坏值就是一个 ValueError。
    `_loop` 刻意不给这条腿包 try（腿死要让心跳消失），所以异常会带走整个
    decide_watch——连拍板哨和收单哨一起停。
    """

    def write_state(self, row: dict) -> None:
        (self.state / "worker_liveness_state.json").write_text(json.dumps({
            "workers": {DEAD_SID: row},
            "ledger_offset": 0,
        }), encoding="utf-8")

    def read_state(self) -> dict:
        return json.loads((self.state / "worker_liveness_state.json")
                          .read_text(encoding="utf-8"))

    def test_bad_idle_rounds_drops_the_row_and_the_leg_keeps_running(self):
        self.write_state({"sid": DEAD_SID, "thread": THREAD, "role": "worker",
                          "idle_rounds": "abc", "first_idle_at": time.time(),
                          "reported": False})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.tick(), 0, "坏行丢弃 ⇒ 本轮从第 1 轮数起，不投帧")
        out = buf.getvalue()
        self.assertNotIn("Traceback", out, "坏字段不许变成 Traceback")
        self.assertNotIn("ValueError", out)
        self.assertIn("idle_rounds", out, "必须 WARN 一行说清哪个字段坏了")
        self.assertIn("abc", out, "WARN 必须带原值，否则现场无从追")
        row = self.read_state()["workers"][DEAD_SID]
        self.assertEqual(row["idle_rounds"], 1,
                         "该行不计入 idle 计数：坏值不许被当成已有的轮数")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.tick(), 1, "腿继续跑：下一轮照常攒满 N 轮并投帧")
        self.assertEqual(len(self.frames()), 1)

    def test_bad_first_idle_at_drops_the_row_and_the_leg_keeps_running(self):
        self.write_state({"sid": DEAD_SID, "thread": THREAD, "role": "worker",
                          "idle_rounds": 1,
                          "first_idle_at": "2026-11-14T00:00:00",
                          "reported": False})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.tick(), 0, "ISO 字符串不是时间戳 ⇒ 该行丢弃、不投帧")
        out = buf.getvalue()
        self.assertNotIn("Traceback", out)
        self.assertNotIn("ValueError", out)
        self.assertIn("first_idle_at", out)
        self.assertIn("2026-11-14T00:00:00", out, "WARN 必须带原值")
        row = self.read_state()["workers"][DEAD_SID]
        self.assertEqual(row["idle_rounds"], 1,
                         "丢弃后重新从第 1 轮数起，不继承坏行的轮数")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.tick(), 1, "腿继续跑：下一轮照常判死")
        self.assertEqual(len(self.frames()), 1)


class ReworkP1StrictBool(LivenessFixture):
    """P1-D：`worker_liveness_enabled` 只认真布尔；错字不许连累收件人。"""

    def test_string_false_is_off(self):
        dw.configure(liveness={"worker_liveness_enabled": "false",
                               "worker_liveness_rounds": 2,
                               "worker_liveness_interval_s": 60})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = dw._liveness_settings()
        self.assertFalse(got["enabled"], 'bool("false") 是 True ⇒ 必须按关处理')
        self.assertIn("worker_liveness_enabled", buf.getvalue(), "必须 WARN 一行")

    def test_string_true_is_also_refused(self):
        dw.configure(liveness={"worker_liveness_enabled": "true",
                               "worker_liveness_rounds": 2,
                               "worker_liveness_interval_s": 60})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(dw._liveness_settings()["enabled"],
                             "非布尔一律按缺省关：只有真 bool 才取原值")

    def test_a_bad_switch_does_not_take_the_recipient_with_it(self):
        """反例：坏的 `worker_liveness_enabled` 不许把同份配置的 `supervisor_sid` 一起丢。"""
        ws = self.root / "workbench"
        (ws / "new_workbench").mkdir(parents=True, exist_ok=True)
        (ws / "new_workbench" / "engine.json").write_text(json.dumps({"engine": {
            "worker_liveness_enabled": "false",     # 写错：字符串
            "worker_liveness_rounds": 2,
            "supervisor_sid": "session_from_same_config",
        }}, ensure_ascii=False), encoding="utf-8")
        dw.configure(dw.DecideWatchConfig(
            state_root=self.state,
            workspace_root=ws,
            sessions_path=self.state / "sessions.json",
            bind_path=self.state / "issue_sessions.json",
            ledger_path=self.state / "ledger.jsonl",
            liveness_state_path=self.state / "worker_liveness_state.json",
            probe_session=self.probe,
            inject_event=lambda path, body, port=0: self.injections.append((path, body)),
        ))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = dw._liveness_settings()
            sid = dw.supervisor_sid("")
        self.assertFalse(got["enabled"], "坏开关按缺省关")
        self.assertIn("worker_liveness_enabled", buf.getvalue(), "必须留痕说明放宽了哪一字段")
        self.assertEqual(sid, "session_from_same_config",
                         "只坏一个字段就只放宽那一个，收件人必须保住")
