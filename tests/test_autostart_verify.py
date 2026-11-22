"""``autostart --verify`` / ``--register``: the three-way consistency check (#101).

Two scheduled tasks were created by hand and point straight at
``tools/*.py`` while the startup script lives in ``new_workbench/scripts/``.
Nothing called the script, so a restart ran the old entry point and nothing
noticed. These tests pin the check that makes that state visible:

* the module has a real ``main``: an unknown flag is rc=2, never the silent
  rc=0 the module produced before (the #87 shape);
* the comparison unit is the **absolute .py path token**, so two checkouts are
  distinguishable -- a basename is the same string in both;
* "not exactly one token" is an answer (rc=22) on **both** sides -- the task's
  command and the process's. Counting an unparsable process line as "not
  running" (rc=21) would blame the wrong thing and disagree with the task side
  about the same input;
* an absent task is rc=21 ("undecidable"), never "consistent";
* the workbench root is ``engine.lwa_root`` and nothing else: unconfigured,
  missing directory and missing script are each rc=2 with the reason on stderr.

No test touches a real scheduled task or a real process: ``read_task`` and
``read_processes`` are patched per test, and the script side reads a temp file.
Run: ``python -m pytest tests/test_autostart_verify.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from super_agent import agent_config, autostart

PYW = r'"D:\Program Files\Python310\pythonw.exe"'


def task_row(script: Path | None = None, py: Path | None = None,
             part: str = "") -> dict:
    if script is not None:
        suffix = f" -Part {part}" if part else ""
        return {"found": True, "execute": PYW,
                "arguments": (f'-NoProfile -ExecutionPolicy Bypass -File "{script}"'
                              + suffix),
                "workdir": "", "trigger": "MSFT_TaskLogonTrigger"}
    return {"found": True, "execute": PYW,
            "arguments": f'"{py}" --daemon', "workdir": "", "trigger": "MSFT_TaskLogonTrigger"}


SCRIPT_TEMPLATE = """\
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\\..')).Path
Start-Process -FilePath $py -ArgumentList @("$root\\tools\\wss_bus.py", '--daemon')
Start-Process -FilePath $py -ArgumentList @("$root\\tools\\decide_watch.py")
"""


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in (agent_config.ENV_FILE,)}
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workbench = self.root / "wb"
        (self.workbench / "new_workbench" / "scripts").mkdir(parents=True)
        (self.workbench / "tools").mkdir(parents=True)
        self.script = self.workbench / "new_workbench" / "scripts" / "start_bus_watch.ps1"
        self.script.write_text(SCRIPT_TEMPLATE, encoding="utf-8")
        agent_config._reset_cache()

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        agent_config._reset_cache()
        self._tmp.cleanup()

    def configure(self, engine: dict | None = None, tasks: dict | None = None) -> None:
        doc: dict = {"engine": {"lwa_root": str(self.workbench)}}
        doc["engine"].update(engine or {})
        doc["autostart"] = {"tasks": tasks if tasks is not None else
                            {"wss_bus": "KimiWssBus", "decide_watch": "KimiDecideWatch"}}
        path = self.root / "agent.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        os.environ[agent_config.ENV_FILE] = str(path)
        agent_config._reset_cache()

    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = autostart.main(argv)
        return rc, out.getvalue(), err.getvalue()


class EntryPointTest(_Base):
    def test_unknown_flag_is_rc2(self) -> None:
        """Before #101 there was no ``main``, so ``--bogus`` exited 0."""
        self.configure()
        with self.assertRaises(SystemExit) as caught:
            autostart.main(["--bogus"])
        self.assertEqual(caught.exception.code, 2)

    def test_no_arguments_is_rc2(self) -> None:
        self.configure()
        with self.assertRaises(SystemExit) as caught:
            autostart.main([])
        self.assertEqual(caught.exception.code, 2)

    def test_verify_and_register_are_mutually_exclusive(self) -> None:
        self.configure()
        with self.assertRaises(SystemExit) as caught:
            autostart.main(["--verify", "--register", "wss_bus"])
        self.assertEqual(caught.exception.code, 2)

    def test_module_has_a_main_guard(self) -> None:
        source = Path(autostart.__file__).read_text(encoding="utf-8")
        self.assertIn('if __name__ == "__main__":', source)


