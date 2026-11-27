"""Which project owns an issue: the live listing first, the repository second.

Extracted from ``engine.py`` under the same rule as :mod:`.frame_text`: a large
file may be edited but may not grow. The two answers have different lifetimes,
and that is the whole point of the module:

* **the listing** is rebuilt every round from open, non-held issues, so a
  suspended or closed issue is *gone* from it;
* **the repository** is declared in the allowlist file and does not change when
  an issue is put on hold.

Answering only with the first turned every pending frame on a suspended issue
into ``project None not allowlisted`` - #122/#123/#129 were refused once per
round for 85 rounds each, and the wording sent readers to the repository when
the real problem was the missing listing entry.
"""
from __future__ import annotations

from .. import issuekey


def project_for(projects: dict, issue, repo: str) -> str | None:
    """Project name that owns ``issue``; ``None`` when nothing claims it.

    ``projects`` is the allowlist ``projects`` mapping. The listing is asked
    first so an explicitly placed issue wins over repository ownership: two
    projects may share a repository during a split, and the file says which one
    is working the issue.
    """
    try:
        number = int(issue)
    except (TypeError, ValueError):
        return None
    for name, entry in projects.items():
        if number in entry["issues"]:
            return name
    if not issuekey.is_repo(str(repo or "")):
        return None
    for name, entry in projects.items():
        if str(entry.get("repo") or "") == str(repo):
            return name
    return None
