"""CLI for AT requirement publication, approval and PM dispatch packets."""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from . import agent_config
from . import at_serve
from . import config as config_mod
from . import issue_cli
from . import requirements as req
from . import session_ledger


def _root(args) -> Path:
    if getattr(args, "state_dir", ""):
        return Path(args.state_dir).expanduser().resolve()
    cfg = config_mod.load(overrides=agent_config.overrides())
    return config_mod.state_root(cfg)


def _ctx(args):
    return issue_cli._Ctx(args)


def _comments(ctx, repo: str, issue: int) -> list[dict]:
    rows = []
    page = 1
    while page <= 100:
        batch = issue_cli._call(
            "GET", f"/repos/{repo}/issues/{int(issue)}/comments"
                   f"?per_page=100&page={page}", **ctx.fetch)
        if not isinstance(batch, list):
            raise req.RequirementError("GitHub comments response is not a list")
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return [{"id": row.get("id"), "body": row.get("body") or "",
             "author": (row.get("user") or {}).get("login", ""),
             "created_at": row.get("created_at") or ""}
            for row in rows if isinstance(row, dict) and row.get("id") is not None]


def _sync(args, ctx=None) -> tuple[dict, str]:
    ctx = ctx or _ctx(args)
    repo = issue_cli._repo(args.project, ctx.projects)
    projection = req.project_comments(_comments(ctx, repo, args.issue))
    path = req.save_projection(_root(args), projection)
    print(f"projection={path}")
    return projection, repo


def _rid(repo: str, issue: int) -> str:
    return f"{repo.lower()}#{int(issue)}"


def cmd_init(args) -> int:
    ctx = _ctx(args)
    repo = issue_cli._repo(args.project, ctx.projects)
    package = {
        "schema_version": req.SCHEMA_VERSION,
        "requirement_id": _rid(repo, args.issue), "revision": args.revision,
        "status": "clarifying", "objective": args.objective,
        "context_refs": [f"issue-{args.issue}"], "scope": [],
        "out_of_scope": [], "constraints": [], "acceptance": [],
        "negative_tests": [], "risk_tier": args.risk_tier,
        "review_policy": {
            "plan_qa": args.risk_tier == "T2", "implementation_qa": True,
            "worker_mode": "separate" if args.risk_tier == "T2" else "plan_and_implement",
            "test_level": "L1",
        },
        "open_questions": [], "approval_ref": "", "updated_at": req.now(),
    }
    package["requirement_hash"] = req.requirement_hash(package)
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(f"draft={output}")
    return 0