class RootSourceTest(_Base):
    """P0-2: one declared source, three rc=2 faults, never a silent fallback."""

    def test_unconfigured_root_is_rc2_on_stderr(self) -> None:
        path = self.root / "agent.json"
        path.write_text("{}", encoding="utf-8")
        os.environ[agent_config.ENV_FILE] = str(path)
        agent_config._reset_cache()
        rc, out, err = self.run_main(["--verify"])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("engine.lwa_root", err)

    def test_missing_directory_is_rc2(self) -> None:
        self.configure({"lwa_root": str(self.root / "nope")})
        rc, _out, err = self.run_main(["--verify"])
        self.assertEqual(rc, 2)
        self.assertIn("工作台根不存在", err)

    def test_missing_script_is_rc2(self) -> None:
        self.script.unlink()
        self.configure()
        rc, _out, err = self.run_main(["--register", "wss_bus", "--dry-run"])
        self.assertEqual(rc, 2)
        self.assertIn("启动脚本不存在", err)

    def test_unconfigured_task_name_is_rc2(self) -> None:
        self.configure(tasks={})
        rc, _out, err = self.run_main(["--verify"])
        self.assertEqual(rc, 2)
        self.assertIn("任务名未配置", err)

    def test_no_parents_chain_fallback(self) -> None:
        """The checkout's ancestors do not contain the workbench, and that is
        exactly why a derived root was removed: it answered ``None``."""
        self.configure({"lwa_root": ""})
        rc, _out, err = self.run_main(["--verify"])
        self.assertEqual(rc, 2)
        self.assertIn("engine.lwa_root", err)


class TokenTest(unittest.TestCase):
    """P0-1: absolute path tokens, not basenames."""

    def test_two_checkouts_are_distinguishable(self) -> None:
        lwa = autostart.token_paths(PYW + r' "D:\github\lwa\tools\wss_bus.py" --daemon')
        saf = autostart.token_paths(
            PYW + r' "D:\github\safkc\tools\wss_bus.py" --daemon')
        self.assertEqual(len(lwa), 1)
        self.assertEqual(len(saf), 1)
        self.assertNotEqual(autostart._normalize(lwa[0]),
                            autostart._normalize(saf[0]))
        # The same two lines are identical under a basename rule.
        self.assertEqual(Path(lwa[0]).name, Path(saf[0]).name)

    def test_path_with_space_is_one_token(self) -> None:
        tokens = autostart.token_paths(PYW + r' "D:\My Projects\wb\tools\wss_bus.py"')
        self.assertEqual(tokens, [r"D:\My Projects\wb\tools\wss_bus.py"])

    def test_unquoted_path_is_found(self) -> None:
        tokens = autostart.token_paths(
            r'"D:\program files\python310\pythonw.exe" D:\github\lwa\tools\wss_bus.py --daemon')
        self.assertEqual([Path(t).name for t in tokens], ["wss_bus.py"])

    def test_zero_tokens_is_reported(self) -> None:
        path, why = autostart._single_token("pythonw.exe --daemon")
        self.assertEqual(path, "")
        self.assertIn("没有绝对", why)

    def test_two_tokens_is_not_first_wins(self) -> None:
        path, why = autostart._single_token(r"D:\a\x.py D:\b\y.py")
        self.assertEqual(path, "")
        self.assertIn("无法判定", why)


class ScriptParseTest(_Base):
    def test_root_is_derived_from_pscriptroot(self) -> None:
        sides, why = autostart.parse_script(self.script)
        self.assertEqual(why, "")
        self.assertEqual(Path(sides["wss_bus"].value),
                         (self.workbench / "tools" / "wss_bus.py").resolve())
        self.assertEqual(Path(sides["decide_watch"].value),
                         (self.workbench / "tools" / "decide_watch.py").resolve())

    def test_script_without_start_process_is_an_error(self) -> None:
        bare = self.root / "bare.ps1"
        bare.write_text("# only a comment\n", encoding="utf-8")
        sides, why = autostart.parse_script(bare)
        self.assertEqual(sides, {})
        self.assertIn("找不到启动行", why)

    def test_missing_script_is_an_error(self) -> None:
        _sides, why = autostart.parse_script(self.root / "nope.ps1")
        self.assertIn("脚本不存在", why)

    def test_script_parsing_is_not_execution(self) -> None:
        """A parse of a file that would start something real must not run it."""
        marker = self.root / "ran.marker"
        probe = self.root / "probe.ps1"
        probe.write_text(
            "$root = (Resolve-Path (Join-Path $PSScriptRoot '..\\..')).Path\n"
            f'New-Item -ItemType File -Path "{marker}" -Force | Out-Null\n'
            'Start-Process -FilePath $py -ArgumentList @("$root\\tools\\wss_bus.py")\n',
            encoding="utf-8")
        autostart.parse_script(probe)
        self.assertFalse(marker.exists())


