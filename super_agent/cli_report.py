"""The ``doctor --project`` row: one project's environment document, verbatim.

Split out of ``cli.py`` under the QA charter's rule for over-limit files
(``rules/charters/qa.md`` §3.6). The seam is the exit-code contract: this
function answers "is this project registered, and what does its document say",
and it prints; the command that calls it does nothing else.

``cli._doctor_project`` still resolves here, so the caller and its tests keep
working.
"""
from __future__ import annotations

import sys
from pathlib import Path


def doctor_project(project: str, root: str = "", origin: str = "") -> int:
    """Print one project's environment document verbatim (ISS-101).

    The file name is the project key itself -- the same token ``gh_projects.json``
    and dispatch use. Nothing here parses the markdown: the document is the
    answer, and a schema on top of it would be a second ``config.validate``.

    ``root`` is the **resolved** directory (``config.load`` already merged the
    file, the environment and the overrides). Reading it from
    ``agent_config`` again would bypass ``--config``: a ``--config`` file's
    ``paths.project_env_dir`` would be ignored and the answer would silently
    come from a different layer -- the same "looks configured, is not" shape
    this change exists to remove.

    Exit codes: 0 printed, 1 no such entry (or two candidates), 2 the directory
    itself is unconfigured or missing -- a configuration fault is a different
    action from a missing registration, so they do not share a code.
    """
    from . import agent_config

    key = str(project or "").strip()
    if not key:
        print("doctor --project: 需要项目键（gh_projects.json 的键）", file=sys.stderr)
        return 2
    if not root:
        print("project_env_dir 未配置（paths.project_env_dir：--config / agent.json / "
              "$SAFKC_PROJECT_ENV_DIR 三处皆空）", file=sys.stderr)
        return 2
    base = Path(root)
    if not base.is_dir():
        print(f"目录不存在：{base}", file=sys.stderr)
        print(f"来源：paths.project_env_dir（{origin or '配置层'}）", file=sys.stderr)
        return 2
    # Two separate branches on purpose: "no candidate" is a missing
    # registration, "several candidates" is an ambiguous one. Taking the first
    # hit would make the answer depend on ``glob``'s order.
    exact = [p for p in base.glob("*.md") if p.stem == key]
    if len(exact) >= 2:
        names = ", ".join(sorted(p.name for p in exact))
        print(f"歧义：{base} 下有 {len(exact)} 个候选：{names}", file=sys.stderr)
        return 1
    if not exact:
        print(f"未登记：{key}（目录 {base} 下无 {key}.md）", file=sys.stderr)
        # A near-miss is reported, never adopted: "looks like" is not "is".
        near = [p for p in base.glob("*.md")
                if p.stem.strip().lower().replace("_", "")
                == key.strip().lower().replace("_", "")]
        if near:
            hint = ", ".join(sorted({p.stem.strip() for p in near}))
            print(f"最接近 {hint}（未自动采用）", file=sys.stderr)
        return 1
    path = exact[0]
    # Name the layer that produced the directory, not just the private file:
    # with ``--config`` the value may have come from there, and a source line
    # that names the wrong file sends the reader to the wrong place.
    print(f"来源：{path}（paths.project_env_dir 来自 {origin or '配置层'}）",
          file=sys.stderr)
    # Bytes out when the stream has one: the document is printed as stored, so
    # ``diff`` against the file is empty whatever its line endings (a text-mode
    # write would translate LF to CRLF on Windows). A redirected StringIO has
    # no buffer; there the text form is the only option and is exact enough.
    raw = path.read_bytes()
    sys.stdout.flush()
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(raw)
        buffer.flush()
    else:
        sys.stdout.write(raw.decode("utf-8-sig", "replace"))
    return 0
