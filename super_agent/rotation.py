"""Rotation records: derive what the system knows, ask for what it cannot.

Why this module exists (ISS-90)
------------------------------
A rotation (换血) is recorded as thirteen fields. Eight of them are **facts the
system already produced** -- who was rotated, from which session to which, when,
and whether the old one was archived. Asking a person to retype them invites
drift: the typed value and the event stream disagree, and the record becomes a
second, worse source of truth.

The other four are **decisions only a person can make**: why the rotation
happened, who ordered it, which handoff document it used, and whether the model
changed. Those are not in the event stream and cannot be inferred from it.

So the split is structural:

    collect()   nine fields, derived -- raises when the stream cannot answer
    compose()   + four fields, supplied -- raises when a person skipped one

`trigger_reason` and `ordered_by` are separate parameters on purpose. The
requirement "automatic because ctx hit the line" and "the user ordered it" are
different facts, and a schema that lets one column hold both cannot answer the
question the record exists for.

What this module does not do: it never writes. `render_markdown` produces the
body; the caller posts it through `issue_cli`, which owns the comment policy.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from . import session_events, sessionkeys

#: Values `trigger_reason` may take. A closed set: "why" is asked once and
#: answered once, and free text makes the field unqueryable.
TRIGGER_REASONS = ("ctx_threshold", "user_order", "model_change", "failure")

#: Values `ordered_by` may take.
ORDERED_BY = ("supervisor", "user")

SCHEMA_VERSION = 1


class RotationIncomplete(RuntimeError):
    """A field the record needs is missing, and guessing it would be wrong."""

    def __init__(self, message: str, *, missing: tuple = ()):
        super().__init__(message)
        self.missing = missing


def _events_path(state_root) -> Path:
    root = Path(state_root)
    return root / "events.jsonl"


def _parse_at(text) -> float | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _rotations(events: list[dict], key: str) -> list[dict]:
    return [e for e in events
            if e.get("type") == session_events.ROTATED
            and str(e.get("key") or "") == key]


def _latest_ctx(ledger_rows: list[dict], key: str, *, before: float | None,
                after: bool) -> str:
    """The nearest ``ctx-gate`` reading for ``key``, before or after ``before``.

    Returns the percentage as written (``"65.9%"``) or ``""`` when the ledger
    has no reading. ``""`` is honest: the caller can see the value is unknown
    rather than reading a fabricated ``0%``.
    """
    best: tuple[float, str] | None = None
    for row in ledger_rows:
        if str(row.get("act") or "") != "ctx-gate":
            continue
        note = str(row.get("note") or "")
        if f"key={key}" not in note:
            continue
        stamp = _parse_at(row.get("t"))
        if stamp is None or before is None:
            continue
        if after and stamp < before:
            continue
        if not after and stamp > before:
            continue
        pct = ""
        for part in note.split():
            if part.startswith("ctx=") and part.endswith("%"):
                pct = part[4:]
                break
        if not pct:
            continue
        if best is None or abs(stamp - before) < abs(best[0] - before):
            best = (stamp, pct)
    return best[1] if best else ""


def _ledger_rows(state_root) -> list[dict]:
    path = Path(state_root) / "ledger.jsonl"
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except OSError:
        return []
    return rows


def collect(key: str, *, state_root) -> dict:
    """The nine derivable fields for the latest rotation of ``key``.

    Raises :class:`RotationIncomplete` when the event stream holds no rotation
    for this key: an empty record that looks complete is worse than a refusal,
    because it would be posted and read as fact.
    """
    events = session_events.load_events(_events_path(state_root))
    rotations = _rotations(events, key)
    if not rotations:
        raise RotationIncomplete(
            f"事件流里没有 {key!r} 的 session.rotated（换血未发生或事件未落）",
            missing=("rotation",))
    rot = rotations[-1]
    from_sid = str(rot.get("from_sid") or "")
    to_sid = str(rot.get("to_sid") or rot.get("sid") or "")
    at = str(rot.get("at") or "")
    stamp = _parse_at(at)

    # Archived: a later session.archived for the old sid. The stream is ordered
    # by seq, so "later" is "after this record in the list", not "bigger at".
    idx = events.index(rot)
    archived = any(
        e.get("type") == session_events.ARCHIVED
        and str(e.get("sid") or "") == from_sid
        for e in events[idx + 1:])

    # Created: the session.created that produced the new sid, if the stream
    # still holds it. A rotation recorded by hand has no such line.
    created = any(
        e.get("type") == session_events.CREATED
        and str(e.get("sid") or "") == to_sid
        for e in events[:idx])

    rows = _ledger_rows(state_root)
    return {
        "schema_version": SCHEMA_VERSION,
        "rotation_id": f"{key}-{_stamp_text(at)}",
        "role": str(rot.get("role") or sessionkeys.role_of(key)),
        "old_session": from_sid,
        "old_session_archived": archived,
        "old_ctx": _latest_ctx(rows, key, before=stamp, after=False),
        "new_session": to_sid,
        "new_session_created_by": "dispatch" if created else "",
        "new_ctx": _latest_ctx(rows, key, before=stamp, after=True),
        "ordered_at": at,
    }


def _stamp_text(at: str) -> str:
    stamp = _parse_at(at)
    if stamp is None:
        return "unknown"
    return datetime.fromtimestamp(stamp).strftime("%Y%m%d-%H%M")


def compose(key: str, *, state_root, trigger_reason: str, ordered_by: str,
            handoff_doc: str, model_change=None, ordered_at: str = "") -> dict:
    """The full thirteen fields. The four a person supplies are **required**.

    A missing one raises rather than defaulting. ``trigger_reason=""`` would
    read as "unknown cause", which is a different statement from "nobody said",
    and the record exists to answer exactly that question.
    """
    missing: list[str] = []
    if trigger_reason not in TRIGGER_REASONS:
        missing.append("trigger_reason")
    if ordered_by not in ORDERED_BY:
        missing.append("ordered_by")
    if not str(handoff_doc or "").strip():
        missing.append("handoff_doc")
    if missing:
        raise RotationIncomplete(
            "换血记录缺人填项：" + "、".join(missing)
            + f"（trigger_reason ∈ {TRIGGER_REASONS}；"
              f"ordered_by ∈ {ORDERED_BY}）",
            missing=tuple(missing))
    rec = collect(key, state_root=state_root)
    rec.update({
        "trigger_reason": trigger_reason,
        "ordered_by": ordered_by,
        "handoff_doc": str(handoff_doc).strip(),
        "model_change": model_change,
    })
    if ordered_at:
        rec["ordered_at"] = ordered_at
    return rec


def self_check(key: str, *, state_root, journal_feed_after: float = 0.0) -> list[str]:
    """The four things that go wrong in a rotation. Empty list means clean.

    Each one is a defect that has actually happened (#90 §七):

    * a date-shaped tail -- the whitelist rejects it, so the key is not a
      credential (the fourteenth PM fell out this way);
    * the old key left behind -- a zombie pointing at an archived session;
    * the old session never archived -- it keeps running;
    * the old session still being fed after archiving -- the "archived" flag
      alone is not proof.
    """
    from . import sessions_ledger as ledger_mod

    problems: list[str] = []
    rec = collect(key, state_root=state_root)

    if not sessionkeys.has_random_suffix(key):
        problems.append(f"键名没有随机尾：{key!r}（白名单会拒，日期尾尤其）")

    table = ledger_mod.load(state_root)
    if key in table:
        sid = (table.get(key) or {}).get("id") if isinstance(table.get(key), dict) else table.get(key)
        if str(sid or "") == rec["old_session"]:
            problems.append(f"旧键仍在 sessions.json 指向旧会话：{key} -> {sid}")

    if not rec["old_session_archived"]:
        problems.append(f"旧会话未归档：{rec['old_session']}")

    if journal_feed_after:
        feeds = journal_sends(rec["old_session"], since=journal_feed_after)
        if feeds:
            problems.append(
                f"归档后旧会话仍收到 {len(feeds)} 条投喂"
                f"（首条 {feeds[0].get('at_str') or feeds[0].get('at')}）")
    return problems


def journal_sends(sid: str, *, since: float = 0.0, port: int | None = None) -> list[dict]:
    """Ask the daemon whether ``sid`` was fed after ``since``.

    Goes through the daemon's ``/journal`` endpoint rather than reading
    ``state/wss_bus_journal.jsonl``. The file belongs to whichever workbench
    started the daemon; the endpoint belongs to the daemon, and a rotation check
    must not depend on a path (ISS-90 §4).

    Returns ``[]`` when the daemon is unreachable: "could not ask" and "nothing
    found" are different answers, and this function cannot tell them apart. The
    caller decides -- `self_check` only calls it with an explicit ``since``.
    """
    import urllib.parse
    import urllib.request

    from . import bus

    query = {"sid": sid, "verb": "send", "limit": "500"}
    if since:
        query["since"] = f"{since:.3f}"
    url = f"http://127.0.0.1:{port or bus._port()}/journal?{urllib.parse.urlencode(query)}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001  daemon down is a caller question
        return []
    if not doc.get("ok"):
        return []
    return list(doc.get("records") or [])


def render_markdown(rec: dict) -> str:
    """The comment body: a JSON block plus the four facts a person reads.

    The JSON block is the machine copy; the summary exists because a rotation
    record is read by someone answering "why did this happen", and they should
    not have to parse JSON to find out.
    """
    body = [
        f"### 换血记录 {rec.get('rotation_id') or ''}",
        "",
        f"- 角色：`{rec.get('role') or '?'}`",
        f"- 旧会话：`{rec.get('old_session') or '?'}`"
        f"（已归档：{'是' if rec.get('old_session_archived') else '否'}）",
        f"- 新会话：`{rec.get('new_session') or '?'}`"
        f"（由 {rec.get('new_session_created_by') or '未知方式'} 创建）",
        f"- 占用：{rec.get('old_ctx') or '?'} → {rec.get('new_ctx') or '?'}",
        f"- 原因：`{rec.get('trigger_reason')}`　下令者：`{rec.get('ordered_by')}`",
        f"- 下令时刻：{rec.get('ordered_at') or '?'}",
        f"- 交接档：`{rec.get('handoff_doc') or '?'}`",
    ]
    if rec.get("model_change"):
        change = rec["model_change"]
        body.append(f"- 模型变更：`{change.get('from')}` → `{change.get('to')}`")
    body += ["", "```json",
             json.dumps(rec, ensure_ascii=False, indent=2, sort_keys=True),
             "```"]
    return "\n".join(body)


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    """``python -m super_agent.rotation {show,record,check} --key <键>``.

    ``show``   print the derivable nine fields (no write)
    ``check``  the four self-checks; rc=1 when any fails
    ``record`` compose and print the comment body (posting is the caller's job:
               `issue_cli` owns comment policy and the five-part title)
    """
    import argparse

    ap = argparse.ArgumentParser(description="换血记录：9 项自动推导 ＋ 4 项人填")
    ap.add_argument("cmd", choices=("show", "record", "check"))
    ap.add_argument("--key", required=True, help="会话键名，如 scout 或 pm-<项目>-<尾>")
    ap.add_argument("--state-root", default="",
                    help="运行态根（events.jsonl / ledger.jsonl 所在）")
    ap.add_argument("--trigger-reason", default="", choices=("",) + TRIGGER_REASONS)
    ap.add_argument("--ordered-by", default="", choices=("",) + ORDERED_BY)
    ap.add_argument("--handoff-doc", default="")
    ap.add_argument("--model-from", default="")
    ap.add_argument("--model-to", default="")
    ap.add_argument("--feed-since", type=float, default=0.0,
                    help="零投喂判据的起点（epoch 秒）；0 = 跳过该项")
    args = ap.parse_args(argv)

    from . import sess_events

    root = args.state_root or str(sess_events.resolve_root())
    try:
        if args.cmd == "show":
            print(json.dumps(collect(args.key, state_root=root),
                             ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "check":
            problems = self_check(args.key, state_root=root,
                                  journal_feed_after=args.feed_since)
            if problems:
                for p in problems:
                    print(f"✗ {p}")
                return 1
            print(f"✓ {args.key} 换血自检通过")
            return 0
        change = ({"from": args.model_from, "to": args.model_to}
                  if args.model_from or args.model_to else None)
        rec = compose(args.key, state_root=root,
                      trigger_reason=args.trigger_reason,
                      ordered_by=args.ordered_by,
                      handoff_doc=args.handoff_doc, model_change=change)
        print(render_markdown(rec))
        return 0
    except RotationIncomplete as exc:
        print(f"[rotation] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
