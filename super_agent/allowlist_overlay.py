"""Writing the untracked overlay: which issues are live right now.

Split out of ``allowlist.py`` under the QA charter's rule for over-limit files
(``rules/charters/qa.md`` §3.6): a touched module over 300 lines must end at or
below 95% of its pre-change size. The seam is the file it writes: everything
here turns "what GitHub says is open" into the overlay on disk, and nothing else
in ``ProjectAllowlist`` touches that file except reading it back.

The function takes the allowlist as its first argument instead of ``self`` - the
class keeps a three-line delegate so the public surface is unchanged.
"""
from __future__ import annotations

import copy
import json
import os


def sync_issues(allow, project: str, issues) -> dict:
        """Record ``issues`` for ``project`` in the untracked overlay.

        The tracked allowlist is a published file: rewriting it from a running
        engine would turn configuration into run state and put a private
        repository's issue numbers into a tracked diff. The overlay
        (``<name>.local.json``) already has ``issues`` in
        :data:`OVERLAY_FIELDS` and is never committed, so that is where the
        engine keeps "which issues are live right now".

        **This call is the single source of truth for the in-flight set**
        (``super_agent/engine.py:3996`` calls it once per round with what GitHub
        reports as open-and-not-on-hold). The ``issues`` list in the *tracked*
        file is an initial snapshot only: :func:`merge_overlay` replaces the
        column wholesale, so a hand-added number there neither guarantees
        delivery nor survives the next round. A number GitHub does not return
        is never in flight, no matter what any file says (ISS-74 E).

        Idempotent: identical content is not rewritten. A write failure is
        reported, never raised - a read-only checkout must still run.
        """
        name = str(project or "").strip()
        wanted = sorted({int(n) for n in (issues or []) if str(n).strip().lstrip("-").isdigit()})
        out = {"project": name, "written": False, "changed": False, "error": ""}
        if not name or allow.data is None or name not in allow.data["projects"]:
            out["error"] = f"project {name!r} not in the allowlist"
            return out
        current = allow.data["projects"][name].get("issues") or []
        if sorted(current) == wanted:
            return out
        raw, error = allow._read_overlay()
        if error:
            out["error"] = error
            return out
        overlay = copy.deepcopy(raw) if raw else {}
        projects = overlay.setdefault("projects", {})
        entry = projects.setdefault(name, {})
        if not isinstance(entry, dict):
            out["error"] = f"overlay project {name!r} is not an object"
            return out
        entry["issues"] = wanted
        text = json.dumps(overlay, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        try:
            allow.overlay_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = allow.overlay_path.with_name(allow.overlay_path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, allow.overlay_path)
        except OSError as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        out["written"] = True
        out["changed"] = True
        allow.poll()
        return out