class VerifyVerdictTest(_Base):
    def _verify(self, rows: dict, processes: list, script: Path | None = None) -> tuple:
        def fake_task(name: str) -> dict:
            return rows.get(name, {"found": False})

        with mock.patch.object(autostart, "read_task", fake_task), \
                mock.patch.object(autostart, "read_processes",
                                  lambda pattern="", pids=None: processes):
            return self.run_main(["--verify"])

    def test_all_three_agree_on_the_script(self) -> None:
        self.configure()
        py = self.workbench / "tools" / "wss_bus.py"
        py2 = self.workbench / "tools" / "decide_watch.py"
        rows = {"KimiWssBus": task_row(script=self.script, part="wss_bus"),
                "KimiDecideWatch": task_row(script=self.script, part="decide_watch")}
        processes = [(10, f"{PYW} {py} --daemon"), (11, f"{PYW} {py2}")]
        rc, out, _err = self._verify(rows, processes)
        self.assertEqual(rc, 0)
        self.assertIn("三方一致", out)
        self.assertNotIn("未指向启动脚本", out)

    def test_all_or_missing_part_is_a_mismatch(self) -> None:
        self.configure()
        py = self.workbench / "tools" / "wss_bus.py"
        py2 = self.workbench / "tools" / "decide_watch.py"
        processes = [(10, f"{PYW} {py} --daemon"), (11, f"{PYW} {py2}")]
        for wrong in ("", "all", "decide_watch"):
            with self.subTest(wrong=wrong):
                rows = {"KimiWssBus": task_row(script=self.script, part=wrong),
                        "KimiDecideWatch": task_row(script=self.script,
                                                   part="decide_watch")}
                rc, out, _err = self._verify(rows, processes)
                self.assertEqual(rc, 20)
                self.assertIn("应为 wss_bus", out)

    def test_task_directly_naming_the_py_is_consistent_but_prompts(self) -> None:
        """Today's real shape: consistent, on an entry point group A replaces."""
        self.configure()
        py = self.workbench / "tools" / "wss_bus.py"
        py2 = self.workbench / "tools" / "decide_watch.py"
        rows = {"KimiWssBus": task_row(py=py),
                "KimiDecideWatch": task_row(py=py2)}
        processes = [(10, f"{PYW} {py} --daemon"), (11, f"{PYW} {py2}")]
        rc, out, _err = self._verify(rows, processes)
        self.assertEqual(rc, 0)
        self.assertIn("三方一致", out)
        self.assertIn("提示：计划任务未指向启动脚本", out)
        self.assertIn("--register wss_bus --apply", out)

    def test_task_and_process_disagree_is_rc20(self) -> None:
        self.configure()
        py = self.workbench / "tools" / "wss_bus.py"
        py2 = self.workbench / "tools" / "decide_watch.py"
        other = self.root / "elsewhere" / "tools" / "wss_bus.py"
        rows = {"KimiWssBus": task_row(py=py),
                "KimiDecideWatch": task_row(py=py2)}
        processes = [(10, f"{PYW} {other} --daemon"), (11, f"{PYW} {py2}")]
        rc, out, _err = self._verify(rows, processes)
        self.assertEqual(rc, 20)
        self.assertIn("1 处不一致", out)
        self.assertIn("建议：python -m super_agent.autostart --register wss_bus --apply", out)

    def test_absent_task_is_rc21_not_consistent(self) -> None:
        self.configure()
        py = self.workbench / "tools" / "wss_bus.py"
        rows = {"KimiWssBus": {"found": False}}
        rc, out, _err = self._verify(rows, [(10, f"{PYW} {py}")])
        self.assertEqual(rc, 21)
        self.assertIn("未注册", out)
        self.assertIn("不可判", out)

    def test_no_process_is_rc21_and_the_other_two_still_compare(self) -> None:
        self.configure()
        rows = {"KimiWssBus": task_row(script=self.script, part="wss_bus"),
                "KimiDecideWatch": task_row(script=self.script, part="decide_watch")}
        rc, out, _err = self._verify(rows, [])
        self.assertEqual(rc, 21)
        self.assertIn("未运行", out)
        self.assertIn("部分不可判", out)
        # The ①-vs-② comparison is still printed: an unrun process is not a
        # reason to hide what the other two agree on.
        self.assertIn("（经启动脚本）", out)

    def test_no_start_process_line_is_rc22(self) -> None:
        self.configure()
        self.script.write_text("# nothing here\n", encoding="utf-8")
        rows = {"KimiWssBus": task_row(script=self.script, part="wss_bus"),
                "KimiDecideWatch": task_row(script=self.script, part="decide_watch")}
        rc, out, _err = self._verify(rows, [])
        self.assertEqual(rc, 22)
        self.assertIn("找不到启动行", out)

    def test_missing_script_is_rc22_not_a_silent_skip(self) -> None:
        self.configure()
        self.script.unlink()
        rows = {"KimiWssBus": task_row(py=self.root / "x" / "wss_bus.py")}
        rc, out, _err = self._verify(rows, [])
        self.assertEqual(rc, 22)
        self.assertIn("脚本不存在", out)


