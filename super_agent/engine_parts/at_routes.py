"""The AT (approval gate) router: verdicts in, frames out, one stall alarm.

Extracted from ``engine.py`` under the QA charter's rule for an over-limit file
(``rules/charters/qa.md`` §3.6): a touched module over 300 lines must end at or
below 95% of its size before the change. Moving a mixin is the shape this
repository already uses for exactly this (``ReviewRoutes``, ``RefusedRoutes``),
so the cut is a seam that existed, not a fragment sliced to satisfy a number.

What lives here and why together: an ``[AT]`` verdict is applied, the frame that
carries it is sent at most once, and a registered AT that stops answering is
asked about once. All three answer the same question - "does a person still need
to hear about this gate?" - and none of them touches the round's cursor.
"""
from __future__ import annotations

from .. import issuekey, requirements
from ..store import action_id
from . import at_ledger as at_ledger_mod
from . import frame_text as frame_text_mod
from . import retry as retry_mod
from . import watchdog as watchdog_mod


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AtRoutes:
    """Mixin: the AT verdict path and its stall watchdog (see module docstring)."""

    def _on_at_verdict(self, index: dict, thread: str, issue: int,
                       comment: dict, verdict: dict) -> tuple[str, str]:
        """Apply one ``[AT]`` verdict and route it.

        Four answers:

        * ``questions`` - a supervisor frame carrying ``question_url``. It is
          **not** an exception frame: a question is a normal stop on the path,
          and marking it as a failure teaches the reader to skip the failures;
        * ``ready``   - a supervisor frame saying the work can be dispatched;
        * ``blocked`` - the same frame with ``exception: true``;
        * unreadable (``""``) - **treated as blocked**, for the same reason as
          the PM side: an answer that arrived and was dropped is invisible.

        At most once per comment: the frame is written to ``release_frames``
        under ``at:<thread>:<cid>`` after it lands, so a re-read comment is
        silent while a *different* comment on the same thread still rings.

        This function reads and writes nothing in ``pm_verdicts``/``pm_stall``,
        and the QA/PM release gate never reads ``at_verdicts``. That is the J4
        property, and it is a property of the tables, not of the call order.
        """
        cid = int(comment["id"])
        state = str(verdict.get("state") or "")
        if not self._at_verified(thread, comment):
            self.store.log_event({
                "type": "at.unverified", "at": _now(), "thread": thread,
                "comment_id": cid, "consumers": ["doctor"],
                "detail": "the verdict did not name an at session key of this "
                          "thread"})
            self._pm_needs_person(index, thread, cid,
                                  "at.verdict_unverified[user]",
                                  "at verdict from unverified author")
            return "needs_person", "at verdict from unverified author"
        named = verdict.get("issue")
        if named is not None and int(named) != int(issue):
            self.store.log_event({
                "type": "at.verdict_wrong_issue", "at": _now(),
                "thread": thread, "comment_id": cid,
                "named_issue": int(named), "thread_issue": int(issue)})
            self._pm_needs_person(
                index, thread, cid, "at.verdict_wrong_issue[user]",
                f"verdict names issue #{int(named)} but thread is #{int(issue)}")
            return "needs_person", (f"verdict names issue #{int(named)} but "
                                    f"thread is #{int(issue)}")
        if self._at_mode(issue) and state in ("ready", "questions"):
            try:
                packages = requirements.blocks(str(comment.get("body") or ""),
                                               requirements.PACKAGE_FENCE)
                if len(packages) != 1:
                    raise requirements.RequirementError(
                        "AT READY/questions requires exactly one requirement fence")
                package = requirements.validate_package(packages[0])
                current = self._requirement_current(index, thread)
                if not current or current.get("source_comment_id") != cid \
                        or current.get("requirement_hash") != package["requirement_hash"]:
                    raise requirements.RequirementError(
                        "AT verdict is not the current readable requirement revision")
                if state == "questions" and not package.get("open_questions"):
                    raise requirements.RequirementError(
                        "questions verdict requires a non-empty open_questions list")
            except requirements.RequirementError as exc:
                self._pm_needs_person(index, thread, cid,
                                      "at.requirement_invalid[user]", str(exc))
                return "needs_person", str(exc)
            verdict = dict(verdict)
            verdict.update(requirement_id=package["requirement_id"],
                           revision=package["revision"],
                           requirement_hash=package["requirement_hash"],
                           _requirement=package)
        self._record_at_verdict(index, thread, verdict, cid)
        if self._at_announced(index, thread, cid) is not None:
            self.store.log_event({
                "type": "route.supervisor_skipped", "at": _now(),
                "thread": thread, "comment_id": cid, "to": "supervisor",
                "reason": "this at verdict already rang",
                "announced_by": "at"})
            return "at_verdict", ""
        if state == "questions":
            kind, exception, reason = "at_question", False, ""
        elif state == "ready":
            kind, exception, reason = "at_ready", False, ""
        else:
            kind, exception = "at_blocked", True
            if not state:
                reason = (verdict.get("coords_error")
                          or "no unmarked at verdict; treated as blocked")
            else:
                reason = "at reports blocked"
        url = (self._at_question_url(thread, issue, cid, verdict)
               if state == "questions" else "")
        result = self._route_at_frame(index, thread, issue, comment, verdict,
                                      exception=exception, kind=kind,
                                      reason=reason, question_url=url)
        dispatched = bool(result.get("dispatched"))
        ok = bool(result.get("ok")) if dispatched else False
        if dispatched and ok:
            self._mark_at_frame(index, thread, cid, kind=kind)
        elif dispatched:
            self._note_undeliverable(
                index, thread, cid,
                "at frame was dispatched but the send was not confirmed; it "
                "may or may not have arrived - check before resending",
                issue=issue, to="supervisor",
                once_key=f"atuncertain:{thread}:{cid}")
        else:
            self._note_undeliverable(
                index, thread, cid,
                "at verdict could not be delivered: "
                f"{result.get('reason') or 'unknown'}",
                issue=issue, to="supervisor", once_key=f"at:{thread}:{cid}")
        return "at_verdict", ""

    def _route_at_frame(self, index: dict, thread: str, issue: int,
                        comment: dict, verdict: dict, *, exception: bool,
                        kind: str, reason: str = "", question_url: str = "",
                        action_key: str = "", body_text: str = "") -> dict:
        """Send one AT frame on ``event.agent.supervisor``; at most once.

        Same order as :meth:`_route_pm_frame` - snapshot first, dispatch
        second - so a crash between them loses one send and never the
        decision. There is no retry pass for AT frames (the PM one is its own
        leg); a frame that could not be dispatched leaves a ``needs_person``
        row through :meth:`_note_undeliverable`, which is what makes the loss
        readable instead of silent.
        """
        cid = int(comment["id"]) if comment.get("id") is not None else 0
        key = action_key or self._at_frame_key(thread, cid)
        base = action_id(key, "bus_inject", str(cid))
        frames = index.setdefault("at_frames", {})
        if not isinstance(frames, dict):
            frames = index["at_frames"] = {}
        record = at_ledger_mod.frame_record(
            frames.get(key), thread=thread, cid=cid, issue=issue,
            kind=kind, exception=exception, question_url=question_url,
            text=(body_text or frame_text_mod.at_frame_text(
                issue, cid, verdict, exception=exception, kind=kind,
                question_url=question_url)),
            action_id=base, now=_now())
        frames[key] = record
        if self.store.action_attempted(base) > 0:
            return {"dispatched": False, "reason": "already attempted",
                    "action_id": base}
        sid = self._supervisor_sid(index)
        if not sid:
            record["ok"] = False
            record["undeliverable"] = True
            self._note_undeliverable(
                index, thread, cid,
                "no supervisor sid: engine.supervisor_sid is empty and none of "
                "engine.supervisor_keys resolve", issue=issue,
                to="supervisor", once_key=f"atsid:{key}")
            return {"dispatched": False, "reason": "no supervisor sid",
                    "action_id": base}
        record["undeliverable"] = False
        payload = {"thread": thread, "issue": int(issue),
                   "review_comment_id": cid, "kind": kind,
                   "state": str(verdict.get("state") or ""),
                   "exception": bool(exception), "reason": reason,
                   "question_url": question_url,
                   "sid": sid, "at": _now(), "text": record["text"]}
        result = self._dispatch(self._route_action(
            thread, issue, cid, payload, "event.agent.supervisor",
            target_role="supervisor", origin_role="at",
            assignment_id=key))
        record["ok"] = bool(result.get("ok")) if result.get("dispatched") else False
        record["refused"] = bool(result.get("refused"))
        record["action_id"] = result.get("action_id") or base
        # ISS-74 C(b): counted here as well. An AT frame has no *retry pass* of
        # its own, but a refused one leaves no attempt on record, so the next
        # round tries it again - a streak is exactly what that loop looks like
        # from outside, and it was invisible until this call existed.
        if result.get("dispatched"):
            self._clear_route_refusal(record)
        elif result.get("refused") is True:
            self._count_route_refusal(index, record, thread, cid, result)
        self.store.log_event({
            "type": "route.decided", "at": _now(), "thread": thread,
            "comment_id": cid, "to": "supervisor", "origin": "at",
            "kind": kind, "state": str(verdict.get("state") or ""),
            "exception": bool(exception),
            "dispatched": bool(result.get("dispatched")),
            "refused": bool(result.get("refused")),
            "reason": result.get("reason") or "",
        })
        return result

    def _check_at_stall(self, index: dict) -> int:
        """Ask once about a registered AT that stopped answering.

        **``at_stall_rounds`` defaults to 15, and that 15 is an independent
        value.** It is not derived from ``release_timeout_s`` and it is not
        copied from ``pm_stall_rounds``: the PM watchdog's number is chosen to
        sit inside the release budget, this one is the AT's own budget. Tune it
        by watching it fire, not by arithmetic on another role's setting.

        The ledger is ``index["at_stall"]`` and never ``pm_stall``. A PM that
        never answered and an AT that never spoke are two different silences
        over two different questions; sharing the counter would let one role's
        answer erase the other role's alarm (J4/J5). Nothing here reads
        ``pm_verdicts``, so a missing PM book cannot change this answer.

        "Registered" means an ``at`` entry in ``issue_sessions.json[thread]`` -
        a thread no AT was ever bound to is not watched, because a watchdog
        with nobody to watch fires on everything.

        Counting is by round; one frame per hold, and a new hold (leaving and
        re-entering the register) is a new notice through ``at_stall_seq``.
        """
        engine_cfg = self.cfg.get("engine") or {}
        try:
            limit = int(engine_cfg.get("at_stall_rounds") or 15)
        except (TypeError, ValueError):
            limit = 15
        if limit <= 0:
            return 0
        stall = watchdog_mod.table(index, "at_stall")
        registered = watchdog_mod.at_registered(
            self._load_issue_sessions(), self._thread_key,
            needs_answer=lambda t, e: self._at_needs_answer(index, t, e))
        for thread in list(stall):
            if thread not in registered:
                watchdog_mod.bump_hold_seq(index, "at_stall_seq", thread)
                stall.pop(thread, None)
        fired = 0
        for thread, at_entry in sorted(registered.items()):
            entry = watchdog_mod.round_count(stall.get(thread), thread, _now())
            stall[thread] = entry
            if not watchdog_mod.due(entry, limit):
                continue
            issue = at_entry.get("issue") or retry_mod.issue_of(index, thread)
            if issue is None:
                # The binding records a session, not a number. The thread key
                # carries it, and ``issue: 0`` would fail the project allowlist
                # - a watchdog whose notice is always refused looks exactly
                # like a watchdog that never fired.
                _repo, parsed = issuekey.split_key(thread)
                issue = parsed
            seq = watchdog_mod.hold_seq(index, "at_stall_seq", thread)
            stall[thread] = self._fire_stall(
                index, thread, issue=issue, comment_id=0, seq=seq,
                table="at_stall_seq", entry=entry, kind="at", assignment="",
                body_text=f"【AT 停摆】{thread} {entry['rounds']} 轮无 AT 结论",
                row_reason="at.stall[user]",
                row_detail=f"{entry['rounds']} 轮无 AT 结论")
            fired += 1
        return fired
