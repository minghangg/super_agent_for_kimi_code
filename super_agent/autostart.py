"""autostart.py —— 双后端登录自启（startup 默认 / scheduled 可选）。

Migrated from the supervisor workbench tools/autostart.py (ISS-25 item 1).
issue #18 前置门禁。用户裁定：**自启用启动文件夹后端（零提权），不等硬化批**。

为什么要两个后端
----------------
`startup`（**默认**）：往用户启动文件夹写一个 `.vbs`，用 `WScript.Shell.Run(..., 0, False)`
以隐藏窗口起 `pythonw.exe <脚本>`。**零提权**、无需 UAC、登录即生效，用户删掉文件即关闭。
`scheduled`：沿用 `decide_watch.py` 既有的 `Register-ScheduledTask` 路径（需 UAC），
保留不动，只作为可选项——已有注册的任务（`KimiDecideWatch`）继续用它。

单一来源
--------
本模块是**两端唯一实现**（`decide_watch.py` 与 `monitor.py` 都 import 它）。
`Spec` 描述「装什么」，两个后端各自把 `Spec` 翻成自己的落地形式；
回读校验也读同一份 `Spec`，所以注册与校验不会漂。

为什么 `.vbs` 用 UTF-16LE + BOM
-------------------------------
`WScript` 读 `.vbs` 时按**系统 ACP（ANSI 代码页）**解析；不吃 BOM 的 UTF-8 在中文
Windows 上通常按 GBK 解，中文路径会乱码。用 **UTF-16LE + BOM** 的结论不变，
但理由是「**不依赖系统 ACP 设置**」而不是「UTF-8 必然乱」——QA 实测本机
ACP=65001（UTF-8）时，无 BOM 的 UTF-8 也能跑通。UTF-16 在任何 ACP 下都成立。
带 BOM 的 UTF-16LE 是 VBScript 的标准编码，实测可正确处理中文路径。
（备选：ANSI/GBK 也能跑，但一旦路径含非 GBK 字符就崩；UTF-16 更稳。）

退出码（与调用方共用）
----------------------
0   成功（含幂等跳过）
10  startup 注册失败（写盘失败 / 回读不符）
11  startup 卸载失败（删了但回读仍在）

（8 是 `--send-mode` 的预留位，9 留给 salarm 的 uninstall —— 自启**不占**这两个，
 详见交付评论里的完整占用表。QA18-3 订正：本段曾误写 8/9。）
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

EXIT_OK = 0
# QA18-3：8 是 `--send-mode` 的预留位（`decide_watch.py:366` 注释明写「勿混」），
# 自启不得占用，故用 10 / 11。完整占用表见交付评论。
EXIT_STARTUP_INSTALL_FAILED = 10
EXIT_STARTUP_UNINSTALL_FAILED = 11

# ISS-101: the three-way consistency check. One code, one action: 20 means
# "fix the registration", 21 "start the process / register the task", 22 "fix
# the script". Collapsing them into 1 would leave a caller unable to tell a
# wrong entry point from an absent one.
EXIT_VERIFY_MISMATCH = 20
EXIT_VERIFY_UNDECIDABLE = 21
EXIT_VERIFY_UNPARSABLE = 22

BACKENDS = ("startup", "scheduled")
DEFAULT_BACKEND = "startup"

VBS_ENCODING = "utf-16-le"
VBS_BOM = b"\xff\xfe"

# ISS-101: the two things a login starts, and the script that starts them.
# These are neutral names -- the task each one maps to is a deployment fact
# and lives in ``agent_config`` (``autostart.tasks``).
PARTS = ("wss_bus", "decide_watch")
SCRIPT_REL = ("new_workbench", "scripts", "start_bus_watch.ps1")

# The comparison unit for the three-way check is the **absolute .py path
# token** in a command line, never the basename: a basename is identical
# wherever the file lives, so it cannot tell one checkout from another. The
# quoted form is the only reliable shape when the path contains a space
# (``D:\Program Files\...``); the unquoted form covers what ``Get-CimInstance``
# prints for a path without one.
_QUOTED_PY = re.compile(r'"([A-Za-z]:[\\/][^"]*?\.py)"')
_UNQUOTED_PY = re.compile(r'(?<!")[A-Za-z]:[\\/][^"\s]*?\.py')
_START_PROCESS = re.compile(r'^\s*Start-Process\b', re.IGNORECASE)
_ROOT_ASSIGN = re.compile(r'^\s*\$root\s*=\s*(.+?)\s*$', re.IGNORECASE)
_QUOTED_LITERAL = re.compile(r"""["']([^"']*\.py)["']""")


@dataclass
class Spec:
    """「装什么」的唯一定义（两个后端共用）。"""

    name: str                  # KimiDecideWatch / KimiMonitor
    script: Path               # 被启动的 .py 绝对路径
    args: list[str] = field(default_factory=list)   # 传给脚本的参数
    workdir: Path | None = None                     # 工作目录（默认脚本所在仓根）
    execute_note: str = ""     # pythonw 降级说明（空＝正常）

    def __post_init__(self) -> None:
        self.script = Path(self.script).resolve()
        if self.workdir is None:
            self.workdir = self.script.parent.parent
        self.workdir = Path(self.workdir).resolve()

    @property
    def startup_file(self) -> Path:
        """`%APPDATA%\\...\\Startup\\<name>.vbs`（可在测试里用 APPDATA 覆盖）。"""
        return startup_dir() / f"{self.name}.vbs"

    def command_line(self) -> str:
        """`.vbs` 里实际执行的命令行（脚本路径与参数都加引号）。"""
        parts = [f'"{self.script}"'] + [f'"{a}"' if " " in a else a for a in self.args]
        return " ".join(parts)


def startup_dir() -> Path:
    """登录启动文件夹。`APPDATA` 可覆盖（测试用）；取不到就走已知默认路径。"""
    appdata = (os.environ.get("APPDATA") or "").strip()
    if appdata:
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return (Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup")


def windowless_python() -> tuple[str, str]:
    """取同目录 `pythonw.exe`；拿不到就如实降级并给出说明（不假装）。"""
    exe = Path(sys.executable).resolve()
    cand = exe.with_name("pythonw.exe")
    if cand.is_file():
        return str(cand), ""
    return str(exe), (f"同目录未找到 pythonw.exe（{cand}），降级用 python.exe —— "
                      f"登录时会**弹控制台窗口**")


def render_vbs(spec: Spec, exe: str) -> str:
    """生成 `.vbs` 全文（**内容与时间无关**，保证幂等）。

    QA18-1：正文曾含「生成于 HH:MM」⇒ 跨分钟重装必判不一致并重写，
    幂等事实上不成立。生成时间只出现在 `--dry-run` 的打印里，**不进文件**。
    """
    cmd = f'"{exe}" {spec.command_line()}'
    # VBScript 里字符串中的双引号要写成两个；这里用 _vbs_str 统一处理，避免转义歧义。
    lines = [
        f"' {spec.name} —— 登录自启（issue #18，startup 后端，零提权）",
        "' 删除本文件即取消自启。",
        "' 用 pythonw.exe 隐藏窗口启动；工作目录固定为仓根。",
        "Option Explicit",
        "Dim sh, rc",
        "On Error Resume Next",
        "Set sh = CreateObject(\"WScript.Shell\")",
        "If Err.Number <> 0 Then WScript.Quit 1",
        f'sh.CurrentDirectory = "{spec.workdir}"',
        "If Err.Number <> 0 Then WScript.Quit 1",
        f"rc = sh.Run({_vbs_str(cmd)}, 0, False)",
        "If Err.Number <> 0 Then WScript.Quit 1",
        "WScript.Quit 0",
    ]
    return "\r\n".join(lines) + "\r\n"


def _vbs_str(s: str) -> str:
    """把任意字符串安全地放进 VBScript 的字符串字面量（引号加倍）。"""
    return '"' + s.replace('"', '""') + '"'


def read_vbs(path: Path) -> str:
    """按 `.vbs` 的实际编码读回（带 BOM 的 UTF-16LE 或 ANSI/GBK 兜底）。"""
    raw = path.read_bytes()
    if raw.startswith(VBS_BOM):
        return raw[2:].decode(VBS_ENCODING, "replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", "replace")


def write_vbs(path: Path, text: str) -> None:
    """原子写（写 .tmp → fsync → replace）。

    QA18-2：上一版没有 try/finally，`os.replace` 失败（目标只读／同名目录）时
    会遗留 1KB 左右的 `.vbs.tmp`，与「失败不留半写文件」的承诺不符。
    现在失败路径**一定**清理 tmp；成功路径 tmp 已被 replace 移走（unlink 幂等）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".vbs.tmp")
    data = VBS_BOM + text.encode(VBS_ENCODING)
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)      # 成功时已不存在；失败时清残file
        except OSError:
            pass


