"""Deterministic fast path. No LLM, no network side effects in observe mode.

One round does: read allow-listed events -> dedupe against persisted cursors ->
triage with fixed rules -> update the local index -> *maybe* dispatch one
action. Dispatch is the single guarded exit: everything it can do is counted
in ``external_actions``, and while ``dispatch_enabled`` is False it does
nothing at all.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from . import github_source
from .store import Store

OBSERVE = "observe"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ts() -> float:
    return time.time()


class Engine:
    def __init__(self, cfg: dict, store: Store, source, bus=None, *, clock=_now_ts):
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
        self.allowlist = set(engine_cfg.get("allowlist") or [])
        self.cooldown_s = float(engine_cfg.get("cooldown_s") or 0)
        self.markers = [s.lower() for s in
                        (engine_cfg.get("escalation_markers") or ["[decision]", "[blocked]"])]

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
    def _dispatch(self, action: dict) -> dict:
        """The only exit to the outside world.

        Counting happens *before* the attempt: an action that was enabled and
        attempted is an external action even if the remote end refuses it.
        """
        if not self.dispatch_enabled:
            self.store.log_event({
                "type": "dispatch.suppressed", "at": _now(),
                "mode": self.mode, "action": action.get("kind"),
                "issue": action.get("issue"),
            })
            return {"dispatched": False, "reason": "dispatch disabled"}
        total = self.store.bump_external_actions()
        ok, err = False, ""
        try:
            if action.get("kind") == "bus_inject":
                if self.bus is None:
                    raise RuntimeError("no bus client configured")
                self.bus.inject(str(action.get("event")), action.get("payload") or {})
                ok = True
            else:
                raise RuntimeError(f"unknown action kind {action.get('kind')!r}")
        except Exception as exc:  # noqa: BLE001  a failed action must not abort the round
            err = f"{type(exc).__name__}: {exc}"
        self.store.log_outbox({
            "at": _now(), "kind": action.get("kind"), "issue": action.get("issue"),
            "event": action.get("event"), "ok": ok, "error": err,
            "external_actions_total": total,
        })
        return {"dispatched": True, "ok": ok, "error": err,
                "external_actions_total": total}

    # ------------------------------------------------------------------- round
    def run_round(self) -> dict:
        index = self.store.load_index()
        cursor = self.store.load_cursor()
        gh_cfg = self.cfg.get("github") or {}
        max_issues = int(gh_cfg.get("max_issues") or 20)
        max_comments = int(gh_cfg.get("max_comments_per_issue") or 50)

        summary = {"mode": self.mode, "dispatch_enabled": self.dispatch_enabled,
                   "issues": 0, "comments_new": 0, "processed": 0,
                   "needs_person": 0, "errors": []}
        self.store.log_event({"type": "run.started", "at": _now(), "mode": self.mode})

        try:
            issue_numbers = self.source.issues(limit=max_issues)
        except Exception as exc:  # noqa: BLE001  a dead source is a reportable state
            summary["errors"].append(f"issues: {type(exc).__name__}: {exc}")
            issue_numbers = []

        for issue in issue_numbers:
            try:
                self._round_issue(issue, index, cursor, max_comments, summary)
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
        return summary

    def _round_issue(self, issue: int, index: dict, cursor: dict,
                     max_comments: int, summary: dict) -> None:
        issue = int(issue)
        key = str(issue)
        summary["issues"] += 1
        issues = index.setdefault("issues", {})
        rec = issues.setdefault(key, {"state": "idle", "comments_seen": 0,
                                      "needs_person": []})
        rec["title"] = rec.get("title") or self.source.title(issue)
        cur = int((cursor.get("issues") or {}).get(key) or 0)
        comments = self.source.comments(issue)[-max_comments:]
        fresh = [c for c in comments if int(c["id"]) > cur]
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
    def status(self) -> dict:
        index = self.store.load_index()
        counters = self.store.load_counters()
        items = []
        for issue_key, rec in sorted((index.get("issues") or {}).items(),
                                     key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
            for entry in rec.get("needs_person") or []:
                items.append({"issue": issue_key, "comment_id": entry.get("comment_id"),
                              "reason": entry.get("reason"), "at": entry.get("at")})
        return {"external_actions": int(counters.get("external_actions") or 0),
                "mode": self.mode, "items": items}

    def doctor(self) -> dict:
        checks = []

        def add(name: str, ok: bool, detail: str, level: str = "fail") -> None:
            checks.append({"name": name, "ok": ok, "detail": detail,
                           "level": "ok" if ok else level})

        health = {}
        if self.bus is not None:
            health = self.bus.healthz()
            connected = bool(health.get("connected"))
            ready = bool(health.get("ready"))
            add("bus", bool(health.get("ok")),
                f"ok={health.get('ok')} ready={ready} connected={connected} "
                f"boot_id={health.get('boot_id') or '-'} "
                f"seq={health.get('seq')} "
                f"{health.get('error') or ''}".strip())
        else:
            add("bus", False, "no bus client configured", level="warn")

        source_name = (self.cfg.get("github") or {}).get("source")
        if source_name == "none":
            add("github", True, "source=none (skipped)")
        else:
            try:
                issues = self.source.issues(limit=1)
                add("github", True, f"source={source_name} reachable, sample={issues}")
            except Exception as exc:  # noqa: BLE001
                add("github", False, f"source={source_name} error: "
                                     f"{type(exc).__name__}: {exc}")

        cursor = self.store.load_cursor()
        bad_cursor = [k for k, v in (cursor.get("issues") or {}).items()
                      if not isinstance(v, int) or v < 0]
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
        index.setdefault("issues", {})[str(issue)] = rec
        index.setdefault("meta", {})["updated_at"] = _now()
        index["meta"]["rebuilt_issue"] = issue
        self.store.save_index(index)
        cursor.setdefault("issues", {})[str(issue)] = rec["last_comment_id"]
        self.store.save_cursor(cursor)
        self.store.log_event({"type": "replay.rebuilt", "at": _now(), "issue": issue,
                              "comments": rec["comments_seen"],
                              "external_actions":
                                  self.store.load_counters().get("external_actions", 0)})
        return {"issue": issue, "comments": rec["comments_seen"],
                "state": rec["state"],
                "external_actions": self.store.load_counters()
                .get("external_actions", 0)}


def build_engine(cfg: dict, store: Store, bus=None, *, source=None) -> Engine:
    """``source`` overrides the configured backend (``status`` needs none)."""
    if source is None:
        try:
            source = github_source.build(cfg)
        except Exception as exc:  # noqa: BLE001  report as a failed check, never crash
            source = github_source.BrokenSource(f"{type(exc).__name__}: {exc}")
    return Engine(cfg, store, source, bus)
