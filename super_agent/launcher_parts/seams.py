"""The injections the launcher must supply to the consumer legs (ISS-132 C2/F).

``decide_watch`` has no default for any of these - it refuses to guess a path, a
transport or a shell - and the workbench used to inject them from outside this
repo. The launcher is a new host, so it needs its own implementation of each, and
putting them in one module keeps "what the host provides" reviewable in one place.

Four of them carry a rule that is easy to undo:

**``ps_json`` never raises.** Its only caller reads back a scheduled task; a
missing PowerShell is "cannot verify", not "the consumer leg dies".

**``windowless_python`` tells the truth when it falls back.** A registered task
that opens a console window is a defect people notice at login, so the note is
returned with the path instead of being dropped.

**``heartbeat`` binds the state root explicitly.** ``wss_heartbeat`` refuses to
guess one on purpose (an isolated instance once wrote production), so the
launcher passes the root it owns on every beat rather than at import time.

**``send_transport`` is the package's own console client** - the same call
``decide_watch`` made in the workbench. It is not a fixture and not a stub: the
fixture gates decide whether a send may leave the machine, not this function.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

#: Default long-poll wait, matching ``decide_watch.main``'s ``--wait``. Lives here
#: because :func:`config` is where it is applied; ``c2`` re-exports the name.
DEFAULT_WAIT_S = 20.0


def powershell_exe() -> str:
    import shutil

    return shutil.which("powershell") or "powershell.exe"


def ps_json(script: str, timeout: float = 60.0):
    """Run a PowerShell snippet, parse stdout as JSON: ``(rc, obj, raw)``."""
    try:
        proc = subprocess.run(
            [powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, {}, f"{type(exc).__name__}: {exc}"
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return proc.returncode, {}, (proc.stderr or raw).strip()
    try:
        return 0, json.loads(raw or "null"), raw
    except ValueError:
        return 1, {}, raw


def windowless_python() -> tuple[str, str]:
    """``pythonw.exe`` beside this interpreter, plus a note when it is missing."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    if candidate.is_file():
        return str(candidate), ""
    return str(exe), "pythonw.exe 不在解释器旁边 ⇒ 注册的任务会带控制台窗口"


def probe_session(sid: str):
    """``(info, error)`` - an unreachable console is "unknown", not a crash."""
    from .. import api

    try:
        return api.session_info(sid), ""
    except Exception as exc:            # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"


def send_transport(sid: str, text: str, timeout: float = 60.0):
    from .. import direct

    return direct.send(sid, text)


def boot_id() -> str:
    """The daemon's boot id, or ``""`` when it is down (which is not an error)."""
    try:
        from .. import wss_bus

        return str((wss_bus.bus_health() or {}).get("boot_id") or "")
    except Exception:                   # noqa: BLE001
        return ""


def thresholds():
    from .. import wss_bus

    return wss_bus.guardian_thresholds()


def patrol_module(bus, *, dw=None):
    """The module whose guardian registry the patrol reads (#138 P0-1).

    Production's only launcher entry calls ``build_runner`` **without** ``bus``
    (``launcher.py``), so the leg is built with ``bus=None`` there. Asking that
    ``None`` for ``register_guardian`` skipped the repair silently, and
    ``/healthz.guardians`` never listed the missing heartbeat - which is the one
    half of the ruling that makes "warn and continue" acceptable.

    ``c2.config()`` already falls back to ``wss_bus`` when ``bus is None``, but the
    fallback only reached ``event_source``. Resolving it here keeps one answer for
    "which module owns the registry" instead of a second copy in the leg.
    """
    if bus is not None:
        return bus
    if dw is None:
        from .. import decide_watch as dw
    source = getattr(getattr(dw, "_cfg")(), "event_source", None)
    if source is not None:
        return source
    from .. import wss_bus

    return wss_bus


def heartbeat(store_root: Path):
    """A heartbeat writer bound to **this** state root on every beat."""
    from .. import wss_heartbeat

    def write(name: str, extra: dict | None = None) -> None:
        try:
            wss_heartbeat.bind(thresholds=thresholds,
                               state_root=lambda: Path(store_root),
                               boot_id=boot_id, log=lambda _msg: None)
            wss_heartbeat.write_heartbeat(name, extra)
        except Exception:               # noqa: BLE001  a beat never kills a round
            pass

    return write


