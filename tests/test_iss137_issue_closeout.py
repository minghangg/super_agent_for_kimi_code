"""Per-issue close-out and the allowlist exception (#124 A2 back half, A15).

QA wrote these against the mut counter-example copy because the existing suite
pinned only half of each rule. Merged here in #137 §2.1: a rule with no test is
a rule the next change breaks silently.

**A2's second half.** ``test_iss130_batch_round.py`` proved a failed issue is not
archived. It never proved the *other* issue still finishes - and "the failing one
comes first in the listing" is the shape that turns one bad read into a stopped
round.

**A15.** The allowlist sync is the one whole-round operation explicitly exempt
from ``consumption_complete``: a new issue must become visible even while another
issue is failing. Only ``source_ok`` gates it. The existing tests covered the
failure side (``allowlist_kept``) and not the positive side, so a change that
blocked the sync behind ``round_complete`` stayed green.

Run: ``python -m pytest tests/test_iss137_issue_closeout.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.github_source import PagedComments
from super_agent.store import Store

GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".", "mode": "assist",
                 "creation_mode": "pm_created", "issues": [1],
                 "actions": ["bus_inject", "collect", "reconcile", "archive"],
                 "max_active_writers": 1, "roles": ["worker", "qa"]},
    },
}


class TwoIssueSource:
    """Both issues are live; the ones in ``fail_issues`` raise on read."""

    def __init__(self, *, fail_issues=()):
        self.fail = {int(n) for n in fail_issues}
        self.reads = []

    def issues(self, limit=20):
        return [1, 2]

    def title(self, issue):
        return f"issue {issue}"

    def flow(self, issue):
        return ""

    def issue_state(self, issue):
        return "open"

    def comments(self, issue):
        return self.paged(issue)

    def paged(self, issue, *, since_id=0, max_pages=10):
        self.reads.append(int(issue))
        if int(issue) in self.fail:
            raise RuntimeError(f"read failed for issue {issue}")
        return PagedComments([{"id": 9000 + int(issue), "issue": int(issue),
                              "body": "", "author": ""}],
                             complete=True, contiguous=True,
                             pages=[{"page": 1, "rows": 1, "status": "ok"}],
                             reason="fixture")


class DownSource(TwoIssueSource):
    def issues(self, limit=20):
        raise RuntimeError("source down")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def overlay(self) -> Path:
        return self.root / "allowlist.local.json"

    def engine(self, source):
        cfg = config.load(None, {
            "mode": "assist",
            "github": {"source": "none", "repo": "owner/demo",
                       "max_issues": 20, "max_comments_per_issue": 50,
                       "max_comment_pages": 10},
            "engine": {"archived_crosscheck": False},
        })
        return Engine(cfg, Store(self.state), source, None,
                      project_allowlist=ProjectAllowlist(self.allow))


class HealthyIssueStillFinishes(Base):
    """A2 back half: a failed issue must not freeze a healthy one's close-out."""

    def test_healthy_issue_is_read_while_the_other_fails(self):
        """The failing issue is listed **first**, so a ``break`` skips the healthy one."""
        source = TwoIssueSource(fail_issues=(1,))
        engine = self.engine(source)
        out = engine.run_once()
        cursors = engine.store.load_cursor().get("issues") or {}
        self.assertEqual(cursors.get("owner/demo#2"), 9002,
                         "健康单必须被读完并推进 cursor，即使失败单排在它前面")
        self.assertNotIn("owner/demo#1", {k for k, v in cursors.items() if v},
                         "失败单的 cursor 不得推进")
        self.assertFalse(out["round_complete"],
                         "有一单失败，整轮不得算 complete")
        self.assertEqual(engine.store.load_index()["meta"].get("run_count", 0), 0,
                         "round_complete=false 时 run_count 不得推进")

    def test_the_healthy_issue_keeps_its_own_state_clean(self):
        source = TwoIssueSource(fail_issues=(1,))
        engine = self.engine(source)
        engine.run_once()
        issues = engine.store.load_index()["issues"]
        self.assertNotEqual(issues["owner/demo#2"].get("state"), "needs_person",
                            "健康单不得被别人的失败拖成 needs_person")
        self.assertIn("owner/demo#1", issues,
                      "失败单自身必须留痕")


class AllowlistException(Base):
    """A15: ``source_ok`` is the only gate on the overlay sync."""

    def test_source_ok_syncs_the_overlay_even_when_an_issue_failed(self):
        source = TwoIssueSource(fail_issues=(1,))
        engine = self.engine(source)
        out = engine.run_once()
        self.assertFalse(out["round_complete"])
        self.assertNotIn("allowlist_kept", out,
                         "source 正常时不得走「保留 overlay」的分支")
        data = json.loads(self.overlay().read_text(encoding="utf-8"))
        overlay = data["projects"]["demo"].get("issues")
        self.assertEqual(sorted(overlay or []), [1, 2],
                         "source 正常返回时 overlay 必须同步为在飞集合")

    def test_source_ok_does_not_advance_run_count(self):
        source = TwoIssueSource(fail_issues=(2,))
        engine = self.engine(source)
        engine.run_once()
        self.assertEqual(engine.store.load_index()["meta"].get("run_count", 0), 0,
                         "allowlist 豁免不等于 run_count 豁免")

    def test_source_failure_leaves_the_overlay_alone(self):
        source = DownSource()
        engine = self.engine(source)
        engine.store.save_index({"version": 1, "meta": {}, "issues": {}})
        out = engine.run_once()
        self.assertEqual(out.get("allowlist_kept"), "source failed this round")
        self.assertFalse(self.overlay().exists(),
                         "取不到时不得写出 overlay（写空集合就是清空）")

    def test_a_real_empty_set_is_synced_to_empty(self):
        """只有 source 真实返回空集才允许同步为空。"""
        source = TwoIssueSource()
        source.issues = lambda limit=20: []
        engine = self.engine(source)
        out = engine.run_once()
        self.assertNotIn("allowlist_kept", out)
        data = json.loads(self.overlay().read_text(encoding="utf-8"))
        self.assertEqual(data["projects"]["demo"].get("issues"), [],
                         "真实空集必须同步为空")


if __name__ == "__main__":
    unittest.main()
