"""``tools/verify_counterexamples.py`` is a check, so pytest must run it.

The script sat outside ``testpaths`` since it was written: 478 green tests said
nothing about it, and example 5 kept asserting a ``completed`` that the archive
gate had already removed. This file closes that hole without putting ``tools/``
into ``testpaths`` (which would collect the non-test scripts too).

Two guards:

* the opt-in half - the read-only ``api.session_info`` call against the real
  session service - is a separate test that skips unless
  ``SAFKC_VERIFY_REAL_SID`` is set, so a machine without that variable stays
  green instead of failing on the network;
* everything else runs in the script's own ``tempfile`` scenario. The child
  process gets the state-directory variables removed as well, because
  ``session_events.resolve_events_path`` honours them and an inherited value
  would point the child at a live workbench.

Run: ``python -m pytest tests/test_verify_scripts.py -q``
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "verify_counterexamples.py"
sys.path.insert(0, str(ROOT / "tools"))

import verify_counterexamples as vc  # noqa: E402

#: Names that make the script read a live workbench instead of its own fixture.
HERMETIC_DROP = ("SESS_EVENTS_STATE_DIR", "LWA_STATE_DIR", "SAFKC_VERIFY_REAL_SID")


def child_env(**overrides: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in HERMETIC_DROP}
    env.update(overrides)
    return env


def run_script(**overrides: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT,
                          capture_output=True, text=True,
                          env=child_env(**overrides))


class Hermetic(unittest.TestCase):
    """No network, no real sid: the accounting half alone must be green."""

    def test_all_five_hold(self):
        done = run_script()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("summary: 5/5 counter-examples hold", done.stdout)
        self.assertNotIn("[FAIL]", done.stdout)

    def test_example5_accounting_stops_at_waiting_release(self):
        # Driven in-process so the failure names the record, not just a return
        # code. ``RESULTS`` is module state, so it is cleared first.
        vc.RESULTS.clear()
        saved = {k: os.environ.pop(k) for k in HERMETIC_DROP if k in os.environ}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                vc.example_5()
        finally:
            os.environ.update(saved)
        self.assertEqual(len(vc.RESULTS), 1)
        result = vc.RESULTS[0]
        self.assertTrue(result["ok"], result["detail"])
        self.assertIn("status=waiting_release", result["detail"])
        self.assertNotIn("status=completed", result["detail"])

    def test_header_states_the_semantics_it_follows(self):
        # The rule Reed made binding: a milestone that moves a transition has to
        # move this file's assertions with it, so the header must say which
        # milestone the assertions follow.
        head = SCRIPT.read_text(encoding="utf-8").split('"""')[1]
        for marker in ("verify-semantics:", "follows:", "synced-at:"):
            self.assertIn(marker, head)
        self.assertIn("milestone change must update the assertions", head)


class ServerSide(unittest.TestCase):
    """Opt-in, read-only: only a machine holding a known archived sid runs it."""

    @unittest.skipUnless(os.environ.get("SAFKC_VERIFY_REAL_SID"),
                         "set SAFKC_VERIFY_REAL_SID=<archived sid> to check the "
                         "real session service")
    def test_probe_session_is_archived_on_the_server(self):
        done = run_script(SAFKC_VERIFY_REAL_SID=os.environ["SAFKC_VERIFY_REAL_SID"])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("summary: 5/5 counter-examples hold", done.stdout)
        self.assertIn("real archived=True", done.stdout)


if __name__ == "__main__":
    unittest.main()
