"""ISS-128: an incomplete AT history must never be accepted, approved or dispatched.

The requirement has one shape and three gates:

* the **walk** must read the whole listing before it may answer the question
  (``PagedComments.pagination_complete``, from ``github_source.paged``);
* the **projection** stores that answer per requirement entry as
  ``history_complete`` - collection metadata, never part of the package hash;
* the **gates** (approval and dispatch) read that stored answer and refuse when
  it is absent, so a projection rebuilt from a prefix cannot license either.

Counter-examples 1-5 from the issue are the tests below, each written so that
removing its guard turns it red rather than leaving it green by accident.

Run: ``python -m pytest tests/test_iss128_history_complete.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from super_agent import (cli, config, github_source, requirement_cli,
                         requirements as req, role_config)
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store

RID = "owner/demo#1"
THREAD = RID
SUP_KEY = "supervisor-demo-7c31ade2"
AT_KEY = "at-demo-3f7c1a90"

GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {"demo": {"repo": "owner/demo", "cwd": ".", "mode": "assist",
                          "creation_mode": "pm_created", "issues": [1],
                          "at_mode": True, "actions": ["bus_inject"],
                          "roles": ["worker", "qa", "pm", "at"]}},
}


def package(revision=1, *, objective="build X"):
    value = {
        "schema_version": 1, "requirement_id": RID, "revision": revision,
        "status": "ready", "objective": objective,
        "context_refs": ["issue-1"], "scope": ["engine"],
        "out_of_scope": ["UI"], "constraints": ["serial"],
        "acceptance": ["approved example passes"],
        "negative_tests": ["stale revision is refused"], "risk_tier": "T1",
        "review_policy": {"plan_qa": False, "implementation_qa": True,
                          "worker_mode": "plan_and_implement", "test_level": "L1"},
        "open_questions": [], "approval_ref": "", "updated_at": "test",
    }
    value["requirement_hash"] = req.requirement_hash(value)
    return value


def package_comment(cid, value):
    return {"id": cid, "body": f"{req.fence(req.PACKAGE_FENCE, value)}\n"
                               f"<!-- session: {AT_KEY} -->"}


def approval_comment(cid, value, package_cid):
    return {"id": cid, "body": req.fence(req.APPROVAL_FENCE, {
        "requirement_id": RID, "revision": value["revision"],
        "requirement_hash": value["requirement_hash"],
        "package_ref": f"issuecomment-{package_cid}"})
        + f"\n<!-- session: {SUP_KEY} -->"}


def project(comments, previous=None, *, complete=None, scope=None):
    return req.project_comments(
        comments, previous,
        package_verifier=lambda comment: True,
        approval_verifier=lambda comment: True,
        history_read_complete=complete, history_scope=scope)


class TruncatedSource(github_source.LiveSource):
    """A listing the walk cannot finish inside its page budget.

    ``truncate_at`` rows are served; the walk is told the budget is one page, so
    it never reaches the last page and must answer "incomplete". This is the
    120-comments/``max_pages=1`` scenario the issue names, made reproducible.
    """

    page_size = 100

    def __init__(self, rows, *, truncate_pages_at=None):
        super().__init__("owner/demo")
        self.rows = sorted(rows)
        self.truncate_pages_at = truncate_pages_at
        self.requests: list[dict] = []

    def issues(self, limit=20):
        return [1]

    def title(self, issue):
        return "t"

    def flow(self, issue):
        return "at"

    def issue_state(self, issue):
        return "open"

    def page_count(self):
        return max(1, -(-len(self.rows) // self.page_size))

    def _get_with_headers(self, path, query=None):
        query = dict(query or {})
        self.requests.append(query)
        page = int(query.get("page") or 1)
        start = (page - 1) * self.page_size
        chunk = self.rows[start:start + self.page_size]
        body = [{"id": cid, "body": "b", "user": {"login": "u"},
                 "created_at": ""} for cid in chunk]
        headers = {}
        total = self.page_count()
        # ``truncate_pages_at`` hides the Link header past that page, which is
        # how a hostile/broken backend hides the real end of the listing.
        if total > 1 and (self.truncate_pages_at is None
                          or page < self.truncate_pages_at):
            headers["Link"] = (f'<https://api.github.com/repos/owner/demo/issues/1/'
                               f'comments?per_page=100&page={total}>; rel="last"')
        return body, headers

    def _get(self, path, query=None):
        return self._get_with_headers(path, query)[0]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        self.events = self.root / "sessions" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, source, *, max_pages=1):
        cfg = config.load(None, {
            "mode": "assist",
            "github": {"source": "none", "repo": "owner/demo",
                       "max_comments_per_issue": 50,
                       "max_comment_pages": max_pages},
            "engine": {"archived_crosscheck": False,
                       "session_events_path": str(self.events)}})
        return Engine(cfg, Store(self.state), source, None,
                      project_allowlist=ProjectAllowlist(self.allow))

    def index(self, engine):
        return engine.store.load_index()


class WalkClaim(unittest.TestCase):
    """Counter-example 4's substrate: the claim comes from the walk, not rows."""

    def test_a_truncated_walk_says_so(self):
        src = TruncatedSource(range(1, 121))
        walked = src.paged(1, since_id=0, max_pages=1)
        # The whole budget went into locating the end, so no rows were collected
        # - and, crucially, the walk says so instead of returning a bare 100.
        self.assertFalse(walked.pagination_complete)
        self.assertFalse(walked.contiguous)

    def test_a_budget_that_reads_the_newest_page_still_claims_nothing(self):
        """Reading rows is not reading the listing: the claim stays False."""
        src = TruncatedSource(range(1, 301))
        walked = src.paged(1, since_id=0, max_pages=2)
        self.assertEqual([c["id"] for c in walked], list(range(201, 301)),
                         "the newest page was the only one the budget allowed")
        self.assertFalse(walked.pagination_complete)
        self.assertFalse(walked.contiguous)

    def test_a_complete_walk_says_so(self):
        src = TruncatedSource(range(1, 121))
        walked = src.paged(1, since_id=0, max_pages=10)
        self.assertTrue(walked.pagination_complete)
        self.assertEqual(len(walked), 120)

    def test_a_plain_list_is_the_legacy_contract(self):
        """A fixture returning rows proves nothing - and must not start pausing."""
        from super_agent.engine_parts import claims
        self.assertTrue(claims.history_is_complete([{"id": 1}]))
        self.assertTrue(claims.walk_covers_cursor([{"id": 1}], 0))


