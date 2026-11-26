"""The C2 consumption leg, with every protection ``decide_watch._loop`` has.

Why this is a module of its own (ISS-132 QA P0-1/P0-2/P0-3): the first delivery
copied the *consumption order* out of ``_loop`` and dropped the loop's **guards**.
That is the failure mode this file exists to make impossible to repeat, because
each dropped guard turns a visible fault into a silent one:

* **no supervisor probe** -> a dead supervisor key is never reported. ``_loop``
  runs ``check_supervisor_health`` at the top of every round precisely because
  its two-channel alert (log + ``event.session.supervisor_key_dead``) is the only
  notice anyone gets that notices are being sent into a void;
* **no process heartbeat** -> ``wss_bus.GUARDIAN_THRESHOLDS`` lists
  ``decide_watch`` as a built-in guarded party, so a running leg that never beats
  is reported ``missing`` forever, and the watchdog is trained to ignore it;
* **no liveness interval** -> the patrol runs once per round. A round still
  covers a 20 s long poll, so the patrol fires several times more often than
  ``worker_liveness_interval_s`` asks for, and the ``supervisor`` override stops
  reaching it - which is the one parameter that makes a rescue possible.

The two P1s are the same shape: eviction now **rescans and says so**, and an
unreachable daemon **backs off** instead of raising (the runner would turn the
raise into a health row, which reads as a fault of the leg rather than of the
daemon).

State that must survive between rounds (``liveness_next``, ``backoff``,
``since``) lives on :class:`C2Leg`, one instance per launcher. A module-level
dict would be shared by two launchers in one process - which a test can do.
"""
from __future__ import annotations

import time
from pathlib import Path

#: Default long-poll wait, matching ``decide_watch.main``'s ``--wait``.
DEFAULT_WAIT_S = 20.0


def config(cfg: dict, store, *, bus=None, port: int = 0,
           wait: float | None = None):
    """The complete ``DecideWatchConfig`` for this launcher. No guesswork.

    Every injectable is supplied, because ``decide_watch.missing_injections()``
    refuses to start on a partial set and a launcher that half-wires the leg would
    register a component that can only fail - a permanent red row in every health
    summary. Returning the config keeps this testable without module state.
    """
    from .. import decide_watch as dw
    from . import seams

    if bus is None:
        from .. import wss_bus as bus

    state = Path(store.root)
    workspace = seams.workspace_root(cfg)
    engine_cfg = cfg.get("engine") or {}
    resolved_port = int(port or getattr(bus, "PORT", 58800))
    waits = float(wait if wait is not None
                  else (engine_cfg.get("decide_watch_wait_s") or DEFAULT_WAIT_S))

    def inject(path: str, body: dict, port_=0):
        return bus.bus_post(path, body, port=port_ or resolved_port)

    return dw.DecideWatchConfig(
        state_root=state,
        workspace_root=workspace,
        event_source=bus,
        heartbeat=seams.heartbeat(state),
        inject_event=inject,
        ps_json=seams.ps_json,
        windowless_python=seams.windowless_python,
        probe_session=seams.probe_session,
        send_transport=seams.send_transport,
        session_script=workspace / "tools" / "session_client.py",
        liveness_state_path=state / "worker_liveness_state.json",
        lock_path=state / "decide_watch.lock",
    ), waits, resolved_port