# --------------------------------------------------------------------- 回读校验
def verify_vbs(spec: Spec, exe: str) -> list[str]:
    """读回 `.vbs` 并逐项比对；返回失败项（空＝全过）。"""
    path = spec.startup_file
    if not path.is_file():
        return [f"启动文件不存在：{path}"]
    try:
        text = read_vbs(path)
    except OSError as exc:
        return [f"读不回：{type(exc).__name__}: {exc}"]
    fails: list[str] = []
    if str(spec.workdir) not in text:
        fails.append(f"工作目录不符：期望含 {spec.workdir!r}")
    if str(spec.script) not in text:
        fails.append(f"脚本路径不符：期望含 {spec.script!r}")
    if Path(exe).name not in text:
        fails.append(f"解释器不符：期望含 {Path(exe).name!r}")
    for a in spec.args:
        if a not in text:
            fails.append(f"参数缺失：{a!r}")
    return fails


def diff_summary(old: str, new: str) -> list[str]:
    """给「覆盖」时的变更点摘要（只报有差异的行，最多 6 条）。"""
    o = old.splitlines()
    n = new.splitlines()
    out: list[str] = []
    for i in range(max(len(o), len(n))):
        a = o[i] if i < len(o) else "<无>"
        b = n[i] if i < len(n) else "<无>"
        if a != b:
            out.append(f"行{i + 1}: - {a.strip()[:70]}  + {b.strip()[:70]}")
        if len(out) >= 6:
            break
    return out