class EngineRefusesToProjectAPrefix(Base):
    """Counter-example 2: 120 comments at ``max_pages=1`` must not be applied."""

    def test_apply_is_not_called_and_the_issue_pauses(self):
        src = TruncatedSource(range(1, 121))
        engine = self.engine(src, max_pages=1)
        with mock.patch.object(Engine, "_apply_requirement_comments") as apply:
            summary = engine.run_once()
        apply.assert_not_called()
        self.assertEqual(summary.get("read_paused"), 1)

        rec = self.index(engine)["issues"][engine._thread_key(1)]
        reasons = [row["reason"] for row in rec["needs_person"]]
        self.assertIn("read.pagination_incomplete", reasons)
        self.assertNotIn("requirements", self.index(engine),
                         "a projection was built from a prefix")

    def test_the_projection_file_is_not_written_from_a_prefix(self):
        src = TruncatedSource(range(1, 121))
        engine = self.engine(src, max_pages=1)
        engine.run_once()
        self.assertFalse((self.state / "requirements.json").exists(),
                         "a prefix was written as if it were the listing")

    def test_a_complete_walk_does_apply(self):
        src = TruncatedSource(range(1, 121))
        engine = self.engine(src, max_pages=10)
        with mock.patch.object(Engine, "_apply_requirement_comments",
                               wraps=engine._apply_requirement_comments) as apply:
            engine.run_once()
        apply.assert_called()
        self.assertTrue((self.state / "requirements.json").exists(),
                        "a complete walk wrote no projection")


