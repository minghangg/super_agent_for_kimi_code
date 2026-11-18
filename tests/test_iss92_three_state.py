"""``accepted`` / ``done`` / ``completed`` are three states, not a ladder (#92).

Run: ``python -m pytest tests/test_iss92_three_state.py -q``

Each state has its **own carrier**, so none of them can be reached by writing
the previous one's file:

* ``accepted`` - ``wss_bus_journal.jsonl`` (transport took the request);
* ``done`` - ``outbox_state.json`` + ``outbox.jsonl`` history (the local action
  finished and got a remote id);
* ``completed`` - ``index.json``'s ``assignments[aid].status`` (the business
  result was read back and confirmed).

The four refusals below are the four ways one state used to be mistaken for the
next. Each is asserted against the code that implements the gate, and each can
go red: change the premise and the assertion fails.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import assignments as asg_mod
from super_agent.store import Store


def _assignment(**kw) -> dict:
    row = {"id": "a1", "thread": "o/r#92", "role": "worker",
           "status": "waiting_release", "qa_verdict": "pass"}
    row.update(kw)
    return row


class Carrier(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        idx = self.store.load_index()
        idx["assignments"] = {"a1": _assignment()}
        self.store.save_index(idx)

    def _status(self) -> str:
        return self.store.load_index()["assignments"]["a1"]["status"]

    # 反例 1：只写传输层 journal，业务层一个字节都不许动。
    #   **同根**：journal 与 index.json 放同一个 state 根（生产布局）。分开放
    #   会让「accepted 漏进业务层」的变异无法被捕获——那是本单第一轮的缺口
    #   （QA 把变异写进 `_journal_put` 并同根后，状态确实变成 completed，
    #   而分根的测试仍 8 passed）。
    def test_1_accepted_never_becomes_completed(self):
        from super_agent import wss_bus as bus_mod

        old = bus_mod.STATE.journal
        bus_mod.STATE.journal = bus_mod.Journal(self.root / "wss_bus_journal.jsonl")
        try:
            bus_mod._journal_put("rid-1", "send", "session_x",
                                 {"prompt_id": "p1"})
            self.assertIsNotNone(bus_mod.STATE.journal.get("rid-1"))
            # Same root, one level apart: the journal sits beside index.json.
            self.assertEqual(self.root, self.store.root)
            self.assertTrue((self.root / "wss_bus_journal.jsonl").is_file())
            self.assertTrue((self.root / "index.json").is_file())
        finally:
            bus_mod.STATE.journal = old
        self.assertEqual(self._status(), "waiting_release")
        self.assertNotEqual(self._status(), "completed")

    # 反例 2：动作层走完（done 从 actions 删除），业务层不动。
    def test_2_done_never_becomes_completed(self):
        aid = self.store.begin_action("a1:archive:999", verb="archive",
                                      kind="archive", assignment_id="a1",
                                      input_ref="999")["action_id"]
        self.store.mark_in_flight(aid)
        self.store.mark_done(aid, sid="s1")
        # ``done`` leaves ``actions`` entirely - the history is outbox.jsonl.
        self.assertIsNone(self.store.outbox_action(aid))
        self.assertEqual(self._status(), "waiting_release")
        rows = [json.loads(line) for line in
                self.store.outbox_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        mine = [r for r in rows
                if str(r.get("action_id") or "").split("#")[0] == "a1:archive:999"]
        self.assertTrue(any(r["status"] == "done" for r in mine))
        self.assertFalse(any(r["status"] == "completed" for r in mine))

    # 反例 3：qa_gate 拦跳级，两种拒绝各一条。
    def test_3_qa_gate_refuses_without_a_pass(self):
        moved = asg_mod.complete(_assignment(qa_verdict=""))
        self.assertFalse(moved["changed"])
        self.assertTrue(moved["refused"])
        self.assertEqual(moved["reason"], "no qa pass recorded")

        moved = asg_mod.complete(_assignment(qa_verdict="reject"))
        self.assertFalse(moved["changed"])
        self.assertTrue(moved["refused"])
        self.assertEqual(moved["reason"],
                         "qa rejected: work must return to the worker first")

    # 反例 4：已 completed 不能再 complete；正向对照证明该条不是恒真。
    def test_4_completed_cannot_complete_again(self):
        moved = asg_mod.complete(_assignment(status="completed", qa_verdict="pass"))
        self.assertFalse(moved["changed"])
        self.assertTrue(moved["refused"])
        self.assertEqual(moved["reason"], "assignment already completed")

        ok = asg_mod.complete(_assignment(qa_verdict="pass"))
        self.assertTrue(ok["changed"])
        self.assertEqual(ok["status"], "completed")


class EachOneCanGoRed(unittest.TestCase):
    """The mutation that flips each refusal, so none of them is vacuous."""

    def test_3_goes_green_the_moment_the_premise_changes(self):
        # 反例 3 的靶子：把 qa_verdict 从 "" 改成 "pass" ⇒ 必须变成 changed。
        before = asg_mod.complete(_assignment(qa_verdict=""))
        after = asg_mod.complete(_assignment(qa_verdict="pass"))
        self.assertFalse(before["changed"])
        self.assertTrue(after["changed"])

    def test_4_goes_green_the_moment_the_status_changes(self):
        blocked = asg_mod.complete(_assignment(status="completed"))
        allowed = asg_mod.complete(_assignment(status="waiting_release"))
        self.assertEqual(blocked["reason"], "assignment already completed")
        self.assertTrue(allowed["changed"])

    def test_the_qa_gate_is_the_single_door(self):
        # ``complete`` writes the terminal state only through ``qa_gate``:
        # remove the gate and the two refused cases above would both move.
        ok, why = asg_mod.qa_gate(_assignment(qa_verdict=""))
        self.assertFalse(ok)
        self.assertEqual(why, "no qa pass recorded")
        ok, why = asg_mod.qa_gate(_assignment())
        self.assertTrue(ok)


class CompletedOutlivesTheOutbox(unittest.TestCase):
    """Why ``index.json`` holds ``completed``: ``outbox.jsonl`` rotates away."""

    def test_an_assignment_survives_outbox_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp), max_outbox_lines=5)
            idx = store.load_index()
            idx["assignments"] = {"a1": _assignment(status="completed")}
            store.save_index(idx)
            first = None
            for i in range(8):
                aid = store.begin_action(f"a1:bus_inject:{i}", verb="bus_inject",
                                         kind="bus_inject", assignment_id="a1",
                                         input_ref=str(i))["action_id"]
                first = first or aid
                store.mark_in_flight(aid)
                store.mark_done(aid, sid="s1")
            # The oldest action is gone from every outbox file...
            gone = all(first not in p.read_text(encoding="utf-8")
                       for p in Path(tmp).glob("outbox.jsonl*") if p.is_file())
            self.assertTrue(gone)
            # ...but the completed status is still readable, and there is no
            # second copy of index.json to fall behind.
            self.assertEqual(
                store.load_index()["assignments"]["a1"]["status"], "completed")
            self.assertEqual(sorted(p.name for p in Path(tmp).glob("index.json*")),
                             ["index.json"])


if __name__ == "__main__":
    unittest.main()
