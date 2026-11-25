"""Report file size, function size and missing module docstrings.

Why this exists (user ruling 2026-11-25)
----------------------------------------
The repository had a rule about **where** code goes and a rule about **how big**
a file may get, but nothing that measured either one. ``engine.py`` grew from
599 to 5787 lines over 76 commits, none of which added 5000 lines -- each added
a few dozen. A rule with no measurement is a wish.

This tool is the measurement. It is a **report**, not a refactor: it never
changes code. Its job is to make the numbers visible in a delivery so a reviewer
can see whether a change made the debt bigger.

Thresholds and their sources
----------------------------
* **750 lines per file** -- SonarQube S104's default ("refactor the file into
  smaller files whose code focuses on well-defined tasks"). The repository's own
  hard line is 300, set in ``rules/shared/skills/engine_incremental_architecture.md``;
  750 is the industry alarm level, so a file over it is a fact nobody can argue
  with.
* **40 lines per function** -- Google Python Style Guide §3.18: "If a function
  exceeds about 40 lines, think about whether it can be broken up."
* **module docstring required** -- Google Python Style Guide §3.8.2.

Why the report separates "already over" from "over and growing"
--------------------------------------------------------------
The rule is not "no file may exceed 750 lines" -- that would demand a rewrite
nobody asked for and would freeze every large file. The rule is "do not make it
worse". So the exit code tracks **growth**, and the baseline is recorded in
``BASELINE`` below, which a change must lower, never raise.

Exit codes: ``0`` = nothing over budget and growing; ``1`` = at least one file
grew past its baseline or a new violation appeared; ``2`` = could not read the
input (a check that did not run must never look clean).

Usage::

    python tools/module_size.py                # full report
    python tools/module_size.py --files        # only files over 750 lines
    python tools/module_size.py --docstrings   # only missing module docstrings
    python tools/module_size.py --json         # machine-readable
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "super_agent"

#: SonarQube S104's default. Not this repository's target -- see the docstring.
FILE_ALARM = 750
#: Google Python Style Guide §3.18.
FUNC_ALARM = 40

#: How many lines an existing large file may grow before it is a failure.
#: Not zero: an ordinary edit adds a few lines, and forbidding that forbids the
#: cleanup the rule asks for -- nobody can refactor a file they are not allowed
#: to touch. Large enough for a real fix, small enough that "just add it here"
#: still gets noticed.
GROWTH_ALLOWANCE = 30

#: Files that were already over ``FILE_ALARM`` when this tool was written, with
#: the size measured then. **Historical debt, not a gate.** See
#: :func:`over_budget` for why these never fail on size alone.
BASELINE = {
    "engine.py": 5787,
    "wss_bus.py": 4289,
    "decide_watch.py": 4237,
    "dispatch_cli.py": 1537,
    "console.py": 1436,
    "cli.py": 1385,
    "reporter.py": 1350,
    "markers.py": 1324,
    "issue_cli.py": 1284,
    "direct.py": 1141,
    "autostart.py": 933,
    "assignments.py": 932,
    "store.py": 821,
}


def _parse(path: Path):
    """The module AST, or ``None`` when it does not parse.

    A syntax error is reported, not raised: one unparsable file must not stop
    the other sixty from being measured, and it must not read as "fine".
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def scan_file(path: Path) -> dict:
    """Size facts for one module. ``error`` is set when it could not be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"path": path.name, "error": str(exc)}
    tree = _parse(path)
    if tree is None:
        return {"path": path.name, "error": "does not parse"}
    funcs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            funcs.append({"name": node.name, "line": node.lineno, "lines": length})
    funcs.sort(key=lambda f: f["lines"], reverse=True)
    return {
        "path": path.name,
        "lines": len(text.splitlines()),
        "docstring": bool(ast.get_docstring(tree)),
        "functions": len(funcs),
        "long_functions": [f for f in funcs if f["lines"] > FUNC_ALARM],
    }


def scan(root: Path = SOURCE_DIR) -> list[dict]:
    if not root.is_dir():
        return []
    return [scan_file(p) for p in sorted(root.glob("*.py"))]


def over_budget(rows: list[dict]) -> list[dict]:
    """Files whose size is a **violation**, i.e. what the exit code refuses.

    The rule this encodes (user ruling 2026-11-25): **a file that was already
    over the line is never a failure for being big.** Blocking those would stop
    work on exactly the code that needs work, and the first thing a blocked
    person does is route around the rule -- a second implementation beside the
    old one, which is worse than the debt. ``worker.md`` §3.1.1 states the same
    thing from the other side: refusing to edit a large file is a P0, because it
    makes the debt permanent.

    So only two things fail:

    * **a file that was under the line and went over it** -- a new problem,
      introduced by this change;
    * **an existing large file that grew by more than ``GROWTH_ALLOWANCE``** --
      a tolerance, not a ban: ordinary edits add a few lines, and a rule that
      forbids that forbids the cleanup it is asking for. The target is
      ``engine_incremental_architecture.md`` §十一's "net -10% per change", but
      that is a goal reported by the tool, not a gate it enforces.

    Everything else is reported and exits 0.
    """
    out = []
    for row in rows:
        if "error" in row:
            continue
        name, lines = row["path"], row["lines"]
        base = BASELINE.get(name)
        if base is None:
            if lines > FILE_ALARM:
                out.append({**row, "baseline": None,
                            "reason": f"new file over {FILE_ALARM}"})
        elif lines > base + GROWTH_ALLOWANCE:
            out.append({**row, "baseline": base,
                        "reason": f"grew {lines - base} lines past {base}"
                                  f" (allowance {GROWTH_ALLOWANCE})"})
    return out


def cleanup_status(rows: list[dict]) -> list[dict]:
    """Existing large files and whether a change shrank them.

    Reported, never failed. This is the "边改边打扫" signal: a delivery that
    touches ``engine.py`` should show a smaller number than the baseline, and
    the reviewer can see at a glance whether it did.
    """
    out = []
    for row in rows:
        if "error" in row:
            continue
        base = BASELINE.get(row["path"])
        if base is None:
            continue
        delta = row["lines"] - base
        out.append({**row, "baseline": base, "delta": delta,
                    "cleaned": delta < 0})
    return out


def missing_docstrings(rows: list[dict]) -> list[str]:
    return [r["path"] for r in rows if "error" not in r and not r["docstring"]]


def render(rows: list[dict], as_json: bool = False) -> str:
    if as_json:
        return json.dumps({"files": rows, "alarm": FILE_ALARM,
                           "function_alarm": FUNC_ALARM}, ensure_ascii=False, indent=1)
    lines = []
    over = over_budget(rows)
    debt = cleanup_status(rows)
    errors = [r for r in rows if "error" in r]
    lines.append(f"扫描 {len(rows)} 个模块；单文件告警线 {FILE_ALARM} 行，"
                 f"函数告警线 {FUNC_ALARM} 行")
    lines.append("")
    if over:
        lines.append(f"判据失败（{len(over)}）:")
        for row in over:
            base = row["baseline"]
            tag = f"基线 {base}" if base is not None else "无基线"
            lines.append(f"  {row['lines']:6d}  {row['path']:22s} {tag}  {row['reason']}")
    else:
        lines.append("判据失败: 无")
    if debt:
        cleaned = [d for d in debt if d["cleaned"]]
        lines.append("")
        lines.append(f"历史超线（{len(debt)}）— 不判失败；改它时目标是净降:")
        for row in sorted(debt, key=lambda d: d["lines"], reverse=True):
            mark = f"净降 {-row['delta']}" if row["cleaned"] else (
                f"+{row['delta']}" if row["delta"] else "未变")
            lines.append(f"  {row['lines']:6d}  {row['path']:22s} 基线 {row['baseline']:5d}  {mark}")
        if cleaned:
            lines.append(f"  本次已打扫 {len(cleaned)} 个")
    long_funcs = [(f["lines"], r["path"], f["name"], f["line"])
                  for r in rows if "error" not in r for f in r["long_functions"]]
    long_funcs.sort(reverse=True)
    lines.append("")
    lines.append(f"超 {FUNC_ALARM} 行的函数: {len(long_funcs)} 个"
                 + (f"（最长 {long_funcs[0][0]} 行）" if long_funcs else ""))
    for n, path, name, line in long_funcs[:5]:
        lines.append(f"  {n:4d}  {path}:{line}  {name}()")
    missing = missing_docstrings(rows)
    lines.append("")
    lines.append(f"缺模块 docstring: {len(missing)} 个"
                 + (f" {missing}" if missing else ""))
    if errors:
        lines.append("")
        lines.append(f"读不到（{len(errors)}）:")
        for row in errors:
            lines.append(f"  {row['path']}: {row['error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, root: Path = SOURCE_DIR) -> int:
    parser = argparse.ArgumentParser(description="Report file/function size and docstrings.")
    parser.add_argument("--files", action="store_true", help="只列超预算文件")
    parser.add_argument("--docstrings", action="store_true", help="只列缺 docstring 的文件")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    rows = scan(root)
    if not rows:
        # A scan that did not run must never look clean.
        print(f"[module_size] 扫不到任何模块：{root}", file=sys.stderr)
        return 2

    if args.files:
        over = over_budget(rows)
        for row in over:
            print(f"{row['lines']:6d}  {row['path']}  {row['reason']}")
        return 0 if not over else 1
    if args.docstrings:
        missing = missing_docstrings(rows)
        for name in missing:
            print(name)
        return 0 if not missing else 1

    print(render(rows, as_json=args.json))
    if any("error" in r for r in rows):
        return 2
    return 0 if not over_budget(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