class ProjectionStoresTheClaim(unittest.TestCase):
    def test_the_flag_is_per_entry_and_hash_neutral(self):
        value = package()
        complete = project([package_comment(100, value)], complete=True)
        self.assertTrue(complete["requirements"][RID]["history_complete"])
        self.assertEqual(req.current(complete, RID)["requirement_hash"],
                         value["requirement_hash"])
        self.assertNotIn("history_complete", req.current(complete, RID),
                         "collection metadata leaked into the package")

    def test_an_incomplete_read_does_not_store_true(self):
        value = package()
        incomplete = project([package_comment(100, value)], complete=False)
        self.assertNotIn("history_complete",
                         incomplete["requirements"][RID])

    def test_no_claim_leaves_the_stored_answer_alone(self):
        value = package()
        complete = project([package_comment(100, value)], complete=True)
        refreshed = project([], complete, complete=None)
        self.assertTrue(refreshed["requirements"][RID]["history_complete"],
                        "a refresh that cannot answer erased the answer")

    def test_the_hash_is_identical_whether_the_flag_is_set_or_not(self):
        value = package()
        one = project([package_comment(100, value)], complete=True)
        two = project([package_comment(100, value)], complete=False)
        self.assertEqual(req.current(one, RID)["requirement_hash"],
                         req.current(two, RID)["requirement_hash"])

    def test_the_field_is_ignored_by_the_hash_not_merely_absent_from_the_tuple(self):
        """P2-3: prove the *behaviour*, not just the ``SEMANTIC_FIELDS`` tuple.

        ``requirement_hash`` hashes ``semantic(package)``, which projects only
        the declared fields. So the honest assertion is the opposite of "the
        field would move the hash": putting ``history_complete`` **into the
        package** must leave the hash untouched, because the field is not part
        of what a requirement *is*. If it ever moved the hash, a re-projection
        would read as a new revision and every approval would go stale.
        """
        value = package()
        with_field = dict(value, history_complete=True)
        self.assertEqual(req.requirement_hash(value),
                         req.requirement_hash(with_field),
                         "a collection field moved the requirement hash")
        self.assertNotIn("history_complete", req.semantic(with_field),
                         "the field leaked into the hashed projection")
        # And it really is excluded by the projection, not by luck: a semantic
        # field does move the hash, which is the contrast that makes this test
        # meaningful rather than a tautology.
        self.assertNotEqual(req.requirement_hash(value),
                            req.requirement_hash(dict(value, objective="other")))

    def test_a_complete_read_of_one_requirement_does_not_mark_another(self):
        """P2-4: the projection is shared, the claim is per requirement.

        ``index["requirements"]`` holds every issue, so a batch that stamps
        "all entries" lets a complete read of B certify A while A contributed no
        row to that snapshot. Scoped marking is what keeps the flag a statement
        about *this* thread's history.
        """
        other = "owner/demo#2"
        a_value = dict(package(), requirement_id=RID)
        b_value = dict(package(), requirement_id=other)
        b_value["requirement_hash"] = req.requirement_hash(b_value)
        projection = project([package_comment(100, a_value)], complete=False,
                             scope=RID)
        self.assertNotIn("history_complete",
                         projection["requirements"][RID])
        after_b = req.project_comments(
            [{"id": 200, "body": req.fence(req.PACKAGE_FENCE, b_value)}],
            projection, package_verifier=lambda comment: True,
            history_read_complete=True, history_scope=other)
        self.assertFalse(after_b["requirements"][RID].get("history_complete"),
                         "B's complete read certified A's history")
        self.assertTrue(after_b["requirements"][other]["history_complete"],
                        "B's own entry was not marked")


