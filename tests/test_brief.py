"""L1 tests for ``brief`` and ``context`` (ISS-29).

Every test builds its own state root and workbench stand-in under a temporary
directory; the production ``state/`` is never read or written.

What each test pins:

* the six sections are always present, in order, empty sections included;
* every waiting item is locatable by ``action_id``/``issue#n`` and the
  advertised retrieve command really fetches it back;
* over budget the brief says ``还有 N 项`` and keeps the highest priority item;
* a corrupt ``health.json``/``index.json`` reports ``未知`` and never ``正常``;
* the pm brief contains no other project's issue, and no comment body slips in;
* ``context`` clips every comment to the preview length.

Run: ``python -m pytest tests/test_brief.py -q``
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from super_agent import brief as brief_mod
from super_agent import config
from super_agent import store as store_mod
from super_agent.github_source import FixtureSource
from super_agent.store import Store

SECTIONS_IN_ORDER = [title for _key, title in brief_mod.SECTIONS]


def _block(text: str, start: str, end: str) -> str:
    head = text.split(start, 1)[1]
    return head.split(end, 1)[0] if end in head else head


def _dropped_total(text: str, start: str, end: str) -> int:
    total = 0
    for line in _block(text, start, end).splitlines():
        if "还有 " in line:
            total += int(line.split("还有 ")[1].split(" 项")[0])
    return total


def _make_workbench(root: Path, sessions: dict) -> Path:
    """A workbench stand-in: state/sessions.json plus the referenced docs."""
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "sessions.json").write_text(json.dumps(sessions), encoding="utf-8")
    for rel in ("playbooks/workflow_12steps.md", "playbooks/closeout_checklist.md",
                "playbooks/review_checklist.md",
                "rules/shared/rules/worker_conduct.md",
                "assistants/supervisor.md"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    return root


class BriefBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / "engine-state"
        self.lwa = _make_workbench(self.root / "wb", {
            "pm-demo": {"id": "session_pmdemo", "updated": "2026-11-08T10:00"},
            "pm-other": {"id": "session_pmother", "updated": "2026-11-08T10:00"},
            "steward": {"id": "session_steward", "updated": "2026-11-08T10:00"},
        })
        self.store = Store(self.state)
        self.cfg = config.load(None, {
            "role": "supervisor",
            "github": {"source": "none"},
            "store": {"max_event_lines": 1000, "max_outbox_lines": 1000},
        })

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def brief(self, **kwargs):
        limits = kwargs.pop("limits", brief_mod.Limits())
        out = brief_mod.build_brief(self.cfg, self.store, limits=limits,
                                    lwa_root=str(self.lwa), **kwargs)
        return out, brief_mod.render(out, limits=limits)

    def write_health(self, **fields) -> None:
        record = {"pid": 4242, "mode": "assist",
                  "last_loop_at": "2026-11-08T04:00:00+00:00",
                  "last_successful_action_at": None, "active_assignments": 0,
                  "degraded": False, "last_error": "", "running": True}
        record.update(fields)
        (self.state / "health.json").write_text(
            json.dumps(record), encoding="utf-8")

    def write_index(self, issues=None) -> None:
        (self.state / "index.json").write_text(json.dumps(
            {"version": 1, "meta": {}, "issues": issues or {},
             "assignments": {}}), encoding="utf-8")


class Sections(BriefBase):
    def test_six_sections_always_in_order(self):
        _raw, text = self.brief()
        positions = [text.index(title) for title in SECTIONS_IN_ORDER]
        self.assertEqual(positions, sorted(positions))
        for title in SECTIONS_IN_ORDER:
            self.assertIn(title, text)
        self.assertEqual(text.count("\n6. 系统健康"), 1)

    def test_empty_sections_print_empty_state(self):
        _raw, text = self.brief()
        lines = text.splitlines()
        for title in SECTIONS_IN_ORDER:
            idx = lines.index(title)
            following = lines[idx + 1]
            self.assertTrue(following.startswith("  "),
                            f"{title} 后没有内容行")
            if title.startswith(("3.", "4.", "5.")):
                self.assertEqual(following, "  无", f"{title} 缺空态")

    def test_project_section_lists_keys_and_ids_only(self):
        _raw, text = self.brief()
        self.assertIn("pm-demo -> session_pmdemo", text)
        self.assertIn("pm-other -> session_pmother", text)
        self.assertNotIn("steward", text.split("1. 角色")[1].split("3. 等 ")[0])

    def test_pm_brief_only_mentions_its_project(self):
        _raw, text = self.brief(target="pm", project="demo")
        self.assertIn("pm-demo -> session_pmdemo", text)
        self.assertNotIn("pm-other", text)
        self.assertNotIn("session_pmother", text)


class WaitingItems(BriefBase):
    def _seed_three(self) -> list[str]:
        ids = []
        for verb, reason, issue in (
                ("send", "awaiting person", 7),
                ("comment", "[user] 需要用户拍板", 8),
                ("dispatch", "release blocked", 9)):
            rec = self.store.begin_action(
                f"a-{verb}", verb=verb, kind="test", input_ref=f"r-{verb}",
                issue=issue)
            if reason.startswith("[user]"):
                state = self.store.load_outbox_state()
                state["actions"][rec["action_id"]]["payload"] = {"awaiting": "user"}
                self.store.save_outbox_state(state)
            self.store.mark_reconciled(rec["action_id"], "waiting_person", reason)
            ids.append(rec["action_id"])
        return ids

    def test_all_three_waiting_items_are_locatable(self):
        ids = self._seed_three()
        raw, text = self.brief()
        for action_id in ids:
            self.assertIn(action_id, text, f"{action_id} 未出现在 brief")
        self.assertIn("issue#7", text)
        self.assertIn("issue#8", text)
        self.assertIn("issue#9", text)
        # every advertised retrieve command is the real one
        self.assertIn("[取回: python -m super_agent.cli status --outbox]", text)

    def test_retrieve_command_actually_returns_the_items(self):
        """The hint must name a command that really lists these items."""
        import subprocess
        ids = self._seed_three()
        done = subprocess.run(
            [sys.executable, "-m", "super_agent.cli", "status", "--outbox",
             "--state-dir", str(self.state)],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        for action_id in ids:
            self.assertIn(action_id, done.stdout,
                          f"status --outbox 没列出 {action_id}")

    def test_default_status_keeps_its_old_shape(self):
        """--outbox is additive: plain status must not start listing outbox rows."""
        import subprocess
        self._seed_three()
        done = subprocess.run(
            [sys.executable, "-m", "super_agent.cli", "status",
             "--state-dir", str(self.state)],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("无", done.stdout)
        self.assertNotIn("a-send", done.stdout)

    def test_user_item_lands_in_section_four_not_three(self):
        ids = self._seed_three()
        lines = self.brief()[1].splitlines()
        three = lines.index("3. 等 supervisor 裁决的事项")
        four = lines.index("4. 等用户的事项")
        five = lines.index("5. 最近跨项目决定的引用")
        block_three = "\n".join(lines[three:four])
        block_four = "\n".join(lines[four:five])
        self.assertIn(ids[1], block_four)
        self.assertNotIn(ids[1], block_three)
        self.assertIn(ids[0], block_three)

    def test_retrieve_command_really_reproduces_the_items(self):
        self._seed_three()
        full = self.brief(limits=brief_mod.UNLIMITED)[1]
        capped = self.brief(limits=brief_mod.Limits(lines=18, bytes=4096))[1]
        self.assertIn("还有 ", capped)
        self.assertIn("--full", capped)
        # the command named in the truncation line fetches the dropped items
        for line in capped.splitlines():
            if "还有 " in line and "取回：" in line:
                self.assertTrue(line.endswith("--full"), line)
                break
        else:
            self.fail("截断行缺失")
        for action_id in self._ids_from_outbox():
            self.assertIn(action_id, full)

    def _ids_from_outbox(self) -> list[str]:
        return sorted(self.store.unfinished_outbox())


class Budget(BriefBase):
    def test_fifty_items_truncate_with_correct_count(self):
        for n in range(50):
            rec = self.store.begin_action(f"a-{n}", verb="send", kind="test",
                                          input_ref=f"r-{n}", issue=100 + n)
            self.store.mark_reconciled(rec["action_id"], "waiting_person",
                                       f"needs a decision {n}")
        limits = brief_mod.Limits(lines=30, bytes=4096)
        raw, text = self.brief(limits=limits)
        self.assertEqual(len(raw["sections"]["decision"]), 50)
        dropped = _dropped_total(text, "3. 等 supervisor 裁决的事项",
                                 "4. 等用户的事项")
        shown = sum(1 for n in range(50) if f"issue#{100 + n} " in _block(
            text, "3. 等 supervisor 裁决的事项", "4. 等用户的事项"))
        self.assertGreater(dropped, 0, "超限必须打印 还有 N 项")
        self.assertEqual(shown + dropped, 50)
        for line in text.splitlines():
            if "还有 " in line:
                self.assertTrue(line.endswith("--full"), line)

    def test_high_priority_survives_truncation(self):
        for n in range(40):
            rec = self.store.begin_action(f"n-{n}", verb="send", kind="test",
                                          input_ref=f"n-{n}")
            self.store.mark_reconciled(rec["action_id"], "waiting_person",
                                       "normal item")
        rec = self.store.begin_action("u-1", verb="comment", kind="test",
                                      input_ref="u")
        self.store.mark_reconciled(rec["action_id"], "waiting_person",
                                   "[user] 等用户拍板")
        _raw, text = self.brief(limits=brief_mod.Limits(lines=22, bytes=3000))
        self.assertIn("u-1", text)
        self.assertIn("还有 ", text)

    def test_full_never_drops(self):
        for n in range(30):
            rec = self.store.begin_action(f"f-{n}", verb="send", kind="test",
                                          input_ref=f"f-{n}")
            self.store.mark_reconciled(rec["action_id"], "waiting_person", "x")
        _raw, text = self.brief(limits=brief_mod.UNLIMITED)
        self.assertNotIn("还有 ", text)
        for n in range(30):
            self.assertIn(f"f-{n}", text)

    def test_no_dropped_item_disappears_silently(self):
        for n in range(20):
            rec = self.store.begin_action(f"s-{n}", verb="send", kind="test",
                                          input_ref=f"s-{n}")
            self.store.mark_reconciled(rec["action_id"], "waiting_person", "y")
        limits = brief_mod.Limits(lines=20, bytes=2048)
        raw, text = self.brief(limits=limits)
        block = _block(text, "3. 等 supervisor 裁决的事项", "4. 等用户的事项")
        shown = sum(1 for n in range(20) if f"s-{n}" in block)
        dropped = _dropped_total(text, "3. 等 supervisor 裁决的事项",
                                 "4. 等用户的事项")
        self.assertEqual(shown + dropped, len(raw["sections"]["decision"]))


class CorruptState(BriefBase):
    def test_corrupt_health_reports_unknown(self):
        (self.state / "health.json").write_text("{ this is not json", encoding="utf-8")
        _raw, text = self.brief()
        health_block = text.split("6. 系统健康")[1]
        self.assertIn("未知", health_block)
        self.assertNotIn("degraded=False", health_block)

    def test_corrupt_index_reports_unknown_and_warns(self):
        (self.state / "index.json").write_text("garbage{", encoding="utf-8")
        self.write_health()
        _raw, text = self.brief()
        self.assertIn("# WARN index.json", text)
        health_block = text.split("6. 系统健康")[1]
        self.assertIn("未知", health_block)
        self.assertNotIn("active_assignments=0", health_block)

    def test_corrupt_sessions_json_reports_unknown(self):
        (self.lwa / "state" / "sessions.json").write_text("nope", encoding="utf-8")
        _raw, text = self.brief()
        self.assertIn("未知（sessions.json", text)

    def test_healthy_state_is_reported_as_healthy(self):
        self.write_health()
        self.write_index()
        _raw, text = self.brief()
        health_block = text.split("6. 系统健康")[1]
        self.assertIn("pid=4242", health_block)
        self.assertIn("degraded=False", health_block)


class NoLongBodies(BriefBase):
    LONG = "很长的一段正文" * 200

    def test_long_comment_does_not_appear_in_brief(self):
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps({"issues": {"29": {
            "title": "brief ticket",
            "comments": [{"id": 777777, "body": self.LONG, "author": "x"}]}}},
        ), encoding="utf-8")
        source = FixtureSource(fixture)
        card = brief_mod.build_context(source, "#29", default_repo="o/r")
        rendered = brief_mod.render_context(card)
        self.assertIn("777777", rendered)
        self.assertNotIn(self.LONG, rendered)
        self.assertNotIn(self.LONG[:200], rendered)
        for row in card["comments"]:
            self.assertLessEqual(len(row["summary"]), 120)
        self.assertLess(len(rendered), 1200)

    def test_brief_has_no_token_shapes(self):
        self._seed = None
        rec = self.store.begin_action("t-1", verb="send", kind="test",
                                      input_ref="t")
        self.store.mark_reconciled(rec["action_id"], "waiting_person",
                                   "check ghp_secretghij sk-abcdefghij")
        _raw, text = self.brief()
        self.assertIn("t-1", text, "id 必须在（否则下面几条是空转）")
        self.assertNotIn("ghp_secret", text, "GitHub PAT 前缀后的正文没被遮住")
        self.assertNotIn("sk-abcdefghij", text, "API key 没被遮住")
        self.assertNotIn("ghp_secretghij", text, "完整 token 漏进了 brief")
        self.assertIn("ghp_<redacted>", text, "遮罩应保留前缀、盖住正文")


class ContextCard(BriefBase):
    def setUp(self) -> None:
        super().setUp()
        self.long_body = "A" * 3000
        self.fixture = self.root / "ctx.json"
        self.fixture.write_text(json.dumps({"issues": {"29": {
            "title": "记忆外包",
            "comments": [
                {"id": 1, "body": "目标：做 brief\n第二行", "author": "pm"},
                {"id": 2, "body": "验收：六节齐全\n" + self.long_body,
                 "author": "qa"},
                {"id": 3, "body": "acceptance: 3/3 可定位", "author": "qa"},
            ]}}}), encoding="utf-8")

    def test_card_has_the_four_parts(self):
        card = brief_mod.build_context(FixtureSource(self.fixture), "o/r#29",
                                       limits=brief_mod.Limits(comment_preview=120))
        text = brief_mod.render_context(card)
        self.assertIn("目标: 记忆外包", text)
        self.assertIn("最近决定: comment 3", text)
        self.assertIn("验收要求:", text)
        self.assertIn("相关评论 id", text)
        self.assertEqual(card["total_comments"], 3)

    def test_every_comment_summary_is_clipped(self):
        card = brief_mod.build_context(FixtureSource(self.fixture), "o/r#29",
                                       limits=brief_mod.Limits(comment_preview=120))
        for row in card["comments"]:
            self.assertLessEqual(len(row["summary"]), 120)
        self.assertNotIn(self.long_body[:200], brief_mod.render_context(card))

    def test_issue_ref_forms(self):
        self.assertEqual(brief_mod.parse_issue_ref("#29", "o/r"), ("o/r", 29))
        self.assertEqual(brief_mod.parse_issue_ref("o/r#29"), ("o/r", 29))
        self.assertEqual(brief_mod.parse_issue_ref("29", "o/r"), ("o/r", 29))
        with self.assertRaises(ValueError):
            brief_mod.parse_issue_ref("#abc", "o/r")
        with self.assertRaises(ValueError):
            brief_mod.parse_issue_ref("#29", "")


class PmIsolation(BriefBase):
    ALLOW = {
        "schema_version": 1,
        "defaults": {"mode": "observe", "creation_mode": "pm_created"},
        "projects": {
            "demo": {"repo": "o/demo", "cwd": ".", "issues": [29],
                     "actions": ["collect"]},
            "other": {"repo": "o/other", "cwd": ".", "issues": [31],
                      "actions": ["collect"]},
        },
    }

    def test_other_project_issue_is_filtered_from_pm_brief(self):
        allow = self.root / "allowlist.json"
        allow.write_text(json.dumps(self.ALLOW), encoding="utf-8")
        self.cfg.setdefault("engine", {})["project_allowlist"] = str(allow)
        for issue in (29, 31):
            rec = self.store.begin_action(f"a-{issue}", verb="send", kind="t",
                                          input_ref=str(issue), issue=issue)
            self.store.mark_reconciled(rec["action_id"], "waiting_person",
                                       f"issue {issue} pending")
        _raw, text = self.brief(target="pm", project="demo")
        self.assertIn("issue#29", text)
        self.assertNotIn("issue#31", text)

    def test_supervisor_brief_keeps_both(self):
        allow = self.root / "allowlist.json"
        allow.write_text(json.dumps(self.ALLOW), encoding="utf-8")
        self.cfg.setdefault("engine", {})["project_allowlist"] = str(allow)
        for issue in (29, 31):
            rec = self.store.begin_action(f"b-{issue}", verb="send", kind="t",
                                          input_ref=str(issue), issue=issue)
            self.store.mark_reconciled(rec["action_id"], "waiting_person",
                                       f"issue {issue} pending")
        _raw, text = self.brief()
        self.assertIn("issue#29", text)
        self.assertIn("issue#31", text)


if __name__ == "__main__":
    unittest.main()
