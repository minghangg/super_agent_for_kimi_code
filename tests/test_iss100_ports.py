"""L1 tests for ISS-100 group 1: dispatch port discovery gap fixes.

Two gaps only (the chain itself came with #71a):

* ``KIMI_CODE_SERVER_PORT`` accepted nothing but digits, so a full base URL was
  silently dropped;
* falling back to ``DEFAULT_PORTS`` printed no warning, so "no instance, default
  ports closed" looked like a plain failure with no pointer to the cause.

What is pinned here: the frozen priority order, the host taken from the instance
file (not hardcoded to 127.0.0.1), the warning text, and the agreement between
``paths.latest_instance()['port']`` and ``dispatch_cli.discover_base()``.

No token, daemon or real server is touched: ``port_open``/``http_get`` are stubs
and every instances directory is a temporary fixture.
Run: ``python -m pytest tests/test_iss100_ports.py -q``
"""
from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from super_agent import dispatch_cli, paths

CLEAN = {paths.ENV_SERVER: "", "KIMI_CODE_SERVER_PORT": ""}


class PortChain(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="iss100_ports_")
        self.inst = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, dict(
            CLEAN, **{paths.ENV_INSTANCES_DIR: str(self.inst)}))
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _instance(self, name: str, payload, mtime: float = 1000.0, root: Path | None = None) -> None:
        target = (root or self.inst) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        target.write_text(text, encoding="utf-8")
        os.utime(target, (mtime, mtime))

    def _bases(self, explicit=None):
        return dispatch_cli.candidate_bases(explicit)

    # -- env parsing -------------------------------------------------------
    def test_env_accepts_bare_port(self) -> None:
        self._instance("a.json", {"port": 15000})
        with mock.patch.dict(os.environ, {"KIMI_CODE_SERVER_PORT": " 15001 "}):
            self.assertEqual(self._bases()[0], "http://127.0.0.1:15001")

    def test_env_accepts_full_base_url_and_keeps_host(self) -> None:
        self._instance("a.json", {"port": 15000})
        with mock.patch.dict(os.environ, {"KIMI_CODE_SERVER_PORT": "http://10.0.0.9:15002"}):
            self.assertEqual(self._bases()[0], "http://10.0.0.9:15002")

    def test_unparsable_env_is_ignored_with_a_warning(self) -> None:
        self._instance("a.json", {"port": 15000})
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"KIMI_CODE_SERVER_PORT": "not-a-port"}):
            with redirect_stderr(err):
                bases = self._bases()
        self.assertEqual(bases[0], "http://127.0.0.1:15000")
        self.assertIn("KIMI_CODE_SERVER_PORT 无法解析", err.getvalue())

    # -- frozen priority ---------------------------------------------------
    def test_priority_order_is_frozen(self) -> None:
        self._instance("a.json", {"port": 15010})
        with mock.patch.dict(os.environ, {"KIMI_CODE_SERVER_PORT": "15011",
                                          paths.ENV_SERVER: "http://127.0.0.1:15012"}):
            self.assertEqual(self._bases(15013)[0], "http://127.0.0.1:15013")
            self.assertEqual(self._bases(), [
                "http://127.0.0.1:15011",
                "http://127.0.0.1:15012",
                "http://127.0.0.1:15010",
                "http://127.0.0.1:58627",
                "http://127.0.0.1:58628",
            ])

    def test_both_env_set_port_wins(self) -> None:
        """``KIMI_CODE_SERVER_PORT`` beats ``KIMI_SERVER`` — the documented winner."""
        with mock.patch.dict(os.environ, {"KIMI_CODE_SERVER_PORT": "15021",
                                          paths.ENV_SERVER: "http://127.0.0.1:15022"}):
            self.assertEqual(self._bases()[0], "http://127.0.0.1:15021")

    def test_explicit_port_wins_over_everything(self) -> None:
        with mock.patch.dict(os.environ, {"KIMI_CODE_SERVER_PORT": "15031"}):
            self.assertEqual(self._bases(15032), ["http://127.0.0.1:15032"])

    def test_kimi_server_env_is_read(self) -> None:
        with mock.patch.dict(os.environ, {paths.ENV_SERVER: "https://h:15041"}):
            self.assertEqual(self._bases()[0], "https://h:15041")

    # -- instance file -----------------------------------------------------
    def test_instance_host_is_respected(self) -> None:
        self._instance("a.json", {"port": 15050, "host": "10.1.2.3"})
        self.assertEqual(self._bases()[0], "http://10.1.2.3:15050")

    def test_bad_instance_files_are_skipped(self) -> None:
        self._instance("bad.json", "{not json", 3000.0)
        self._instance("range.json", {"port": 70000}, 2500.0)
        self._instance("good.json", {"port": 15060}, 2000.0)
        self.assertEqual(paths.latest_instance()["port"], 15060)
        self.assertEqual(self._bases()[0], "http://127.0.0.1:15060")

    # -- fallback warning --------------------------------------------------
    def test_no_instance_warns_on_fallback(self) -> None:
        err = io.StringIO()
        with mock.patch.object(dispatch_cli, "port_open", lambda *a, **k: False):
            with redirect_stderr(err):
                with self.assertRaises(dispatch_cli.DispatchError):
                    dispatch_cli.discover_base("", None)
        self.assertIn("实例文件不可用，回落探测 58627/58628", err.getvalue())
        self.assertEqual(self._bases(), ["http://127.0.0.1:58627",
                                         "http://127.0.0.1:58628"])

    def test_instance_present_warns_with_its_port(self) -> None:
        self._instance("a.json", {"port": 15070})
        err = io.StringIO()
        with mock.patch.object(dispatch_cli, "port_open", lambda *a, **k: False):
            with redirect_stderr(err):
                with self.assertRaises(dispatch_cli.DispatchError):
                    dispatch_cli.discover_base("", None)
        self.assertIn("实例端口 15070 未响应，回落探测 58627/58628", err.getvalue())

    def test_listing_candidates_alone_prints_nothing(self) -> None:
        """The fallback warning belongs to probing, not to enumerating."""
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(self._bases(), ["http://127.0.0.1:58627",
                                             "http://127.0.0.1:58628"])
        self.assertEqual(err.getvalue(), "")
        self._instance("a.json", {"port": 15080})
        err = io.StringIO()
        with redirect_stderr(err):
            dispatch_cli.candidate_bases(None)
        self.assertNotIn("回落探测", err.getvalue())

    # -- discovery ---------------------------------------------------------
    def test_discover_base_accepts_first_live_candidate(self) -> None:
        self._instance("a.json", {"port": 15090, "host": "127.0.0.5"})
        probed: list[tuple[int, str]] = []

        def fake_open(port, host="127.0.0.1"):
            probed.append((port, host))
            return port == 15090

        with mock.patch.object(dispatch_cli, "port_open", fake_open), \
                mock.patch.object(dispatch_cli, "http_get",
                                  lambda *a, **k: {"code": 0, "data": {}}):
            base = dispatch_cli.discover_base("", None)
        self.assertEqual(base, "http://127.0.0.5:15090")
        self.assertEqual(probed[0], (15090, "127.0.0.5"))

    def test_discover_base_warns_then_fails(self) -> None:
        err = io.StringIO()
        with mock.patch.object(dispatch_cli, "port_open", lambda *a, **k: False):
            with redirect_stderr(err):
                with self.assertRaises(dispatch_cli.DispatchError) as ctx:
                    dispatch_cli.discover_base("", None)
        self.assertIn("实例文件不可用，回落探测 58627/58628", err.getvalue())
        self.assertIn("实例文件不可用且默认端口均 closed", str(ctx.exception))
        self.assertIn("no live kimi-code server found", str(ctx.exception))

    def test_latest_instance_port_matches_discover_base(self) -> None:
        """Criterion added at review: the two entry points must name one port."""
        self._instance("a.json", {"port": 15100})
        with mock.patch.object(dispatch_cli, "port_open", lambda *a, **k: True), \
                mock.patch.object(dispatch_cli, "http_get",
                                  lambda *a, **k: {"code": 0, "data": {}}):
            base = dispatch_cli.discover_base("", None)
        self.assertEqual(base, f"http://127.0.0.1:{paths.latest_instance()['port']}")

    def test_no_second_instance_parser(self) -> None:
        source = (Path(dispatch_cli.__file__)).read_text(encoding="utf-8")
        self.assertNotIn("instances/*.json", source)
        # 实例解析只许走 paths.latest_instance()：禁止 glob instances 目录
        self.assertNotRegex(source, r"glob\([^)]*instances")


if __name__ == "__main__":
    unittest.main()
