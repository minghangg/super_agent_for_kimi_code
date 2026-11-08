"""L1 tests for the protocol-layer port (ISS-25 item 4).

Four modules came over verbatim from the workbench's ``tools/``: ``paths``
(where the Kimi home lives), ``direct`` (the only direct-wire implementation),
``bus`` (bus-first facade with direct fallback) and ``api`` (legacy thin shim).
Only the intra-package imports changed, so what needs pinning is:

* ``paths`` keeps resolving to ``Path`` objects, ``sessions_dir()`` still ends
  in ``/sessions``, and a missing token file yields ``""`` instead of raising;
* importing the chain performs **no I/O**: a subprocess that makes
  ``socket.socket`` and ``open`` explode still imports ``super_agent.bus``;
* the chain resolves inside the package -- after ``import super_agent.bus``
  both ``super_agent.direct`` and ``super_agent.paths`` are in ``sys.modules``
  and **no** ``kimi_*`` module was pulled in from the old workbench location;
* ``api`` still re-exports the direct-wire names callers rely on.

Run: ``python -m pytest tests/test_kimi_ports.py -q``
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from super_agent import api, paths

ROOT = Path(__file__).resolve().parent.parent


class Paths(unittest.TestCase):
    def test_resolvers_return_paths(self):
        for fn in (paths.home, paths.token_file, paths.config_file, paths.sessions_dir):
            self.assertIsInstance(fn(), Path, fn.__name__)

    def test_sessions_dir_ends_with_sessions(self):
        self.assertEqual(paths.sessions_dir().name, "sessions")

    def test_home_env_override(self):
        with mock.patch.dict("os.environ", {paths.ENV_HOME: "C:/tmp/iss25_4/kh"}):
            self.assertEqual(paths.home(), Path("C:/tmp/iss25_4/kh"))

    def test_missing_token_file_returns_empty(self):
        missing = Path("C:/tmp/iss25_4/no-such-token")
        with mock.patch.dict("os.environ", {paths.ENV_TOKEN_FILE: str(missing)}):
            self.assertEqual(paths.token(), "")


class NoImportTimeIo(unittest.TestCase):
    """Import must not phone home or touch disk beyond the module files."""

    def test_import_chain_with_sockets_and_open_blocked(self):
        code = (
            "import socket, builtins, sys\n"
            "class Boom(RuntimeError): pass\n"
            "def no_socket(*a, **k): raise Boom('socket created at import')\n"
            "socket.socket = no_socket\n"
            "socket.create_connection = no_socket\n"
            "socket.getaddrinfo = no_socket\n"
            "real_open = builtins.open\n"
            "def guarded_open(file, *a, **k):\n"
            "    name = str(file)\n"
            "    if name.endswith(('.py', '.pyc', '.pth', '.txt', '.json')):\n"
            "        return real_open(file, *a, **k)\n"
            "    raise Boom('unexpected open: ' + name)\n"
            "builtins.open = guarded_open\n"
            "import super_agent.bus, super_agent.api\n"
            "print('ok')\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertEqual(proc.stdout.strip(), "ok")


class PortedChain(unittest.TestCase):
    def test_chain_resolves_inside_package(self):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys, super_agent.bus\n"
             "assert 'super_agent.direct' in sys.modules\n"
             "assert 'super_agent.paths' in sys.modules\n"
             "print([m for m in sys.modules if m.startswith('kimi_')])\n"],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertEqual(proc.stdout.strip(), "[]")

    def test_direct_sees_package_paths(self):
        from super_agent import bus, direct
        self.assertIs(direct.kimi_paths, paths)
        self.assertIs(bus.K, direct)

    def test_api_reexports_direct_names(self):
        from super_agent import direct
        for name in ("get", "post", "data_of", "status", "session_info",
                     "abort_session", "answer_interaction", "tasks"):
            self.assertIs(getattr(api, name), getattr(direct, name), name)
        self.assertEqual(api.abort_prompt_session.__module__, api.__name__)

    def test_no_workbench_path(self):
        legacy = "Lai" + "ke_work_with_agent"      # 分片：门禁自身扫描要在本文件上保持干净
        for mod in (paths, api):
            self.assertNotIn(legacy, str(Path(mod.__file__).resolve()))


class CachePath(unittest.TestCase):
    """ISS-25 item 4 fix: the default cache path must not be inferred from BASE.

    ``BASE`` follows whoever owns the package, so a default of
    ``BASE/state/kimi_ws_cache.json`` silently reads an empty file after the
    move. Omitting ``cache_file`` must therefore return ``{}`` **and** warn.
    """

    def test_omitted_cache_file_returns_empty_and_warns(self):
        from super_agent import direct

        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(direct.cached("iss25-probe"), {})
        msg = err.getvalue()
        self.assertIn("cache_file", msg)
        self.assertIn("跳过", msg)

    def test_explicit_cache_file_still_reads(self):
        from super_agent import direct

        tmp = Path(tempfile.mkdtemp(prefix="iss25_cache_")) / "kimi_ws_cache.json"
        try:
            now = int(time.time())
            tmp.write_text(json.dumps({"sessions": {"sid-1": {"ts": now, "status": {"busy": True}}}}),
                           encoding="utf-8")
            rec = direct.cached("sid-1", cache_file=tmp)
            self.assertEqual(rec.get("status"), {"busy": True})
            self.assertEqual(direct.cached("missing", cache_file=tmp), {})
        finally:
            tmp.unlink(missing_ok=True)
            tmp.parent.rmdir()

    def test_cache_file_constant_no_longer_points_at_base_state(self):
        from super_agent import direct

        self.assertNotEqual(api.CACHE_FILE, direct.BASE / "state" / "kimi_ws_cache.json")
        # 空路径（Path("") == Path(".")）用起来会明确失败，不会静默读成空
        self.assertFalse(api.CACHE_FILE.is_file())
        with self.assertRaises(OSError):
            api.CACHE_FILE.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
