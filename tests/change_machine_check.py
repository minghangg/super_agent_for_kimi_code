#!/usr/bin/env python
"""Change-machine simulation (ISS-100 D7): does a bare clone still work?

A fresh clone has no ``new_workbench/`` -- that directory is private, untracked
and absent from the repository. This script builds exactly that state, from a
directory **outside** the clone, and runs the commands a new machine would run.
It is scaffolding, not a product capability: it lives in ``tests/`` so it adds
no module to ``super_agent/``.

    python tests/change_machine_check.py --clone D:/temp_safkc/cm_<ts> [--forbid <name>]

Two environment rules, both measured, both load-bearing:

* ``PYTHONPATH`` is **never set** by this script. It outranks an editable
  install *and* is inherited by grandchildren, so setting it would hide the very
  defect the archive gate exists to catch (``issue_cli`` injects
  ``PYTHONPATH`` into the archive child itself; gate 7 verifies that).
* the code under test is selected with ``sys.path.insert`` in a ``-c`` bootstrap,
  not with ``PYTHONPATH``: that affects this process only, so a grandchild
  started by the product still shows which checkout it really imports.

Every gate is "allowed rc set + required line", never "rc == 0": a command that
correctly reports a missing session exits non-zero, and demanding 0 would turn
a working command into a red gate.

Read-only, with one exception: gate 8 talks to a local stub it starts itself.
Gate 0 additionally runs an md5 double-check around every read-only action, so
"the check did not write the event stream" is a measurement, not a claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parent

# Gate 0 state: the md5 double check asserts this file is unchanged by the
# read-only gates. It is the workbench stream when the workbench exists, and
# the clone's own stream otherwise -- either way, one fixed file.
WATCH_NAME = "events.jsonl"


def md5_state(path: Path) -> tuple[int, str]:
    """``(size, md5)`` -- the pair Reed fixed, because ``last_seq`` cannot see
    an appended line that carries no ``seq`` (measured: +60 B, one row, and
    ``last_seq`` unchanged)."""
    if not path.exists():
        return (0, "")
    data = path.read_bytes()
    return (len(data), hashlib.md5(data).hexdigest())


class Gate:
    def __init__(self, num: int, title: str):
        self.num = num
        self.title = title
        self.rc: int | None = None
        self.output = ""
        self.ok = False
        self.detail = ""
        self.skipped = False


def _run(argv: list[str], cwd: Path, env: dict, timeout: int = 120):
    proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True,
                          text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _bootstrap(clone: Path) -> str:
    """``-c`` code that runs ``python -m super_agent.cli`` from *clone*.

    ``sys.path.insert`` rather than ``PYTHONPATH``: the variable is inherited by
    the archive grandchild, so a run that passes because of it proves nothing
    about a machine that only has the clone.
    """
    return ("import sys; sys.path.insert(0, r'" + str(clone) + "'); "
            "from super_agent.cli import main; raise SystemExit(main())")


def _bootstrap_module(clone: Path, module: str) -> str:
    return ("import sys, runpy; sys.path.insert(0, r'" + str(clone) + "'); "
            "sys.argv = ['" + module + "'] + sys.argv[1:]; "
            "runpy.run_module('" + module + "', run_name='__main__')")


class Stub:
    """A local HTTP stub for the two faces ``close`` talks to.

    GitHub: the comments list (must carry one ``[QA]`` row or ``cmd_close``
    refuses before archiving) and the PATCH that closes the issue.
    Sessions: ``meta``, ``sessions:archive``, ``sessions/<sid>``.

    It records every request so the test can assert **exactly one** archive
    call carrying the right sid, rather than "no exception was raised".
    """

    def __init__(self, sid: str):
        self.sid = sid
        self.requests: list[tuple[str, str, str]] = []
        self._server = None
        self._thread = None
        self.base = ""

    def __enter__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):        # keep the console readable
                return

            def _body(self) -> str:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n).decode("utf-8", "replace") if n else ""

            def _send(self, payload, status=200):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                stub.requests.append(("GET", self.path, ""))
                # ``endswith`` would miss the query string the CLI appends
                # (``/comments?per_page=100``), and a missing ``[QA]`` row makes
                # ``cmd_close`` refuse before it ever archives.
                if "/comments" in self.path:
                    return self._send([{"body": "[QA] 归档目标：本单已验"}])
                if "/api/v1/meta" in self.path:
                    return self._send({"code": 0, "data": {}})
                if "/api/v1/sessions/" in self.path:
                    return self._send({"code": 0, "data": {
                        "archived": True, "busy": False, "id": stub.sid}})
                return self._send({"code": 0, "data": {}})

            def do_PATCH(self):
                stub.requests.append(("PATCH", self.path, self._body()))
                return self._send({"number": 1, "state": "closed"})

            def do_POST(self):
                stub.requests.append(("POST", self.path, self._body()))
                if "sessions:archive" in self.path:
                    return self._send({"code": 0, "data": {
                        "results": [{"ok": True}]}})
                return self._send({"code": 0, "data": {}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        return False

    def archive_calls(self) -> list[tuple[str, str, str]]:
        return [r for r in self.requests if "sessions:archive" in r[1]]


def make_clone(dest: Path) -> Path:
    """``git clone --no-hardlinks`` this checkout, then strip private config.

    ``new_workbench/`` is deleted even though ``.gitignore`` already excludes it,
    because a clone that happens to be made from a dirty tree would otherwise
    carry it -- the point is "a machine that has only the clone".
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--no-hardlinks", str(CHECKOUT), str(dest)],
                   check=True, capture_output=True, text=True)
    wb = dest / "new_workbench"
    if wb.exists():
        shutil.rmtree(wb)
    cfg_dir = wb / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json").write_text(json.dumps({
        "paths": {"state_dir": "state"},
        "github": {"projects_file": "config/gh_projects.json"}},
        ensure_ascii=False), encoding="utf-8")
    sample = dest / "config" / "sample.projects.json"
    if sample.is_file():
        shutil.copyfile(sample, dest / "config" / "gh_projects.json")
    return dest


