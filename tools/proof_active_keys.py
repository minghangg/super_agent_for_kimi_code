"""QA P1-1 self-proof: does replacing ``active_keys`` really change the answer?

Run from the repo root:

    python tools/proof_active_keys.py
    python tools/proof_active_keys.py --events <path/to/events.jsonl>

It never edits a tracked file. BEFORE is the pre-change copy kept under
``backup/super_agent/``; AFTER is the same file with ``active_keys`` swapped for
the fixed body. Both are imported from a temporary directory and the loaded
function bodies are hashed, so a silent no-op patch shows up as
``BEFORE_HASH == AFTER_HASH`` instead of a plausible-looking number.

Judgement (both must hold, no hard-coded key counts - the live stream grows):

    BEFORE != AFTER        the replacement took effect on the answer
    lost == []             the fix never drops a key it used to return
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXED = '''
def active_keys(state: dict) -> dict:
    """Live keys: key map UNION session-level bindings, minus released/archived."""
    sessions = state.get("sessions") or {}
    released = set(state.get("released") or ())
    out = {}
    for key, sid in (state.get("keys") or {}).items():
        rec = sessions.get(sid) or {}
        if rec.get("archived"):
            continue
        out[key] = sid
    for sid, rec in sessions.items():
        key = str(rec.get("key") or "")
        if not key or key in out or key in released or rec.get("archived"):
            continue
        if rec.get("name_only"):
            continue
        out[key] = sid
    return out
'''


def _function_text(source: str, name: str) -> str:
    start = source.index(f"def {name}(")
    match = re.search(r"\ndef ", source[start:])
    end = start + match.start() + 1 if match else len(source)
    return source[start:end].strip()


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", default="", help="events.jsonl to replay (default: resolved)")
    ap.add_argument("--baseline", default="",
                    help="pre-change copy (default: newest backup/super_agent/session_events_*.py)")
    args = ap.parse_args(argv)

    from super_agent import session_events as live  # noqa: E402  (after sys.path)

    base_path = Path(args.baseline) if args.baseline else sorted(
        (ROOT / "backup" / "super_agent").glob("session_events_*.py"))[-1]
    if not base_path.is_file():
        print(f"missing pre-change copy: {base_path}", file=sys.stderr)
        return 2
    source = base_path.read_text(encoding="utf-8")
    if "released" in _function_text(source, "active_keys"):
        print(f"NOT A BASELINE: {base_path} already carries the fix", file=sys.stderr)
        return 2

    start = source.index("def active_keys(")
    tail = re.search(r"\ndef ", source[start:])
    end = start + tail.start() + 1 if tail else len(source)
    patched = source[:start] + FIXED.lstrip("\n") + "\n\n" + source[end:]
    assert patched != source, "the patch changed nothing"

    tmp = Path(tempfile.mkdtemp(prefix="active_keys_proof_"))
    base_copy = tmp / "baseline.py"
    base_copy.write_text(source, encoding="utf-8")
    fixed_copy = tmp / "fixed.py"
    fixed_copy.write_text(patched, encoding="utf-8")

    baseline = _load(base_copy, "proof_baseline")
    fixed = _load(fixed_copy, "proof_fixed")
    before_hash = _hash(inspect.getsource(baseline.active_keys).strip())
    after_hash = _hash(inspect.getsource(fixed.active_keys).strip())

    events_path = Path(args.events) if args.events else live.resolve_events_path()
    events = live.load_events(events_path)
    rows = live.replay(events)          # replay is identical in both copies
    import copy
    before = baseline.active_keys(copy.deepcopy(rows))
    after = fixed.active_keys(copy.deepcopy(rows))
    lost = sorted(set(before) - set(after))

    print(f"events      : {events_path} ({len(events)} rows, read now)")
    print(f"baseline    : {base_path.relative_to(ROOT).as_posix()}")
    print(f"active_keys : BEFORE={before_hash[:16]} AFTER={after_hash[:16]} "
          f"replaced={before_hash != after_hash}")
    print(f"BEFORE      : {len(before)} keys")
    print(f"AFTER       : {len(after)} keys")
    for key in ("qa-lwa-ISS-20",):
        if key in before or key in after:
            print(f"{key}: {key in before} -> {key in after}")
    print(f"lost        : {lost}")
    ok = (before_hash != after_hash) and not lost and before != after
    print(f"verdict     : {'PASS' if ok else 'FAIL'} "
          f"(needs BEFORE != AFTER and lost == [])")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
