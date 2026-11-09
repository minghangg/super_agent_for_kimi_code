"""Local store: compact index, append-only event log, outbox.

Three properties are load-bearing for M0 and each is testable on its own:

* ``index.json`` is replaced atomically. A crash after the temporary file is
  written but before the rename leaves the previous index intact.
* ``events.jsonl`` and ``outbox.jsonl`` are bounded; when the current segment
  reaches its line limit it rotates to ``.1`` (and ``.1`` to ``.2``), so disk
  usage does not grow without limit.
* ``cursor.json`` records per-issue comment cursors so a second run does not
  re-read comments the first run already saw.

The outbox has two layers. ``outbox.jsonl`` stays the bounded transition
history (one line per state change, rotated like every other log).
``outbox_state.json`` holds the authoritative state of actions that are *not*
finished, written atomically. Rotation therefore only discards history, never
an unfinished action: a ``prepared``/``in_flight``/``uncertain`` record is
always recoverable from ``outbox_state.json``.

State machine::

    prepared -> in_flight -> done
                   |
                   +-> uncertain -> reconcile -> done | waiting_person

``prepared`` is persisted *before* the external action runs, so a crash before
the call is certain: that action may be replayed. Reaching ``in_flight`` means
the call may or may not have landed, so a restart never resends it: it is moved
to ``uncertain`` and queued for reconciliation by its recorded remote ids.

``waiting_person`` is unfinished in the sense that matters here: nobody has
resolved it yet, so it stays in ``outbox_state.json`` (and therefore survives
history rotation) until a person acts. It is deliberately not in the reconcile
queue - only ``uncertain``/``reconcile`` are - and not an ``outbox-stuck``
alarm - it is already waiting for someone.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# Test seam: called with the temporary path right before the atomic rename.
# A test can set this to a callable that raises, proving the old file survives.
FAULT_HOOK = None

OUTBOX_PREPARED = "prepared"
OUTBOX_IN_FLIGHT = "in_flight"
OUTBOX_DONE = "done"
OUTBOX_UNCERTAIN = "uncertain"
OUTBOX_RECONCILE = "reconcile"
OUTBOX_WAITING = "waiting_person"

# Statuses that mean "not finished"; these live in outbox_state.json.
# ``waiting_person`` is included even though it is a reconcile *outcome*: a
# person has not decided yet, so the record must stay visible in the state file
# (and survive history rotation) rather than vanish the moment it is written.
UNFINISHED = (OUTBOX_PREPARED, OUTBOX_IN_FLIGHT, OUTBOX_UNCERTAIN,
              OUTBOX_RECONCILE, OUTBOX_WAITING)

# Actions whose remote effect is not observable by id alone. A timeout is never
# retried blind; it must be reconciled against the recorded remote id first.
NON_IDEMPOTENT = frozenset({"create", "send", "comment", "create_worker",
                            "create_qa", "archive", "close"})


def action_id(assignment_id: str | None, verb: str, input_ref: str) -> str:
    """Stable identity: ``assignment + verb + input_ref``.

    Without an assignment the triple is hashed so the id stays stable across
    restarts while remaining short and filesystem-safe.

    The id is the *base* identity, not necessarily the key of one attempt: a
    second attempt of the same action gets a ``#<n>`` suffix from
    :meth:`Store.begin_action`, so it can never overwrite the first one.
    """
    verb = str(verb or "")
    input_ref = str(input_ref or "")
    if assignment_id:
        return f"{assignment_id}:{verb}:{input_ref}"
    digest = hashlib.sha256(f"{verb}|{input_ref}".encode("utf-8")).hexdigest()
    return f"h-{digest[:24]}"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        hook = FAULT_HOOK
        if hook is not None:
            hook(tmp)
        os.replace(tmp, path)
    finally:
        # After a successful rename the temporary file is already gone, so this
        # is a no-op there; after a failed write it removes the residue.
        tmp.unlink(missing_ok=True)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(
        timespec="seconds")


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except FileNotFoundError:
        return 0
    except OSError:
        return 0


class Store:
    def __init__(self, root: str | Path, *, max_event_lines: int = 10_000,
                 max_outbox_lines: int = 5_000, outbox_stale_s: float = 300):
        self.root = Path(root)
        self.max_event_lines = max_event_lines
        self.max_outbox_lines = max_outbox_lines
        self.outbox_stale_s = float(outbox_stale_s)
        self.index_path = self.root / "index.json"
        self.events_path = self.root / "events.jsonl"
        self.outbox_path = self.root / "outbox.jsonl"
        self.outbox_state_path = self.root / "outbox_state.json"
        self.cursor_path = self.root / "cursor.json"
        self.counters_path = self.root / "counters.json"
        self.heartbeat_path = self.root / "run_heartbeat.json"
        self.health_path = self.root / "health.json"
        self.pid_path = self.root / "engine.pid"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lines = {
            self.events_path: _count_lines(self.events_path),
            self.outbox_path: _count_lines(self.outbox_path),
        }

    # ------------------------------------------------------------------ index
    def load_index(self) -> dict:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "meta": {}, "issues": {}, "assignments": {}}
        if not isinstance(data, dict):
            return {"version": 1, "meta": {}, "issues": {}, "assignments": {}}
        data.setdefault("version", 1)
        data.setdefault("meta", {})
        data.setdefault("issues", {})
        data.setdefault("assignments", {})
        return data

    def save_index(self, data: dict) -> None:
        atomic_write_text(self.index_path,
                          json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")

    # ----------------------------------------------------------------- events
    def _rotate(self, path: Path) -> None:
        second = path.with_name(path.name + ".2")
        first = path.with_name(path.name + ".1")
        if second.exists():
            second.unlink()
        if first.exists():
            os.replace(first, second)
        if path.exists():
            os.replace(path, first)
        self._lines[path] = 0

    def log_event(self, event: dict) -> None:
        self._append_bounded(self.events_path, event, self.max_event_lines)

    def log_outbox(self, record: dict) -> None:
        """Bounded transition history.

        Kept as the one-shot shortcut used by :meth:`Engine._dispatch`: a
        record passed here is already a finished observation and only goes to
        the rotating history. Stateful actions go through :meth:`begin_action`
        and its transitions, whose unfinished records live in
        ``outbox_state.json`` and therefore survive rotation.
        """
        self._append_bounded(self.outbox_path, record, self.max_outbox_lines)

    # ------------------------------------------------------- outbox state machine
    def load_outbox_state(self) -> dict:
        try:
            data = json.loads(self.outbox_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "actions": {}, "attempts": {}}
        if not isinstance(data, dict):
            return {"version": 1, "actions": {}, "attempts": {}}
        data.setdefault("version", 1)
        actions = data.get("actions")
        data["actions"] = actions if isinstance(actions, dict) else {}
        attempts = data.get("attempts")
        data["attempts"] = attempts if isinstance(attempts, dict) else {}
        return data

    def save_outbox_state(self, data: dict) -> None:
        atomic_write_text(self.outbox_state_path,
                          json.dumps(data, ensure_ascii=False, indent=1,
                                     sort_keys=True) + "\n")

    def _transition(self, aid: str, status: str, **fields) -> dict:
        """Write one history line and persist the new state atomically."""
        state = self.load_outbox_state()
        actions = state["actions"]
        rec = dict(actions.get(aid) or {})
        rec.update(fields)
        rec["action_id"] = aid
        rec["status"] = status
        rec["updated_at"] = time.time()
        rec.setdefault("created_at", rec["updated_at"])
        history = {"at": _iso(rec["updated_at"]), "action_id": aid,
                   "status": status, "kind": rec.get("kind"),
                   "external_actions_total": self.load_counters().get("external_actions", 0)}
        for key in ("verb", "input_ref", "assignment_id", "sid", "prompt_id",
                    "comment_id", "error", "reason", "attempt"):
            if fields.get(key) is not None:
                history[key] = fields[key]
        self.log_outbox(history)
        if status in UNFINISHED:
            actions[aid] = rec
        else:
            actions.pop(aid, None)
        self.save_outbox_state(state)
        return rec

    def begin_action(self, aid: str, *, verb: str, kind: str = "",
                     assignment_id: str | None = None,
                     input_ref: str = "", payload: dict | None = None,
                     issue: int | None = None,
                     reuse: bool = False) -> dict:
        """Persist ``prepared`` *before* the external call runs.

        ``aid`` is a *base* identity (:func:`action_id`). A new attempt of an
        action whose base id is still in ``outbox_state.json`` (``prepared``,
        ``in_flight``, ``uncertain``, ``reconcile``, ``waiting_person``) gets a
        ``#<n>`` suffix, so a second dispatch can never overwrite the first
        record - an ``uncertain`` action stays uncertain until it is
        reconciled. The counter lives in the state file, so it survives a
        restart.

        ``reuse=True`` keeps the given key instead: only replay of a
        ``prepared`` action uses it, because that record is certain not to have
        run and is the same attempt.
        """
        state = self.load_outbox_state()
        actions, attempts = state["actions"], state["attempts"]
        base = str(aid)
        n = int(attempts.get(base) or 0)
        if reuse and base in actions:
            n = max(n, 1)
        elif n < 1:
            n = 1 if base not in actions else 2
        else:
            n += 1
        key = base if n == 1 else f"{base}#{n}"
        while key in actions and key != base:
            n += 1
            key = f"{base}#{n}"
        attempts[base] = n
        # Persist the counter first: ``_transition`` reloads the state file.
        self.save_outbox_state(state)
        return self._transition(key, OUTBOX_PREPARED, verb=verb, kind=kind,
                                assignment_id=assignment_id,
                                input_ref=input_ref, payload=payload or {},
                                issue=issue, attempt=n)

    def mark_in_flight(self, aid: str) -> dict:
        return self._transition(aid, OUTBOX_IN_FLIGHT)

    def mark_done(self, aid: str, *, sid: str | None = None,
                  prompt_id: str | None = None, comment_id=None) -> dict:
        """Record the remote identity first, then mark ``done``."""
        return self._transition(aid, OUTBOX_DONE, sid=sid, prompt_id=prompt_id,
                                comment_id=comment_id)

    def mark_uncertain(self, aid: str, reason: str = "") -> dict:
        return self._transition(aid, OUTBOX_UNCERTAIN, reason=reason)

    def mark_reconciling(self, aid: str) -> dict:
        return self._transition(aid, OUTBOX_RECONCILE)

    def mark_reconciled(self, aid: str, outcome: str,
                        detail: str = "") -> dict:
        """``done`` or ``waiting_person`` after checking the remote side."""
        status = OUTBOX_WAITING if outcome == OUTBOX_WAITING else OUTBOX_DONE
        return self._transition(aid, status, reason=detail or outcome)

    def outbox_action(self, aid: str) -> dict | None:
        return (self.load_outbox_state()["actions"]).get(aid)

    def action_attempted(self, aid: str) -> int:
        """How many times this **base** action id has been attempted.

        Read-only. The counter is incremented in :meth:`begin_action` *before*
        the external call, and it outlives the action's own record, so a caller
        reads this to enforce at-most-once: ``0`` means "never attempted, safe
        to dispatch", anything else means "an attempt exists, do not send a
        second copy blindly".
        """
        state = self.load_outbox_state()
        try:
            return int((state.get("attempts") or {}).get(str(aid)) or 0)
        except (TypeError, ValueError):
            return 0

    def prune_attempts(self, prefix: str) -> list[str]:
        """Drop the ``attempts`` counters under ``prefix`` and return their keys.

        Only appropriate once every action under that prefix reached a terminal
        state and its tombstone is persisted: a terminal action is never
        re-read by ``_on_delivery``, so dropping the counter cannot resurrect a
        send. Other prefixes are untouched - the file is shared by every action
        the engine has ever attempted.
        """
        state = self.load_outbox_state()
        attempts = state.get("attempts") or {}
        victims = sorted(k for k in attempts if str(k).startswith(str(prefix)))
        if not victims:
            return []
        for key in victims:
            attempts.pop(key, None)
        self.save_outbox_state(state)
        return victims

    def unfinished_outbox(self) -> dict:
        return dict(self.load_outbox_state()["actions"])

    def recover_outbox(self) -> dict:
        """Startup recovery. Never resends an in-flight action.

        ``prepared`` records are certain not to have run: they are the only
        replayable set. ``in_flight`` records may have run, so they become
        ``uncertain`` and join the reconcile queue. Anything already
        ``uncertain``/``reconcile`` stays there.
        """
        state = self.load_outbox_state()
        replayable, reconcile, moved = [], [], []
        for aid, rec in sorted(state["actions"].items()):
            status = rec.get("status")
            if status == OUTBOX_PREPARED:
                replayable.append(dict(rec))
            elif status == OUTBOX_IN_FLIGHT:
                moved.append(self.mark_uncertain(
                    aid, "restart while in_flight: remote result unknown"))
                reconcile.append(dict(moved[-1]))
            elif status in (OUTBOX_UNCERTAIN, OUTBOX_RECONCILE):
                reconcile.append(dict(rec))
        return {"replayable": replayable, "reconcile": reconcile,
                "moved_to_uncertain": [r["action_id"] for r in moved]}

    def reconcile_queue(self) -> list[dict]:
        actions = self.load_outbox_state()["actions"]
        return [dict(rec) for _, rec in sorted(actions.items())
                if rec.get("status") in (OUTBOX_UNCERTAIN, OUTBOX_RECONCILE)]

    def waiting_person(self) -> list[dict]:
        """Actions a person still has to resolve (doctor reports them apart)."""
        actions = self.load_outbox_state()["actions"]
        return [dict(rec) for _, rec in sorted(actions.items())
                if rec.get("status") == OUTBOX_WAITING]

    def stuck_outbox(self, now: float | None = None) -> list[dict]:
        """Unfinished actions older than ``outbox_stale_s`` (doctor's alarm)."""
        now = time.time() if now is None else float(now)
        out = []
        for rec in self.unfinished_outbox().values():
            if rec.get("status") not in (OUTBOX_IN_FLIGHT, OUTBOX_UNCERTAIN,
                                         OUTBOX_RECONCILE):
                continue
            try:
                age = now - float(rec.get("updated_at") or now)
            except (TypeError, ValueError):
                age = 0.0
            if age >= self.outbox_stale_s:
                item = dict(rec)
                item["age_s"] = round(age, 3)
                out.append(item)
        return out

    def _append_bounded(self, path: Path, record: dict, limit: int) -> None:
        if self._lines.get(path, 0) >= limit:
            self._rotate(path)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self._lines[path] = self._lines.get(path, 0) + 1

    # ----------------------------------------------------------------- cursor
    def load_cursor(self) -> dict:
        try:
            data = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "issues": {}}
        if not isinstance(data, dict):
            return {"version": 1, "issues": {}}
        data.setdefault("version", 1)
        data.setdefault("issues", {})
        return data

    def save_cursor(self, data: dict) -> None:
        atomic_write_text(self.cursor_path,
                          json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")

    # --------------------------------------------------------------- counters
    def load_counters(self) -> dict:
        try:
            data = json.loads(self.counters_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"external_actions": 0}
        if not isinstance(data, dict):
            return {"external_actions": 0}
        data.setdefault("external_actions", 0)
        return data

    def save_counters(self, data: dict) -> None:
        atomic_write_text(self.counters_path,
                          json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")

    def bump_external_actions(self, n: int = 1) -> int:
        data = self.load_counters()
        data["external_actions"] = int(data.get("external_actions") or 0) + n
        data["last_external_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.save_counters(data)
        return data["external_actions"]

    # -------------------------------------------------------------- heartbeat
    def save_heartbeat(self, record: dict) -> None:
        """One line, replaced atomically every round of ``run``.

        An external watchdog reads this file to tell a live loop from a dead
        one: ``ts`` moves every round, ``round`` grows, and
        ``external_actions`` is the same persisted counter ``status`` prints.
        """
        atomic_write_text(self.heartbeat_path,
                          json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def load_heartbeat(self) -> dict:
        try:
            data = json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