class C2Leg:
    """One consumption round per call, carrying the loop's guards and its state.

    ``__call__`` is the thread body the runner schedules, so the same object can
    be registered directly (``rn.register("decide_watch", leg, ...)``).

    The guards run **before** anything can fail: a round whose daemon is down must
    still have probed the supervisor key and beaten, or the two facts that matter
    most during an outage - is the supervisor alive, is this leg alive - are the
    two facts nobody learns.
    """

    def __init__(self, cfg: dict, store, *, bus=None, port: int = 0,
                 dry: bool = False, wait: float | None = None,
                 supervisor: str | None = None,
                 decide_watch_config=None, wait_override: float | None = None,
                 port_override: int | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.port = int(port)
        self.dry = bool(dry)
        self.wait_override = wait
        # A caller that already assembled the boundary (a test rig, or a host
        # that wants its own injections) passes it here. Without this seam the
        # leg would re-derive its config on every round and silently discard
        # whatever the caller configured - which is what made the first
        # delivery's guards untestable.
        self._config = decide_watch_config
        self._waits = wait_override
        self._resolved_port = port_override
        # ``None`` means "resolve the way decide_watch does" (config chain).
        # An explicit value is the ``--supervisor`` rescue parameter.
        self.supervisor = "" if supervisor is None else str(supervisor)
        self.liveness_next = 0.0          # first round patrols immediately
        self.backoff = 1.0
        self._prepared = False

    # ------------------------------------------------------------------ setup
    def prepare(self) -> tuple[float, int]:
        """Configure the module and return ``(wait, port)``. Idempotent."""
        from .. import decide_watch as dw

        if self._config is not None:
            config_obj = self._config
            waits = float(self._waits if self._waits is not None else DEFAULT_WAIT_S)
            resolved_port = int(self._resolved_port or self.port
                                or getattr(self.bus, "PORT", 58800))
        else:
            config_obj, waits, resolved_port = config(
                self.cfg, self.store, bus=self.bus, port=self.port,
                wait=self.wait_override)
            self._resolved_port = resolved_port
        dw.configure(config_obj)
        refused = dw.gate_check() or dw.notice_registration_problem()
        if refused:
            raise RuntimeError(f"decide_watch refused to run: {refused}")
        self._prepared = True
        return waits, resolved_port

    # ------------------------------------------------------------------- round
    def __call__(self) -> dict:
        from .. import decide_watch as dw

        waits, resolved_port = self.prepare()

        # 1) supervisor key probe, at the **top** of the round. ``_loop`` moved it
        #    here for issue #16 P1-3: when the daemon is unreachable the code used
        #    to ``continue`` before the probe, so the one moment the supervisor's
        #    liveness mattered most was the moment nobody checked.
        dw.check_supervisor_health(resolved_port)
        # 2) this leg's own heartbeat. ``decide_watch`` is a built-in guardian;
        #    a running leg that never beats is reported ``missing`` for as long as
        #    it runs, which teaches the watchdog to ignore the field.
        dw._write_hb("decide_watch", {"loop": True})

        try:
            dw.delivery_tick(dry=self.dry)
        except Exception as exc:      # noqa: BLE001  a broken send must not stop the round
            dw.delivery_log(f"delivery_tick 异常（不阻断主循环）：{type(exc).__name__}: {exc}")

        cursor = dw.load_cursor()
        since = int(cursor.get("seq") or 0)
        boot = ""
        health = dw.health(resolved_port)
        if health:
            boot = str(health.get("boot_id") or "")
            if since <= 0:
                since = int(health.get("seq") or 0)
                dw.save_cursor(since, boot)
            elif cursor.get("boot_id") and boot and cursor["boot_id"] != boot:
                # A daemon restart renumbers the ring; rescan from the start.
                since = 0

        data = dw.fetch_events(since, waits, resolved_port)
        if data is None:
            # 3) back off instead of raising. Raising once per jitter would leave
            #    a ``last_error`` row on ``/healthz`` that blames this leg for the
            #    daemon being down, and would skip the retry the next round owes.
            slept = self.backoff
            self.backoff = min(self.backoff * 2, dw.BACKOFF_MAX)
            dw.log(f"daemon 不可达，{slept:.1f}s 后重试")
            return {"events": 0, "since": since, "unreachable": True,
                    "backoff_s": slept}
        self.backoff = dw.BACKOFF_MIN

        delta = dw.notice_cursor_rollback(since, int(data.get("seq") or 0))
        if delta is not None:
            return {"events": 0, "since": 0, "rescan": "cursor_ahead"}

        oldest = int(data.get("oldest_seq") or 0)
        if since and oldest and since + 1 < oldest:
            # 4) the ring evicted rows this cursor never read. The fetch below
            #    would still return what is left, so nothing is *lost* today - but
            #    the rescan is what makes the gap auditable, and it is what keeps
            #    the leg correct if ``/events`` ever gains another lower bound.
            dw.log(f"游标 seq={since} 早于环形缓冲起点 {oldest} ⇒ 有事件被淘汰，全量重扫一次")
            return {"events": 0, "since": 0, "rescan": "evicted"}

        dropped = data.get("dropped") or []
        if dropped:
            dw.log(f"daemon 报了丢失区间（{dropped[-1]}）⇒ 已越过，继续")

        events = data.get("events") or []
        dw.handle(events, dw.supervisor_sid(self.supervisor), self.dry, dw.MAX_AGE_S)
        attempted: set = set()
        dw.handle_supervisor_notices(events, dry=self.dry, port=resolved_port,
                                     attempted=attempted)
        dw._retry_pending(dry=self.dry, port=resolved_port, attempted=attempted)

        # 5) the liveness patrol keeps its own clock. A round is a long poll, not
        #    a fixed interval, so counting rounds would run the patrol several
        #    times per configured interval - and each patrol reads the ledger.
        now = time.time()
        if self.liveness_next <= now:
            dw.worker_liveness_tick(dry=self.dry, port=resolved_port,
                                    supervisor=self.supervisor)
            self.liveness_next = now + dw._liveness_settings()["interval_s"]

        nxt = int(data.get("next_seq") or (since + 1))
        if events:
            since = max(int(ev.get("seq") or since) for ev in events)
        else:
            since = max(since, nxt - 1)
        dw.save_cursor(since, str(data.get("boot_id") or boot))
        return {"events": len(events), "since": since}