def gate_env(clone: Path, outside: Path, extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["SAFKC_AGENT_CONFIG"] = str(clone / "new_workbench" / "config" / "agent.json")
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("SESS_EVENTS_STATE_DIR", None)
    if extra:
        env.update(extra)
    return env


def run_gate(gate: Gate, argv_factory, cwd: Path, env: dict,
             allowed: set[int], must: list[str]) -> None:
    try:
        rc, out = _run(argv_factory(), cwd, env)
    except subprocess.TimeoutExpired:
        gate.detail = "超时"
        return
    gate.rc, gate.output = rc, out
    missing = [m for m in must if m not in out]
    gate.ok = (rc in allowed) and not missing
    gate.detail = (f"rc={rc} 允许{sorted(allowed)} "
                   f"缺行={missing if missing else '无'}")


def gate0(gate: Gate, clone: Path, outside: Path, env: dict, py: str) -> None:
    """Gate 0: ``state_dir()`` is the same string and absolute in two cwds."""
    code = (f"import sys; sys.path.insert(0, r'{clone}'); "
            "from super_agent import agent_config as A; import os; "
            "print(A.state_dir(), os.path.isabs(A.state_dir()))")
    outs = []
    for cwd in (clone, outside):
        rc, out = _run([py, "-c", code], cwd, env)
        if rc != 0:
            gate.rc, gate.output = rc, out
            gate.detail = f"rc={rc}（{cwd}）"
            return
        outs.append(out.strip().splitlines()[-1])
    same = outs[0] == outs[1]
    absolute = outs[0].rstrip().endswith("True")
    gate.rc = 0
    gate.output = "\n".join(outs)
    gate.ok = same and absolute
    gate.detail = f"两处相同={same} 绝对={absolute}"


def gate7(gate: Gate, clone: Path, outside: Path, py: str) -> None:
    """Gate 7: the archive child imports the clone, and ``PYTHONPATH`` is why.

    In-process call to ``issue_cli._archive_session`` so the child under test is
    the one the product starts. ``inject=True`` must print a path inside the
    clone; the same call with the injection removed must **not** -- otherwise
    the gate would be green on a machine where the injection does nothing.
    """
    code = r'''
import os, sys
clone = r"%s"
sys.path.insert(0, clone)
from super_agent import issue_cli
tmpl = '{python} -c "import super_agent;print(super_agent.__file__)"'
mode = sys.argv[1]
if mode == "noinject":
    import subprocess as sp
    real = sp.run
    def run(argv, **kw):
        kw.pop("env", None)
        return real(argv, **kw)
    issue_cli.subprocess.run = run
os.chdir(r"%s")
print("OK" if issue_cli._archive_session(
    "session_00000000-0000-0000-0000-000000000000",
    {"session_archive_cmd": tmpl}) is not None else "NONE")
''' % (clone, outside)
    env = gate_env(clone, outside)
    results = {}
    for mode in ("inject", "noinject"):
        rc, out = _run([py, "-c", code, mode], outside, env)
        # The child prints ``<checkout>/super_agent/__init__.py``; both the
        # package marker and the clone prefix are required, because a path
        # outside the clone with the same tail would otherwise pass.
        flat = out.lower().replace("\\", "/")
        inside = "super_agent/__init__.py" in flat and str(clone).lower().replace(
            "\\", "/") in flat
        results[mode] = (rc, out, inside)
    rc_i, out_i, in_i = results["inject"]
    rc_n, out_n, in_n = results["noinject"]
    gate.rc = rc_i
    gate.output = f"[inject]\n{out_i}\n[noinject]\n{out_n}"
    gate.ok = rc_i == 0 and in_i
    # The negative half is informational: on a machine whose editable install
    # points at the clone, removing the injection changes nothing, and failing
    # the gate for that would be a false red. Reported, never asserted.
    gate.detail = (f"inject 落在 clone={in_i}；去掉注入后仍落 clone={in_n}"
                   "（editable 指向 clone 时此项不判，只记录）")


GATE8_DRIVER = r'''
import json, os, sys
PROJECT = "example-project"
clone, state, cfg_path, github_base, projects_file, creds = sys.argv[1:7]
sys.path.insert(0, clone)

popen = []
def hook(event, args):
    # args[1] is the argv list (args[0] is the executable, None on POSIX-only
    # paths). Recorded raw: joining it first would be the form that made the
    # first version's criterion permanently false.
    if event == "subprocess.Popen":
        popen.append(list(args[1]))
sys.addaudithook(hook)

from super_agent import issue_cli
# The module global is read on every call (``url = f"{API}{path}"``), so
# assigning it routes the GitHub side to the stub without touching the source.
issue_cli.API = github_base

B = lambda argv: [a for a in argv if "sessions.py" in a]
A = lambda argv: [a for a in argv if "recycle" in a and "archive" in a]

# ``--state-dir`` and not a config key: ``agent_config.overrides()`` is merged
# last and derives ``engine.session_state_dir`` from ``paths.state_dir``, so a
# ``--config`` value for it is silently replaced by the clone's own root. The
# flag is the top of the resolution chain, which is the only place that wins.
rc = issue_cli.main(["close", "--role", "supervisor", "--project", PROJECT,
                     "--issue", "100", "--config", cfg_path,
                     "--projects-file", projects_file,
                     "--env-file", creds, "--state-dir", state])
print("RC", rc)
print("A", len(A(popen)), "B", len(B(popen)))
print("KEY_KEPT", PROJECT + "#100" in json.load(
    open(os.path.join(state, "issue_sessions.json"), encoding="utf-8")))
'''


def _write_gate8_cfg(path: Path, state: Path, template: str) -> None:
    path.write_text(json.dumps({"engine": {
        "session_archive_cmd": template}}, ensure_ascii=False), encoding="utf-8")


def gate8(gate: Gate, clone: Path, outside: Path, py: str,
          work: Path, roles_file: Path, projects_file: Path) -> None:
    """Gate 8 (D6): one ``close``, one ``recycle archive``, zero ``sessions.py``.

    The audit hook lives in the process that calls ``issue_cli.main``: that is
    where ``subprocess.run`` is started, so the event is raised there. A hook in
    a parent of the CLI would only see the CLI itself -- the archive child is a
    grandchild (P0-4).

    The two assertions stay separate because the two templates pick **different**
    commands: the good one runs ``recycle archive`` (A=1, B=0), the bad one runs
    ``sessions.py archive`` (A=0, B=1). Asserting A==1 on the bad branch would
    fail before the B gate could run, and the B gate is the one that proves it
    can still go red (P0-D).
    """
    sid = "session_00000000-0000-0000-0000-000000000000"
    good_tmpl = "{python} -m super_agent.cli recycle archive {sid}"
    # The bad template names a ``sessions.py`` that does not have to exist: the
    # audit hook records argv before the child runs, so the question "did the
    # close path call the old script" is answered by the argv, not by the file.
    # Keeping the path abstract also keeps gate 6 honest -- a literal private
    # repository name in this file would make that gate fail on itself.
    # ``as_posix()``: the product refuses a backslash template on purpose
    # (shlex.split eats it), so the bad template must still be a valid one --
    # it fails for the right reason (it runs sessions.py), not because of a
    # path separator.
    bad_tmpl = "{python} " + (work / "legacy" / "tools" / "sessions.py").as_posix() \
        + " archive {sid}"
    results = {}
    with Stub(sid) as stub:
        for mode, tmpl in (("good", good_tmpl), ("bad", bad_tmpl)):
            state = work / f"state_{mode}"
            state.mkdir(parents=True, exist_ok=True)
            (state / "issue_sessions.json").write_text(json.dumps({
                "example-project#100": {"worker": {"session": sid,
                                                    "name": "wk-demo"}}}),
                encoding="utf-8")
            cfg = work / f"cfg_{mode}.json"
            _write_gate8_cfg(cfg, state, tmpl)
            # A token file rather than $GITHUB_TOKEN: ``github.token_env`` is
            # itself read through the same env layer, so exporting GITHUB_TOKEN
            # makes the *value* come back as the variable name. One fake file
            # keeps this gate about the archive path.
            creds = work / "creds.env"
            creds.write_text("GITHUB_TOKEN=stub-token\n", encoding="utf-8")
            env = gate_env(clone, outside, {
                "KIMI_SERVER": stub.base,
                "KIMI_CODE_SERVER_PORT": "",
            })
            rc, out = _run([py, "-c", GATE8_DRIVER, str(clone), str(state),
                            str(cfg), stub.base, str(projects_file),
                            str(creds)], outside, env)
            results[mode] = (rc, out, stub.archive_calls())
    good_rc, good_out, good_calls = results["good"]
    bad_rc, bad_out, bad_calls = results["bad"]

    def counts(text: str) -> tuple[int, int]:
        a = b = -1
        for line in text.splitlines():
            if line.startswith("A "):
                parts = line.split()
                a, b = int(parts[1]), int(parts[3])
        return a, b

    ga, gb = counts(good_out)
    ba, bb = counts(bad_out)

    failures = []
    if ga != 1:
        failures.append(f"好模板 A={ga}（期望 1）")
    if gb != 0:
        failures.append(f"好模板 B={gb}（期望 0）")
    if bb != 1:
        failures.append(f"坏模板 B={bb}（期望 1，反例闸）")
    if len(good_calls) != 1:
        failures.append(f"stub 收到归档请求 {len(good_calls)} 次（期望 1）")
    elif sid not in good_calls[0][2]:
        failures.append("stub 收到的归档请求不带本单 sid")

    gate.rc = good_rc
    gate.output = f"[good]\n{good_out}\n[bad]\n{bad_out}"
    gate.ok = not failures
    gate.detail = (f"good A={ga} B={gb} | bad B={bb} | stub 归档={len(good_calls)}"
                   f"{'  ' + '; '.join(failures) if failures else ''}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ISS-100 D7 change-machine check")
    ap.add_argument("--clone", required=True, help="where to build the clone")
    ap.add_argument("--forbid", default="", help="private repo name that must not appear")
    ap.add_argument("--with-close", action="store_true",
                    help="also run gate 8 (stub + in-process close)")
    ap.add_argument("--keep", action="store_true", help="keep the clone directory")
    args = ap.parse_args(argv)

    py = sys.executable
    clone = Path(args.clone).resolve()
    outside = Path(tempfile.mkdtemp(prefix="cm_outside_"))
    gates: list[Gate] = []
    watch = Path("D:/github/Laike_work_with_agent/state") / WATCH_NAME
    before = md5_state(watch)

    try:
        make_clone(clone)
        env = gate_env(clone, outside)

        g = Gate(0, "state_dir 两处一致且绝对")
        gate0(g, clone, outside, env, py)
        gates.append(g)

        g = Gate(1, "cli --help 含 recycle")
        run_gate(g, lambda: [py, "-c", _bootstrap(clone), "--help"],
                 outside, env, {0}, ["recycle"])
        gates.append(g)

        fake = "session_00000000-0000-0000-0000-000000000000"
        g = Gate(2, "recycle status 能跑（不证回收可用）")
        run_gate(g, lambda: [py, "-c", _bootstrap(clone), "recycle", "status", fake],
                 outside, env, {0, 1, 5}, ["archived=unknown", "cron: 未确认"])
        gates.append(g)

        g = Gate(3, "gh_issue projects 出一行短名")
        run_gate(g, lambda: [py, str(clone / "tools" / "gh_issue.py"), "projects"],
                 outside, env, {0}, ["/"])
        gates.append(g)

        g = Gate(4, "dispatch_cli --fetch 打印 server（不建会话）")
        run_gate(g, lambda: [py, "-c", _bootstrap_module(clone, "super_agent.dispatch_cli"),
                             "--fetch", "--session", fake],
                 outside, env, {0, 1}, ["[dispatch] server http://"])
        gates.append(g)

        g = Gate(5, "brief 的 # state= 是绝对路径")
        run_gate(g, lambda: [py, "-c", _bootstrap(clone), "brief"],
                 outside, env, {0}, ["# state="])
        if g.ok:
            line = next((x for x in g.output.splitlines() if x.startswith("# state=")), "")
            val = line.split("=", 1)[1].split()[0] if "=" in line else ""
            g.ok = os.path.isabs(val)
            g.detail += f" state={val} 绝对={os.path.isabs(val)}"
        gates.append(g)

        g = Gate(6, "私有仓名 0 命中")
        if not args.forbid:
            g.skipped = True
            g.detail = "SKIP(未给 --forbid)"
        else:
            hits = []
            for p in clone.rglob("*"):
                if p.suffix not in (".py", ".json") or not p.is_file():
                    continue
                # This file names the private workbench on purpose: it must
                # watch that workbench's event stream to prove the read-only
                # gates left it alone. Excluding it keeps gate 6 about the
                # product, not about its own checker.
                if p.name == Path(__file__).name:
                    continue
                try:
                    if args.forbid in p.read_text(encoding="utf-8", errors="ignore"):
                        hits.append(str(p.relative_to(clone)))
                except OSError:
                    continue
            g.rc = 0
            g.output = "\n".join(hits)
            g.ok = not hits
            g.detail = f"命中={len(hits)} {hits[:5]}"
        gates.append(g)

        g = Gate(7, "归档子进程落在 clone 内（PYTHONPATH 注入生效）")
        gate7(g, clone, outside, py)
        gates.append(g)

        g = Gate(8, "close 归档恰一次走 recycle、零次 sessions.py")
        if not args.with_close:
            g.skipped = True
            g.detail = "SKIP(未给 --with-close)"
        else:
            work = Path(tempfile.mkdtemp(prefix="cm_gate8_"))
            try:
                roles = clone / "config" / "sample.roles.json"
                projects = clone / "config" / "gh_projects.json"
                gate8(g, clone, outside, py, work, roles, projects)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        gates.append(g)

    finally:
        after = md5_state(watch)
        try:
            shutil.rmtree(outside)
        except OSError:
            pass

    # Reed's criterion: the read-only gates must not have written the stream.
    # ``last_seq`` is printed as a note only -- an appended row without ``seq``
    # leaves it unchanged, so it cannot be the criterion.
    unchanged = before == after
    try:
        sys.path.insert(0, str(CHECKOUT))
        from super_agent.sess_events import last_seq
        seq_note = f"last_seq={last_seq()}"
    except Exception as exc:                      # noqa: BLE001  note only
        seq_note = f"last_seq=读不出({type(exc).__name__})"

    print(f"watch = {watch}")
    for g in gates:
        mark = "SKIP" if g.skipped else ("OK  " if g.ok else "RED ")
        print(f"[{mark}] {g.num} {g.title}  {g.detail}")
        if not g.ok and not g.skipped:
            for line in g.output.splitlines()[-8:]:
                print(f"        {line}")
    print(f"md5 双判 before={before} after={after} 相同={unchanged}  {seq_note}")
    # The double check is its own gate: a read-only script that writes the
    # stream is a defect even when every command above exited 0.
    assert unchanged, ("只读门禁写动了事件流（含无 seq 行，last_seq 抓不到）："
                       f"{watch} before={before} after={after}")   # 防「只读脚本偷写事件流」

    skipped = [g for g in gates if g.skipped]
    red = [g for g in gates if not g.ok and not g.skipped]
    if red:
        print(f"结论：有红（{len(red)} 条）；rc=1")
        return 1
    if skipped:
        print(f"结论：不完整（{len(skipped)} 条 SKIP）；rc=2")
        return 2
    print("结论：全绿；rc=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