class InjectTest(_Base):
    def test_command_line_replaces_the_process_read(self) -> None:
        self.configure()
        py = self.workbench / "tools" / "wss_bus.py"

        def no_read(*_a, **_kw):
            raise AssertionError("read_processes must not be called")

        with mock.patch.object(autostart, "read_processes", no_read), \
                mock.patch.object(autostart, "read_task",
                                  lambda name: task_row(
                    script=self.script,
                    part="wss_bus" if name == "KimiWssBus" else "decide_watch")):
            rc, out, _err = self.run_main([
                "--verify", "--part", "wss_bus",
                "--command-line", f"{PYW} {py} --daemon"])
        self.assertEqual(rc, 0)
        self.assertIn("注入命令行", out)

    def test_injected_line_still_goes_through_the_same_parser(self) -> None:
        """Zero tokens is "cannot decide", never "not running" (decision 1).

        The old code skipped the line and reported ``absent`` -> rc=21, which
        says "start the process" about a process whose command line simply is
        not one file. The task side already answered ``unparsable`` for the same
        input; the two sides now agree.
        """
        self.configure()
        with mock.patch.object(autostart, "read_task",
                               lambda name: task_row(
                    script=self.script,
                    part="wss_bus" if name == "KimiWssBus" else "decide_watch")):
            rc, out, _err = self.run_main([
                "--verify", "--part", "wss_bus",
                "--command-line", "pythonw.exe --daemon"])
        self.assertEqual(rc, 22)
        self.assertIn("没有绝对", out)
        self.assertNotIn("未运行", out)

    def test_two_tokens_in_a_process_line_is_rc22(self) -> None:
        """Two candidate files: "the first hit wins" is the answer we refuse."""
        self.configure()
        with mock.patch.object(autostart, "read_task",
                               lambda name: task_row(
                    script=self.script,
                    part="wss_bus" if name == "KimiWssBus" else "decide_watch")):
            rc, out, _err = self.run_main([
                "--verify", "--part", "wss_bus",
                "--command-line",
                f'{PYW} "D:/a/tools/wss_bus.py" "D:/b/tools/wss_bus.py" --daemon'])
        self.assertEqual(rc, 22)
        self.assertIn("多半目标", out)

    def test_both_sides_agree_on_the_same_unparsable_input(self) -> None:
        """Task side and process side return the same verdict for one input.

        They used to disagree (task 22, process 21) because the process loop
        swallowed the parse failure. One input, one verdict.
        """
        root = self.workbench
        script = autostart.script_path(root)
        for arguments in (r"D:/a/wss_bus.py D:/b/wss_bus.py", "-c \"print(1)\""):
            row = {"found": True, "execute": "py", "arguments": arguments}
            task = autostart._task_side(row, root, script)
            proc = autostart._process_side(
                "wss_bus", [(0, f"py {arguments}")], injected=True)
            self.assertEqual(task.state, "unparsable", arguments)
            self.assertEqual(proc.state, "unparsable", arguments)

    def test_same_path_duplicate_processes_are_refused(self) -> None:
        self.configure()
        command = r'"py.exe" D:\wb\tools\decide_watch.py'
        proc = autostart._process_side(
            "decide_watch", [(15068, command), (15076, command)], injected=True)
        self.assertEqual(proc.state, "duplicate")
        self.assertIn("重复实例", proc.detail)
        self.assertIn("15068", proc.detail)
        self.assertIn("15076", proc.detail)
        with mock.patch.object(autostart, "read_task",
                               lambda name: task_row(
                    script=self.script,
                    part="wss_bus" if name == "KimiWssBus" else "decide_watch")):
            rc, out, _err = self.run_main([
                "--verify", "--part", "decide_watch",
                "--command-line", command, "--command-line", command])
        self.assertEqual(rc, 22)
        self.assertIn("重复实例", out)
        self.assertIn("结论：检测到 1 个部件的重复实例", out)
        self.assertNotIn("结论：部分无法判定（解析失败）", out)

    def test_multiple_pid_filters_use_or(self) -> None:
        with mock.patch.object(autostart, "_ps", return_value=(0, "", "")) as ps:
            self.assertEqual(autostart.read_processes(pids=[101, 202]), [])
        script = ps.call_args.args[0]
        self.assertIn("ProcessId=101 or ProcessId=202", script)
        self.assertNotIn("ProcessId=101,ProcessId=202", script)

    def test_process_side_accepts_the_shim_name(self) -> None:
        """`<part>_<suffix>.py` names the same part as `<part>.py` (ISS-102).

        ``parse_script`` learned the shim form when the switch moved the bus to
        ``wss_bus_safkc.py``; ``_process_side`` did not, so ``--verify`` read a
        live process whose command line names its file plainly as "命令行里没有
        绝对 .py 路径". Both sides must accept the same two shapes.
        """
        shim = r'"py.exe" D:\wb\tools\wss_bus_safkc.py --daemon'
        proc = autostart._process_side("wss_bus", [(11568, shim)], injected=True)
        self.assertEqual(proc.state, "ok", proc.detail)
        self.assertEqual(proc.value, r"d:\wb\tools\wss_bus_safkc.py")

        direct = r'"py.exe" D:\wb\tools\wss_bus.py --daemon'
        proc = autostart._process_side("wss_bus", [(11568, direct)], injected=True)
        self.assertEqual(proc.state, "ok", proc.detail)

    def test_process_side_does_not_claim_another_parts_shim(self) -> None:
        """A `wss_bus_*` shim is wss_bus; `decide_watch_*` must not become it."""
        other = r'"py.exe" D:\wb\tools\decide_watch_fast.py'
        proc = autostart._process_side("wss_bus", [(1, other)], injected=True)
        self.assertEqual(proc.state, "absent", proc.detail)


