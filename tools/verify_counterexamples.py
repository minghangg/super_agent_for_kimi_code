"""Five counter-examples for the single-issue loop (ISS-20), run for real.

Every scenario uses its own temporary state directory and an explicitly
injected session stream: ``--state-dir`` does *not* isolate the lifecycle
stream, so the path is always passed through ``engine.session_events_path``.
No network, no session service, no change to the live workbench state.

    python tools/verify_counterexamples.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from super_agent import config, session_events  # noqa: E402
from super_agent.allowlist import ProjectAllowlist  # noqa: E402
from super_agent.engine import Engine  # noqa: E402
from super_agent.store import Store, action_id  # noqa: E402

THREAD = "owner/demo#1"
GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {"demo": {"repo": "owner/demo", "cwd": ".", "mode": "assist",
                          "creation_mode": "pm_created", "issues": [1],
                          "actions": ["bus_inject", "collect", "reconcile",
                                      "archive"],
                          "max_active_writers": 1, "roles": ["worker", "qa"]}},
}


class FakeBus:
    def __init__(self, fail=False):
        self.injected, self.fail = [], fail

    def healthz(self):
        return {"ok": True, "ready": True, "connected": True, "seq": 1}

    def inject(self, event_type, payload):
        if self.fail:
            raise RuntimeError("bus down (injected fault)")
        self.injected.append((event_type, payload))
        return {"ok": True, "seq": len(self.injected)}


class FakeSessions:
    def __init__(self, archive_ok=True):
        self.archive_ok, self.archived, self.calls = archive_ok, set(), []

    def archive_session(self, sid):
        self.calls.append(sid)
        if self.archive_ok:
            self.archived.add(sid)
        return self.archive_ok, {"ids": [sid]}

    def session_info(self, sid):
        return {"archived": sid in self.archived}


class Comments:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def add(self, cid, body):
        self.rows.append({"id": cid, "issue": 1, "body": body,
                          "author": "worker", "created_at": ""})

    def issues(self, limit=20):
        return [1]

    def title(self, issue):
        return "probe"

    def comments(self, issue):
        return sorted(self.rows, key=lambda c: c["id"])

    def paged(self, issue, *, since_id=0, max_pages=10):
        return self.comments(issue)


def scenario():
    tmp = Path(tempfile.mkdtemp(prefix="cx_"))
    events = tmp / "lifecycle" / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    allow = tmp / "allowlist.json"
    allow.write_text(json.dumps(GOOD), encoding="utf-8")
    return tmp, events, allow


def bind(events, sid="session_w1", name="wk-probe-1", archive=False, role="worker"):
    rows = [
        {"seq": 1, "at": "2026-11-09T10:00:00+00:00",
         "type": session_events.CREATED, "sid": sid, "key": name},
        {"seq": 2, "at": "2026-11-09T10:00:01+00:00",
         "type": session_events.BOUND, "sid": sid, "key": THREAD,
         "role": role, "issue": 1},
    ]
    if archive:
        rows.append({"seq": 3, "at": "2026-11-09T10:00:02+00:00",
                     "type": session_events.ARCHIVED, "sid": sid})
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def engine(tmp, events, allow, source, bus, sessions, mode="assist"):
    cfg = config.load(None, {
        "mode": mode,
        "github": {"source": "none", "repo": "owner/demo",
                   "max_comments_per_issue": 50},
        "engine": {"session_events_path": str(events),
                   "allowlist": ["issue_comment.created"],
                   "cooldown_s": 900},
    })
    return Engine(cfg, Store(tmp / "state"), source, bus,
                  project_allowlist=ProjectAllowlist(allow),
                  session_api=sessions)


def events_of(tmp):
    path = tmp / "state" / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def counters(tmp):
    path = tmp / "state" / "counters.json"
    if not path.is_file():
        return 0
    return int(json.loads(path.read_text("utf-8")).get("external_actions") or 0)


RESULTS = []


def record(name, ok, detail):
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


# --- 1: a failed send (bus raises) is uncertain and never re-created/re-sent ---
def example_1():
    tmp, events, allow = scenario()
    bind(events)
    src = Comments([{"id": 9001, "issue": 1, "body": "[WK:wk-probe-1] done",
                     "author": "worker", "created_at": ""}])
    sessions = FakeSessions()
    eng = engine(tmp, events, allow, src, FakeBus(fail=True), sessions)
    eng.run_once()
    base = action_id("asg_1_worker_1", "bus_inject", "9001")
    first = eng.store.action_attempted(base)
    state = eng.store.load_outbox_state()
    status = (state["actions"].get(base) or {}).get("status")
    eng.run_once()                      # restart-equivalent: same state dir
    second = eng.store.action_attempted(base)
    again = len([e for e in events_of(tmp)
                 if e["type"] == "assignment.waiting_review"])
    record("1 uncertain-not-resent",
           first == 1 and second == 1 and status == "uncertain" and again == 1,
           f"attempts={first}->{second} status={status} waiting_review_events={again}")


# --- 2: an uncertain action never produces a second comment/notification ---
def example_2():
    tmp, events, allow = scenario()
    bind(events)
    src = Comments([{"id": 9002, "issue": 1, "body": "[WK:wk-probe-1] done",
                     "author": "worker", "created_at": ""}])
    eng = engine(tmp, events, allow, src, FakeBus(fail=True), FakeSessions())
    eng.run_once()
    probe = lambda rec: {"found": False, "detail": "remote side has nothing"}  # noqa: E731
    reconciled = eng.reconcile_uncertain(probe)
    base = action_id("asg_1_worker_1", "bus_inject", "9002")
    status = (eng.store.load_outbox_state()["actions"].get(base) or {}).get("status")
    eng.run_once()
    after = eng.store.action_attempted(base)
    record("2 reconcile-then-no-resend",
           status == "waiting_person" and after == 1
           and not any(r["outcome"] == "done" for r in reconciled),
           f"reconciled={[r['outcome'] for r in reconciled]} status={status} "
           f"attempts={after}")


# --- 3: after archive, dispatch to the old sid is refused and not counted ---
def example_3():
    tmp, events, allow = scenario()
    bind(events, archive=True)
    eng = engine(tmp, events, allow, Comments(), FakeBus(), FakeSessions())
    before = counters(tmp)
    out = eng._dispatch({"kind": "bus_inject", "event": "event.agent.waiting_review",
                         "issue": 1, "input_ref": "1", "payload": {"sid": "session_w1"}})
    refusals = len([e for e in events_of(tmp)
                    if e["type"] == "dispatch.archived_sid"])
    record("3 archived-sid-refused",
           out.get("refused") and not out.get("dispatched") and refusals >= 1
           and counters(tmp) == before,
           f"refused={out.get('refused')} archived_sid_events={refusals} "
           f"external_actions={before}->{counters(tmp)}")


# --- 4: a second trigger inside the per-issue cooldown adds no external action ---
def example_4():
    tmp, events, allow = scenario()
    bind(events)
    src = Comments([
        {"id": 9003, "issue": 1, "body": "[decision] please pick", "author": "u",
         "created_at": ""},
        {"id": 9004, "issue": 1, "body": "[decision] and again", "author": "u",
         "created_at": ""},
    ])
    eng = engine(tmp, events, allow, src, FakeBus(), FakeSessions())
    eng.run_once()
    cd = (eng.store.load_index()["issues"][THREAD].get("cooldowns") or {})
    # Model-level cooldown is deliberately out of scope for this milestone.
    record("4 cooldown-blocks-second-action",
           counters(tmp) == 1 and "needs_person" in cd,
           f"external_actions={counters(tmp)} cooldowns={sorted(cd)} "
           f"(model cooling is out of scope)")


# --- 5: every probe session ends archived; final check via issue_sessions/session_info ---
def example_5():
    tmp, events, allow = scenario()
    bind(events)
    src = Comments([{"id": 9005, "issue": 1, "body": "[WK:wk-probe-1] done",
                     "author": "worker", "created_at": ""}])
    sessions = FakeSessions()
    eng = engine(tmp, events, allow, src, FakeBus(), sessions)
    eng.run_once()
    rec = eng.store.load_index()["assignments"]["asg_1_worker_1"]
    info = sessions.session_info("session_w1")
    record("5 probe-session-archived",
           rec["status"] == "completed" and info.get("archived") is True
           and sessions.calls == ["session_w1"],
           f"status={rec['status']} archived={info.get('archived')} "
           f"archive_calls={sessions.calls}")


def main() -> int:
    for fn in (example_1, example_2, example_3, example_4, example_5):
        fn()
    bad = [r for r in RESULTS if not r["ok"]]
    print(f"\nsummary: {len(RESULTS) - len(bad)}/{len(RESULTS)} counter-examples hold")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
