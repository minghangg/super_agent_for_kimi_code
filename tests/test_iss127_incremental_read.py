"""ISS-127: incremental comment reading must not lose, repeat or stall.

The requirement has two halves and both are exercised here against a source
that behaves like the real REST API (page size 100, ``direction`` decides which
end page 1 starts from):

* **read side** - a page that merely contains old rows must not end the walk,
  a failure must not look like an empty listing, and a full page must not look
  like the end of the listing;
* **consumer side** - one round consumes only the prefix it can prove is
  contiguous, the cursor moves to the last id actually consumed, and a hard cap
  stops reading for real (with one manual way back).

The 120-comment scenario is the one the issue names: 120 comments, 100 per page,
50 consumed per round. The pre-fix code consumed 50, then 50, then nothing
forever, because an ascending walk's page 1 *is* the whole older half and the
stop rule "this page contains an old row" fired on it.

Run: ``python -m pytest tests/test_iss127_incremental_read.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from super_agent import cli, config, github_source
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store

GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {"demo": {"repo": "owner/demo", "cwd": ".",
                          "mode": "assist", "creation_mode": "pm_created",
                          "issues": [1], "actions": ["bus_inject"],
                          "max_active_writers": 1}},
}


class RealisticSource(github_source.LiveSource):
    """``LiveSource`` whose ``_get_with_headers`` copies the *measured* API.

    Two measured facts are reproduced on purpose, because a stub that "helps"
    by honouring ``direction`` is what let the previous revision look green:

    * **``direction`` is ignored** - the per-issue comment endpoint answers in
      ascending id order no matter what the query says;
    * **``Link`` states the pagination** - ``rel="next"``/``rel="last"`` carry
      the page numbers, and ``direction`` (if sent) is echoed back.

    ``fail_pages`` injects a transient error on a page number.
    """

    page_size = 100

    def __init__(self, rows, *, per_page=100, fail_pages=(), last_link=True):
        super().__init__("owner/demo")
        self.rows = sorted(rows)
        self.per_page = per_page
        self.page_size = per_page
        self.fail_pages = set(fail_pages)
        self.last_link = last_link
        self.requests: list[dict] = []

    def issues(self, limit=20):
        return [1]

    def title(self, issue):
        return "t"

    def flow(self, issue):
        return ""

    def page_count(self) -> int:
        return max(1, -(-len(self.rows) // self.per_page))

    def _get_with_headers(self, path, query=None):
        query = dict(query or {})
        self.requests.append(query)
        page = int(query.get("page") or 1)
        if page in self.fail_pages:
            raise github_source.SourceError(f"GitHub 502 on {path} (page {page})")
        # Ascending always - ``direction`` is deliberately not consulted.
        start = (page - 1) * self.per_page
        chunk = self.rows[start:start + self.per_page]
        body = [{"id": cid, "body": f"b{cid}", "user": {"login": "u"},
                 "created_at": ""} for cid in chunk]
        headers = {}
        total = self.page_count()
        if self.last_link and total > 1:
            links = []
            if page < total:
                links.append(f'<https://api.github.com/repos/owner/demo/issues/1/'
                             f'comments?per_page={self.per_page}&page={page + 1}>; '
                             f'rel="next"')
            links.append(f'<https://api.github.com/repos/owner/demo/issues/1/'
                         f'comments?per_page={self.per_page}&page={total}>; '
                         f'rel="last"')
            headers["Link"] = ", ".join(links)
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
        # ``--state-dir`` does not isolate the lifecycle stream, so pin it to an
        # empty file in the temp dir (same reason as test_comment_paging).
        self.events = self.root / "sessions" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, source, *, max_comments=50, max_pages=10) -> Engine:
        cfg = config.load(None, {
            "mode": "assist",
            "github": {"source": "none", "repo": "owner/demo",
                       "max_comments_per_issue": max_comments,
                       "max_comment_pages": max_pages},
            "engine": {"archived_crosscheck": False,
                       "session_events_path": str(self.events)}})
        return Engine(cfg, Store(self.state), source, None,
                      project_allowlist=ProjectAllowlist(self.allow))

    def cursor_of(self, engine) -> int:
        return int((engine.store.load_cursor().get("issues") or {})
                   .get(engine._thread_key(1)) or 0)

    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(argv)
        return rc, out.getvalue()


class TwoPagesOf120(Base):
    """The acceptance scenario: 120 comments, 100 per page, 50 per round."""

    def test_cursor_walks_50_100_120_and_nothing_is_lost(self):
        src = RealisticSource(range(1, 121))
        engine = self.engine(src)

        self.assertEqual(engine.run_once()["processed"], 50)
        self.assertEqual(self.cursor_of(engine), 50)
        self.assertEqual(engine.run_once()["processed"], 50)
        self.assertEqual(self.cursor_of(engine), 100)

        # Counter-example 1: the third round must consume 101-120. The pre-fix
        # code processed 0 here and kept the cursor at 100 for every later round.
        third = engine.run_once()
        self.assertEqual(third["processed"], 20,
                         "round 3 consumed nothing: 101-120 were left behind")
        self.assertEqual(self.cursor_of(engine), 120)

        fourth = engine.run_once()
        self.assertEqual(fourth["processed"], 0)
        self.assertEqual(self.cursor_of(engine), 120, "a settled cursor stays put")

    def test_round3_fetches_the_newest_page_first_and_stops_at_the_cursor(self):
        """Positive evidence: the walk starts at ``rel="last"``, not at page 1."""
        src = RealisticSource(range(1, 121))
        engine = self.engine(src)
        for _ in range(2):
            engine.run_once()
        src.requests.clear()
        engine.run_once()
        pages = [q.get("page") for q in src.requests]
        self.assertEqual(pages[0], 1, "page 1 is probed for Link")
        self.assertEqual(pages[1], 2,
                         f"the first real page must be the newest one, got {pages}")
        self.assertEqual(len(pages), 2, f"the walk stopped early: {pages}")

    def test_no_comment_id_is_consumed_twice(self):
        src = RealisticSource(range(1, 121))
        engine = self.engine(src)
        for _ in range(4):
            engine.run_once()
        seen = self._processed_ids()
        self.assertEqual(seen, list(range(1, 121)),
                         "every id exactly once, in order")
        self.assertEqual(len(seen), len(set(seen)))

    def _processed_ids(self) -> list[int]:
        path = Store(self.state).events_path
        ids = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "comment.processed":
                ids.append(int(row["comment_id"]))
        return ids


class TheEndpointIgnoresOrdering(unittest.TestCase):
    """Measured API facts, pinned so the old assumption cannot come back.

    Both were observed against the public API directly (with a token): sending
    ``direction=desc`` returns the same ascending ids, and the ``Link`` header
    echoes ``direction`` back. The engine must therefore never depend on it.
    """

    def _source(self):
        return RealisticSource(range(1, 21), per_page=5)

    def test_direction_is_ignored_by_the_backend(self):
        src = self._source()
        body, _headers = src._get_with_headers(
            "/repos/owner/demo/issues/1/comments",
            {"per_page": 5, "page": 1, "direction": "desc"})
        self.assertEqual([r["id"] for r in body], [1, 2, 3, 4, 5],
                         "desc must not reverse the listing: the real API ignores it")

    def test_link_is_the_only_source_of_the_page_count(self):
        src = self._source()
        _body, headers = src._get_with_headers(
            "/repos/owner/demo/issues/1/comments", {"per_page": 5, "page": 1})
        self.assertIn('rel="last"', headers.get("Link", ""))
        self.assertEqual(src._link_last_page(headers), 4)

    def test_the_engine_never_sends_sort_or_direction(self):
        src = RealisticSource(range(1, 121))
        engine_tests = Base()
        engine_tests.setUp()
        try:
            engine = engine_tests.engine(src)
            engine.run_once()
        finally:
            engine_tests.tearDown()
        sent = [q for q in src.requests if "direction" in q or "sort" in q]
        self.assertEqual(sent, [],
                         "the reader must not rely on parameters the endpoint ignores")

    def test_a_backend_without_link_still_works_by_short_page(self):
        """No Link at all: a short page is the end, a full one is probed once."""
        src = RealisticSource(range(1, 6), per_page=5, last_link=False)
        rows = src.paged(1)
        self.assertTrue(rows.pagination_complete)
        self.assertEqual([c["id"] for c in rows], [1, 2, 3, 4, 5])


class ReadSideCounterExamples(unittest.TestCase):
    """Counter-examples 2, 3 and 4 as executable checks."""

    def test_2b_the_live_walk_survives_comments_from(self):
        """A tuple-returning ``paged`` must break here, not in production."""
        src = RealisticSource(range(1, 31), per_page=10)
        rows = github_source.comments_from(src, 1)
        self.assertEqual([c["id"] for c in rows], list(range(1, 31)))
        self.assertIsInstance(github_source.paged_result(src, 1), list)

    def test_2_a_plain_list_backend_is_never_unpacked(self):
        class Plain:
            def paged(self, issue, *, since_id=0, max_pages=10):
                return [{"id": 9001}]

        self.assertEqual(
            [c["id"] for c in github_source.comments_from(Plain(), 1)],
            [9001], "comments_from must not assume (rows, complete)")

    def test_3_a_full_page_is_not_a_finished_listing(self):
        """Counter-example 3: a full page is not proof, only ``rel="last"`` is."""
        src = RealisticSource(range(1, 401))
        rows = src.paged(1, since_id=0, max_pages=1)
        self.assertFalse(rows.pagination_complete,
                         "one full page is not proof the listing ended")
        self.assertEqual([q.get("page") for q in src.requests], [1],
                         "only the Link probe happened; no full page was consumed")

    def test_3b_a_budget_that_stops_before_the_oldest_page_is_incomplete(self):
        src = RealisticSource(range(1, 301))
        rows = src.paged(1, since_id=0, max_pages=2)
        self.assertFalse(rows.pagination_complete,
                         "two of three pages is not the whole listing")
        self.assertEqual([q.get("page") for q in src.requests], [1, 3],
                         "probe page 1, then the newest page")

    def test_3c_the_whole_listing_is_complete_when_the_budget_allows(self):
        src = RealisticSource(range(1, 301))
        rows = src.paged(1, since_id=0, max_pages=4)
        self.assertTrue(rows.pagination_complete)
        self.assertEqual([c["id"] for c in rows], list(range(1, 301)))

    def test_4_a_dead_page_raises_and_returns_nothing(self):
        src = RealisticSource(range(1, 301), fail_pages={2})
        with self.assertRaises(github_source.SourceError):
            src.paged(1, since_id=0)
        self.assertIn(2, [q.get("page") for q in src.requests],
                      "the failure happened on the page it asked for")
        self.assertEqual([q.get("page") for q in src.requests], [1, 3, 2],
                         "probe page 1, newest page 3, then page 2 fails; no retry")


class ContiguousFalseIsARefusal(Base):
    """P0-1 (arbitration 7133149401): ``contiguous=False`` must never be overridden.

    The deleted fallback branch let a walk that *stated* it had not proven the
    gap-free prefix pass anyway, whenever any returned row happened to be at or
    below the cursor. A walk with a missing middle page looks exactly like that
    from the rows alone, so the inference is unsound - and it is what produced
    the silent stall. Only the backend's own claim may license a read.
    """

    class LossyWalk(github_source.PagedComments):
        """Reports rows reaching the cursor while denying the claim."""

        def __init__(self, rows):
            super().__init__(rows, complete=False, contiguous=False,
                             pages=[{"page": 2, "rows": len(rows), "status": "ok"}],
                             reason="middle page missing")

    def test_rows_reaching_the_cursor_do_not_override_the_claim(self):
        # 100 IS the cursor: the deleted fallback passed such a walk.
        walked = self.LossyWalk([{"id": 90}, {"id": 100}, {"id": 101}, {"id": 120}])
        self.assertFalse(
            Engine._walk_covers_cursor(walked, 100),
            "a row <= cursor must not turn contiguous=False into a green light")

    def test_the_engine_pauses_instead_of_consuming(self):
        class LossySource(RealisticSource):
            """Walks reach the cursor but never claim contiguity."""

            def paged(self, issue, *, since_id=0, max_pages=10):
                rows = super().paged(issue, since_id=since_id, max_pages=max_pages)
                return self.Lossy(rows)

            class Lossy(github_source.PagedComments):
                def __init__(self, rows):
                    super().__init__(list(rows), complete=False, contiguous=False,
                                     pages=[{"page": 1, "rows": len(rows),
                                             "status": "ok"}],
                                     reason="middle page missing")

        src = LossySource(range(1, 121))
        engine = self.engine(src)
        # L4 (ISS-128): the cursor must already be **above 0**, or no returned
        # row can be at/below it and the deleted fallback branch is never
        # reached - the test would stay green after restoring that branch.
        # 50 is a mid-listing cursor, so rows 51..120 are above it and the walk
        # reaches it while still denying contiguity: exactly the shape the
        # fallback used to wave through.
        engine.store.save_cursor({"issues": {engine._thread_key(1): 50}})
        summary = engine.run_once()
        self.assertEqual(summary["processed"], 0,
                         "rows were consumed from an unproven walk")
        self.assertEqual(self.cursor_of(engine), 50,
                         "the cursor moved on a walk that proved nothing")
        rec = engine.store.load_index()["issues"][engine._thread_key(1)]
        self.assertEqual(rec["read_paused"]["reason"], "read.pagination_incomplete")
        self.assertEqual(int(rec["read_paused"]["cursor"]), 50,
                         "the pause must record the cursor it could not cover")

    def test_a_row_at_the_cursor_does_not_turn_the_refusal_into_a_read(self):
        """The fallback branch, exercised end to end with a real engine.

        ``Lossy`` returns rows that *include* an id at the cursor, which is the
        exact condition the deleted branch tested: "some returned row is
        ``<= cursor``, therefore the prefix is fine". Any restoration of that
        inference turns this red.
        """
        class AtCursorWalk(github_source.PagedComments):
            def __init__(self, rows, cursor):
                kept = [row for row in rows if row["id"] <= cursor + 5]
                super().__init__(kept, complete=False, contiguous=False,
                                 pages=[{"page": 1, "rows": len(kept),
                                         "status": "ok"}],
                                 reason="middle page missing")

        class AtCursorSource(RealisticSource):
            def paged(self, issue, *, since_id=0, max_pages=10):
                rows = super().paged(issue, since_id=since_id, max_pages=max_pages)
                return AtCursorWalk(rows, since_id)

        src = AtCursorSource(range(1, 121))
        engine = self.engine(src)
        engine.store.save_cursor({"issues": {engine._thread_key(1): 50}})
        summary = engine.run_once()
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(self.cursor_of(engine), 50)


class FullSecondPageWithoutLink(Base):
    """P0-2 (arbitration 7133149401): a full page 2 with no ``Link`` is unknown.

    The duplicated ``return 2, ...`` read "page 2 is full, therefore the listing
    has two pages", which lets the walk reach page 1, declare itself complete and
    truncate everything past row 200.

    ISS-128 (L2) keeps that rule and adds the other half: the probe must go as
    deep as the budget allows, so a bigger budget can *learn* the page count
    instead of answering "unknown" forever.
    """

    def test_a_full_second_page_is_not_two_pages(self):
        src = RealisticSource(range(1, 301), last_link=False)
        last, pages, rows = src._locate_last_page(1, max_pages=2)
        self.assertIsNone(last, "a full page 2 with no Link proves nothing")
        self.assertEqual([p["page"] for p in pages], [1, 2])

    def test_a_deeper_budget_learns_the_page_count(self):
        """L2: the same listing, one page deeper, is a known 3 pages."""
        src = RealisticSource(range(1, 301), last_link=False)
        last, pages, rows = src._locate_last_page(1, max_pages=10)
        self.assertEqual(last, 4, "an empty page 4 is proof the listing ended at 3")
        self.assertEqual([p["page"] for p in pages], [1, 2, 3, 4])

    def test_the_walk_does_not_claim_completeness(self):
        src = RealisticSource(range(1, 301), last_link=False)
        walked = src.paged(1, since_id=0, max_pages=3)
        self.assertFalse(walked.pagination_complete,
                         "the walk reported the listing as fully read")
        self.assertFalse(walked.contiguous)

    def test_no_prefix_is_consumed_from_the_truncated_walk(self):
        src = RealisticSource(range(1, 301), last_link=False)
        engine = self.engine(src, max_pages=2)
        summary = engine.run_once()
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(self.cursor_of(engine), 0)
        self.assertEqual(summary.get("read_paused"), 1)


class ManualResumeMustActuallyDeepen(Base):
    """L2 (K1): a whole-number-of-pages listing without ``Link`` must be liftable.

    The bug this pins: 200 rows, 100 per page, no ``Link``. The old probe looked
    at page 1 and page 2 only, both full, and answered "unknown" - so the issue
    paused. ``resume-read --max-pages 10`` then only raised the *budget*; the
    probe still stopped at page 2, so the next round paused again and the cursor
    sat at 0 forever. A manual lift that cannot work is not a way back in.
    """

    def _paused_engine(self):
        src = RealisticSource(range(1, 201), per_page=100, last_link=False)
        engine = self.engine(src, max_comments=1000, max_pages=2)
        summary = engine.run_once()
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(summary.get("read_paused"), 1,
                         "the listing was consumed without proving contiguity")
        rec = engine.store.load_index()["issues"][engine._thread_key(1)]
        self.assertEqual(rec["read_paused"]["reason"], "read.pagination_incomplete")
        return src, engine

    def test_a_deeper_probe_learns_the_page_count(self):
        src = RealisticSource(range(1, 201), per_page=100, last_link=False)
        last, _pages, _rows = src._locate_last_page(1, max_pages=2)
        self.assertIsNone(last, "two full pages prove nothing about a third")
        last, _pages, _rows = src._locate_last_page(1, max_pages=10)
        self.assertEqual(last, 3, "page 3 is empty: the listing ended at page 2")

    def test_resume_read_max_pages_really_reads_101_200(self):
        src, engine = self._paused_engine()
        out = engine.resume_read(1, by="probe", max_pages=10)
        self.assertTrue(out["changed"])

        src.requests.clear()
        summary = engine.run_once()
        self.assertEqual(summary.get("read_paused"), 0,
                         "the lift was a no-op: the same probe depth ran again")
        self.assertEqual(self.cursor_of(engine), 200,
                         "the lifted budget read neither half of the listing")
        self.assertEqual(summary["processed"], 200)
        rec = engine.store.load_index()["issues"][engine._thread_key(1)]
        self.assertEqual(rec.get("read_paused"), {}, "paused again after the lift")


class HardCap(Base):
    """Counter-example 5: reaching the cap must stop reading, not retry."""

    def test_a_walk_that_ended_above_the_cursor_pauses_the_issue(self):
        src = RealisticSource(range(1, 401), per_page=100)
        engine = self.engine(src, max_pages=1)
        summary = engine.run_once()
        self.assertEqual(summary["processed"], 0,
                         "the unread prefix may have a hole; nothing may be consumed")
        self.assertEqual(self.cursor_of(engine), 0)
        rec = engine.store.load_index()["issues"][engine._thread_key(1)]
        self.assertEqual(rec["read_paused"]["reason"], "read.pagination_incomplete")

    def test_the_next_round_issues_no_request_at_all(self):
        src = RealisticSource(range(1, 401))
        engine = self.engine(src, max_pages=1)
        engine.run_once()
        src.requests.clear()
        for _ in range(2):
            summary = engine.run_once()
            self.assertEqual(summary["processed"], 0)
        self.assertEqual(src.requests, [],
                         "a paused issue must not be read again - that is the retry "
                         "the issue forbids")

    def test_the_pause_currently_needs_a_person(self):
        """One row, not one per round: the queue must not grow while it waits."""
        src = RealisticSource(range(1, 401))
        engine = self.engine(src, max_pages=1)
        for _ in range(3):
            engine.run_once()
        rec = engine.store.load_index()["issues"][engine._thread_key(1)]
        rows = [r for r in rec["needs_person"]
                if r.get("reason") == "read.pagination_incomplete"]
        self.assertEqual(len(rows), 1)

    def test_cli_resume_read_is_the_way_back_in(self):
        src = RealisticSource(range(1, 401))
        engine = self.engine(src, max_pages=1)
        engine.run_once()

        rc, out = self.run_cli(["resume-read", "--state-dir", str(self.state),
                                "--allowlist", str(self.allow),
                                "--issue", "1", "--by", "probe"])
        self.assertEqual(rc, 0, out)
        rec = Store(self.state).load_index()["issues"]["owner/demo#1"]
        self.assertEqual(rec.get("read_paused"), {})

        src.requests.clear()
        engine = self.engine(src, max_pages=1)
        engine.run_once()
        self.assertEqual(len(src.requests), 1, "after the lift it reads again")

    def test_resume_read_max_pages_makes_the_lift_stick(self):
        """P1-1: a lift must not re-pause on the very next round."""
        src = RealisticSource(range(1, 1201))
        engine = self.engine(src, max_comments=50, max_pages=10)
        self.assertEqual(engine.run_once()["processed"], 0)
        self.assertEqual(self.cursor_of(engine), 0)

        out = engine.resume_read(1, by="probe", max_pages=14)
        self.assertTrue(out["changed"])
        src.requests.clear()
        summary = engine.run_once()
        self.assertEqual(summary.get("read_paused"), 0,
                         "the lifted pause came straight back")
        self.assertEqual(summary["processed"], 50)
        self.assertEqual(self.cursor_of(engine), 50)

    def test_the_lifted_budget_applies_once(self):
        src = RealisticSource(range(1, 1201))
        engine = self.engine(src, max_comments=50, max_pages=10)
        engine.run_once()
        engine.resume_read(1, by="probe", max_pages=14)
        engine.run_once()
        rec = engine.store.load_index()["issues"][engine._thread_key(1)]
        self.assertEqual(rec.get("read_budget"), {},
                         "the pending budget is spent, not a standing setting")

    def test_resume_read_clears_the_person_surface(self):
        """P1-2: one command, not ``resume-read`` plus ``resolve``."""
        src = RealisticSource(range(1, 401))
        engine = self.engine(src, max_pages=1)
        engine.run_once()
        rec = engine.store.load_index()["issues"][engine._thread_key(1)]
        self.assertEqual(rec["state"], "needs_person")
        self.assertEqual(len(engine.status()["items"]), 1)

        engine.resume_read(1, by="probe")
        rec = engine.store.load_index()["issues"][engine._thread_key(1)]
        self.assertEqual(rec.get("read_paused"), {})
        self.assertEqual(rec["state"], "idle",
                         "the orchestrator still sees 'waiting for a person'")
        self.assertEqual(engine.status()["items"], [],
                         "status still lists the dismissed reason")

    def test_resume_read_reports_nothing_to_do(self):
        engine = self.engine(RealisticSource(range(1, 5)))
        engine.run_once()
        rc, out = self.run_cli(["resume-read", "--state-dir", str(self.state),
                                "--allowlist", str(self.allow),
                                "--issue", "1", "--by", "probe"])
        self.assertEqual(rc, 1, out)
        self.assertIn("no read pause", out)


if __name__ == "__main__":
    unittest.main()
