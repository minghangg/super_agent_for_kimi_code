"""The meta-path blocker the shim tests need, written once, in the PEP 451 form.

``find_module``/``load_module`` are gone in CPython 3.12, and 3.13 does not fail
loudly when a finder still defines only those: the finder is **skipped**, the
import succeeds, and a test that waits for ``ModuleNotFoundError`` fails for a
reason that has nothing to do with the code under test. Measured before ISS-136
fixed it: ``1 failed`` on 3.13 against ``25 passed`` on 3.10.

``find_spec`` (PEP 451, 3.4+) is the only protocol both target interpreters
honour, so the blocker is built here and never re-typed at a call site - two
copies is how one of them gets left on the legacy protocol again.

Usage: the returned source is run in a **subprocess**, so the block never leaks
into the test process itself::

    code = "import sys, importlib\n" + blocker_source(
        "fullname.startswith('super_agent.')", message="inner missing")
    subprocess.run([sys.executable, "-c", code + "importlib.import_module(...)"])
"""
from __future__ import annotations


def blocker_source(condition: str, *, name: str = "spec.name",
                   message: str = "blocked") -> str:
    """Source for a finder that refuses every ``fullname`` matching ``condition``.

    ``condition`` is a Python expression over ``fullname`` - the *selection* stays
    at the call site because "which modules" is the thing each test is about.
    ``name`` is the ``ModuleNotFoundError.name`` expression, so a test can assert
    which module the caller was told is missing. ``message`` is the exception text.
    """
    if not condition or not condition.strip():
        raise ValueError("condition must select the blocked modules")
    return (
        "import importlib.machinery\n"
        "class Blocker:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        f"        if {condition}:\n"
        "            return importlib.machinery.ModuleSpec(fullname, self)\n"
        "    def create_module(self, spec):\n"
        f"        raise ModuleNotFoundError({message!r}, name={name})\n"
        "    def exec_module(self, module):\n"
        "        pass\n"
        "sys.meta_path.insert(0, Blocker())\n"
    )