def cmd_validate(args) -> int:
    raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    package = req.validate_package(raw)
    print(json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_sync(args) -> int:
    _sync(args)
    return 0


def _question_link(args, ctx, repo: str, package: dict, comment_id: int) -> str:
    database = session_ledger.load_issue_sessions(ctx.cfg)
    binding_keys = [package["requirement_id"], f"{args.project}#{args.issue}",
                    f"{repo.rsplit('/', 1)[-1]}#{args.issue}"]
    binding = {}
    for key in binding_keys:
        candidate = database.get(key) or {}
        at = candidate.get("at") if isinstance(candidate, dict) else {}
        if isinstance(at, dict) and at.get("session"):
            binding = at
            break
    try:
        port = int((ctx.cfg.get("engine") or {}).get("at_question_port")
                   or at_serve.DEFAULT_PORT)
    except (TypeError, ValueError):
        port = at_serve.DEFAULT_PORT
    qid = at_serve.ensure_question(
        root=_root(args), thread=package["requirement_id"], issue=args.issue,
        comment_id=comment_id, sid=str(binding.get("session") or ""),
        question=str(package["open_questions"][0]), requirement=package,
        binding_keys=binding_keys)
    return at_serve.url(port, qid)


def cmd_publish(args) -> int:
    ctx = _ctx(args)
    role = issue_cli._role(args.role, ctx.roles)
    if role not in ("analyst", "at"):
        raise issue_cli.Denied("需求包只能由 AT/analyst 发布")
    issue_cli._check("comment", role, ctx.roles)
    if not args.session_key.startswith("at-"):
        raise issue_cli.Denied("AT 发布需求包必须带自己的 --session-key")
    repo = issue_cli._repo(args.project, ctx.projects)
    projection = req.project_comments(_comments(ctx, repo, args.issue))
    raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    raw["requirement_id"] = _rid(repo, args.issue)
    raw["status"] = "clarifying" if raw.get("open_questions") else "ready"
    raw["approval_ref"] = ""
    raw["updated_at"] = req.now()
    package = req.validate_package(raw)
    rid = package["requirement_id"]
    old = req.current(projection, rid)
    if old and int(old.get("revision") or 0) > package["revision"]:
        raise req.RequirementError("cannot publish an older requirement revision")
    if old and old.get("revision") == package["revision"]:
        if old.get("requirement_hash") != package["requirement_hash"]:
            raise req.RequirementError(
                "same revision has different semantics; increment revision")
        print(f"unchanged {rid} r{package['revision']} {old.get('source_ref')}")
        if package["open_questions"]:
            print("question_url=" + _question_link(
                args, ctx, repo, package, int(old["source_comment_id"])))
        req.save_projection(_root(args), projection)
        return 0
    if package["open_questions"]:
        state, label = "questions", "有疑问"
        question = args.question or package["open_questions"][0]
    else:
        state, label = "ready", "可派单"
        question = ""
    summary = args.summary or package["objective"]
    body = (f"{issue_cli._prefix(role, None, ctx.roles)} {label} "
            f"#{args.issue} <!-- verdict: {state} -->\n"
            f"{summary}\n\n{req.fence(req.PACKAGE_FENCE, package)}")
    if question:
        body += f"\n\n本轮只问一件事：{question}"
    body += f"\n<!-- session: {args.session_key} -->"
    out = issue_cli._call("POST", f"/repos/{repo}/issues/{args.issue}/comments",
                          {"body": body}, dry_run=args.dry_run, **ctx.call)
    if out.get("dry_run"):
        return 0
    print(f"published {rid} r{package['revision']} "
          f"hash={package['requirement_hash']} cid={out.get('id')}")
    if package["open_questions"]:
        print("question_url=" + _question_link(
            args, ctx, repo, package, int(out["id"])))
    issue_cli._ledger(role, "comment", repo, args.issue, ctx.cfg,
                      note=f"requirement:r{package['revision']};cid:{out.get('id')}")
    _sync(args, ctx)
    return 0


def cmd_approve(args) -> int:
    ctx = _ctx(args)
    role = issue_cli._role(args.role, ctx.roles)
    if role != "supervisor":
        raise issue_cli.Denied("需求批准只能由 supervisor 执行")
    issue_cli._check("comment", role, ctx.roles)
    repo = issue_cli._repo(args.project, ctx.projects)
    projection = req.project_comments(_comments(ctx, repo, args.issue))
    rid = _rid(repo, args.issue)
    package = req.current(projection, rid)
    if package is None:
        raise req.RequirementError("no readable requirement package")
    if package.get("status") != "ready":
        raise req.RequirementError(f"requirement status is {package.get('status')}, not ready")
    if args.revision and package.get("revision") != args.revision:
        raise req.RequirementError("the current revision differs from --revision")
    if args.hash and package.get("requirement_hash") != args.hash:
        raise req.RequirementError("the current hash differs from --hash")
    approval = {
        "requirement_id": rid, "revision": package["revision"],
        "requirement_hash": package["requirement_hash"],
        "package_ref": package["source_ref"], "approved_at": req.now(),
    }
    body = (f"{ctx.roles.brackets(role)} 批准需求 #{args.issue} "
            f"r{package['revision']}\n{req.fence(req.APPROVAL_FENCE, approval)}")
    out = issue_cli._call("POST", f"/repos/{repo}/issues/{args.issue}/comments",
                          {"body": body}, dry_run=args.dry_run, **ctx.call)
    if out.get("dry_run"):
        return 0
    print(f"approved {rid} r{package['revision']} approval=issuecomment-{out.get('id')}")
    issue_cli._ledger(role, "comment", repo, args.issue, ctx.cfg,
                      note=f"requirement_approval:r{package['revision']};cid:{out.get('id')}")
    _sync(args, ctx)
    return 0


def _local_or_sync(args) -> dict:
    if getattr(args, "sync", False):
        projection, _repo_name = _sync(args)
        return projection
    return req.load_projection(_root(args))


def cmd_show(args) -> int:
    projection = _local_or_sync(args)
    ctx = _ctx(args)
    repo = issue_cli._repo(args.project, ctx.projects)
    package = req.current(projection, _rid(repo, args.issue))
    if package is None:
        print("(no requirement)")
        return 1
    print(json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_packet(args) -> int:
    projection = _local_or_sync(args)
    ctx = _ctx(args)
    repo = issue_cli._repo(args.project, ctx.projects)
    packet = req.dispatch_packet(projection, _rid(repo, args.issue), args.assignment,
                                 role=args.dispatch_role, profile=args.profile)
    text = json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(f"packet={output}")
    else:
        print(text)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m super_agent.requirements")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, *, role=False):
        p.add_argument("--project", required=True)
        p.add_argument("--issue", type=int, required=True)
        p.add_argument("--state-dir", default="")
        p.add_argument("--projects-file")
        p.add_argument("--roles-file")
        p.add_argument("--env-file")
        p.add_argument("--allowlist")
        p.add_argument("--config")
        p.add_argument("--dry-run", action="store_true")
        if role:
            p.add_argument("--role", required=True)

    p = sub.add_parser("init", help="create a local requirement draft")
    common(p)
    p.add_argument("--objective", required=True)
    p.add_argument("--revision", type=int, default=1)
    p.add_argument("--risk-tier", choices=req.RISK_TIERS, default="T1")
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("validate", help="validate and normalize a local package")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("sync", help="rebuild the local projection from GitHub")
    common(p)
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("publish", help="AT: publish READY or questions to the Issue")
    common(p, role=True)
    p.add_argument("--file", required=True)
    p.add_argument("--session-key", required=True)
    p.add_argument("--worker-name", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--question", default="")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("approve", help="supervisor: approve the exact current hash")
    common(p, role=True)
    p.add_argument("--revision", type=int, default=0)
    p.add_argument("--hash", default="")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("show", help="print the current projected package")
    common(p)
    p.add_argument("--sync", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("packet", help="PM: emit a dispatch-only packet")
    common(p)
    p.add_argument("--sync", action="store_true")
    p.add_argument("--assignment", required=True)
    p.add_argument("--dispatch-role", default="worker")
    p.add_argument("--profile", default="worker")
    p.add_argument("--output", default="")
    p.set_defaults(func=cmd_packet)
    return parser


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (req.RequirementError, issue_cli.Denied, OSError, ValueError) as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
