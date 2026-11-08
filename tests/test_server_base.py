"""L1 tests for ISS-38: lazy server port resolution in super_agent.paths.

Fixtures are temporary instances directories; no token, daemon or workbench
module is touched. Run: ``python -m pytest tests/test_server_base.py -q``.
"""
from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from super_agent import direct, paths

ROOT = Path(__file__).resolve().parent.parent


class ServerBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="iss38_server_base_")
        self.root = Path(self.tmp.name)
        self.inst = self.root / "instances"
        self.inst.mkdir(parents=True, exist_ok=True)
        self.env = mock.patch.dict(os.environ, {
            paths.ENV_SERVER: "",
            paths.ENV_INSTANCES_DIR: str(self.inst),
        })
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _write(self, name: str, payload, mtime: float) -> Path:
        path = self.inst / name
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_env_has_highest_priority(self) -> None:
        self._write("latest.json", {"port": 59001}, 1000.0)
        with mock.patch.dict(os.environ, {paths.ENV_SERVER: "https://example.invalid:64443"}):
            self.assertEqual(paths.server_base(), "https://example.invalid:64443")
            self.assertEqual(paths.server_id(), "")
            self.assertEqual(direct._ws_url(), "wss://example.invalid:64443/api/v1/ws")

    def test_latest_valid_instance_skips_bad_out_of_range_and_missing_port(self) -> None:
        self._write("bad.json", "{not json", 3000.0)
        self._write("range.json", {"port": 70000}, 2500.0)
        self._write("missing.json", {"host": "127.0.0.1"}, 2400.0)
        self._write("not_object.json", "[1, 2]", 2300.0)
        self._write("valid.json", {"server_id": "INST-1", "port": "59002",
                                   "host": "127.0.0.2"}, 2000.0)
        self._write("old.json", {"port": 59003}, 1000.0)
        self.assertEqual(paths.server_base(), "http://127.0.0.2:59002")
        rec = paths.latest_instance()
        self.assertEqual(rec["server_id"], "INST-1")
        self.assertEqual(rec["port"], 59002)
        self.assertEqual(paths.server_id(), "INST-1")

    def test_missing_dir_warns_and_falls_back(self) -> None:
        self.inst.rmdir()
        with self.assertLogs("super_agent.paths", level="WARNING") as logs:
            base = paths.server_base()
        self.assertEqual(base, "http://127.0.0.1:58627")
        text = "\n".join(logs.output)
        self.assertIn("instances 解析失败，回落 58627", text)
        self.assertIn("FileNotFoundError", text)
        self.assertIn(str(self.inst), text)

    def test_all_bad_warns_and_falls_back(self) -> None:
        self._write("bad.json", " ", 1000.0)
        self._write("range.json", {"port": 0}, 900.0)
        with self.assertLogs("super_agent.paths", level="WARNING") as logs:
            base = paths.server_base()
        self.assertEqual(base, "http://127.0.0.1:58627")
        text = "\n".join(logs.output)
        self.assertIn("instances 解析失败，回落 58627", text)
        self.assertIn("JSONDecodeError", text)
        self.assertIn(str(self.inst), text)

    def test_lazy_no_module_cache(self) -> None:
        self._write("first.json", {"server_id": "FIRST", "port": 59010}, 1000.0)
        before = paths.server_base()
        self._write("second.json", {"server_id": "SECOND", "port": 59011}, 9000.0)
        after = paths.server_base()
        self.assertEqual(before, "http://127.0.0.1:59010")
        self.assertEqual(after, "http://127.0.0.1:59011")
        self.assertEqual(paths.server_id(), "SECOND")

    def test_direct_request_uses_lazy_base(self) -> None:
        import urllib.request

        captured: list[str] = []
        saved = urllib.request.Request

        def fake_request(url, *a, **kw):
            captured.append(url)
            raise RuntimeError("stop-after-url")

        try:
            urllib.request.Request = fake_request
            self._write("inst.json", {"port": 59021}, 5000.0)
            try:
                direct._request("GET", "/api/v1/meta")
            except Exception:
                pass
        finally:
            urllib.request.Request = saved
        self.assertEqual(captured, ["http://127.0.0.1:59021/api/v1/meta"])

    def test_no_workbench_import_and_no_private_words(self) -> None:
        source = (ROOT / "super_agent" / "paths.py").read_text(encoding="utf-8")
        direct_source = (ROOT / "super_agent" / "direct.py").read_text(encoding="utf-8")
        for text in (source, direct_source):
            self.assertNotIn("kimi_server", text)
            self.assertNotIn("Lai" + "ke", text)
            self.assertNotIn("Re" + "ed", text)
            self.assertNotIn("C:/Users/" + "mingh", text)
        self.assertIsNone(re.search(r"^\s*SERVER\s*=", direct_source, re.M))
        self.assertNotIn("58627", direct_source)