class RegisterTest(_Base):
    def test_dry_run_prints_and_does_not_apply(self) -> None:
        self.configure()

        def no_apply(*_a, **_kw):
            raise AssertionError("--dry-run must not write a task")

        with mock.patch.object(autostart, "_apply_task", no_apply):
            rc, out, _err = self.run_main(["--register", "wss_bus", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", out)
        self.assertIn("Action", out)

    def test_printed_action_is_the_one_the_script_path_produces(self) -> None:
        self.configure()
        rc, out, _err = self.run_main(["--register", "wss_bus"])
        self.assertEqual(rc, 0)
        self.assertIn(str(self.script), out)
        display, _argv = autostart.register_action(self.workbench, "wss_bus")
        self.assertIn(display, out)
        self.assertIn("-Part wss_bus", display)

    def test_action_does_not_name_a_tools_py_directly(self) -> None:
        self.configure()
        display, _argv = autostart.register_action(self.workbench, "decide_watch")
        self.assertIn("start_bus_watch.ps1", display)
        self.assertNotIn("tools", display)
        self.assertIn("-NoProfile", display)
        self.assertIn("-ExecutionPolicy Bypass", display)
        self.assertIn("-File", display)
        self.assertIn("-Part decide_watch", display)


if __name__ == "__main__":
    unittest.main()
