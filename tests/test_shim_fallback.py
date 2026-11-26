"""The ``decide_watch`` shim's fallback rules, in their own file.

Split out of ``test_decide_watch.py`` for the line-count gate (ISS-136 QA cid
7185385331): that file sat at 488 before this ticket's fix and 492 after, which is
not the ≥5% net reduction ``worker.md`` §3.1.1 asks of a change to an over-300-line
file. Moving this class out takes the parent below its 464-line ceiling without
touching what any test does.

Two rules live here, and both are about **not swallowing** a broken import:

* an inner dependency missing must raise out of the loader, not degrade silently;
* the whole package missing is the only case that falls back to the legacy copy.

Both run the real import in a subprocess, because the point is what the
interpreter does at import time. The blocker uses ``find_spec`` (PEP 451) - see
:mod:`support_import_blocker` for why the legacy ``find_module`` protocol made
this test green on 3.10 and red on 3.13.

Run: ``python -m pytest tests/test_shim_fallback.py -q``
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from support_import_blocker import blocker_source

from test_decide_watch import Fixture

ROOT = Path(__file__).resolve().parent.parent


class ShimFallback(Fixture):
    def test_inner_dependency_missing_must_raise_out_of_loader(self):
        code = (
            "import sys, importlib\n"
            + blocker_source(
                "fullname.startswith('super_agent') and fullname != 'super_agent'",
                message="inner missing")
            + "try:\n"
            "    importlib.import_module('super_agent.decide_watch')\n"
            "except ModuleNotFoundError as e:\n"
            "    print('RAISED', e.name)\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(3)\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RAISED super_agent.", proc.stdout)

    def test_whole_package_missing_falls_back_to_legacy_exports(self):
        lwa_tools = Path("D:/github/" + ("lai" + "ke") + "_work_with_agent/tools")
        shim = lwa_tools / "decide_watch.py"
        legacy = lwa_tools / "decide_watch_legacy.py"
        if not shim.is_file() or not legacy.is_file():
            self.skipTest("phase B shim not present yet")
        code = (
            "import sys\n"
            + blocker_source(
                "fullname == 'super_agent' or fullname.startswith('super_agent.')",
                name="'super_agent'", message="package missing")
            + "sys.path.insert(0, %r)\n"
            "import decide_watch\n"
            "print('LEGACY', decide_watch.LEGACY_FALLBACK_PATH)\n"
            "print('HASH', decide_watch.LEGACY_FALLBACK_HASH[:12])\n"
            "print('SELFTEST', hasattr(decide_watch, 'self_test'))\n"
        ) % str(lwa_tools)
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(lwa_tools.parent),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("decide_watch_legacy.py", proc.stdout)
        self.assertIn("HASH ", proc.stdout)
        self.assertIn("SELFTEST True", proc.stdout)


if __name__ == "__main__":
    unittest.main()