class Gate1IncompleteCannotBeApproved(unittest.TestCase):
    """Counter-example 1: ``cmd_approve`` must refuse a truncated projection."""

    def _run(self, projection):
        state = tempfile.mkdtemp()
        req.save_projection(state, projection)
        ctx = SimpleNamespace(
            roles=role_config.load("missing-roles.json"),
            projects={"demo": "owner/demo"}, call={}, fetch={},
            cfg=config.load(None, {"engine": {"supervisor_keys": [SUP_KEY]}}))
        args = SimpleNamespace(role="supervisor", project="demo", issue=1,
                               revision=1, hash="", session_key=SUP_KEY,
                               dry_run=False, state_dir=state)
        with mock.patch.object(requirement_cli, "_ctx", return_value=ctx), \
                mock.patch.object(requirement_cli, "_project",
                                  return_value=projection), \
                mock.patch.object(requirement_cli, "_refuse_standard"), \
                mock.patch.object(requirement_cli.issue_cli, "_call") as call:
            with self.assertRaises(req.RequirementError) as caught:
                requirement_cli.cmd_approve(args)
        call.assert_not_called()
        return str(caught.exception)

    def test_ready_but_truncated_is_refused(self):
        value = package()
        projection = project([package_comment(100, value)], complete=False)
        message = self._run(projection)
        self.assertIn("history is incomplete", message)

    def test_the_same_projection_with_the_flag_is_approvable(self):
        value = package()
        projection = project([package_comment(100, value)], complete=True)
        state = tempfile.mkdtemp()
        req.save_projection(state, projection)
        ctx = SimpleNamespace(
            roles=role_config.load("missing-roles.json"),
            projects={"demo": "owner/demo"}, call={}, fetch={},
            cfg=config.load(None, {"engine": {"supervisor_keys": [SUP_KEY]}}))
        args = SimpleNamespace(role="supervisor", project="demo", issue=1,
                               revision=1, hash="", session_key=SUP_KEY,
                               dry_run=False, state_dir=state)
        with mock.patch.object(requirement_cli, "_ctx", return_value=ctx), \
                mock.patch.object(requirement_cli, "_project",
                                  return_value=projection), \
                mock.patch.object(requirement_cli, "_sync"), \
                mock.patch.object(requirement_cli.issue_cli, "_ledger"), \
                mock.patch.object(requirement_cli.issue_cli, "_call",
                                  return_value={"id": 101}):
            self.assertEqual(requirement_cli.cmd_approve(args), 0)

    def test_a_rebuilt_projection_cannot_stand_in_for_the_stored_one(self):
        """Counter-example 4: the gate reads storage, never a ``previous=None``.

        The rebuilt projection must be **approvable on every other count** or
        this test proves nothing: with an approval in it, ``cmd_approve`` stops
        at ``status != "ready"`` and never reaches the history gate - which is
        exactly why the first revision of this test stayed green while the gate
        was deleted (QA 7136422892).

        So the rebuild here is ``status == ready`` (package published, not yet
        approved) and *does* carry ``history_complete``, while the **stored**
        projection has no such field. If the gate ever reads the rebuild instead
        of storage, approval succeeds and this goes red; deleting the gate makes
        it go red for the same reason.
        """
        value = package()
        rebuilt = project([package_comment(100, value)], complete=True)
        self.assertEqual(req.status(rebuilt, RID), "ready",
                         "the rebuild must get past the status check")
        self.assertTrue(rebuilt["requirements"][RID].get("history_complete"),
                        "the rebuild must claim completeness, or the test is inert")
        state = tempfile.mkdtemp()
        req.save_projection(state, {"schema_version": 1, "requirements": {},
                                    "seen_comment_ids": [], "errors": []})
        self.assertFalse(req.history_complete(req.load_projection(state), RID),
                         "the stored projection must carry no claim")
        ctx = SimpleNamespace(
            roles=role_config.load("missing-roles.json"),
            projects={"demo": "owner/demo"}, call={}, fetch={},
            cfg=config.load(None, {"engine": {"supervisor_keys": [SUP_KEY]}}))
        args = SimpleNamespace(role="supervisor", project="demo", issue=1,
                               revision=1, hash="", session_key=SUP_KEY,
                               dry_run=False, state_dir=state)
        with mock.patch.object(requirement_cli, "_ctx", return_value=ctx), \
                mock.patch.object(requirement_cli, "_project",
                                  return_value=rebuilt), \
                mock.patch.object(requirement_cli, "_refuse_standard"), \
                mock.patch.object(requirement_cli.issue_cli, "_call") as call:
            with self.assertRaises(req.RequirementError) as caught:
                requirement_cli.cmd_approve(args)
        call.assert_not_called()
        self.assertIn("history is incomplete", str(caught.exception),
                      "it was refused for some other reason; the gate never ran")


class Gate2IncompleteCannotBeDispatched(unittest.TestCase):
    """Counter-example 2: ``dispatch_packet`` must refuse a truncated projection."""

    def test_a_truncated_projection_makes_no_packet(self):
        value = package()
        projection = project(
            [package_comment(100, value), approval_comment(101, value, 100)],
            complete=False)
        self.assertEqual(req.status(projection, RID), "approved",
                         "the package itself is approved; only the history is not")
        with self.assertRaisesRegex(req.RequirementError, "history is incomplete"):
            req.dispatch_packet(projection, RID, "asg_1_worker_1")

    def test_the_same_projection_with_the_flag_dispatches(self):
        value = package()
        projection = project(
            [package_comment(100, value), approval_comment(101, value, 100)],
            complete=True)
        packet = req.dispatch_packet(projection, RID, "asg_1_worker_1")
        self.assertEqual(packet["revision"], 1)

    def test_the_packet_carries_no_collection_fields(self):
        """Counter-example 3: no ``history_complete`` in the AT schema or packet."""
        value = package()
        projection = project(
            [package_comment(100, value), approval_comment(101, value, 100)],
            complete=True)
        packet = req.dispatch_packet(projection, RID, "asg_1_worker_1")
        self.assertNotIn("history_complete", packet)
        self.assertNotIn("history_complete", req.SEMANTIC_FIELDS)
        self.assertNotIn("history_complete", req.current(projection, RID))


