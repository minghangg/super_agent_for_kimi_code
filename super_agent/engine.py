"""Deterministic fast path. No LLM, no network side effects in observe mode.

One round does: read allow-listed events -> dedupe against persisted cursors ->
triage with fixed rules -> update the local index -> *maybe* dispatch one
action. Dispatch is the single guarded exit: everything it can do is counted
in ``external_actions``, and while ``dispatch_enabled`` is False it does
nothing at all.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import github_source, health, issuekey, session_events
from .allowlist import ProjectAllowlist
from .store import Store, action_id

OBSERVE = "observe"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ts() -> float:
    return time.time()


def _default_allowlist_path() -> Path:
    """``config/allowlist.json`` next to the package, i.e. inside the repo."""
    return Path(__file__).resolve().parent.parent / "config" / "allowlist.json"


class Engine:
    def __init__(self, cfg: dict, store: Store, source, bus=None, *, clock=_now_ts,
                 project_allowlist: ProjectAllowlist | None = None):
        self.cfg = cfg
        self.store = store
        self.source = source
        self.bus = bus
        self.clock = clock
        self.mode = str(cfg.get("mode") or OBSERVE)
        # Machine-checkable switch. Observe => always False. A test flips this
        # attribute to prove the external-action counter is not hard-wired to 0.
        self.dispatch_enabled = self.mode != OBSERVE
        self.role = str(cfg.get("role") or "")
        engine_cfg = cfg.get("engine") or {}
        # Event-type allowlist. The project/issue/action allowlist is the other
        # dimension and lives in self.projects (see super_agent.allowlist).
        self.allowlist = set(engine_cfg.get("allowlist") or [])
        self.cooldown_s = float(engine_cfg.get("cooldown_s") or 0)
        self.markers = [s.lower() for s in
                        (engine_cfg.get("escalation_markers") or ["[decision]", "[blocked]"])]
        self.creation_mode = str(engine_cfg.get("creation_mode") or "pm_created")
        self.projects = project_allowlist
        if self.projects is None:
            path = str(engine_cfg.get("project_allowlist") or "").strip()
            if path:
                # The last-good cache belongs to run state, not next to the
                # tracked config file.
                self.projects = ProjectAllowlist(
                    path, cache_path=self.store.root / "allowlist.last-good")
        self._recovered = False
        self.last_successful_action_at: str | None = None

    # ------------------------------------------------------------ allowlist
    def allowlist_allows(self, issue: int | None, action: str | None) -> tuple[bool, str]:
        if self.projects is None:
            return True, "no project allowlist configured"
        return self.projects.check(self._project_for(issue), issue, action)

    def _thread_key(self, issue) -> str:
        """Qualified thread key for ``issue`` (falls back to the bare number)."""
        repo = str((self.cfg.get("github") or {}).get("repo") or "")
        if not issuekey.is_repo(repo):
            entry = self.projects.project(self._project_for(issue)) if self.projects else None
            repo = str((entry or {}).get("repo") or "")
        return issuekey.thread_key(repo, issue)

    def _project_for(self, issue) -> str | None:
        """Project name that lists ``issue``; None when nothing claims it."""
        if self.projects is None or self.projects.data is None:
            return None
        try:
            issue = int(issue)
        except (TypeError, ValueError):
            return None
        for name, entry in self.projects.data["projects"].items():
            if issue in entry["issues"]:
                return name
        return None

    # ------------------------------------------------------------------ health
    def health_record(self, *, error: str = "", degraded: bool = False,
                      running: bool = True, phase: str = "") -> dict:
        index = self.store.load_index()
        record = {"pid": os.getpid(), "mode": self.mode,
                  "last_loop_at": _now(),
                  "last_successful_action_at": self.last_successful_action_at,
                  "active_assignments": len(index.get("assignments") or {}),
                  "degraded": bool(degraded or error), "last_error": error,
                  "running": bool(running)}
        if phase:
            record["phase"] = phase
        return record

    def write_health(self, *, error: str = "", degraded: bool = False,
                     running: bool = True, phase: str = "") -> dict:
        return health.write(self.store.health_path,
                            self.health_record(error=error, degraded=degraded,
                                               running=running, phase=phase))

    # ------------------------------------------------------------------ state
    def _cooldown_ok(self, issue_state: dict, kind: str, now: float) -> bool:
        last = (issue_state.get("cooldowns") or {}).get(kind)
        if not last:
            return True
        try:
            return (now - float(last)) >= self.cooldown_s
        except (TypeError, ValueError):
            return True

    def _mark_cooldown(self, issue_state: dict, kind: str, now: float) -> None:
        issue_state.setdefault("cooldowns", {})[kind] = now

    @staticmethod
    def _action_name(action: dict) -> str:
        """Allowlist action name: explicit ``action``, else the action kind."""
        explicit = action.get("action")
        return str(explicit) if explicit else str(action.get("kind") or "")

    def _triage(self, comment: dict) -> tuple[str, str]:
        """Fixed rules only: role-authored noise is ignored, markers escalate."""
        author = str(comment.get("author") or "").strip().lower()
        if self.role and author == self.role.lower():
            return "ignored", "authored by configured role"
        body = str(comment.get("body") or "").lower()
        for marker in self.markers:
            if marker and marker in body:
                return "needs_person", f"marker {marker!r}"
        return "logged", ""

    # --------------------------------------------------------------- dispatch
    def _dispatch(self, action: dict, *, reuse_action_id: str | None = None) -> dict:
        """The only exit to the outside world.

        Order is load-bearing: the project allowlist is checked first (an
        unlisted issue/action never becomes an external action), then
        ``prepared`` is persisted, then the counter moves, then the call runs.
        A call that raises is left ``in_flight`` -> ``uncertain`` so it is
        reconciled, never blindly resent.

        Counting happens *before* the attempt: an action that was enabled and
        attempted is an external action even if the remote end refuses it.

        ``reuse_action_id`` replays an existing ``prepared`` record (startup
        recovery) instead of opening a new attempt.
        """
        if not self.dispatch_enabled:
            self.store.log_event({
                "type": "dispatch.suppressed", "at": _now(),
                "mode": self.mode, "action": action.get("kind"),
                "issue": action.get("issue"),
            })
            return {"dispatched": False, "reason": "dispatch disabled"}

        verb = str(action.get("kind") or "")
        issue = action.get("issue")
        allowed, reason = self.allowlist_allows(issue, self._action_name(action))
        if not allowed:
            self.store.log_event({
                "type": "dispatch.refused", "at": _now(), "action": verb,
                "issue": issue, "reason": reason,
            })
            return {"dispatched": False, "reason": reason, "refused": True}

        aid = reuse_action_id or action_id(
            action.get("assignment_id"), verb,
            str(action.get("input_ref") or issue or ""))
        prepared = self.store.begin_action(
            aid, verb=verb, kind=verb,
            assignment_id=action.get("assignment_id"),
            input_ref=str(action.get("input_ref") or issue or ""),
            payload=action.get("payload") or {},
            issue=issue,
            reuse=bool(reuse_action_id))
        # A second attempt of the same action carries a ``#<n>`` suffix; every
        # later transition must use that key or it would resurrect the base id
        # and overwrite the earlier (possibly ``uncertain``) record.
        aid = str(prepared["action_id"])
        total = self.store.bump_external_actions()
        self.store.mark_in_flight(aid)
        ok, err = False, ""
        try:
            if action.get("kind") == "bus_inject":
                if self.bus is None:
                    raise RuntimeError("no bus client configured")
                reply = self.bus.inject(str(action.get("event")),
                                        action.get("payload") or {})
                if isinstance(reply, dict) and reply.get("ok") is False:
                    raise RuntimeError(str(reply.get("error") or "bus refused the event"))
                ok = True
            else:
                raise RuntimeError(f"unknown action kind {action.get('kind')!r}")
        except Exception as exc:  # noqa: BLE001  a failed action must not abort the round
            err = f"{type(exc).__name__}: {exc}"
        if ok:
            self.last_successful_action_at = _now()
            self.store.mark_done(aid)
        else:
            self.store.mark_uncertain(aid, err)
        self.store.log_outbox({
            "at": _now(), "kind": action.get("kind"), "issue": issue,
            "event": action.get("event"), "ok": ok, "error": err,
            "action_id": aid, "external_actions_total": total,
        })
        return {"dispatched": True, "ok": ok, "error": err,
                "external_actions_total": total}

    # ------------------------------------------------------------------- round
    def run_once(self) -> dict:
        """One round. ``cli once`` and every iteration of ``cli run`` use this."""
        return self.run_round()

    def recover(self) -> dict:
        """Startup recovery: poll the allowlist, then reconcile the outbox.

        Only ``prepared`` actions are replayed (they are certain not to have
        run). ``in_flight`` becomes ``uncertain`` and waits for reconciliation.
        Nothing here resends a create/send/comment.
        """
        if self.projects is not None:
            try:
                self.projects.poll()
            except Exception as exc:  # noqa: BLE001  hot reload never crashes a round
                self.store.log_event({"type": "allowlist.error", "at": _now(),
                                      "error": f"{type(exc).__name__}: {exc}"})
        result = self.store.recover_outbox()
        self._recovered = True
        self.store.log_event({"type": "outbox.recovered", "at": _now(),
                              "replayable": len(result["replayable"]),
                              "reconcile": len(result["reconcile"]),
                              "moved_to_uncertain": len(result["moved_to_uncertain"])})
        return result

    def startup(self, probe=None) -> dict:
        """The production startup entry: recover -> replay -> reconcile.

        ``cli`` calls this once before the first round. The three steps are
        ordered and each is the *only* place that touches its state:
        ``store.recover_outbox()`` moves ``in_flight`` to ``uncertain``,
        :meth:`replay_prepared` re-issues only the records that are certain not
        to have run, and :meth:`reconcile_uncertain` resolves the rest against
        the remote side - with no probe every one of them lands in
        ``waiting_person``, never silently dropped and never re-sent.

        A failure here is fatal for the startup: the caller must not start a
        round with unresolved actions, and the health snapshot records
        ``degraded``, ``last_error`` and ``phase="startup-failed"`` so a
        watchdog sees why - and sees that this was not a crash of a running
        loop.
        """
        out = {"ok": True, "error": "", "recovered": {}, "replayed": [],
               "reconciled": []}
        try:
            out["recovered"] = self.recover()
            out["replayed"] = self.replay_prepared()
            out["reconciled"] = self.reconcile_uncertain(probe)
        except Exception as exc:  # noqa: BLE001  a failed recovery is a reportable state
            out["ok"] = False
            out["error"] = f"{type(exc).__name__}: {exc}"
            self.store.log_event({"type": "outbox.recovery_failed", "at": _now(),
                                  "error": out["error"]})
            self.write_health(error=f"outbox recovery failed: {out['error']}",
                              degraded=True, running=False,
                              phase="startup-failed")
            return out
        self.store.log_event({
            "type": "outbox.startup", "at": _now(),
            "replayed": len(out["replayed"]),
            "reconciled": [r["outcome"] for r in out["reconciled"]],
            "waiting_person": sum(1 for r in out["reconciled"]
                                  if r["outcome"] == "waiting_person"),
        })
        return out

    def replay_prepared(self) -> list[dict]:
        """Replay only actions recorded as ``prepared`` (certain not to have run).

        ``in_flight`` and ``uncertain`` records are deliberately excluded: they
        may have landed, so they go through :meth:`reconcile_uncertain` instead.
        Each replay reuses the exact record key (``reuse_action_id``): the
        attempt already exists, so it must not fork a second attempt id.
        """
        out = []
        for rec in self.store.recover_outbox()["replayable"]:
            payload = rec.get("payload") or {}
            action = {"kind": rec.get("kind") or rec.get("verb"),
                      "assignment_id": rec.get("assignment_id"),
                      "input_ref": rec.get("input_ref") or rec.get("comment_id"),
                      "issue": rec.get("issue") if rec.get("issue") is not None
                      else payload.get("issue"),
                      "event": payload.get("event"),
                      "payload": payload.get("payload") if isinstance(
                          payload.get("payload"), dict) else payload}
            action.setdefault("event", payload.get("event"))
            result = self._dispatch(action, reuse_action_id=rec.get("action_id"))
            result["action_id"] = rec.get("action_id")
            out.append(result)
        return out

    def reconcile_uncertain(self, probe=None) -> list[dict]:
        """Check each uncertain action against the remote side. Never resends.

        ``probe(record) -> dict`` returns ``{"found": bool, "sid"/"prompt_id"/
        "comment_id": ...}``. ``found`` marks it ``done``; not found (or no
        probe) marks it ``waiting_person`` so a human decides. The action is
        never re-issued from here.
        """
        results = []
        for rec in self.store.reconcile_queue():
            aid = str(rec.get("action_id") or "")
            self.store.mark_reconciling(aid)
            verdict = {"found": False, "detail": "no probe configured"}
            if probe is not None:
                try:
                    verdict = probe(dict(rec)) or verdict
                except Exception as exc:  # noqa: BLE001  a failed probe waits for a person
                    verdict = {"found": False,
                               "detail": f"probe failed: {type(exc).__name__}: {exc}"}
            if verdict.get("found"):
                self.store.mark_done(aid, sid=verdict.get("sid"),
                                     prompt_id=verdict.get("prompt_id"),
                                     comment_id=verdict.get("comment_id"))
                outcome = "done"
            else:
                self.store.mark_reconciled(
                    aid, "waiting_person",
                    verdict.get("detail") or "remote side did not confirm the action")
                outcome = "waiting_person"
            results.append({"action_id": aid, "outcome": outcome,
                            "detail": verdict.get("detail") or ""})
        return results

    def run_round(self) -> dict:
        index = self.store.load_index()
        cursor = self.store.load_cursor()
        gh_cfg = self.cfg.get("github") or {}
        max_issues = int(gh_cfg.get("max_issues") or 20)
        max_comments = int(gh_cfg.get("max_comments_per_issue") or 50)
        max_pages = int(gh_cfg.get("max_comment_pages") or 10)

        # Legacy numeric keys are copied forward once, before anything reads
        # them: from here on every write uses the qualified form.
        repo = str(gh_cfg.get("repo") or "")
        if issuekey.is_repo(repo):
            issuekey.migrate(index, cursor, repo, log_event=self.store.log_event)

        # Hot reload before reading anything: a valid new file applies to this
        # round onwards, a broken one leaves the previous copy in force.
        allowlist_reload = ""
        if self.projects is not None:
            try:
                polled = self.projects.poll()
                if polled.get("error"):
                    allowlist_reload = polled["error"]
            except Exception as exc:  # noqa: BLE001  never abort a round
                allowlist_reload = f"{type(exc).__name__}: {exc}"

        summary = {"mode": self.mode, "dispatch_enabled": self.dispatch_enabled,
                   "issues": 0, "comments_new": 0, "processed": 0,
                   "needs_person": 0, "errors": []}
        if allowlist_reload:
            summary["allowlist_reload"] = allowlist_reload
        self.store.log_event({"type": "run.started", "at": _now(), "mode": self.mode})

        try:
            issue_numbers = self.source.issues(limit=max_issues)
        except Exception as exc:  # noqa: BLE001  a dead source is a reportable state
            summary["errors"].append(f"issues: {type(exc).__name__}: {exc}")
            issue_numbers = []

        for issue in issue_numbers:
            try:
                self._round_issue(issue, index, cursor, max_comments, summary,
                                  max_pages)
            except Exception as exc:  # noqa: BLE001  isolate one bad issue
                summary["errors"].append(f"issue {issue}: {type(exc).__name__}: {exc}")
                self.store.log_event({"type": "issue.error", "at": _now(),
                                      "issue": int(issue),
                                      "error": f"{type(exc).__name__}: {exc}"})

        # Index is written once per round, atomically: a crash leaves the old one.
        index["meta"].update({
            "updated_at": _now(), "mode": self.mode, "role": self.role,
            "dispatch_enabled": self.dispatch_enabled,
            "run_count": int(index["meta"].get("run_count") or 0) + 1,
        })
        self.store.save_index(index)
        self.store.save_cursor(cursor)
        self.store.log_event({"type": "run.completed", "at": _now(),
                              "processed": summary["processed"],
                              "needs_person": summary["needs_person"],
                              "external_actions": self.store.load_counters()
                              .get("external_actions", 0)})
        summary["external_actions"] = self.store.load_counters().get("external_actions", 0)
        health_errors = list(summary["errors"])
        if allowlist_reload:
            health_errors.append(f"allowlist kept previous copy: {allowlist_reload}")
        self.write_health(error="; ".join(health_errors))
        return summary

    def _round_issue(self, issue: int, index: dict, cursor: dict,
                     max_comments: int, summary: dict,
                     max_pages: int = 10) -> None:
        issue = int(issue)
        key = self._thread_key(issue)
        legacy = str(issue)
        summary["issues"] += 1
        issues = index.setdefault("issues", {})
        rec = issues.setdefault(key, {"state": "idle", "comments_seen": 0,
                                      "needs_person": []})
        rec["title"] = rec.get("title") or self.source.title(issue)
        # Qualified key first, legacy second: a mixed file converges instead of
        # re-reading comments already consumed under the old form.
        cur = int(issuekey.read_both(cursor.get("issues") or {}, key, legacy) or 0)
        # Paged read from the cursor, then take the **oldest** N new comments:
        # taking the newest N (the old slice) silently skipped the middle when
        # an issue had more new comments than the cap.
        comments = github_source.comments_from(self.source, issue, since_id=cur,
                                               max_pages=max_pages)
        fresh = [c for c in comments if int(c["id"]) > cur][:max_comments]
        summary["comments_new"] += len(fresh)

        seen_this_round: set[int] = set()
        for comment in fresh:
            event_type = "issue_comment.created"
            if event_type not in self.allowlist:
                continue
            cid = int(comment["id"])
            if cid in seen_this_round:          # in-run dedupe only
                continue
            seen_this_round.add(cid)
            state, reason = self._triage(comment)
            summary["processed"] += 1
            rec["comments_seen"] = int(rec.get("comments_seen") or 0) + 1
            rec["last_comment_id"] = cid
            rec["last_event_ts"] = _now()
            self.store.log_event({"type": "comment.processed", "at": _now(),
                                  "issue": issue, "comment_id": cid, "state": state,
                                  "reason": reason})
            if state == "needs_person":
                rec["state"] = "needs_person"
                rec.setdefault("needs_person", []).append({
                    "comment_id": cid, "reason": reason, "at": _now(),
                })
                summary["needs_person"] += 1
                now = self.clock()
                if self._cooldown_ok(rec, "needs_person", now):
                    self._mark_cooldown(rec, "needs_person", now)
                    self._dispatch({"kind": "bus_inject",
                                    "event": "event.agent.needs_person",
                                    "issue": issue,
                                    "payload": {"issue": issue, "comment_id": cid,
                                                "reason": reason}})
            elif rec.get("state") == "idle":
                rec["state"] = "tracked"

        if fresh:
            cursor.setdefault("issues", {})[key] = max(int(c["id"]) for c in fresh)
        else:
            cursor.setdefault("issues", {}).setdefault(key, cur)

    # ------------------------------------------------------------- status/doctor
    def status(self, *, outbox: bool = False) -> dict:
        """Items that need a person. ``outbox=True`` adds the outbox layer.

        ``index.json`` holds triage results; ``outbox_state.json`` holds actions
        that are still unfinished. ``waiting_person``/``uncertain`` live in the
        outbox and were invisible here, so a brief that pointed at ``status`` for
        them named a command that returned nothing. ``outbox`` defaults to False
        so the printed shape of plain ``status`` does not change.
        """
        index = self.store.load_index()
        counters = self.store.load_counters()
        items = []
        for issue_key, rec in sorted((index.get("issues") or {}).items(),
                                     key=lambda kv: issuekey.issue_number(kv[0]) or 0):
            for entry in rec.get("needs_person") or []:
                items.append({"issue": issue_key, "comment_id": entry.get("comment_id"),
                              "reason": entry.get("reason"), "at": entry.get("at"),
                              "source": "index", "action_id": None})
        if outbox:
            for aid, rec in sorted(self.store.unfinished_outbox().items()):
                status = str(rec.get("status") or "")
                if status not in ("waiting_person", "uncertain"):
                    continue
                items.append({"issue": rec.get("issue"), "comment_id": None,
                              "reason": rec.get("reason"), "at": rec.get("updated_at"),
                              "source": "outbox", "action_id": aid,
                              "status": status})
        return {"external_actions": int(counters.get("external_actions") or 0),
                "mode": self.mode, "items": items}

    def doctor(self) -> dict:
        """Distinguishable faults, one readable label each.

        ``process-dead`` / ``startup-failed`` / ``bus-down`` / ``github-down`` /
        ``config-bad`` / ``outbox-stuck``. A fault is only ever reported once,
        with the detail that makes it actionable.
        """
        checks = []

        def add(name: str, ok: bool, detail: str, level: str = "fail",
                label: str = "") -> None:
            checks.append({"name": name, "ok": ok, "detail": detail,
                           "level": "ok" if ok else level,
                           "label": "ok" if ok else (label or name)})

        add(*self._check_process())

        health = {}
        if self.bus is not None:
            health = self.bus.healthz()
            connected = bool(health.get("connected"))
            ready = bool(health.get("ready"))
            add("bus", bool(health.get("ok")),
                f"ok={health.get('ok')} ready={ready} connected={connected} "
                f"boot_id={health.get('boot_id') or '-'} "
                f"seq={health.get('seq')} "
                f"{health.get('error') or ''}".strip(), label="bus-down")
        else:
            add("bus", False, "no bus client configured", level="warn",
                label="bus-down")

        source_name = (self.cfg.get("github") or {}).get("source")
        if source_name == "none":
            add("github", True, "source=none (skipped)")
        else:
            try:
                issues = self.source.issues(limit=1)
                add("github", True, f"source={source_name} reachable, sample={issues}")
            except Exception as exc:  # noqa: BLE001
                add("github", False, f"source={source_name} error: "
                                     f"{type(exc).__name__}: {exc}",
                    label="github-down")

        add(*self._check_config())

        stuck = self.store.stuck_outbox()
        if stuck:
            detail = "; ".join(f"{r.get('action_id')} {r.get('status')} "
                               f"age={r.get('age_s')}s" for r in stuck[:5])
            add("outbox", False,
                f"{len(stuck)} action(s) past {self.store.outbox_stale_s:g}s: {detail}",
                label="outbox-stuck")
        else:
            add("outbox", True, "no prepared/in_flight/uncertain action is overdue")

        # Reported separately on purpose: ``outbox-stuck`` means "nobody is
        # looking", ``waiting-person`` means "a person has to decide". Merging
        # the two would make a resolved-looking alarm hide a real queue.
        waiting = self.store.waiting_person()
        add("waiting-person", not waiting,
            "none" if not waiting else
            f"{len(waiting)} action(s) need a person: " + "; ".join(
                f"{r.get('action_id')} ({r.get('reason') or 'no remote confirmation'})"
                for r in waiting[:5]),
            label="waiting-person")

        cursor = self.store.load_cursor()
        bad_cursor = [k for k, v in (cursor.get("issues") or {}).items()
                      if not isinstance(v, int) or v < 0
                      or issuekey.issue_number(k) is None]
        add("cursor", not bad_cursor,
            "cursor ok" if not bad_cursor else f"invalid cursors: {bad_cursor}")

        index = self.store.load_index()
        residual = index.get("assignments") or {}
        add("assignments", not residual,
            "none" if not residual else f"residual: {sorted(residual)}", level="warn")

        counters = self.store.load_counters()
        return {"checks": checks, "external_actions":
                int(counters.get("external_actions") or 0),
                "mode": self.mode}

    # ------------------------------------------------------ doctor sub-checks
    def _check_process(self) -> tuple:
        """Fault 1: a dead pid lock, or a health record with no live engine."""
        from .pidlock import process_alive
        # Read the snapshot first: the real sequence for a failed startup is
        # "startup failed -> process exits (rc=1) -> the lock was not cleaned
        # up", so the stale pid lock and the startup-failed snapshot arrive
        # together. Judging the lock first would answer "the next start takes
        # it over" and never mention why the last start gave up.
        snap = health.read(self.store.health_path)
        startup_failed = (snap.get("phase") == "startup-failed"
                          and not snap.get("running"))
        try:
            info = json.loads(self.store.pid_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        if info.get("pid"):
            pid = info.get("pid")
            if process_alive(pid):
                return ("process", True,
                        f"engine.pid pid={pid} alive since={info.get('started_at')}",
                        "fail", "process-dead")
            if startup_failed:
                return ("process", False,
                        f"startup-failed with a stale engine.pid pid={pid}: "
                        f"last_error={snap.get('last_error') or 'unknown'}; "
                        f"the last start failed during startup and left the "
                        f"lock behind", "fail", "startup-failed")
            return ("process", False,
                    f"engine.pid pid={pid} is not alive (stale lock; the next "
                    f"start takes it over)", "fail", "process-dead")
        if startup_failed:
            # The process wrote its snapshot, failed during startup and exited
            # (rc=1) before any round ran. It did not crash mid-loop, so the
            # label says so and the detail carries the reason it gave up.
            return ("process", False,
                    f"startup-failed: the engine exited while starting up "
                    f"(last_error={snap.get('last_error') or 'unknown'}, "
                    f"pid={snap.get('pid')}); no round ran, so this is not a "
                    f"crash of a running loop", "fail", "startup-failed")
        if snap.get("running"):
            return ("process", False,
                    f"crashed: no pid lock but health.json is still running=true "
                    f"(last_loop_at={snap.get('last_loop_at')}, pid={snap.get('pid')}); "
                    f"the process died without a clean exit", "fail",
                    "process-dead")
        if snap.get("last_loop_at"):
            return ("process", True,
                    f"engine stopped cleanly (last_loop_at={snap.get('last_loop_at')}, "
                    f"pid={snap.get('pid')})", "warn", "process-dead")
        return ("process", True,
                "engine not running (no pid lock, no health record)", "warn",
                "process-dead")

    def _check_config(self) -> tuple:
        """Fault 4: allowlist rejected; the previous copy is still in force."""
        if self.projects is None:
            return ("config", True, "no project allowlist configured", "fail",
                    "config-bad")
        snap = self.projects.summary()
        if snap["valid"] and not snap["error"]:
            return ("config", True,
                    f"allowlist ok projects={snap['projects']} "
                    f"allowlist={self.projects.source_label()} "
                    f"generation={snap['generation']}", "fail", "config-bad")
        if snap["valid"]:
            return ("config", False,
                    f"config-bad: {snap['error']} - still using the previous copy "
                    f"(last-good cache, generation={snap['generation']})",
                    "fail", "config-bad")
        return ("config", False,
                f"config-bad: {snap['error'] or 'no valid copy'}; nothing is allowed",
                "fail", "config-bad")

    # -------------------------------------------------------------------- replay
    def replay(self, issue: int) -> dict:
        """Rebuild one issue's index entry from the source. Read-only."""
        issue = int(issue)
        index = self.store.load_index()
        cursor = self.store.load_cursor()
        rec = {"state": "idle", "comments_seen": 0, "needs_person": [],
               "title": self.source.title(issue), "rebuilt_at": _now()}
        comments = self.source.comments(issue)
        for comment in comments:
            cid = int(comment["id"])
            rec["comments_seen"] += 1
            state, reason = self._triage(comment)
            if state == "needs_person":
                rec["state"] = "needs_person"
                rec["needs_person"].append({"comment_id": cid, "reason": reason,
                                            "at": _now()})
            elif rec["state"] == "idle":
                rec["state"] = "tracked"
        rec["last_comment_id"] = max((int(c["id"]) for c in comments), default=0)
        key = self._thread_key(issue)
        index.setdefault("issues", {})[key] = rec
        index.setdefault("meta", {})["updated_at"] = _now()
        index["meta"]["rebuilt_issue"] = issue
        self.store.save_index(index)
        cursor.setdefault("issues", {})[key] = rec["last_comment_id"]
        self.store.save_cursor(cursor)
        self.store.log_event({"type": "replay.rebuilt", "at": _now(), "issue": issue,
                              "comments": rec["comments_seen"],
                              "external_actions":
                                  self.store.load_counters().get("external_actions", 0)})
        return {"issue": issue, "comments": rec["comments_seen"],
                "state": rec["state"],
                "external_actions": self.store.load_counters()
                .get("external_actions", 0)}

    # ------------------------------------------------------ session truth (ISS-27)
    def _session_stream(self) -> tuple[Path, Path]:
        engine_cfg = self.cfg.get("engine") or {}
        events = session_events.resolve_events_path(
            self.cfg, engine_cfg.get("session_events_path") or None)
        sessions = session_events.resolve_sessions_path(self.cfg)
        return events, sessions

    def replay_sessions(self, at: str | None = None) -> dict:
        """Rebuild ``sid -> (role, issue, assignment, profile)`` as of ``at``.

        Reads the workbench lifecycle stream (``tools/sess_events.py:emit`` wrote
        it). Strictly read-only and it never writes into ``store/``: the engine's
        own ``events.jsonl`` is a different stream and stays untouched.
        """
        events_path, _sessions_path = self._session_stream()
        events = session_events.load_events(events_path)
        state = session_events.replay(events, at)
        state["events_path"] = str(events_path)
        state["keys_active"] = session_events.active_keys(state)
        return state

    def reconcile_sessions(self, at: str | None = None,
                           server_sids: set[str] | None = None) -> dict:
        """Three-way: replay × ``sessions.json`` × live server. Read-only."""
        events_path, sessions_path = self._session_stream()
        return session_events.reconcile(events_path, sessions_path, server_sids, at)


def build_engine(cfg: dict, store: Store, bus=None, *, source=None) -> Engine:
    """``source`` overrides the configured backend (``status`` needs none)."""
    if source is None:
        try:
            source = github_source.build(cfg)
        except Exception as exc:  # noqa: BLE001  report as a failed check, never crash
            source = github_source.BrokenSource(f"{type(exc).__name__}: {exc}")
    return Engine(cfg, store, source, bus)
