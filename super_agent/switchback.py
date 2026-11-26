"""Audit a launcher/legacy switch snapshot before declaring it complete.

Port values are snapshot claims, not an ownership probe; a foreign process on a
port must be rejected by the PowerShell preflight, not inferred from this JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LAUNCHER_TASK = "KimiSafkcLauncher"
LEGACY_TASKS = ("KimiWssBus", "KimiDecideWatch")
LEGACY_ROLES = ("engine", "at", "wss", "decide_watch")


def process_role(command: str) -> str:
    text = str(command or "").lower()
    if "super_agent.launcher" in text and " run" in text:
        return "launcher"
    if "super_agent.cli run" in text:
        return "engine"
    if "super_agent.at serve" in text:
        return "at"
    if "wss_bus" in text:
        return "wss"
    if "decide_watch" in text:
        return "decide_watch"
    return ""


def audit(snapshot: dict, expected: str) -> list[str]:
    roles = []
    errors = []
    for row in snapshot.get("processes") or []:
        command = str(row.get("command") or "")
        role = process_role(command)
        declared = str(row.get("role") or "")
        if declared and declared != role:
            errors.append(f"role field disagrees with command: {declared} != {role or 'unknown'}")
        if role:
            roles.append((role, int(row.get("pid") or 0), command))
        if role and "pythonw" not in command.lower():
            errors.append(f"console window risk: {role} is not pythonw")
    counts = {role: sum(1 for item, _pid, _cmd in roles if item == role)
              for role in {item for item, _pid, _cmd in roles}}
    for role, count in sorted(counts.items()):
        if count != 1:
            errors.append(f"duplicate daemon: {role} count={count}")

    tasks = {str(row.get("name")): bool(row.get("enabled"))
             for row in snapshot.get("tasks") or []}
    lock_pid = int((snapshot.get("engine_lock") or {}).get("pid") or 0)
    if expected == "launcher":
        if counts.get("launcher") != 1:
            errors.append("expected exactly one launcher process")
        for role in LEGACY_ROLES:
            if counts.get(role):
                errors.append(f"old writer still running: {role}")
        enabled = [name for name, on in tasks.items() if on]
        if enabled != [LAUNCHER_TASK]:
            errors.append(f"launcher task set must be [{LAUNCHER_TASK}], got {enabled}")
        launcher_pid = next((pid for role, pid, _cmd in roles if role == "launcher"), 0)
        if not lock_pid or lock_pid != launcher_pid:
            errors.append(f"engine lock does not belong to launcher: {lock_pid}")
    elif expected == "legacy":
        if counts.get("launcher"):
            errors.append("rollback left the launcher running")
        for role in LEGACY_ROLES:
            if counts.get(role) != 1:
                errors.append(f"rollback missing {role}")
        enabled = sorted(name for name, on in tasks.items() if on)
        if enabled != sorted(LEGACY_TASKS):
            errors.append(f"legacy task set must be {sorted(LEGACY_TASKS)}, got {enabled}")
        engine_pid = next((pid for role, pid, _cmd in roles if role == "engine"), 0)
        if not lock_pid or lock_pid != engine_pid:
            errors.append(f"engine lock does not belong to legacy engine: {lock_pid}")
    else:
        errors.append(f"unknown expected mode: {expected}")
    for port in (58800, 58731):
        if not (snapshot.get("ports") or {}).get(str(port)):
            errors.append(f"required port is not listening: {port}")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect", choices=("launcher", "legacy"), required=True)
    parser.add_argument("--snapshot", required=True)
    args = parser.parse_args(argv)
    data = json.loads(Path(args.snapshot).read_text(encoding="utf-8-sig"))
    errors = audit(data, args.expect)
    print(json.dumps({"ok": not errors, "expected": args.expect, "errors": errors},
                     ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