# ------------------------------------------------------------------ ISS-101
# Login autostart: the three-way consistency check.
#
# Why this exists: the two scheduled tasks were created by hand, so their
# Action points straight at ``tools/*.py`` while the startup script moved to
# ``new_workbench/scripts/``. Nothing called the script and nothing noticed, so
# a restart ran the old entry point -- and the script's only read-back line was
# silently dead (its regex never compiled, see ``start_bus_watch.ps1:25``).
#
# Three sides, one question: *which file does each of them start?*
#
#   1. the scheduled task's Action            (``Get-ScheduledTask``)
#   2. the startup script's ``Start-Process`` (parsed, never executed)
#   3. the running process's command line     (``Get-CimInstance``)
#
# The unit of comparison is the **absolute .py path token** in a command line.
# A basename is the same string wherever the file lives, so it cannot tell one
# checkout from another -- which is the entire question here. The ps1 read-back
# line keeps using a basename on purpose: its reader is a person asking "what
# is it called", not the check asking "where is it".


class AutostartError(RuntimeError):
    """A configuration fault: bad root, missing script, unconfigured task name.

    rc=2 for every instance, because they all mean "go fix agent.json or move
    a file" -- never "go re-register".
    """


@dataclass
class Side:
    """One of the three answers about one part."""

    state: str = "ok"      # ok | via_script | unregistered | unparsable | absent
    value: str = ""        # normalized absolute .py path (state ok/via_script)
    detail: str = ""       # human text: script path, why it failed
    pid: int = 0


def _normalize(path: str) -> str:
    """Case-folded, symlink-resolved form -- what "same file" means here."""
    return os.path.normcase(os.path.realpath(str(path)))


def token_paths(command_line: str) -> list[str]:
    """Every absolute ``.py`` path token in a command line, in order.

    Two shapes merged: quoted (the only reliable form when the path contains a
    space, e.g. ``D:\\Program Files\\...``) and bare. The tokens come back as
    written -- no anchor is added, because a token already carries one.
    """
    text = command_line or ""
    found: list[str] = []
    for match in _QUOTED_PY.finditer(text):
        found.append(match.group(1))
    for match in _UNQUOTED_PY.finditer(text):
        token = match.group(0)
        if token not in found:
            found.append(token)
    return found


def _single_token(command_line: str) -> tuple[str, str]:
    """``(path, "")`` when exactly one token, else ``("", reason)``.

    "Not exactly one" is an answer, not a gap to fill with the first hit:
    taking the first makes the verdict depend on how the line happens to be
    ordered.
    """
    tokens = token_paths(command_line)
    if not tokens:
        return "", "命令行里没有绝对 .py 路径"
    if len(tokens) >= 2:
        return "", f"命令行里有 {len(tokens)} 个绝对 .py 路径，多半目标，无法判定"
    return tokens[0], ""


