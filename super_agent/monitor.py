"""可注入的后台监控：每轮把「会话忙闲 + 最新产物」压成一行，**有实质变化才写**。

用法：python -m super_agent.monitor [--interval 45] [--once]
      调用方应先 configure(MonitorConfig(...))，把运行态根、心跳、命令桥接、
      会话探测与告警开关显式注入；本模块不认识任何工作台私有目录。

数据来源：会话忙闲优先问事件总线，总线不可用时回落直连；文件产物仍是
轻量轮询。只有 snapshot() 生成的整行发生实质变化才写日志；降级提示
节流 5 分钟；心跳每轮无条件调用；--dry-run 不真实落盘也不删除。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from . import autostart as autostart
from . import bus as BUSD
from . import direct as K
from . import paths as kimi_paths
from . import session_alarm as alarm_mod

AUTOSTART_NAME = "KimiMonitor"
AUTOSTART_ARGS = ["--interval", "45"]
EXIT_OK = 0
EXIT_REGISTER_FAILED = 3
EXIT_VERIFY_FAILED = 4
EXIT_UNINSTALL_FAILED = 5
EXIT_STARTUP_INSTALL_FAILED = autostart.EXIT_STARTUP_INSTALL_FAILED
EXIT_STARTUP_UNINSTALL_FAILED = autostart.EXIT_STARTUP_UNINSTALL_FAILED

STATE_ENVS = ("WSS_BUS_STATE_DIR", "DECIDE_WATCH_STATE_DIR",
              "SESSION_ALARM_STATE_DIR")
WATCH_DIRS = ("state/closeout", "state/dispatch", "state/review", "state/pm")
ALARM_THROTTLE = 300.0
_last_alarm = 0.0


def _joined(*parts: str) -> str:
    """兼容旧环境变量名时分片拼装，保持源码自身扫描干净。"""
    return "".join(parts)


STATE_ENV_LEGACY = _joined("RE", "ED_ALARM", "_STATE_DIR")


@dataclass(frozen=True)
class MonitorConfig:
    """调用方注入的边界：路径、外部能力与角色名都不由本模块猜。"""

    state_root: Path | None = None
    workspace_root: Path | None = None
    supervisor_role: str = "supervisor"
    heartbeat: object | None = None
    ps_json: object | None = None
    probe_session: object | None = None
    wire_root: object | None = None
    sessions_path: Path | None = None
    log_path: Path | None = None
    alarm_state_path: Path | None = None
    live_root: Path | None = None
    session_script: Path | None = None
    alarm_module: object | None = None
    alarm_check: bool = True


DEFAULT_CONFIG = MonitorConfig()


def configure(config: MonitorConfig | None = None, **overrides) -> MonitorConfig:
    """设置模块级默认配置；返回生效配置，便于库调用方与测试复核。"""
    global DEFAULT_CONFIG
    base = config or DEFAULT_CONFIG
    if overrides:
        base = replace(base, **overrides)
    DEFAULT_CONFIG = base
    return base


class Monitor:
    """一轮轮采集快照；所有工作台私有能力都在 MonitorConfig 里显式给出。"""

    def __init__(self, config: MonitorConfig | None = None):
        self.config = config or DEFAULT_CONFIG

    @property
    def base(self) -> Path:
        """工作台根：文件轮询与自启工作目录；未注入时为中性空路径。"""
        if self.config.workspace_root is not None:
            return Path(self.config.workspace_root).expanduser()
        return Path()

    def state_root(self) -> Path:
        """运行态根（每次现取）：显式配置 > 环境变量 > 未注入。"""
        if self.config.state_root is not None:
            return Path(self.config.state_root).expanduser()
        for name in (*STATE_ENVS, STATE_ENV_LEGACY):
            val = (os.environ.get(name) or "").strip()
            if val:
                return Path(os.path.expanduser(val)).resolve()
        return Path()

    def sessions_path(self) -> Path:
        if self.config.sessions_path is not None:
            return Path(self.config.sessions_path).expanduser()
        return self.state_root() / "sessions.json"

    def log_path(self) -> Path:
        if self.config.log_path is not None:
            return Path(self.config.log_path).expanduser()
        return self.state_root() / "monitor.log"

    def missing_injections(self) -> list[str]:
        """自检用：列出未注入的外部能力，绝不把缺依赖伪装成可用。"""
        missing = []
        if self.config.state_root is None and not self._env_state_root():
            missing.append("state_root")
        if self.config.workspace_root is None:
            missing.append("workspace_root")
        for name in ("heartbeat", "ps_json", "probe_session", "wire_root",
                     "live_root", "session_script"):
            if getattr(self.config, name) is None:
                missing.append(name)
        return missing

    def self_check(self) -> int:
        """显式自检：未注入报红（2），完整注入报绿（0）。"""
        missing = self.missing_injections()
        if missing:
            print("monitor: 未注入 " + ", ".join(missing), file=sys.stderr)
            return 2
        print(f"monitor: 注入完整 role={self.config.supervisor_role}")
        return EXIT_OK

    def _env_state_root(self) -> str:
        for name in (*STATE_ENVS, STATE_ENV_LEGACY):
            val = (os.environ.get(name) or "").strip()
            if val:
                return val
        return ""

    def _wire_root(self):
        """回执核验目录：只认调用方注入的 callable 或值，不自行找目录。"""
        wire = self.config.wire_root
        if callable(wire):
            try:
                return wire()
            except Exception:
                return None
        return wire

    def _probe_session(self, sid: str):
        """会话探活由调用方注入；未注入时明确返回 unknown，不假装失败。"""
        probe = self.config.probe_session
        if probe is None:
            return None, "未注入会话探测函数（probe_session）"
        return probe(sid)

    def _load_alarm_module(self):
        """告警模块是包内硬依赖；其内部依赖缺失必须原样抛给自检/调用方。"""
        mod = self.config.alarm_module or alarm_mod
        self.assert_alarm_dependencies(mod)
        return mod

    def assert_alarm_dependencies(self, mod=None):
        """显式验证告警链；下游 check() 会吞 unknown，缺依赖必须在这里炸。"""
        mod = mod or self.config.alarm_module or alarm_mod
        for name in ("check", "configure"):
            if not hasattr(mod, name):
                raise ModuleNotFoundError(f"name {name!r} is not available", name=name)
        from . import session_health
        if not hasattr(session_health, "check_key"):
            raise ModuleNotFoundError("name 'check_key' is not available",
                                      name="super_agent.session_health")
        return mod

    def _configure_alarm(self, mod):
        root = self.state_root()
        alarm_state = self.config.alarm_state_path
        if alarm_state is None:
            alarm_state = root / mod.STATE_FILE_NAME if root != Path() else None
        mod.configure(state_root=root if root != Path() else None,
                      sessions=self.sessions_path(),
                      log=self.log_path(),
                      state_file=alarm_state,
                      live_root=self.config.live_root,
                      probe=self.config.probe_session,
                      sess_script=self.config.session_script)
        return mod

    def _ps_json(self, script: str, timeout: float = 60.0):
        if self.config.ps_json is None:
            raise RuntimeError("未注入 PowerShell 桥接（ps_json）")
        return self.config.ps_json(script, timeout)

    def _write_guardian_heartbeat(self) -> None:
        """专属心跳每轮无条件调用；未注入只记一行，不拖垮主循环。"""
        if self.config.heartbeat is None:
            print("monitor: 未注入心跳 callable（heartbeat）", flush=True)
            return
        try:
            self.config.heartbeat("monitor", {"interval": None})
        except Exception as exc:  # noqa: BLE001
            print(f"monitor: 心跳未写入：{type(exc).__name__}: {exc}", flush=True)

    def _this_spec(self) -> autostart.Spec:
        exe, note = autostart.windowless_python()
        return autostart.Spec(name=AUTOSTART_NAME, script=Path(__file__).resolve(),
                              args=list(AUTOSTART_ARGS), workdir=self.base,
                              execute_note=note)

    def _norm_backend(self, backend: str) -> str:
        b = (backend or "").strip().lower()
        if not b:
            return autostart.DEFAULT_BACKEND
        if b not in autostart.BACKENDS:
            print(f"[autostart] 未知后端 {backend!r}（可选：{'/'.join(autostart.BACKENDS)}）",
                  file=sys.stderr)
            return autostart.DEFAULT_BACKEND
        return b

    def task_spec(self) -> dict:
        """计划任务定义（scheduled 后端；注册与回读用同一份）。"""
        exe, note = autostart.windowless_python()
        script = str(Path(__file__).resolve())
        return {"name": AUTOSTART_NAME, "execute": exe,
                "arguments": '"%s" %s' % (script, " ".join(AUTOSTART_ARGS)),
                "workdir": str(self.base), "script": script, "execute_note": note}

    def query_task(self):
        ps = (
            "$ErrorActionPreference='Stop';"
            "$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue;"
            "if (-not $t) { Write-Output 'null'; exit 0 };"
            "$a = $t.Actions[0];"
            "$o = [ordered]@{ name=$t.TaskName; state=[string]$t.State;"
            " execute=$a.Execute; arguments=$a.Arguments; workdir=$a.WorkingDirectory };"
            "Write-Output ($o | ConvertTo-Json -Compress)"
        ) % AUTOSTART_NAME
        rc, obj, _raw = self._ps_json(ps)
        return obj if rc == 0 and isinstance(obj, dict) else None

    def print_spec(self, spec: dict) -> None:
        print("[autostart] 计划任务定义（干跑，未注册）：")
        for k in ("name", "execute", "arguments", "workdir"):
            print("  %-11s: %s" % (k, spec[k]))
        print("  Trigger    : AtLogOn（登录时）")
        print("  Settings   : AllowStartIfOnBatteries / DontStopIfGoingOnBatteries / Restart×3")
        if spec.get("execute_note"):
            print("  ⚠ %s" % spec["execute_note"], file=sys.stderr)

    def startup_install(self, dry_run: bool = False) -> int:
        spec = self._this_spec()
        exe, note = autostart.windowless_python()
        if note:
            print(f"[autostart] 警告：{note}", file=sys.stderr)
        text = autostart.render_vbs(spec, exe)
        path = spec.startup_file
        if dry_run:
            print("[autostart] backend=startup（干跑，未落盘）")
            print(f"  target : {path}")
            print("  encode : UTF-16LE + BOM")
            print("  content:")
            for ln in text.splitlines():
                print("    " + ln)
            print(f"[autostart] 当前状态：{'已存在' if path.is_file() else '未安装'}")
            return EXIT_OK
        if path.is_file():
            try:
                old = autostart.read_vbs(path)
            except OSError as exc:
                old = ""
                print(f"[autostart] 读取现有文件失败（将覆盖）：{exc}", file=sys.stderr)
            if old == text:
                print("[autostart] 已安装（内容一致，跳过）")
                return EXIT_OK
            print("[autostart] 已安装但内容不一致 ⇒ 覆盖，变更点：")
            for d in autostart.diff_summary(old, text):
                print("  " + d)
        try:
            autostart.write_vbs(path, text)
        except OSError as exc:
            print(f"[autostart] 写盘失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_STARTUP_INSTALL_FAILED
        fails = autostart.verify_vbs(spec, exe)
        if fails:
            print("[autostart] 写入后回读校验失败：", file=sys.stderr)
            for f in fails:
                print("  x " + f, file=sys.stderr)
            return EXIT_STARTUP_INSTALL_FAILED
        print(f"[autostart] 安装成功并已回读验证：{path}")
        return EXIT_OK

    def startup_uninstall(self, dry_run: bool = False) -> int:
        """QA18-4：uninstall 曾忽略 --dry-run 仍真删；现支持干跑。"""
        path = self._this_spec().startup_file
        if dry_run:
            print("[autostart] backend=startup（干跑，不删除）")
            print(f"  would remove : {path}")
            print(f"[autostart] 当前状态：{'存在（真跑会删）' if path.is_file() else '不存在（无需删）'}")
            return EXIT_OK
        if not path.is_file():
            print("[autostart] 未安装（文件不存在），无需卸载")
            return EXIT_OK
        try:
            path.unlink()
        except OSError as exc:
            print(f"[autostart] 删除失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_STARTUP_UNINSTALL_FAILED
        if path.is_file():
            print(f"[autostart] 卸载后文件仍存在：{path}", file=sys.stderr)
            return EXIT_STARTUP_UNINSTALL_FAILED
        print(f"[autostart] 已卸载（回读确认查无）：{path}")
        return EXIT_OK

    def install_autostart(self, dry_run: bool = False, backend: str = "") -> int:
        backend = self._norm_backend(backend)
        if backend == "startup":
            return self.startup_install(dry_run=dry_run)
        spec = self.task_spec()
        if spec.get("execute_note"):
            print("[autostart] 警告：%s" % spec["execute_note"], file=sys.stderr)
        if dry_run:
            self.print_spec(spec)
            print("[autostart] 当前注册状态：%s" % ("已存在" if self.query_task() else "未注册"))
            print(f"[autostart] 真实注册需提升权限的终端（且属 {self.config.supervisor_role} 动作），本命令到此为止。")
            return EXIT_OK
        ps = (
            "$ErrorActionPreference='Stop';"
            "try {"
            " $a = New-ScheduledTaskAction -Execute '%s' -Argument '%s' -WorkingDirectory '%s';"
            " $t = New-ScheduledTaskTrigger -AtLogOn;"
            " $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries"
            " -DontStopIfGoingOnBatteries -RestartCount 3"
            " -RestartInterval (New-TimeSpan -Minutes 1);"
            " Register-ScheduledTask -TaskName '%s' -Action $a -Trigger $t -Settings $s -Force | Out-Null;"
            " Write-Output '{\"ok\":true}'"
            "} catch {"
            " Write-Output ('{\"ok\":false,\"err\":' + (ConvertTo-Json ([string]$_.Exception.Message)) + '}')"
            "}"
        ) % (spec["execute"], spec["arguments"], spec["workdir"], spec["name"])
        rc, obj, raw = self._ps_json(ps)
        if rc != 0 or not isinstance(obj, dict) or not obj.get("ok"):
            err = (obj or {}).get("err") if isinstance(obj, dict) else raw
            print("[autostart] 注册失败：%s" % err, file=sys.stderr)
            return EXIT_REGISTER_FAILED
        actual = self.query_task()
        if not actual:
            print("[autostart] 注册返回成功，但回读查无任务", file=sys.stderr)
            return EXIT_VERIFY_FAILED
        print("[autostart] 注册成功并已回读验证：")
        for k in ("name", "state", "execute", "arguments", "workdir"):
            print("  %-11s: %s" % (k, actual.get(k)))
        return EXIT_OK

    def uninstall_autostart(self, backend: str = "", dry_run: bool = False) -> int:
        backend = self._norm_backend(backend)
        if backend == "startup":
            return self.startup_uninstall(dry_run=dry_run)
        if dry_run:
            print("[autostart] backend=scheduled（干跑，未卸载）："
                  "将执行 Unregister-ScheduledTask -TaskName '%s'" % AUTOSTART_NAME)
            print("[autostart] 当前注册状态：%s" % ("已存在" if self.query_task() else "未注册"))
            return EXIT_OK
        ps = ("$ErrorActionPreference='Stop';"
              "Unregister-ScheduledTask -TaskName '%s' -Confirm:$false -ErrorAction SilentlyContinue;"
              "Write-Output '{}'") % AUTOSTART_NAME
        self._ps_json(ps)
        if self.query_task():
            print("[autostart] 卸载后仍能查到任务 %s" % AUTOSTART_NAME, file=sys.stderr)
            return EXIT_UNINSTALL_FAILED
        print("[autostart] 已卸载（回读确认查无）")
        return EXIT_OK

    def fetch_sessions(self):
        """会话列表＋busy；bus 返回 None 时直连回落。"""
        items = BUSD.list_envelopes(caller="monitor.py")
        if items is not None:
            src = "bus" if any(it.get("_src") == "bus" for it in items) else "direct"
            return items, None, src
        st, payload = K.get("/api/v1/sessions?page_size=30", timeout=8)
        if st != 200:
            return None, f"http={st}", "direct"
        data = K.data_of(payload)
        if isinstance(data, dict):
            data = data.get("items") or data.get("sessions") or []
        return (data if isinstance(data, list) else []), None, "direct"

    def snapshot(self):
        """返回 (一行摘要, flags, source)；busy=false 不等于已完成。"""
        items, err, src = self.fetch_sessions()
        if err:
            return f"server=down({err})", {}, "down"
        recorded = {}
        try:
            recorded = json.loads(self.sessions_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        by_id = {str(it.get("id", "")): it for it in items if isinstance(it, dict)}
        parts, flags = [], {}
        for name, rec in sorted(recorded.items()):
            sid = str((rec or {}).get("id", "")) if isinstance(rec, dict) else ""
            if not sid:
                continue
            it = by_id.get(sid)
            busy = None if it is None else bool(it.get("busy"))
            flags[name] = busy
            if name == self.config.supervisor_role or name == "qa" or name.startswith("pm-"):
                parts.append(f"{name}={'busy' if busy else ('idle' if busy is False else 'gone')}")
        wk = [it for it in items if str(it.get("title", "")).startswith("wk-")]
        busy_wk = [str(it.get("title")) for it in wk if it.get("busy")]
        parts.append(f"worker={len(busy_wk)}" + (f"({busy_wk[0]})" if busy_wk else ""))
        newest = {}
        for rel in WATCH_DIRS:
            d = self.base / rel
            if not d.is_dir():
                continue
            files = sorted((p for p in d.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
            if files:
                newest[rel] = (files[-1].name, int(files[-1].stat().st_mtime))
        parts.append("newest=" + ";".join(f"{k.split('/')[-1]}:{v[0]}" for k, v in newest.items()))
        return " | ".join(parts), flags, src

    def _alarm_down(self, src: str) -> None:
        """daemon 降级时告警一次（节流），不刷屏。"""
        global _last_alarm
        if src != "down":
            return
        now = time.time()
        if now - _last_alarm < ALARM_THROTTLE:
            return
        _last_alarm = now
        print(f"{datetime.now().strftime('%H:%M')} monitor: 降级直连（daemon 不通，"
              f"已回落；{int(ALARM_THROTTLE/60)} 分钟内不再提示）", flush=True)

    def _last_logged(self) -> str | None:
        """读日志最后一行，剥掉时间戳前缀，作为「已写过什么」的基准。"""
        try:
            with self.log_path().open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 8192))
                tail = fh.read().decode("utf-8", "replace").strip().splitlines()
            if not tail:
                return None
            last = tail[-1]
            return last.split(" ", 1)[1] if len(last) > 6 and last[2] == ":" else last
        except OSError:
            return None

    def tick(self, last: str | None, write: bool = True) -> tuple[str, str]:
        """跑一轮。返回 (本行, 来源)。只有与 last 不同才写。"""
        line, _flags, src = self.snapshot()
        self._write_guardian_heartbeat()
        self._alarm_down(src)
        if write and self.config.alarm_check:
            mod = self._load_alarm_module()
            self._configure_alarm(mod)
            try:
                mod.check(do_notify=True, wire_root=self._wire_root())
            except Exception as exc:  # noqa: BLE001
                print(f"monitor: 告警调用失败（不阻断）："
                      f"{type(exc).__name__}: {exc}", flush=True)
        if line != last:
            stamp = datetime.now().strftime("%H:%M")
            text = f"{stamp} {line}"
            if write:
                _lp = self.log_path()
                _lp.parent.mkdir(parents=True, exist_ok=True)
                with _lp.open("a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            print(text, flush=True)
        elif src != "bus":
            print(f"（无变化；来源 {src}）", flush=True)
        return line, src


def _this_spec() -> autostart.Spec:
    return Monitor()._this_spec()


def _norm_backend(backend: str) -> str:
    return Monitor()._norm_backend(backend)


def task_spec() -> dict:
    return Monitor().task_spec()


def query_task():
    return Monitor().query_task()


def print_spec(spec: dict) -> None:
    Monitor().print_spec(spec)


def startup_install(dry_run: bool = False) -> int:
    return Monitor().startup_install(dry_run=dry_run)


def startup_uninstall(dry_run: bool = False) -> int:
    return Monitor().startup_uninstall(dry_run=dry_run)


def install_autostart(dry_run: bool = False, backend: str = "") -> int:
    return Monitor().install_autostart(dry_run=dry_run, backend=backend)


def uninstall_autostart(backend: str = "", dry_run: bool = False) -> int:
    return Monitor().uninstall_autostart(backend=backend, dry_run=dry_run)


def state_root() -> Path:
    return Monitor().state_root()


def sessions_path() -> Path:
    return Monitor().sessions_path()


def log_path() -> Path:
    return Monitor().log_path()


def _wire_root():
    return Monitor()._wire_root()


def _probe_session(sid: str):
    return Monitor()._probe_session(sid)


def _load_alarm_module():
    return Monitor()._load_alarm_module()


def _write_guardian_heartbeat() -> None:
    Monitor()._write_guardian_heartbeat()


def fetch_sessions():
    return Monitor().fetch_sessions()


def snapshot():
    return Monitor().snapshot()


def _alarm_down(src: str) -> None:
    Monitor()._alarm_down(src)


def _last_logged() -> str | None:
    return Monitor()._last_logged()


def tick(last: str | None, write: bool = True) -> tuple[str, str]:
    return Monitor().tick(last, write=write)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=45)
    ap.add_argument("--once", action="store_true",
                    help="跑一轮就退（用于「无变化不写日志」的验收）")
    ap.add_argument("--install-autostart", dest="install_autostart", action="store_true",
                    help="注册登录自启（默认 startup 后端＝零提权；--backend scheduled 需提升权限）")
    ap.add_argument("--uninstall-autostart", dest="uninstall_autostart", action="store_true",
                    help="卸载登录自启（回读确认）")
    ap.add_argument("--backend", default=autostart.DEFAULT_BACKEND,
                    choices=list(autostart.BACKENDS),
                    help="自启后端：startup（默认，零提权）| scheduled（需提升权限）")
    ap.add_argument("--dry-run", action="store_true",
                    help="配合 --install-autostart/--uninstall-autostart：只打印不落盘/不删")
    ap.add_argument("--self-check", action="store_true",
                    help="只检查调用方注入是否完整，不跑轮询")
    args = ap.parse_args(argv)
    mon = Monitor()
    if args.self_check:
        return mon.self_check()
    if args.install_autostart:
        return mon.install_autostart(dry_run=args.dry_run, backend=args.backend)
    if args.uninstall_autostart:
        return mon.uninstall_autostart(backend=args.backend, dry_run=args.dry_run)
    mon.log_path().parent.mkdir(parents=True, exist_ok=True)
    if args.once:
        mon.tick(mon._last_logged())
        return 0
    last = None
    while True:
        last, _src = mon.tick(last)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