class Gate3NothingErasesIt(unittest.TestCase):
    """Counter-example 5: sync / publish / approve write-backs keep the field."""

    def test_sync_keeps_the_flag_and_the_seen_ids(self):
        value = package()
        projection = project([package_comment(100, value)], complete=True)
        state = tempfile.mkdtemp()
        req.save_projection(state, projection)
        seen_before = list(projection["seen_comment_ids"])
        self.assertTrue(seen_before)
        refreshed = project([], req.load_projection(state), complete=True)
        self.assertTrue(refreshed["requirements"][RID]["history_complete"])
        self.assertEqual(refreshed["seen_comment_ids"], seen_before)

    def test_approval_keeps_the_flag(self):
        value = package()
        projection = project([package_comment(100, value)], complete=True)
        approved = project([approval_comment(101, value, 100)], projection,
                           complete=True)
        self.assertEqual(req.status(approved, RID), "approved")
        self.assertTrue(approved["requirements"][RID]["history_complete"])
        self.assertIn(100, approved["seen_comment_ids"])

    def test_save_and_load_round_trip_the_flag(self):
        value = package()
        projection = project([package_comment(100, value)], complete=True)
        state = tempfile.mkdtemp()
        path = req.save_projection(state, projection)
        loaded = req.load_projection(state)
        self.assertTrue(loaded["requirements"][RID]["history_complete"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))
                         ["requirements"][RID]["history_complete"], True)