def _powershell_exe() -> str:
    import shutil

    return shutil.which("powershell") or "powershell.exe"


def _ps(script: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run one read-only PowerShell snippet; never raises for a missing shell."""
    try:
        proc = subprocess.run(
            [_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def _ps_literal(text: str) -> str:
    """A single-quoted PowerShell string holding ``text`` verbatim."""
    return "'" + str(text).replace("'", "''") + "'"


def resolve_root(override: str | None = None) -> Path:
    """The workbench root: ``engine.lwa_root``, and nothing else.

    No parents-chain search and no fallback. Deriving it from this checkout's
    ancestors cannot work -- the workbench is a sibling directory, never an
    ancestor -- and a rule that finds nothing on the machine it ships on is not
    a rule. ``brief._lwa_root`` already treats ``engine.lwa_root`` as the
    declared answer to this same question (ISS-79), so a second derivation here
    would be a second answer to one question.
    """
    from . import agent_config

    if override:
        root = Path(os.path.expanduser(override)).resolve()
    else:
        raw = str(agent_config.get("engine.lwa_root") or "").strip()
        if not raw:
            raise AutostartError(
                "工作台根未配置：agent.json 缺 engine.lwa_root；无法生成计划任务 Action。\n"
                '  写法示例：{ "engine": { "lwa_root": "D:/github/<工作台>" } }')
        root = Path(os.path.expanduser(raw)).resolve()
    if not root.is_dir():
        raise AutostartError(f"工作台根不存在：{root}（来自 engine.lwa_root）")
    return root


def script_path(root: Path) -> Path:
    return root.joinpath(*SCRIPT_REL)


def task_name(part: str) -> str:
    """The scheduled task for one part; unconfigured is an error, not a guess."""
    from . import agent_config

    table = agent_config.autostart_tasks()
    name = str(table.get(part) or "").strip()
    if not name:
        raise AutostartError(
            f"任务名未配置：agent.json 缺 autostart.tasks.{part}；不能猜任务名。\n"
            f'  写法示例：{{ "autostart": {{ "tasks": {{ "{part}": "<任务名>" }} }} }}')
    return name


def _root_hops(text: str) -> tuple[int, str]:
    """How many levels ``$root`` climbs from ``$PSScriptRoot``.

    Only the shipped shape is understood -- ``$root = Resolve-Path (Join-Path
    $PSScriptRoot '..\\..')``. Guessing a rewrite for any other expression is
    how a parser starts inventing paths.
    """
    hops = -1
    for line in text.splitlines():
        match = _ROOT_ASSIGN.match(line)
        if not match:
            continue
        rhs = match.group(1)
        if "$PSScriptRoot" not in rhs:
            return 0, "$root 不是由 $PSScriptRoot 推导的"
        hops = rhs.count("..")
    if hops < 0:
        return 0, "脚本里没有 $root 赋值"
    return hops, ""


def parse_script(path: Path) -> tuple[dict, str]:
    """``({part: Side}, reason)`` for the script; reason is "" when it parsed.

    Parsing, not rewriting: a ``Start-Process`` line either names a literal
    ``.py`` or a ``$root\\...`` path whose root the script itself derives. Any
    other shape is reported unparsable instead of guessed at, and a script with
    no ``Start-Process`` at all is an error -- silently skipping it is how the
    check would pass on a script that starts nothing.
    """
    if not path.is_file():
        return {}, f"脚本不存在：{path}"
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return {}, f"脚本读不出：{path}（{type(exc).__name__}）"

    # "No start line" is checked before the root derivation: a script that
    # starts nothing has no root worth resolving, and reporting the missing
    # root first would name the symptom instead of the fault.
    lines = [ln for ln in text.splitlines() if _START_PROCESS.match(ln)]
    if not lines:
        return {}, f"脚本内找不到启动行（{path}）"

    hops, why = _root_hops(text)
    if why:
        return {}, f"脚本内无法解析 $root：{why}（{path}）"
    root = path.parent
    for _ in range(hops):
        root = root.parent

    out: dict = {}
    for line in lines:
        literals = [raw.strip() for raw in _QUOTED_LITERAL.findall(line)]
        literals = [raw for raw in literals if raw.lower().endswith(".py")]
        if not literals:
            return {}, f"脚本内无法解析启动行：{line.strip()[:90]}（{path}）"
        targets: list[str] = []
        for raw in literals:
            if "$root" in raw:
                tail = raw.split("$root", 1)[1].lstrip("\\/").replace("\\", "/")
                targets.append(str(root / tail))
            elif re.match(r"^[A-Za-z]:[\\/]", raw):
                targets.append(raw)
            else:
                return {}, f"脚本内无法解析启动行：{line.strip()[:90]}（{path}）"
        if len(targets) >= 2:
            return {}, (f"启动行里有 {len(targets)} 个 .py，多半目标，无法判定："
                        f"{line.strip()[:90]}（{path}）")
        target = targets[0]
        stem = Path(target).stem.lower()
        for part in PARTS:
            # `<part>.py` is the direct form; `<part>_<suffix>.py` is the shim
            # form (ISS-102: `wss_bus_safkc.py` is the injection shell in front
            # of the safkc module). Both name the same part -- refusing the
            # second shape made the check report "no start line" for a script
            # that does start the bus.
            if stem == part or stem.startswith(f"{part}_"):
                out[part] = Side("ok", _normalize(target), str(path))
    return out, ""


def read_task(name: str) -> dict:
    """The Action and first Trigger of one scheduled task (read-only)."""
    import json

    script = (
        f"$n = {_ps_literal(name)}; "
        "$t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue; "
        "if ($null -eq $t) { '{\"found\":false}' } else { "
        "$a = $t.Actions[0]; "
        "$o = [ordered]@{found=$true; execute=[string]$a.Execute; "
        "arguments=[string]$a.Arguments; workdir=[string]$a.WorkingDirectory; "
        "trigger=''}; "
        "if ($t.Triggers.Count -gt 0) { "
        "$o.trigger = [string]$t.Triggers[0].CimClass.CimClassName }; "
        "$o | ConvertTo-Json -Compress }")
    rc, out, err = _ps(script)
    if rc != 0:
        raise AutostartError(f"取不到计划任务 {name}：{err.strip()[:200]}")
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    try:
        return json.loads(lines[-1])
    except (ValueError, IndexError):
        raise AutostartError(f"计划任务 {name} 的输出无法解析：{out.strip()[:200]}") from None


def read_processes(pattern: str = "wss_bus|decide_watch",
                   pids: list | None = None) -> list[tuple[int, str]]:
    """``[(pid, command line)]`` from ``Get-CimInstance``; ``pids`` narrows it."""
    import json

    if pids:
        filters = ",".join(f"ProcessId={int(p)}" for p in pids)
        query = f"@(Get-CimInstance Win32_Process -Filter {_ps_literal(filters)})"
    else:
        query = ("@(Get-CimInstance Win32_Process | Where-Object { "
                 f"$_.CommandLine -match {_ps_literal(pattern)} }})")
    script = (query + " | ForEach-Object { [ordered]@{pid=[int64]$_.ProcessId; "
                     "cmd=[string]$_.CommandLine} | ConvertTo-Json -Compress }")
    rc, out, err = _ps(script)
    if rc != 0:
        raise AutostartError(f"取不到进程列表：{err.strip()[:200]}")
    rows: list[tuple[int, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        rows.append((int(row.get("pid") or 0), str(row.get("cmd") or "")))
    return rows


def _short(path: str, root: Path | None) -> str:
    """``<rootname>/<rel>`` inside the root; the full path otherwise.

    Display only: the comparison already ran on the normalized form, so the
    original casing comes from the token, not from a second resolution.
    """
    rel = None
    if root:
        try:
            candidate = Path(path)
            base = Path(os.path.realpath(str(root)))
            real = Path(os.path.realpath(str(candidate)))
            if os.path.normcase(str(base)) == os.path.normcase(str(real)):
                rel = None
            elif os.path.normcase(str(real)).startswith(
                    os.path.normcase(str(base)) + os.sep):
                rel = str(real)[len(str(base)) + 1:].replace("\\", "/")
        except OSError:
            rel = None
    if rel is None:
        return path
    return f"{root.name}/{rel}"


def _task_side(row: dict, root: Path | None, script: Path) -> Side:
    """What the task starts: the startup script, or a ``.py`` of its own."""
    if not row.get("found"):
        return Side("unregistered")
    combined = f"{row.get('execute') or ''} {row.get('arguments') or ''}"
    if _normalize(script) in _normalize(combined):
        return Side("via_script", detail=str(script))
    path, why = _single_token(combined)
    if why:
        return Side("unparsable", detail=why)
    return Side("ok", _normalize(path), "直指 .py")


def _process_side(part: str, rows: list[tuple[int, str]],
                  injected: bool = False) -> Side:
    """What the running process starts, or why that cannot be decided.

    A command line that yields zero or two absolute ``.py`` tokens is
    ``unparsable``, not "no such process": the same rule the task side applies
    (``_task_side``). Skipping an unparsable line and then reporting
    ``absent`` ("未运行", rc=21) would blame the wrong thing -- the process is
    there, its command line is just not one file -- and would disagree with the
    task side about the same input. One input, one verdict, both sides.

    ``injected`` separates the two sources. A ``--command-line`` line was typed
    for this part, so any unparsable line is this part's answer. A line read
    from ``Get-CimInstance`` is filtered only by ``wss_bus|decide_watch``, so it
    may belong to the *other* part; counting it here would let a broken
    ``decide_watch`` line mark ``wss_bus`` unparsable.
    """
    unparsable: list[str] = []
    hits: list[tuple[int, str]] = []
    for pid, cmd in rows:
        if not injected and part not in (cmd or "").lower():
            continue
        path, why = _single_token(cmd)
        if why:
            unparsable.append(why)
            continue
        if Path(path).name.lower() == f"{part}.py":
            hits.append((pid, path))
    if hits:
        values = {_normalize(p) for _, p in hits}
        if len(values) >= 2:
            return Side("unparsable",
                        detail=f"{len(hits)} 个进程指向不同文件，无法判定")
        pid = min(p for p, _ in hits)
        # pid 0 means the line came from ``--command-line``, which has no
        # process behind it. Printing "pid 0" would read as a real process
        # with a real id.
        detail = "" if pid else "注入命令行"
        return Side("ok", _normalize(hits[0][1]), detail, pid)
    if unparsable:
        return Side("unparsable", detail=unparsable[0])
    return Side("absent", detail="未运行")


def register_action(root: Path) -> tuple[str, list[str]]:
    """``(display, argv)`` for the Action that runs the startup script.

    ``-NoProfile`` so a login start cannot depend on an interactive profile;
    ``-ExecutionPolicy Bypass`` so an unknown machine policy cannot block it;
    ``-File`` rather than ``-Command`` because ``-Command`` re-parses the
    quotes, and this machine's interpreter path contains a space.
    """
    script = script_path(root)
    if not script.is_file():
        raise AutostartError(f"启动脚本不存在：{script}（工作台根 {root}）")
    display = (f'powershell.exe -NoProfile -ExecutionPolicy Bypass '
               f'-File "{script}"')
    argv = [_powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script)]
    return display, argv


def _apply_task(name: str, root: Path) -> int:
    """Register or re-point one task. 0 on success, 10 on failure."""
    exists = bool(read_task(name).get("found"))
    script = script_path(root)
    inner = f'-NoProfile -ExecutionPolicy Bypass -File "{script}"'
    action = (f"New-ScheduledTaskAction -Execute {_ps_literal(_powershell_exe())} "
              f"-Argument {_ps_literal(inner)} "
              f"-WorkingDirectory {_ps_literal(str(root))}")
    if exists:
        # Only the Action: re-creating the Trigger would silently change when
        # the login start fires, which nobody asked this command to do.
        label = "Set-ScheduledTask"
        body = (f"$a = {action}; "
                f"Set-ScheduledTask -TaskName {_ps_literal(name)} -Action $a | Out-Null")
    else:
        label = "Register-ScheduledTask"
        body = (f"$a = {action}; $t = New-ScheduledTaskTrigger -AtLogOn; "
                f"Register-ScheduledTask -TaskName {_ps_literal(name)} "
                "-Action $a -Trigger $t | Out-Null")
    rc, _out, err = _ps(body, timeout=60.0)
    if rc != 0:
        print(f"[autostart] {label} 失败：{err.strip()[:300]}", file=sys.stderr)
        print("[autostart] 需 UAC：以管理员身份重跑同一条命令。", file=sys.stderr)
        return EXIT_STARTUP_INSTALL_FAILED
    return EXIT_OK


def verify(parts: list[str], *, script: Path | None = None,
           names: dict | None = None, pids: list | None = None,
           command_lines: list | None = None,
           process_filter: str = "wss_bus|decide_watch",
           root: Path | None = None) -> int:
    """Compare the three sides for ``parts``. Returns 0 / 20 / 21 / 22."""
    names = dict(names or {})
    if script is None and root is not None:
        script = script_path(root)
    script_sides: dict = {}
    script_fault = ""
    if script is not None:
        script_sides, script_fault = parse_script(script)

    if command_lines:
        rows = [(0, text) for text in command_lines]
    else:
        rows = read_processes(process_filter, list(pids or []))

    print("自启动一致性自检（只读）")
    print()
    header = ("部件", "计划任务", "启动脚本", "当前进程")
    table: list[tuple[str, ...]] = []
    mismatches: list[str] = []
    unregistered: list[str] = []
    unparsable: list[str] = []
    not_running: list[str] = []
    direct_py: list[str] = []
    unparsed_html: list[str] = []

    for part in parts:
        name = names.get(part) or ""
        row = read_task(name) if name else {"found": False}
        task = _task_side(row, root, script) if script is not None else \
            Side("unregistered")
        if not row.get("found"):
            task = Side("unregistered")
        scr = script_sides.get(part)
        if scr is None:
            scr = Side("unparsable" if script_fault else "absent",
                       detail=script_fault or f"脚本里没有 {part}.py 的启动行")
        proc = _process_side(part, rows, injected=bool(command_lines))

        if task.state == "via_script":
            task_value = scr.value if scr.state == "ok" else ""
            task_text = f"{name}（经启动脚本）" if name else "（经启动脚本）"
        elif task.state == "ok":
            task_value = task.value
            task_text = f"{name}（直指 .py）" if name else "（直指 .py）"
            direct_py.append(part)
        elif task.state == "unregistered":
            task_value = ""
            task_text = f"未注册（{name}）" if name else "未注册"
        else:
            task_value = ""
            task_text = f"（{task.detail}）"

        if scr.state == "ok":
            scr_text = _short(scr.value, root)
        else:
            scr_text = scr.detail or "无法解析"
            if script_fault:
                unparsed_html.append(part)
        proc_text = (f"{_short(proc.value, root)}（{proc.detail}）"
                     if proc.state == "ok" and proc.detail
                     else (f"{_short(proc.value, root)}（pid {proc.pid}）"
                           if proc.state == "ok"
                           else (proc.detail or "未运行")))

        verdict = "是"
        if task.state == "unregistered":
            unregistered.append(part)
            verdict = "未注册"
        elif task.state == "unparsable":
            unparsable.append(part)
            verdict = "不可判"
        elif scr.state != "ok":
            unparsable.append(part)
            verdict = "不可判"
        elif proc.state == "unparsable":
            unparsable.append(part)
            verdict = "不可判"
        elif task_value and task_value != scr.value:
            mismatches.append(part)
            verdict = "否"
        elif proc.state == "absent":
            not_running.append(part)
            verdict = "部分"
        elif proc.value != task_value:
            mismatches.append(part)
            verdict = "否"

        table.append((part, task_text, scr_text, proc_text, verdict))

    widths = [max(len(r[i]) for r in [header] + table) for i in range(4)]
    print("  ".join(header[i].ljust(widths[i]) for i in range(4)) + "  一致?")
    for row in table:
        print("  ".join(row[i].ljust(widths[i]) for i in range(4)) + "  " + row[4])
    print()

    for part in mismatches:
        print(f"  · {part}：三方不一致")
        row = next(r for r in table if r[0] == part)
        print(f"    计划任务  {row[1]}")
        print(f"    启动脚本  {row[2]}")
        print(f"    当前进程  {row[3]}")
        print(f"    建议：python -m super_agent.autostart --register {part} --apply")
    for part in unparsable:
        print(f"  · {part}：无法判定（见上表）")
    for part in unregistered:
        print(f"  · {part}：计划任务未注册")
        print(f"    建议：python -m super_agent.autostart --register {part} --apply")
    for part in not_running:
        print(f"  · {part}：进程侧未运行；另两方仍已比对")

    if mismatches:
        print(f"结论：{len(mismatches)} 处不一致")
        rc = EXIT_VERIFY_MISMATCH
    elif unparsable:
        print("结论：部分无法判定（解析失败）")
        rc = EXIT_VERIFY_UNPARSABLE
    elif unregistered:
        print("结论：未注册，不可判")
        rc = EXIT_VERIFY_UNDECIDABLE
    elif not_running:
        print("结论：部分不可判（进程侧未运行）")
        rc = EXIT_VERIFY_UNDECIDABLE
    else:
        print("结论：三方一致")
        rc = EXIT_OK

    # A prompt, not a verdict: the three sides can agree on an entry point that
    # group A is meant to replace. It does not move the exit code.
    if direct_py:
        print("提示：计划任务未指向启动脚本（直指 .py）。组 A 之后应变为「经启动脚本」。")
        for part in direct_py:
            print(f"      建议：python -m super_agent.autostart "
                  f"--register {part} --apply")
    print(f"退出码：{rc}")
    return rc


def register(part: str, *, apply: bool, root: Path) -> int:
    """Print the Action for ``part`` (and apply it when ``apply``)."""
    parts = list(PARTS) if part == "all" else [part]
    for one in parts:
        name = task_name(one)
        display, _argv = register_action(root)
        print(f"[autostart] 部件     {one}")
        print(f"[autostart] 任务名   {name}（来自 agent.json: autostart.tasks）")
        print(f"[autostart] 工作台   {root}（engine.lwa_root）")
        print(f"[autostart] 启动脚本 {script_path(root)}")
        print(f"[autostart] Action   {display}")
        print("[autostart] Trigger  AtLogOn")
        if apply:
            rc = _apply_task(name, root)
            if rc != EXIT_OK:
                return rc
        else:
            print("[autostart] dry-run：未执行。去掉 --dry-run 并加 --apply "
                  "才注册（需 UAC）。")
        print()
    if not apply:
        return EXIT_OK
    # Registered means "and it checks out": the same check a caller would run.
    return verify(parts, root=root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m super_agent.autostart",
        description="登录自启：三方一致性自检与计划任务登记（ISS-101）")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--verify", action="store_true",
                        help="三方比对：计划任务 / 启动脚本 / 当前进程（只读）")
    action.add_argument("--register", choices=list(PARTS) + ["all"],
                        help="生成计划任务的 Action（--apply 时才真注册）")
    action.add_argument("--list", action="store_true",
                        help="列出部件与已配置的任务名")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="只打印，不执行（--register 的默认）")
    mode.add_argument("--apply", action="store_true",
                      help="真正注册/改写计划任务（需 UAC）")
    parser.add_argument("--part", default="all", choices=list(PARTS) + ["all"],
                        help="--verify 只查这些部件（default all）")
    parser.add_argument("--script", default=None,
                        help="--verify 读这个启动脚本（默认从 engine.lwa_root 推导）")
    parser.add_argument("--task-name", default=None,
                        help="--verify 查这个任务名（默认 agent.json: autostart.tasks）")
    parser.add_argument("--pid", action="append", type=int, default=None,
                        help="--verify 只读这些 pid（仍是真实进程）")
    parser.add_argument("--command-line", action="append", default=None,
                        help="--verify 用这条命令行代替读进程（同一套解析；"
                             "用来构造本机不存在的状态）")
    parser.add_argument("--process-filter", default="wss_bus|decide_watch",
                        help="--verify 选进程的正则（default wss_bus|decide_watch）")
    parser.add_argument("--lwa-root", default=None,
                        help="工作台根（默认 agent.json: engine.lwa_root）")
    return parser


def main(argv=None) -> int:
    """Entry point. An unknown flag is argparse's own rc=2, never a silent 0.

    Before ISS-101 this module had no ``main`` and no ``__main__`` guard, so
    ``python -m super_agent.autostart --verify`` did nothing and exited 0 -- the
    #87 shape: the tool exists, the production path never reaches it, and the
    failure reads as success.
    """
    args = build_parser().parse_args(argv)
    try:
        root = resolve_root(args.lwa_root)
    except AutostartError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.list:
        for part in PARTS:
            try:
                name = task_name(part)
            except AutostartError:
                name = "(未配置)"
            print(f"{part}\t{name}")
        return EXIT_OK

    if args.register:
        try:
            return register(args.register, apply=bool(args.apply), root=root)
        except AutostartError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    parts = list(PARTS) if args.part == "all" else [args.part]
    if args.task_name and len(parts) > 1:
        print("--task-name 只能配一个部件：加 --part <部件>", file=sys.stderr)
        return 2
    names: dict = {}
    for part in parts:
        if args.task_name:
            names[part] = args.task_name
            continue
        try:
            names[part] = task_name(part)
        except AutostartError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    script = Path(os.path.expanduser(args.script)).resolve() if args.script else None
    try:
        return verify(parts, script=script, names=names, pids=args.pid,
                      command_lines=args.command_line,
                      process_filter=args.process_filter, root=root)
    except AutostartError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
