"""The read side says why it missed, and stays off stdout (#92 §F).

Run: ``python -m pytest tests/test_iss92_read_side.py -q``

Two contracts, both broken by a silent ``{}``:

* a cache miss must be **machine-checkable** (``_degraded``), or a caller cannot
  tell "the daemon has nothing" from "nobody told me where the cache is";
* a degradation notice must go to **stderr**, or every consumer that parses
  ``sess.py status``'s stdout gets ``Extra data`` instead of a session.
"""
from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from super_agent import direct


def _cache(path: Path, sid: str, *, age_s: int = 0, ctx: int = 12345) -> Path:
    path.write_text(json.dumps({"sessions": {sid: {
        "ts": int(time.time()) - age_s,
        "status": {"busy": False, "context_tokens": ctx,
                   "model": "m", "max_context_tokens": 1000},
    }}}), encoding="utf-8")
    return path


class CacheMisses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_no_cache_file_is_visible(self):
        with redirect_stderr(io.StringIO()) as err:
            rec = direct.cached("sid-1")
        self.assertEqual(rec, {"_degraded": "no_cache_file"})
        self.assertIn("cache_file", err.getvalue())

    def test_a_missing_file_is_visible(self):
        rec = direct.cached("sid-1", cache_file=self.root / "nope.json")
        self.assertEqual(rec.get("_degraded"), "missing")

    def test_an_unknown_sid_is_visible(self):
        path = _cache(self.root / "c.json", "other")
        self.assertEqual(direct.cached("sid-1", cache_file=path).get("_degraded"),
                         "missing")

    def test_a_stale_record_is_visible_with_its_timestamp(self):
        path = _cache(self.root / "c.json", "sid-1", age_s=3600)
        rec = direct.cached("sid-1", max_age=1, cache_file=path)
        self.assertEqual(rec.get("_degraded"), "stale")
        self.assertIn("ts", rec)

    def test_a_fresh_record_still_reads(self):
        path = _cache(self.root / "c.json", "sid-1")
        rec = direct.cached("sid-1", max_age=180, cache_file=path)
        self.assertNotIn("_degraded", rec)
        self.assertEqual(rec["status"]["context_tokens"], 12345)

    def test_the_old_kimi_ws_cache_is_not_a_fix(self):
        # §P1-2 反例 A: pointing at the dead file must still miss, because the
        # target sid is simply not in it - "change the path" is not a fix.
        path = _cache(self.root / "kimi_ws_cache.json", "someone-else")
        self.assertEqual(direct.cached("sid-1", cache_file=path).get("_degraded"),
                         "missing")


class Threading(unittest.TestCase):
    """The module's own callers pass the path down (#92 §F.3)."""

    def test_all_three_internal_reads_pass_the_argument(self):
        import inspect

        src = inspect.getsource(direct)
        for line in src.splitlines():
            if "rec = cached(" in line:
                with self.subTest(line=line.strip()):
                    self.assertIn("cache_file=cache_file", line)

    def test_status_reports_the_degraded_reason(self):
        with mock.patch.object(direct, "session_info", lambda sid: {"_err": "dead"}):
            with mock.patch.object(direct, "is_new", lambda: True):
                with mock.patch.object(direct, "_ws_status", lambda sid: {}):
                    with redirect_stderr(io.StringIO()):
                        out = direct.status("sid-1")
        self.assertEqual(out.get("_degraded"), "no_cache_file")
        self.assertIn("cache:no_cache_file", out.get("_source") or "")

    def test_bus_status_passes_the_path(self):
        from super_agent import bus

        seen = {}

        def fake(sid, cache_file=None):
            seen["cache_file"] = cache_file
            return {"context_tokens": 1, "_source": "x"}

        marker = Path("state/wss_bus_cache.json")
        with mock.patch.object(bus, "mode", lambda: "direct"):
            with mock.patch.object(bus.K, "status", fake):
                bus.status("sid-1", caller="t", cache_file=marker)
        self.assertEqual(seen["cache_file"], marker)


class StdoutIsForData(unittest.TestCase):
    """A degradation notice is stderr; the daemon's console log is not."""

    def test_note_degraded_writes_stderr_not_stdout(self):
        from super_agent import wss_bus

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(wss_bus, "current_state_root",
                               lambda: Path(tempfile.gettempdir()) / "iss92_probe"):
            with redirect_stdout(out), redirect_stderr(err):
                wss_bus.note_degraded("probe: test")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("degraded: probe: test", err.getvalue())

    def test_log_still_writes_stdout(self):
        # The global console log is deliberately unchanged (§P1-3 处置表).
        from super_agent import wss_bus

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(wss_bus, "current_state_root",
                               lambda: Path(tempfile.gettempdir()) / "iss92_probe"):
            with redirect_stdout(out), redirect_stderr(err):
                wss_bus.log("plain line")
        self.assertIn("plain line", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_a_consumer_parsing_stdout_survives_a_degradation(self):
        # The shape the six consumers rely on: ``json.loads(stdout)``.
        from super_agent import wss_bus

        payload = {"busy": False}
        out = io.StringIO()
        with mock.patch.object(wss_bus, "current_state_root",
                               lambda: Path(tempfile.gettempdir()) / "iss92_probe"):
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                wss_bus.note_degraded("sess.py status: daemon 不可用或未 ready")
                print(json.dumps(payload, ensure_ascii=False), file=out)
        self.assertEqual(json.loads(out.getvalue()), payload)


if __name__ == "__main__":
    unittest.main()
