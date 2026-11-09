"""Five counter-examples for the single-issue loop (ISS-20), run for real.

Every scenario uses its own temporary state directory and an explicitly
injected session stream: ``--state-dir`` does *not* isolate the lifecycle
stream, so the path is always passed through ``engine.session_events_path``.
No network, no session service, no change to the live workbench state.

    python tools/verify_counterexamples.py
"""

from __future__ import annotations

import json
import os
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


# --- 3: a dispatch to an archived sid is refused, counted nowhere, queued nowhere ---
def example_3():
    tmp, events, allow = scenario()
    bind(events, archive=True)
    eng = engine(tmp, events, allow, Comments(), FakeBus(), FakeSessions())
    before = eng.store.load_counters()
    out = eng._dispatch({"kind": "bus_inject", "event": "event.agent.waiting_review",
                         "issue": 1, "input_ref": "1", "payload": {"sid": "session_w1"}})
    after = eng.store.load_counters()
    kinds = [e["type"] for e in events_of(tmp)]
    # The guard returns before ``bump_external_actions``, so a 0->0 counter would
    # be true even if the guard were removed (the dispatch would fail later and
    # still not bump). The real assertions are the ones that can distinguish:
    # no refusal event would exist, and the aborted action would have left a
    # record in ``outbox_state.json``.
    base = action_id(None, "bus_inject", "1")
    queued = eng.store.load_outbox_state()["actions"]
    touched = [k for k in queued if k == base or k.startswith(base + "#")]
    record("3 archived-sid-refused",
           out.get("refused") is True and not out.get("dispatched")
           and "dispatch.archived_sid" in kinds
           and "dispatch.refused" not in kinds
           and before == after and touched == [],
           f"refused={out.get('refused')} archived_sid_events="
           f"{kinds.count('dispatch.archived_sid')} "
           f"counters={before.get('external_actions')}->"
           f"{after.get('external_actions')} outbox_records={touched}")


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


# --- 5: the probe session is archived; accounting here, server-side opt-in ---
def example_5():
    tmp, events, allow = scenario()
    bind(events)
    src = Comments([
        {"id": 9005, "issue": 1, "body": "[WK:wk-probe-1] done",
         "author": "worker", "created_at": ""},
        # The archive pass sits behind ``assignments.qa_gate`` since M2 C4: a
        # delivery without a ``pass`` verdict is refused and archived never
        # happens. The example is about the archive, so it feeds the verdict.
        {"id": 9100, "issue": 1, "body": "[QA] **通过**",
         "author": "qa", "created_at": ""},
    ])
    sessions = FakeSessions()
    eng = engine(tmp, events, allow, src, FakeBus(), sessions)
    # The round must end where the archive ends, at ``waiting_release``: the
    # close reconciliation needs a provable Issue state, which this fake source
    # does not have, so a real reconciler would (correctly) demote the thread to
    # ``close_reconcile`` in the very same round. Completion is the subject of
    # ``tests/test_close_reconcile.py``, not of this example.
    eng._round_closed_threads = lambda index: []
    eng.run_once()
    rec = eng.store.load_index()["assignments"]["asg_1_worker_1"]
    info = sessions.session_info("session_w1")
    # This half proves the *bookkeeping*: the archive was called once, the
    # read-back was consulted, and the record claims ``waiting_release`` because
    # the read-back said archived. Since M2 C4 (70a2a48) a successful archive
    # stops at ``waiting_release`` - ``completed`` is written only by the close
    # reconciliation two rounds later, never here. What this half cannot prove is
    # that the real server agrees - the injected fake answers by construction.
    accounting = (rec["status"] == "waiting_release"
                  and bool(rec.get("archived_at"))
                  and info.get("archived") is True
                  and sessions.calls == ["session_w1"])
    detail = (f"accounting status={rec['status']} "
              f"archived_at={rec.get('archived_at')} "
              f"archived={info.get('archived')} archive_calls={sessions.calls}")

    sid = (os.environ.get("SAFKC_VERIFY_REAL_SID") or "").strip()
    if not sid:
        record("5 probe-session-archived", accounting,
               detail + " | server side NOT checked here (accounting only; "
               "QA verifies the real server independently). "
               "Set SAFKC_VERIFY_REAL_SID=<sid> to include it.")
        return
    # Opt-in, read-only check against the real session service. It never
    # archives anything: it asks whether the server already says archived.
    try:
        from super_agent import api
        real = api.session_info(sid)
    except Exception as exc:  # noqa: BLE001  a failed check is a failed check
        record("5 probe-session-archived", False,
               detail + f" | real session_info({sid}) raised "
               f"{type(exc).__name__}: {exc}")
        return
    record("5 probe-session-archived",
           accounting and bool(real.get("archived")) and not real.get("_err"),
           detail + f" | real archived={real.get('archived')} "
           f"archived_at={real.get('archived_at')} sid={sid}")


def main() -> int:
    for fn in (example_1, example_2, example_3, example_4, example_5):
        fn()
    bad = [r for r in RESULTS if not r["ok"]]
    print(f"\nsummary: {len(RESULTS) - len(bad)}/{len(RESULTS)} counter-examples hold")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
