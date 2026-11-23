"""Deterministic PM routing for an approved AT requirement.

The PM supplies a packet and an event. This module never interprets the
objective, acceptance wording, or QA prose; those have already been reduced to
structured values by AT and QA.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import requirements

EVENTS = ("start", "plan_delivered", "plan_qa", "implementation_delivered",
          "implementation_qa", "requirement_changed")
VERDICTS = ("", "pass", "reject", "redirect", "requirement_ambiguous")


class RouteError(ValueError):
    pass


def next_route(packet: dict, event: str, verdict: str = "") -> dict:
    if event not in EVENTS:
        raise RouteError(f"event must be one of {EVENTS}")
    if verdict not in VERDICTS:
        raise RouteError(f"verdict must be one of {VERDICTS}")
    if event == "requirement_changed":
        return _route("hold", "at", "new", "waiting_requirement", packet,
                      "需求已变更；暂停旧 Assignment，等待新批准")
    if verdict == "requirement_ambiguous":
        return _route("return", "at", "current", "requirement_ambiguous", packet,
                      "需求歧义退回 AT；PM 不补答案")
    if verdict == "redirect":
        stage = "plan" if event == "plan_qa" else "implement"
        return _route("dispatch", "worker", "new", stage, packet,
                      "按原 QA redirect 意见新建 worker")
    if verdict == "reject":
        stage = "plan" if event == "plan_qa" else "implement"
        return _route("dispatch", "worker", "same", stage, packet,
                      "原样退回原 worker")
    policy = packet.get("review_policy") or {}
    if event == "start":
        if policy.get("worker_mode") == "plan_and_implement":
            return _route("dispatch", "worker", "new", "implement", packet,
                          "同会话先写短技术方案，再实现")
        return _route("dispatch", "worker", "new", "plan", packet,
                      "只写独立技术方案，不开发")
    if event == "plan_delivered":
        if policy.get("plan_qa"):
            return _route("dispatch", "qa", "new", "plan_qa", packet,
                          "独立方案 QA")
        return _route("dispatch", "worker", "new", "implement", packet,
                      "方案已交付，进入独立开发")
    if event == "plan_qa":
        if verdict != "pass":
            raise RouteError("plan_qa requires a structured verdict")
        return _route("dispatch", "worker", "new", "implement", packet,
                      "方案 QA 已通过，新建开发 worker")
    if event == "implementation_delivered":
        if policy.get("implementation_qa"):
            return _route("dispatch", "qa", "new", "implementation_qa", packet,
                          "独立实现 QA")
        return _route("release", "supervisor", "current", "release", packet,
                      "按策略无需实现 QA，交 supervisor")
    if event == "implementation_qa":
        if verdict != "pass":
            raise RouteError("implementation_qa requires a structured verdict")
        return _route("release", "supervisor", "current", "release", packet,
                      "实现 QA 通过，交 supervisor 收口")
    raise RouteError("no route for this event")


def _route(action, role, session_mode, stage, packet, instruction):
    return {
        "action": action, "target_role": role, "session": session_mode,
        "stage": stage, "requirement_id": packet.get("requirement_id"),
        "requirement_ref": packet.get("requirement_ref"),
        "revision": packet.get("revision"),
        "requirement_hash": packet.get("requirement_hash"),
        "approval_ref": packet.get("approval_ref"),
        "assignment": packet.get("assignment"),
        "requirement_marker": requirements.reference_marker(packet),
        "task": (f"{packet.get('requirement_id')} {packet.get('requirement_ref')} "
                 f"r{packet.get('revision')}：{instruction}。只读该需求包及引用；"
                 f"交付/QA 评论带 {requirements.reference_marker(packet)}"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m super_agent.pm_router")
    parser.add_argument("--packet", required=True)
    parser.add_argument("--event", required=True, choices=EVENTS)
    parser.add_argument("--verdict", default="", choices=VERDICTS)
    parser.add_argument("--task-output", default="",
                        help="write the exact route task text for comment/dispatch reuse")
    args = parser.parse_args(argv)
    try:
        packet = json.loads(Path(args.packet).read_text(encoding="utf-8"))
        route = next_route(packet, args.event, args.verdict)
        if args.task_output:
            Path(args.task_output).write_text(route["task"] + "\n", encoding="utf-8")
    except (OSError, ValueError, RouteError) as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(route, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