def session_files(cfg: dict) -> tuple[dict, str]:
    """The three session-side files the C2 leg **reads**, plus why when it fell back.

    Returns ``(files, problem)``. ``problem`` is ``""`` when the session root
    resolved normally; otherwise it names the reason and ``files`` is empty.

    **Never raises** (ISS-139 P0-1). ``resolve_session_root`` raises
    ``SessionStreamNotConfigured`` when nothing declares a session stream, and
    that is a *configuration* answer, not a crash: ``c2.config()`` used to be
    pure assembly, and turning it into a call that can raise took the whole
    launcher down at build time (``build_runner`` -> ``register_c2`` ->
    ``host.py:62``), measured as two red tests in ``test_launcher_wss.py``. The
    caller decides what to do with the answer -- silence is not one of the
    options: ``register_c2`` writes the reason onto the health row.

    Why the root comes from :func:`session_events.resolve_session_root` and not
    from ``cfg["state_dir"]`` (ruling ``7207170455`` §3.1): that is the **same**
    function ``issue_cli`` and ``ctx_gate`` write through. Composing the path
    here would be a second resolution chain, and the symptom of a second chain
    is silence - the leg scans a file nobody writes and reports ``pending=0``
    forever. ``paths.state_dir`` and ``paths.engine_state_dir`` are deliberately
    allowed to differ (a test pins that), so "the engine root" is **not** the
    answer, and neither is ``state_dir`` on its own.

    **What goes where** - the two roots are not interchangeable:

    ``session_root`` (these files), written by the CLIs, read by the leg
        ``ledger.jsonl``          delivery rows to scan
        ``sessions.json``         ``sid`` per key, for ``resolve_recipients``
        ``issue_sessions.json``   worker name per thread, for ``_bind_name``

    ``state_root`` (``store.root``), the leg's **own** artifacts, never the CLIs'
        ``delivery_watch_state.json`` (scan offset), ``delivery_watch.log``,
        ``delivery_undelivered.log``, ``delivery_watch_stats.jsonl``,
        ``worker_liveness_state.json``, ``decide_watch.lock``, ``heartbeat/``.

    Reading those three from ``state_root`` is what broke #139: the leg found an
    empty ``ledger.jsonl`` that had never been written, and resolved no PM at all.
    """
    from .. import session_events

    try:
        root = session_events.resolve_session_root(cfg)
    except session_events.SessionStreamNotConfigured as exc:
        return {}, str(exc)
    return {
        "ledger": root / "ledger.jsonl",
        "sessions": root / "sessions.json",
        "bind": root / "issue_sessions.json",
    }, ""


def session_files_or_engine_root(cfg: dict, state_root: Path) -> dict:
    """The three session paths, degraded to ``state_root`` when nothing declares one.

    This is the fallback half of :func:`session_files`: it keeps ``c2.config()``
    from raising (ISS-139 P0-1) without hiding the fact, which travels through
    :func:`session_problem` onto the health row. ``state_root`` is the leg's own
    artifact root, so the degraded leg scans an empty ledger and resolves no PM -
    the pre-#139 behaviour, now reported instead of silent.
    """
    files, _problem = session_files(cfg)
    if files:
        return files
    return {"ledger": state_root / "ledger.jsonl",
            "sessions": state_root / "sessions.json",
            "bind": state_root / "issue_sessions.json"}


def session_problem(cfg: dict) -> str:
    """Why the three session paths degraded; ``""`` when they resolved normally.

    One question, one function: :func:`session_files` answers it, but
    ``c2.config()`` must not raise and cannot grow a fourth return value (every
    caller unpacks the triple). ``register_c2`` asks this and puts the answer on
    the health row - the same shape as ``heartbeat_problem`` (#138), where the
    leg stays healthy and the gap is still visible.
    """
    try:
        _files, problem = session_files(cfg)
    except Exception as exc:          # noqa: BLE001  never let the probe raise
        return f"{type(exc).__name__}: {exc}"
    return problem


def workspace_root(cfg: dict) -> Path:
    return Path(str(cfg.get("workspace_root") or os.getcwd())).resolve()


def config(cfg: dict, store, *, bus=None, port: int = 0,
           wait: float | None = None):
    """The complete ``DecideWatchConfig`` for this launcher. No guesswork.

    Assembled here rather than in ``c2`` (#138 rework): every injection this
    function supplies is a host seam, and ``c2`` is the consumer leg itself.

    Every injectable is supplied, because ``decide_watch.missing_injections()``
    refuses to start on a partial set and a launcher that half-wires the leg would
    register a component that can only fail - a permanent red row in every health
    summary. Returning the config keeps this testable without module state.

    The three session-side paths are passed **explicitly** (#139): omitting them
    let ``decide_watch._path()`` fall back to ``state_root``, which holds no
    session files. **Assembly never raises** (P0-1): an undeclared session root
    degrades through :func:`seams.session_files_or_engine_root`, and the reason
    reaches the health row via ``register_c2``.
    """
    from .. import decide_watch as dw

    if bus is None:
        from .. import wss_bus as bus

    state = Path(store.root)
    paths = session_files_or_engine_root(cfg, state)
    workspace = workspace_root(cfg)
    engine_cfg = cfg.get("engine") or {}
    resolved_port = int(port or getattr(bus, "PORT", 58800))
    waits = float(wait if wait is not None
                  else (engine_cfg.get("decide_watch_wait_s") or DEFAULT_WAIT_S))

    def inject(path: str, body: dict, port_=0):
        return bus.bus_post(path, body, port=port_ or resolved_port)

    return dw.DecideWatchConfig(
        state_root=state,
        sessions_path=paths["sessions"],
        bind_path=paths["bind"],
        ledger_path=paths["ledger"],
        workspace_root=workspace,
        event_source=bus,
        heartbeat=heartbeat(state),
        inject_event=inject,
        ps_json=ps_json,
        windowless_python=windowless_python,
        probe_session=probe_session,
        send_transport=send_transport,
        session_script=workspace / "tools" / "session_client.py",
        liveness_state_path=state / "worker_liveness_state.json",
        lock_path=state / "decide_watch.lock",
    ), waits, resolved_port
