"""Pin the suite to "no private file", so a run does not depend on the laptop.

``agent_config`` reads ``new_workbench/config/agent.json`` when
``$SAFKC_AGENT_CONFIG`` is unset, and that file is private, untracked and absent
from a clone. A checkout that happens to carry one then runs a *different*
suite: measured 11 failed live against 2 failed in a clean clone (P1-1, #98
fix 1), which makes every red ambiguous between "the code is wrong" and "this
machine is configured".

Pointing the variable at a path that cannot exist makes the two runs one shape:
every test starts from the public defaults, and a test that wants a private
file sets its own variable (``tests/test_agent_config.py`` does, per test).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ``<checkout>/tests/no-such-agent.json``: inside the checkout only so the
# name is self-explanatory in a traceback; it is never created.
os.environ["SAFKC_AGENT_CONFIG"] = str(ROOT / "tests" / "no-such-agent.json")
