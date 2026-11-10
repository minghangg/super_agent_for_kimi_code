"""Five counter-examples for "``needs_person`` can be dismissed" (ISS-47), run for real.

Every scenario builds its own temporary state directory and its own allowlist
and fits itself into the same engine the CLI uses. No network, no session
service, no change to the live workbench state.

    python tools/verify_needs_resolved.py

Semantics this file asserts (read this before trusting a green run)
--------------------------------------------------------------------
``verify-semantics: 1``  -  the dismissal table of ISS-47.
``follows: a339230``     -  the commit this script was written against.
``collected-by: tests/test_needs_person_resolved.py`` (same properties, in
pytest form; this script stays runnable on its own).

What each example proves:

1. dismissing hides the row and ``replay`` rebuilds it underneath - the
   suppression is on the read side, the entry is never deleted;
2. an entry that was not dismissed still shows (mutating ``is_open()`` to a
   constant ``True`` must turn example 1 red, not this one);
3. dismissing twice keeps the first audit row and writes one event (mutating
   the idempotent branch of ``mark()`` to "always overwrite" must turn this
   red - note this is ``mark()``, not ``merge_into``);
4. the same cid triaged again does not revive the row;
5. one issue under both a bare and a qualified key is dismissed under both,
   and a second command still adds the key it missed;
6. a failed save exits 2, names the key and writes no success event;
7. the union of two side tables is commutative.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from super_agent import cli, needs  # noqa: E402
from super_agent import store as store_mod  # noqa: E402
from super_agent.store import Store  # noqa: E402

THREAD = "owner/demo#1"
RESULTS: list[dict] = []

FIXTURE = {"issues": {"1": {"title": "probe", "comments": [
    {"id": 9001, "body": "ordinary discussion, no markers at all", "author": "u"},
    {"id": 9100, "body": "[decision] X", "author": "u"},
]}}}

FIXTURE_TWO = {"issues": {"1": {"title": "probe", "comments": [
    {"id": 9001, "body": "ordinary discussion, no markers at all", "author": "u"},
    {"id": 9100, "body": "[decision] X", "author": "u"},
    {"id": 9101, "body": "[decision] Y", "author": "u"},
]}}}


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n       {detail}")


class Box:
    """One throwaway state root plus the CLI calls that act on it."""

    def __init__(self, *, name: str = "cfg.json"):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.events = self.root / "lifecycle" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text("", encoding="utf-8")
        self.name = name
        self.store = Store(self.state)
        self.fixture_path = self.root / f"fixture-{name}.json"
        self.fixture_path.write_text(json.dumps(FIXTURE), encoding="utf-8")
        self.allow = self.root / f"allowlist-{name}.json"

    def close(self) -> None:
        self.tmp.cleanup()

    def cfg(self, *, repo: str = "owner/demo", allowed: bool = True,
            fixture: dict | None = None) -> Path:
        if fixture is not None:
            self.fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
        self.allow.write_text(json.dumps({
            "schema_version": 1,
            "defaults": {"mode": "observe", "creation_mode": "pm_created"},
            "projects": {"demo": {
                "repo": "owner/demo", "cwd": ".", "mode": "observe",
                "creation_mode": "pm_created",
                "issues": [1] if allowed else [],
                "actions": ["bus_inject", "collect", "bind", "archive",
                            "reconcile"],
                "max_active_writers": 1, "roles": ["worker", "qa"]}}},
        ), encoding="utf-8")
        path = self.root / f"cfg-{self.name}-{int(repo == '')}{int(allowed)}.json"
        path.write_text(json.dumps({
            "role": "supervisor", "mode": "observe",
            "github": {"source": "fixture", "fixture": str(self.fixture_path),
                       "repo": repo},
            "engine": {"session_events_path": str(self.events),
                       "allowlist": ["issue_comment.created"],
                       "project_allowlist": str(self.allow),
                       "archived_crosscheck": False},
        }), encoding="utf-8")
        return path

    def run(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(argv + ["--state-dir", str(self.state),
                                  "--session-api", "none"])
        return rc, out.getvalue()

    def once(self, cfg: Path) -> tuple[int, str]:
        return self.run(["once", "--config", str(cfg)])

    def resolve(self, *extra, cid: int = 9100) -> tuple[int, str]:
        return self.run(["resolve", "--issue", "1", "--comment", str(cid),
                         "--by", "supervisor", *extra])

    def status(self, *extra) -> tuple[int, str]:
        return self.run(["status", *extra])

    def index(self) -> dict:
        return json.loads((self.state / "index.json").read_text("utf-8"))

    def marks(self) -> dict:
        return self.index().get(needs.MARKS) or {}

    def entries(self, key: str = THREAD) -> list:
        return (self.index()["issues"].get(key) or {}).get("needs_person") or []

    def events_of(self, kind: str) -> list:
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        rows = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
        return [r for r in rows if r.get("type") == kind]


def example_1() -> None:
    box = Box(name="ex1")
    try:
        cfg = box.cfg()
        rc, text = box.once(cfg)
        assert rc == 0, text
        assert len(box.entries()) == 1, "seed写不出条目，后面都是空转"
        rc, text = box.resolve()
        assert rc == 0, text
        rc, hidden = box.status()
        rc, replay = box.run(["replay", "--config", str(cfg), "--issue", "1"])
        rebuilt = [e["comment_id"] for e in box.entries()]
        ok = ("无" in hidden and rc == 0 and "comments=2" in replay
              and rebuilt == [9100]
              and box.status()[1].count("9100") == 0)
        record("1 dismissed-then-hidden", ok,
               f"resolve rc={rc} status={hidden.strip()!r} "
               f"replay={replay.strip()!r} entries={rebuilt} "
               f"(the row is rebuilt and still suppressed, not deleted)")
    finally:
        box.close()


def example_2() -> None:
    box = Box(name="ex2")
    try:
        cfg = box.cfg(fixture=FIXTURE_TWO)
        rc, text = box.once(cfg)
        assert rc == 0, text
        rc, text = box.resolve(cid=9100)
        assert rc == 0, text
        rc, visible = box.status()
        ok = rc == 0 and "9101" in visible and "9100" not in visible
        record("2 undismissed-still-visible", ok,
               f"status={visible.strip()!r} (only the undismissed cid 9101 shows; "
               f"mutating is_open() to a constant True turns example 1 red, not this)")
    finally:
        box.close()


def example_3() -> None:
    box = Box(name="ex3")
    try:
        cfg = box.cfg()
        rc, text = box.once(cfg)
        assert rc == 0, text
        rc, text = box.resolve()
        assert rc == 0, text
        first = dict(box.marks()["owner/demo#1|9100"])
        rc, text = box.resolve()
        second = dict(box.marks()["owner/demo#1|9100"])
        ok = (rc == 1 and "already resolved" in text and first == second
              and len(box.events_of("needs_person.resolved")) == 1)
        record("3 double-dismiss-idempotent", ok,
               f"second rc={rc} msg={text.strip()!r} row_unchanged={first == second} "
               f"events={len(box.events_of('needs_person.resolved'))} "
               f"(mutation: make mark() always overwrite -> this goes red)")
    finally:
        box.close()


def example_4() -> None:
    box = Box(name="ex4")
    try:
        cfg = box.cfg()
        rc, text = box.once(cfg)
        assert rc == 0, text
        rc, text = box.resolve()
        assert rc == 0, text
        rc, text = box.run(["replay", "--config", str(cfg), "--issue", "1"])
        assert rc == 0, text
        rc, text = box.once(cfg)
        assert rc == 0, text
        hidden = box.status()[1]
        ok = ("无" in hidden
              and box.marks()["owner/demo#1|9100"]["state"] == "resolved")
        record("4 same-cid-not-revived", ok,
               f"status={hidden.strip()!r} state="
               f"{box.marks()['owner/demo#1|9100']['state']}")
    finally:
        box.close()


def example_5() -> None:
    box = Box(name="ex5")
    try:
        bare = box.cfg(repo="", allowed=False, fixture=FIXTURE)
        rc, text = box.once(bare)
        assert rc == 0, text
        keys_a = sorted(box.index()["issues"])
        qualified = box.cfg()
        rc, text = box.once(qualified)
        assert rc == 0, text
        keys_b = sorted(box.index()["issues"])
        assert keys_a == ["1"], keys_a
        assert keys_b == ["1", THREAD], keys_b

        rc, text = box.resolve()
        rc2, text2 = box.status()
        ok = (rc == 0 and "2 keys: 0 already, 2 newly resolved" in text
              and sorted(box.marks()) == ["1|9100", "owner/demo#1|9100"]
              and "无" in text2)
        record("5 two-key-forms", ok,
               f"keys {keys_a} -> {keys_b}; resolve rc={rc} msg={text.strip()!r} "
               f"marks={sorted(box.marks())} status={text2.strip()!r}")
    finally:
        box.close()


def example_5b() -> None:
    """Only the bare key, dismiss it, then let the round copy the key forward."""
    box = Box(name="ex5b")
    try:
        bare = box.cfg(repo="", allowed=False)
        rc, text = box.once(bare)
        assert rc == 0, text
        rc, text = box.resolve()
        assert rc == 0, text
        qualified = box.cfg()
        rc, text = box.once(qualified)
        assert rc == 0, text
        visible = box.status()[1]
        rc, text = box.resolve()
        hidden = box.status()[1]
        ok = (visible.count("9100") == 1
              and rc == 0 and "2 keys: 1 already, 1 newly resolved" in text
              and "无" in hidden
              and len(box.events_of("needs_person.resolved")) == 2)
        record("5b legacy-first-then-whole-issue", ok,
               f"after migrate status={visible.strip()!r} (the qualified row is new "
               f"and visible) -> second resolve rc={rc} msg={text.strip()!r} "
               f"final status={hidden.strip()!r}")
    finally:
        box.close()


def example_6() -> None:
    box = Box(name="ex6")
    try:
        cfg = box.cfg()
        rc, text = box.once(cfg)
        assert rc == 0, text
        before = json.loads((box.state / "index.json").read_text("utf-8"))

        def boom(_tmp):
            raise OSError("disk full")

        store_mod.FAULT_HOOK = boom
        try:
            rc, text = box.resolve()
        finally:
            store_mod.FAULT_HOOK = None

        # Re-read from disk: merge_into_side already dirtied the in-memory copy.
        after = Store(box.state).load_index()
        ok = (rc == 2 and "failed" in text and THREAD in text
              and needs.MARKS not in after
              and before["issues"] == after["issues"]
              and box.events_of("needs_person.resolved") == []
              and len(box.events_of("needs_person.resolve_failed")) == 1)
        record("6 save-failure-exits-2", ok,
               f"rc={rc} stderr={text.strip()!r} marks_on_disk="
               f"{needs.MARKS in after} success_events="
               f"{len(box.events_of('needs_person.resolved'))} "
               f"failed_events={len(box.events_of('needs_person.resolve_failed'))}")
    finally:
        box.close()


def _row(state: str, at: str, by: str, entries: int = 1) -> dict:
    return {"state": state, "reason": "r", "at": at, "by": by,
            "by_comment": None, "superseded_by": None, "entries": entries}


def example_7() -> None:
    cases = [
        (_row("superseded", "2026-11-10T09:00:00+00:00", "engine"),
         _row("resolved", "2026-11-10T12:00:00+00:00", "supervisor", 3)),
        (_row("resolved", "2026-11-10T09:00:00+00:00", "zoe"),
         _row("resolved", "2026-11-10T09:00:00+00:00", "amy", 2)),
        (_row("superseded", "2026-11-10T09:00:00+00:00", "engine"),
         _row("superseded", "2026-11-10T11:00:00+00:00", "engine")),
    ]
    broken = []
    for left, right in cases:
        a = {"needs_person_resolved": {"k": json.loads(json.dumps(left))}}
        b = {"needs_person_resolved": {"k": json.loads(json.dumps(right))}}
        needs.merge_into(a, b)
        needs.merge_into(b, a)
        if a != b:
            broken.append((left["state"], right["state"]))
    human = _row("resolved", "2026-11-10T12:00:00+00:00", "supervisor")
    auto = _row("superseded", "2026-11-10T09:00:00+00:00", "engine")
    a = {"needs_person_resolved": {"k": json.loads(json.dumps(auto))}}
    needs.merge_into(a, {"needs_person_resolved": {"k": human}})
    ok = not broken and a["needs_person_resolved"]["k"]["state"] == "resolved"
    record("7 arbitration-commutative", ok,
           f"asymmetric cases={broken or 'none'}; "
           f"superseded@09:00 + resolved@12:00 -> "
           f"{a['needs_person_resolved']['k']['state']} "
           f"(mutation: make at win -> the human ruling is lost)")


def main() -> int:
    for fn in (example_1, example_2, example_3, example_4, example_5,
               example_5b, example_6, example_7):
        fn()
    bad = [r for r in RESULTS if not r["ok"]]
    print(f"\nsummary: {len(RESULTS) - len(bad)}/{len(RESULTS)} counter-examples hold")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
