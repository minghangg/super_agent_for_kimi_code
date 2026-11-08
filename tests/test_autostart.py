"""L1 tests for the migrated ``super_agent/autostart.py`` (ISS-25 item 1).

The module is a verbatim port, so these tests pin the contract the two callers
(``decide_watch`` and ``monitor``) rely on rather than re-deriving behaviour:

* the rendered ``.vbs`` carries the interpreter and every argument;
* ``write_vbs`` produces UTF-16LE with a BOM and ``read_vbs`` round-trips it;
* ``verify_vbs`` fails on a wrong interpreter name and on a dropped argument;
* ``diff_summary`` reports nothing for identical text and caps at six lines;
* ``Spec`` resolves ``script``/``workdir`` in ``__post_init__``.

Every file goes under ``C:/tmp/iss25_1``; ``APPDATA`` is redirected there so the
real startup folder and scheduled tasks are never touched. Run:
``python -m pytest tests/test_autostart.py -q``
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from super_agent import autostart

TMP_ROOT = Path("C:/tmp/iss25_1")


class AutostartCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.tmp = Path(self._tmp.name)
        self.script = self.tmp / "repo" / "tools" / "demo.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("print('demo')\n", encoding="utf-8")
        self.apdata = self.tmp / "appdata"
        self.apdata.mkdir()
        patcher = mock.patch.dict("os.environ", {"APPDATA": str(self.apdata)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def spec(self, **kw) -> autostart.Spec:
        params = {"name": "KimiMonitor", "script": self.script,
                  "args": ["--interval", "45"]}
        params.update(kw)
        return autostart.Spec(**params)


class RenderTests(AutostartCase):
    def test_render_includes_exe_and_all_args(self) -> None:
        spec = self.spec()
        text = autostart.render_vbs(spec, r"C:\py\pythonw.exe")
        self.assertIn("pythonw.exe", text)
        self.assertIn(str(self.script), text)
        for arg in spec.args:
            self.assertIn(arg, text)

    def test_render_is_deterministic(self) -> None:
        spec = self.spec()
        self.assertEqual(autostart.render_vbs(spec, "py.exe"),
                         autostart.render_vbs(spec, "py.exe"))


class VbsIoTests(AutostartCase):
    def test_write_starts_with_bom_and_reads_back(self) -> None:
        target = self.tmp / "Startup" / "KimiMonitor.vbs"
        text = autostart.render_vbs(self.spec(), "py.exe")
        autostart.write_vbs(target, text)
        raw = target.read_bytes()
        self.assertTrue(raw.startswith(autostart.VBS_BOM))
        self.assertTrue(raw.startswith(b"\xff\xfe"))
        self.assertEqual(autostart.read_vbs(target), text)
        self.assertEqual(list(target.parent.glob("*.tmp")), [])


class VerifyTests(AutostartCase):
    def test_ok_when_file_matches(self) -> None:
        spec = self.spec()
        exe = "pythonw.exe"
        autostart.write_vbs(spec.startup_file, autostart.render_vbs(spec, exe))
        self.assertEqual(autostart.verify_vbs(spec, exe), [])

    def test_fails_on_wrong_interpreter(self) -> None:
        spec = self.spec()
        autostart.write_vbs(spec.startup_file, autostart.render_vbs(spec, "pythonw.exe"))
        fails = autostart.verify_vbs(spec, "other_python.exe")
        self.assertTrue(fails)
        self.assertTrue(any("解释器" in f for f in fails))

    def test_fails_on_missing_arg(self) -> None:
        spec = self.spec()
        written = self.spec(args=["--interval"])
        autostart.write_vbs(spec.startup_file,
                            autostart.render_vbs(written, "pythonw.exe"))
        fails = autostart.verify_vbs(spec, "pythonw.exe")
        self.assertTrue(fails)
        self.assertTrue(any("参数缺失" in f for f in fails))


class DiffTests(AutostartCase):
    def test_identical_returns_empty(self) -> None:
        self.assertEqual(autostart.diff_summary("a\nb\n", "a\nb\n"), [])

    def test_differences_capped_at_six(self) -> None:
        old = "\n".join(f"line{i}" for i in range(20))
        new = "\n".join(f"changed{i}" for i in range(20))
        out = autostart.diff_summary(old, new)
        self.assertTrue(out)
        self.assertEqual(len(out), 6)


class SpecTests(AutostartCase):
    def test_script_and_workdir_resolved(self) -> None:
        rel = self.script.parent / "." / "demo.py"
        spec = autostart.Spec(name="X", script=rel)
        self.assertEqual(spec.script, self.script.resolve())
        self.assertEqual(spec.workdir, self.script.parent.parent.resolve())
        self.assertTrue(spec.script.is_absolute())
        self.assertTrue(spec.workdir.is_absolute())

    def test_explicit_workdir_resolved(self) -> None:
        spec = autostart.Spec(name="X", script=self.script, workdir=self.tmp / "wd")
        self.assertEqual(spec.workdir, (self.tmp / "wd").resolve())


if __name__ == "__main__":
    unittest.main()