class BothGatesFireInOneRound(Base):
    """P0-1 (QA 7136422892): the AT branch must leave a trace, not raise.

    The reachable shape is ``at_mode and not read_paused and not history_done
    and paused_now``: one AT round where the page walk pauses (no proven prefix)
    and the history is therefore incomplete too. The first revision read
    ``len(comments)`` there - a name that does not exist on that path - so
    ``run_once`` swallowed a ``NameError`` into ``summary["errors"]`` and the
    ``requirement.history_incomplete`` event was never written, the cursor never
    persisted, and ``_sync_assignments`` was skipped for the whole issue.

    ``errors == []`` is part of every assertion here on purpose: a green
    ``processed == 0`` proves nothing while an exception is being absorbed.
    """

    def _paused_round(self):
        src = TruncatedSource(range(1, 121))
        src.flow = lambda issue: "at"
        engine = self._engine(src)
        summary = engine.run_once()
        return src, engine, summary

    def _engine(self, source):
        import tempfile
        from pathlib import Path
        tmp = tempfile.mkdtemp()
        return _tmp_engine(Path(tmp), source, max_pages=1)

    def test_the_round_reports_no_exception(self):
        _src, _engine, summary = self._paused_round()
        self.assertEqual(summary["errors"], [],
                         "the round swallowed an exception into summary['errors']")

    def test_the_history_gate_leaves_its_event(self):
        _src, engine, _summary = self._paused_round()
        types = [json.loads(line)["type"] for line in
                 (engine.store.root / "events.jsonl")
                 .read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertIn("requirement.history_incomplete", types,
                      "the AT gate fired without leaving a trace")
        self.assertIn("read.paused", types)

    def test_the_cursor_and_the_assignment_sync_still_happen(self):
        _src, engine, _summary = self._paused_round()
        cursor = engine.store.load_cursor()
        self.assertIn(engine._thread_key(1), cursor.get("issues") or {},
                      "the round died before the cursor was persisted")
        self.assertEqual(int((cursor.get("issues") or {})
                             [engine._thread_key(1)] or 0), 0)

    def test_one_cause_leaves_one_queue_row(self):
        _src, engine, _summary = self._paused_round()
        rec = engine.store.load_index()["issues"][engine._thread_key(1)]
        rows = [r for r in rec["needs_person"]
                if r["reason"] == "read.pagination_incomplete"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rec["read_paused"]["reason"], "read.pagination_incomplete",
                         "the page-level pause must stay the recorded cause")


class HistoryTruncatedWhileThePrefixIsFine(Base):
    """L3 (K2): the prefix is proven, the history is not - the case that matters.

    ``walk_covers_cursor`` answers on ``contiguous`` (the unread prefix) while
    the projection needs ``pagination_complete`` (the whole listing). A walk can
    satisfy the first and fail the second: it stopped on a page that reaches the
    cursor while older pages went unread. Before ISS-128 that shape wrote a
    projection rebuilt from the prefix - and a requirement package on an unread
    page simply vanished from it. The page-pause gate does not cover this: no
    pause fires, because nothing above the cursor is missing.
    """

    class PrefixOnly(github_source.PagedComments):
        def __init__(self, rows):
            super().__init__(rows, complete=False, contiguous=True,
                             pages=[{"page": 2, "rows": len(rows),
                                     "status": "ok"}],
                             reason="stopped at the cursor; page 1 unread")

    class PrefixOnlySource(TruncatedSource):
        def paged(self, issue, *, since_id=0, max_pages=10):
            rows = super().paged(issue, since_id=since_id, max_pages=max_pages)
            return HistoryTruncatedWhileThePrefixIsFine.PrefixOnly(list(rows))

    def test_the_prefix_is_accepted_and_the_history_is_not(self):
        from super_agent.engine_parts import claims
        walked = self.PrefixOnlySource(range(1, 121)).paged(1, since_id=0)
        self.assertTrue(claims.walk_covers_cursor(walked, 0),
                        "the unread prefix is proven")
        self.assertFalse(claims.history_is_complete(walked),
                         "the listing is not")

    def test_no_projection_is_written_from_the_prefix(self):
        src = self.PrefixOnlySource(range(1, 121))
        engine = self.engine(src, max_pages=10)
        with mock.patch.object(Engine, "_apply_requirement_comments") as apply:
            engine.run_once()
        apply.assert_not_called()
        self.assertFalse((self.state / "requirements.json").exists(),
                         "the prefix was written as if it were the listing")
        rec = self.index(engine)["issues"][engine._thread_key(1)]
        self.assertEqual(rec["read_paused"]["reason"],
                         "requirement.history_incomplete",
                         "an incomplete history must stop the issue, not retry it")
        self.assertEqual(rec["state"], "needs_person")

    def test_the_pause_is_lifted_by_the_same_command(self):
        src = self.PrefixOnlySource(range(1, 121))
        engine = self.engine(src, max_pages=10)
        engine.run_once()
        out = engine.resume_read(1, by="probe", max_pages=10)
        self.assertTrue(out["changed"])
        rec = self.index(engine)["issues"][engine._thread_key(1)]
        self.assertEqual(rec.get("read_paused"), {})
        self.assertEqual(rec["state"], "idle", "the queue still says 'needs a person'")
        self.assertEqual(engine.status()["items"], [],
                         "the dismissed row is still listed")


class EngineDeliverableStop(unittest.TestCase):
    """The stall must be a stop with a way back, not a silent retry loop."""

    def test_the_pause_is_one_row_and_reads_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = TruncatedSource(range(1, 121))
            src.flow = lambda issue: "at"
            env = _tmp_engine(Path(tmp), src, max_pages=1)
            for _ in range(3):
                summary = env.run_once()
            rec = env.store.load_index()["issues"][env._thread_key(1)]
            rows = [r for r in rec["needs_person"]
                    if r["reason"] == "read.pagination_incomplete"]
            self.assertEqual(len(rows), 1, "the queue grew one row per round")
            src.requests.clear()
            env.run_once()
            self.assertEqual(src.requests, [],
                             "a paused issue was read again - that is the retry")

    def test_resume_read_with_a_budget_lets_the_history_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = TruncatedSource(range(1, 121))
            src.flow = lambda issue: "at"
            env = _tmp_engine(Path(tmp), src, max_pages=1)
            env.run_once()
            self.assertEqual(env.store.load_index()["issues"]
                             [env._thread_key(1)]["read_paused"]["reason"],
                             "read.pagination_incomplete")
            self.assertTrue(env.resume_read(1, by="probe", max_pages=10)["changed"])
            env.run_once()
            rec = env.store.load_index()["issues"][env._thread_key(1)]
            self.assertEqual(rec.get("read_paused"), {},
                             "the lift re-paused on the next round")
            self.assertTrue(
                rec["needs_person"] == [] or all(
                    r["reason"] != "requirement.history_incomplete"
                    for r in rec["needs_person"]),
                "the history still reads as incomplete after a deep enough walk")


class CrossIssueMarkingIsScoped(Base):
    """P2-4 through the **engine** path, not just ``project_comments``.

    QA's synthetic reproduction showed B's complete read stamping A's entry.
    The fix scopes the marking to ``history_scope`` plus the rids whose fences
    appeared in this batch. This test drives the real ``_round_issue`` with two
    issues so the shared ``index["requirements"]`` is exercised the way the
    engine uses it.

    ``_tmp_engine`` pins ``issues: [1]``, so the two-issue allowlist is written
    here; B's package declares **B's** rid (a fixture that reused A's rid would
    legitimately mark A, and would test nothing).
    """

    def _engine_two_issues(self, source):
        import tempfile
        from pathlib import Path
        root = Path(tempfile.mkdtemp())
        allow = root / "allowlist.json"
        good = json.loads(json.dumps(GOOD))
        good["projects"]["demo"]["issues"] = [1, 2]
        allow.write_text(json.dumps(good), encoding="utf-8")
        events = root / "sessions" / "events.jsonl"
        events.parent.mkdir(parents=True, exist_ok=True)
        events.write_text("", encoding="utf-8")
        cfg = config.load(None, {
            "mode": "assist",
            "github": {"source": "none", "repo": "owner/demo",
                       "max_comments_per_issue": 50, "max_comment_pages": 10},
            "engine": {"archived_crosscheck": False,
                       "session_events_path": str(events)}})
        engine = Engine(cfg, Store(root / "state"), source, None,
                        project_allowlist=ProjectAllowlist(allow))
        engine.store.save_index({
            "version": 1, "meta": {}, "issues": {}, "assignments": {},
            "requirements": {"schema_version": 1, "seen_comment_ids": [],
                             "errors": [], "requirements": {
                                 "owner/demo#1": {
                                     "requirement_id": "owner/demo#1",
                                     "current_revision": 1, "status": "ready",
                                     "revisions": {}, "approvals": [],
                                     "errors": []}}}})
        return engine, root

    def _source(self, rows_by_issue, *, truncate_issue_1=False):
        class TwoIssues(TruncatedSource):
            page_size = 100

            def issues(self, limit=20):
                return [1, 2]

            def flow(self, issue):
                return "at"

            def _get_with_headers(self, path, query=None):
                query = dict(query or {})
                self.requests.append(query)
                issue = int(path.split("/issues/")[1].split("/")[0])
                page = int(query.get("page") or 1)
                rows = rows_by_issue.get(issue, [])
                start = (page - 1) * self.page_size
                chunk = rows[start:start + self.page_size]
                headers = {}
                # ``truncate_issue_1`` hides the page count on issue 1: the
                # walk then cannot prove it reached the end and must say so.
                if chunk and not (truncate_issue_1 and issue == 1 and page < 3):
                    total = max(1, -(-len(rows) // self.page_size))
                    headers["Link"] = (
                        f'<https://api.github.com/repos/owner/demo/issues/{issue}'
                        f'/comments?per_page=100&page={total}>; rel="last"')
                return chunk, headers
        return TwoIssues([])

    def test_a_complete_read_of_b_does_not_mark_a(self):
        """A's own read is truncated this round, so its flag must stay absent.

        Both issues are read every round, so "A was not read at all" is not a
        shape the engine produces. The discriminating case is A **incomplete**
        and B **complete** in the same round: the shared projection must not let
        B's claim reach A. A truncated A is built by giving issue 1 a listing
        whose end the budget cannot prove.
        """
        value = package()
        value["requirement_id"] = "owner/demo#2"
        value["requirement_hash"] = req.requirement_hash(value)
        rows = {
            # Twelve full pages with no page count against a budget of ten:
            # the probe cannot reach the end, which is the incomplete shape.
            1: [{"id": 100 + n, "body": "filler", "user": {"login": "u"},
                 "created_at": ""} for n in range(1200)],
            2: [{"id": 300, "body": package_comment(300, value)["body"],
                 "user": {"login": "at"}, "created_at": ""}],
        }
        source = self._source(rows, truncate_issue_1=True)
        engine, root = self._engine_two_issues(source)
        summary = engine.run_once()
        self.assertEqual(summary["errors"], [])
        stored = req.load_projection(root / "state")
        self.assertFalse(req.history_complete(stored, "owner/demo#1"),
                         "B's complete read certified A's truncated history")
        self.assertTrue(req.history_complete(stored, "owner/demo#2"),
                        "B's own entry was not marked")

    def test_a_read_with_no_fence_still_marks_its_own_thread(self):
        """The case ``history_scope`` exists for: nothing to learn from fences.

        A thread whose history contains no requirement fence at all (first
        round, or a thread that has published nothing) must still record that
        *its* listing was read whole - otherwise its first approval would be
        refused for a reason nobody can see. Marking by fence alone cannot do
        that, which is why the engine passes the thread explicitly.

        Both issues are read this round (both listings are empty and therefore
        complete), so both entries are legitimately ``True``; what this pins is
        that ``owner/demo#2`` - which has no fence anywhere - got an entry at
        all.
        """
        source = self._source({2: []})
        engine, root = self._engine_two_issues(source)
        summary = engine.run_once()
        self.assertEqual(summary["errors"], [])
        stored = req.load_projection(root / "state")
        self.assertIn("owner/demo#2", stored.get("requirements") or {},
                      "a complete read of a thread with no fences recorded nothing")
        self.assertTrue(req.history_complete(stored, "owner/demo#2"),
                        "the thread's own read was not recorded")
        self.assertTrue(req.history_complete(stored, "owner/demo#1"),
                        "issue 1 was read this round too and must say so")


class PagedBatchIsOneSnapshot(unittest.TestCase):
    """The incremental rows and the history belong to **one** request run.

    Issue #128 names ``github_source.paged_batch``: two walks could straddle a
    comment arriving between them, so the round would consume a row the
    projection never saw while both sides believe they agree.
    """

    def test_it_reads_once_and_carries_both_claims(self):
        src = TruncatedSource(range(1, 121))
        batch = github_source.paged_batch(src, 1, cursor_before=50,
                                          max_pages=10, whole_listing=True)
        self.assertEqual(len(src.requests), 2,
                         f"one walk, page 1 then the newest page: {src.requests}")
        self.assertTrue(batch.complete)
        self.assertEqual(batch.cursor_before, 50)
        self.assertEqual([c["id"] for c in batch.rows], list(range(1, 121)))

    def test_an_incomplete_walk_is_reported_in_the_batch(self):
        src = TruncatedSource(range(1, 121))
        batch = github_source.paged_batch(src, 1, max_pages=1)
        self.assertFalse(batch.complete)
        self.assertFalse(batch.contiguous)
        self.assertTrue(batch.reason)

    def test_a_plain_list_backend_claims_its_whole_listing(self):
        class Plain:
            def paged(self, issue, *, since_id=0, max_pages=10):
                return [{"id": 1}, {"id": 2}]

        batch = github_source.paged_batch(Plain(), 1)
        self.assertTrue(batch.complete)
        self.assertEqual(len(batch), 2)

    def test_the_batch_speaks_the_same_claim_vocabulary(self):
        """A missing attribute reads as "no claim" - i.e. proceed. Never again.

        ``walk_covers_cursor`` answers ``True`` for anything without
        ``pagination_complete`` (the legacy plain-list contract). A batch that
        named its claims ``complete``/``contiguous`` therefore sailed straight
        through the guard while claiming nothing - the exact silent-stall shape
        this whole issue is about, introduced by a rename.
        """
        src = TruncatedSource(range(1, 121))
        batch = github_source.paged_batch(src, 1, max_pages=1)
        self.assertTrue(hasattr(batch, "pagination_complete"))
        self.assertTrue(hasattr(batch, "contiguous"))
        self.assertEqual(batch.pagination_complete, batch.complete)
        self.assertEqual(batch.stop_reason, batch.reason)
        from super_agent.engine_parts import claims
        self.assertFalse(claims.walk_covers_cursor(batch, 0))
        self.assertFalse(claims.history_is_complete(batch))


def _tmp_engine(root: Path, source, *, max_pages: int) -> Engine:
    state = root / "state"
    allow = root / "allowlist.json"
    allow.write_text(json.dumps(GOOD), encoding="utf-8")
    events = root / "sessions" / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text("", encoding="utf-8")
    cfg = config.load(None, {
        "mode": "assist",
        "github": {"source": "none", "repo": "owner/demo",
                   "max_comments_per_issue": 50, "max_comment_pages": max_pages},
        "engine": {"archived_crosscheck": False,
                   "session_events_path": str(events)}})
    return Engine(cfg, Store(state), source, None,
                  project_allowlist=ProjectAllowlist(allow))


if __name__ == "__main__":
    unittest.main()
