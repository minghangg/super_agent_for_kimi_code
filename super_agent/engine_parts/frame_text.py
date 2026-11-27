"""The one line a person reads for each kind of frame.

Extracted from ``engine.py`` (4735 lines before this move) rather than added to
it: a large file may be edited, but a change may not make it longer. These four
builders are pure - text in, text out - so they move without dragging state.

The wording is not decoration. ``release_text`` is what the supervisor reads
when a release is announced, ``qa_notice_text`` names the project and the cid a
project PM must answer, ``pm_frame_text`` is the PM verdict frame, and
``at_frame_text`` is the line the AT flow's frame carries. Each is asserted by
tests through the engine's call sites, and keeping them in one module is what
makes "did we change what a person reads?" a question with one file as the
answer.
"""
from __future__ import annotations


def release_text(issue, cid) -> str:
    """The issue and the verdict that closed it."""
    return f"【可关单】#{int(issue)}，QA cid {cid}（P0=0，无必修项）"


def qa_notice_text(project, issue, cid, verdict) -> str:
    """One QA verdict, addressed to the project PM."""
    return f"【QA 结论】{project}#{int(issue)} {verdict}，QA cid {cid}"


def pm_frame_text(issue, cid, verdict: dict, *, exception: bool) -> str:
    """The one line a person reads for a PM frame.

    ``escalation_only`` is the third case, and calling it "unreadable" was
    actively wrong: the comment declared no state on purpose, because it asks
    for a decision rather than reporting one. The engine read a supervisor's
    own ``[告警]`` back as "the PM's answer could not be read" and filed a row
    nobody could resolve (#140 cid 7222965791 / 7223070715).
    """
    if verdict.get("escalation_only"):
        who = str(((verdict.get("five") or {}).get("who")) or "?")
        return f"【例外】#{int(issue)} 请求裁决（{who}，cid {cid}）"
    state = str(verdict.get("state") or "") or "unreadable"
    if exception:
        return (f"【PM 例外】#{int(issue)} PM 结论 {state}"
                f"（cid {cid}），需要人看一眼")
    return (f"【可关单】#{int(issue)}，PM 结论 ready"
            f"（cid {cid}），可以关单")


def at_frame_text(issue, cid, verdict: dict, *, exception: bool, kind: str,
                  question_url: str = "") -> str:
    """The one line an AT frame carries: a question, an exception, or a go."""
    state = str(verdict.get("state") or "") or "unreadable"
    if kind == "at_question":
        return (f"【AT 有疑问】#{int(issue)}（cid {cid}），请回答："
                f"{question_url or '（问卷链接未生成，见事件日志）'}")
    if exception:
        return (f"【AT 例外】#{int(issue)} AT 结论 {state}"
                f"（cid {cid}），需要人看一眼")
    return f"【AT 可派单】#{int(issue)}（cid {cid}），可以派单"

def parse_ts(value: str):
    """Parse an ISO timestamp; ``None`` when unusable.

    One copy, three former callers (``engine._parse_ts``, ``watchdog._parse``,
    ``doctor._parse``). Each carried the same four branches and the same
    ``Z`` handling bug: ``fromisoformat`` before 3.11 rejects ``Z``, so the
    copies that did not translate it answered ``None`` for a perfectly good
    timestamp - a silent "unusable" that reads like a data problem.
    """
    from datetime import datetime

    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
