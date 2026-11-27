"""``cli resolve``: dismiss one ``needs_person`` entry under every key form.

Split out of ``cli.py`` for the reason ``worker.md`` §3.1 gives: the command is
one closed piece of logic that only needs ``(args, cfg, store)``, so it can live
in its own module instead of adding another hundred lines to the CLI host. The
flag surface still lives in ``cli.build_parser``; nothing here parses argv.

Exit codes differ from the rest of the CLI on purpose: **2 means "some keys were
not written"**, not "bad arguments" (argparse still uses 2 for that, and it
prints ``usage:``). 0 = at least one key newly resolved, 1 = nothing left to do
or nothing matched.
"""
from __future__ import annotations

import sys

from . import issuekey
from . import needs as needs_mod
from . import session_events


def resolve_keys(index: dict, number: int) -> list:
    """Every issue key that carries this issue number, legacy and qualified."""
    return [k for k in sorted((index.get("issues") or {}), key=str)
            if issuekey.issue_number(k) == number]


def resolve_rows(index: dict, keys, cid: int) -> dict:
    """``{key: [entries with this cid]}`` - only the keys that actually match."""
    out = {}
    for key in keys:
        rows = [entry for entry in
                ((index.get("issues") or {}).get(key) or {}).get("needs_person") or []
                if needs_mod.cid_of(entry) == cid]
        if rows:
            out[key] = rows
    return out


def run(args, cfg: dict, store) -> int:
    """Dismiss one entry.

    The index that gets saved is the one re-read immediately before the save,
    and the only thing added to it is the side-table row: ``issues``,
    ``cursor`` and ``comments_seen`` are never rewritten by this command.
    """
    cid = int(args.comment)
    state = getattr(args, "state", None) or needs_mod.RESOLVED
    number = issuekey.issue_number(getattr(args, "issue", ""))
    if number is None:
        print(f"resolve: cannot read an issue number from {args.issue!r}", file=sys.stderr)
        return 1

    disk0 = store.load_index()
    keys0 = resolve_keys(disk0, number)
    if not keys0:
        known = ", ".join(sorted((disk0.get("issues") or {}), key=str)) or "(none)"
        print(f"resolve: no issue #{number} in the index; keys present: {known}",
              file=sys.stderr)
        return 1
    matched0 = resolve_rows(disk0, keys0, cid)
    if not matched0:
        known = sorted({needs_mod.cid_of(e) for k in keys0
                        for e in ((disk0.get("issues") or {}).get(k) or {})
                        .get("needs_person") or []})
        print(f"resolve: comment {cid} is not a needs_person entry of #{number}; "
              f"comment ids present: {known}", file=sys.stderr)
        return 1

    # Re-read right before the save: the file on disk is the only truth.
    disk1 = store.load_index()
    matched1 = resolve_rows(disk1, resolve_keys(disk1, number), cid)

    # W/A count KEYS, M counts ROWS - the same number in one line means two
    # different things here, so never mix them up.
    #
    # The planned/already split is decided by ``needs.mark`` itself, on a
    # scratch copy: "is this key dismissed" must have exactly one definition,
    # otherwise a second dismissal could rewrite the audit row that ``mark``
    # is supposed to protect.
    #
    # Only real rows go into the copy. A damaged value is not a ruling (it reads
    # as open, see ``needs.is_open``), so it must not block the dismissal - and
    # copying it with ``dict()`` is what used to raise on a truncated write.
    existing = needs_mod.marks(disk1)
    scratch = {needs_mod.MARKS: {k: dict(v) for k, v in existing.items()
                                 if isinstance(v, dict)}}
    planned, already = [], []
    for key, _rows in matched0.items():
        count = len(matched1.get(key) or [])
        got = needs_mod.mark(
            scratch, key, cid, state=state, by=str(args.by or ""),
            reason=str((matched0.get(key) or [{}])[0].get("reason") or ""),
            by_comment=getattr(args, "comment_ref", None),
            superseded_by=(str(getattr(args, "note", "") or "")
                           if state == needs_mod.SUPERSEDED else None),
            entries=count)
        (planned if got["changed"] else already).append((key, count))
    side = {}
    for key, _count in planned:
        built = needs_mod.stored_row(scratch, key, cid)
        if built is not None:
            side[needs_mod.entry_key(key, cid)] = built

    written, failed, error = [], [], ""
    if side:
        try:
            needs_mod.merge_into_side(disk1, side)
            store.save_index(disk1)
        except OSError as exc:
            failed = [(key, count) for key, count in planned]   # one atomic save
            error = f"{type(exc).__name__}: {exc}"
        else:
            back = needs_mod.marks(store.load_index())
            for key, count in planned:
                (written if needs_mod.entry_key(key, cid) in back else failed).append(
                    (key, count))

    if disk1.get("issues") != disk0.get("issues"):
        store.log_event({"type": "needs_person.resolve_raced", "at": session_events.now_iso(),
                         "issue": number, "comment_id": cid,
                         "keys": [key for key, _n in planned],
                         "detail": "issues changed while resolving"})

    if failed:
        store.log_event({"type": "needs_person.resolve_failed", "at": session_events.now_iso(),
                         "issue": number, "comment_id": cid,
                         "keys": [key for key, _n in failed],
                         "error": error or "read-back did not find the side-table row"})
        print(f"resolve {cid}: {len(failed)} failed "
              f"(keys: {', '.join(k for k, _n in failed)}): "
              f"{error or 'row missing after save'}", file=sys.stderr)
        return 2

    if written:
        folded = sum(count for _key, count in written)
        stored = needs_mod.marks(store.load_index())
        first = stored.get(needs_mod.entry_key(written[0][0], cid))
        first = first if isinstance(first, dict) else {}
        store.log_event({"type": f"needs_person.{state}", "at": session_events.now_iso(),
                         "issue": number, "comment_id": cid,
                         "reason": first.get("reason") or "",
                         "by": str(args.by or ""),
                         "by_comment": first.get("by_comment"),
                         "superseded_by": first.get("superseded_by"),
                         "keys": [key for key, _n in written], "entries": folded,
                         "already": [key for key, _n in already],
                         "entries_skipped": sum(count for _key, count in already)})
        print(f"resolve {cid}: {len(written) + len(already)} keys: "
              f"{len(already)} already, {len(written)} newly {state} "
              f"({folded} entries folded)")
        return 0

    row = needs_mod.stored_row(disk1, already[0][0], cid) or {}
    print(f"already {state} {cid} under all {len(already)} keys "
          f"(at {row.get('at')}, by {row.get('by')})", file=sys.stderr)
    return 1
