"""Review routing: one QA verdict, the PM notice, and the supervisor's ring.

Why this module exists (§3.1.1 cleanup of #130): these nine methods carry one
responsibility - take a claimed QA verdict and decide who hears about it - and
they were the largest single body inside the 5k-line ``Engine`` class. The
rules they keep are not obvious from any one of them, and that is exactly why
they belong together and away from the read/collect path:

* a **release rings once**. The QA pass and the PM answer to it are two
  comments about one delivery, so both gates read the same ledger
  (``_release_announced``) and the second one is silent - whether it lands in
  this batch or a later round;
* a shippable verdict reaches the supervisor **before** the PM early return,
  under its own action id and its own table: a refused PM attempt must never be
  the reason the supervisor never hears about a release;
* ``at-most-once`` is the attempt counter (``store.action_attempted``), not the
  record's presence, so a crash between dispatch and the ledger costs one
  duplicate and never a lost release;
* refusals are split by whether a retry can ever succeed
  (``is_permanent_refusal``), and the wording is not the criterion -
  ``allowlist.check`` owns it.

The class is a mixin: it reads ``self.store``, ``self.cfg`` and the routers on
``Engine``, so it is not a free function whichever file it lives in. It lives
here because a module of its own makes the responsibility visible.
"""
from __future__ import annotations

from ..store import action_id
from .. import markers, needs
from .. import assignments as asg_mod


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewRoutes:
    """Mixin: the review → PM → supervisor routing family."""

    # A refusal is permanent only when no retry can ever succeed (the creation
    # mode forbids the session), so asking again is asking the same question.
    PERMANENT_REFUSAL_PREFIXES = ("creation_mode_forbidden",)

    # How many refused rounds in a row earn one visible alert, and the event
    # type it carries. Three matches the E61 liveness window, so a person hears
    # about a stuck verdict on the same rhythm they hear about a stuck session.
    REFUSED_RETRY_ROUNDS = 3
    REFUSED_RETRY_ROW = "route.refused_retryable"

    def _route_review(self, index: dict, thread: str, issue: int, comment: dict,
                      review: dict, *, log_unverified: bool = True) -> dict:
        """Send one verdict to the project PM and record the attempt.

        The route record lives in ``index["routes"]`` keyed by thread and
        comment, so a refused or suppressed dispatch can be retried next round
        under the **same** action id - at-most-once is the attempt counter, not
        the record's presence.

        A shippable verdict also goes to the supervisor. That decision is taken
        **before** the PM early return below, under its own action id and in its
        own table: the PM attempt counter must never be the reason the
        supervisor never hears about a release.
        """
        cid = int(comment["id"])
        key = f"route:{thread}:{cid}"
        target = self._pm_target(thread, issue)
        verified = self._review_verified(thread, comment)
        if not verified and log_unverified:
            # Doctor is the only consumer on purpose: a key miss is a warning
            # to look at, not a reason to wake the PM twice for one verdict.
            self.store.log_event({
                "type": "review.unverified", "at": _now(), "thread": thread,
                "comment_id": cid, "level": "warn", "consumers": ["doctor"],
                "detail": "the verdict did not name a qa session key of this thread",
            })
        sup_key = f"sup:{thread}:{cid}"
        sup_routes = index.setdefault("supervisor_routes", {})
        if not isinstance(sup_routes, dict):
            sup_routes = index["supervisor_routes"] = {}
        sup_rec = sup_routes.get(sup_key)
        sup_rec = sup_rec if isinstance(sup_rec, dict) else None
        if (sup_rec is not None and "sup_eligible" not in sup_rec
                and "fix_needed" not in review):
            self._undecidable(thread, cid, sup_key)
        if self._sup_eligible(review, sup_rec) and not (
                sup_rec is not None and sup_rec.get("ok")):
            # A release rings **once**, and the question is whether this
            # release already rang - not whether a PM verdict exists right now.
            # The QA pass and the PM answer to it are two comments about one
            # delivery, so both gates read the same ledger and the second one
            # is silent whether it lands in this batch or a later round.
            announced = self._release_announced(index, thread, cid)
            if announced is not None:
                # Record the decision, not just the log line: the retry pass
                # reads ``sup_eligible`` from this table, and a record that is
                # merely absent would be re-sent by it next round.
                if sup_rec is None:
                    sup_routes[sup_key] = {
                        "thread": thread, "comment_id": cid, "at": _now(),
                        "verdict": str(review.get("verdict") or ""),
                        "sup_eligible": False, "skipped": "release announced",
                        "ok": False}
                self.store.log_event({
                    "type": "route.supervisor_skipped", "at": _now(),
                    "thread": thread, "comment_id": cid, "to": "supervisor",
                    "reason": "this release already rang",
                    "announced_by": str(announced.get("source") or "")})
            else:
                result = self._route_supervisor(
                    index, thread, issue, comment, review,
                    sup_key, action_id(sup_key, "bus_inject", str(cid)))
                # The ledger means "this happened", so it is written only when
                # the frame landed. A crash between dispatch and this line
                # costs one duplicate, never a lost release with nothing to
                # read; an unwritten ledger lets the next round retry.
                if result.get("dispatched") and result.get("ok"):
                    self._mark_release(index, thread, cid, source="qa",
                                       comment_id=cid)
                # Not delivered: ``_route_supervisor`` already recorded why, in
                # the ``[user]`` bucket, once per record. Repeating it here
                # would be a second row for one failure.

        if (review.get("fix_structured") is False
                and str(review.get("verdict") or "") == "pass"):
            # An unmarked *pass* answers the safe side (work is left) and so
            # tells the supervisor nothing - which is *safe* but *silent*: the
            # absence of a notice is not observable. Say it out loud once per
            # verdict, in the same ``[user]`` bucket as every other "this could
            # not be sent" row.
            #
            # A reject is excluded on purpose: it never reaches the supervisor
            # whatever the marker says, so "please add the marker" would be
            # advice with no effect - one ``[user]`` row per reject, piling up
            # in the bucket a person reads.
            #
            # A retry stub carries no ``fix_structured`` key, so the strict
            # identity test (never ``not``) keeps the re-sends quiet.
            self._note_undeliverable(
                index, thread, cid,
                "QA 结论未带 fix_needed 标记，已按「有必修项」处理并暂不通知 "
                "supervisor；请 QA 补标记",
                issue=issue, to="supervisor",
                once_key=f"unmarked:{thread}:{cid}")
        payload = {"thread": thread, "issue": int(issue),
                   "review_comment_id": cid,
                   "verdict": str(review.get("verdict") or ""),
                   "project": target["project"], "pm_sid": target["pm_sid"],
                   "sid": target["pm_sid"],
                   "text": self._qa_notice_text(
                       target["project"], issue, cid,
                       str(review.get("verdict") or "")),
                   "unverified": not verified}
        base = action_id(key, "bus_inject", str(cid))
        routes = index.setdefault("routes", {})
        if routes is None or not isinstance(routes, dict):
            routes = index["routes"] = {}
        if key not in routes:
            routes[key] = {"thread": thread, "comment_id": cid, "at": _now(),
                           "verdict": payload["verdict"], "ok": None,
                           "action_id": base}
        route = routes[key]
        if not target["pm_sid"]:
            route.update(ok=False, refused=True, session_delivery="failed")
            self.store.log_event({
                "type": "route.decided", "at": _now(), "thread": thread,
                "comment_id": cid, "to": "project_pm",
                "project": target["project"], "verdict": payload["verdict"],
                "dispatched": False, "refused": True,
                "session_delivery": "failed",
                "reason": "no project pm session resolved",
            })
            self._route_pm_delivery_failure(index, thread, issue, cid, target)
            self._append_needs_person(index, thread, {
                "comment_id": cid, "reason": "route.undeliverable", "at": _now()})
            return {"dispatched": False, "refused": True,
                    "reason": "no project pm session resolved", "action_id": base}
        if self.store.action_attempted(base) > 0:
            return {"dispatched": False, "reason": "already attempted",
                    "action_id": base}
        result = self._dispatch(self._route_action(
            thread, issue, cid, payload, "event.agent.project_pm",
            target_role=target["target_role"], origin_role=markers.QA.lower(),
            assignment_id=key))
        route["ok"] = bool(result.get("ok")) if result.get("dispatched") else False
        route["refused"] = bool(result.get("refused"))
        route["session_delivery"] = "ring" if result.get("dispatched") else "failed"
        route["action_id"] = result.get("action_id") or base
        # The one place a round learns whether this route was refused. Reading
        # the returned flag, never the wording, keeps the count honest when the
        # refusal text changes; a delivered dispatch ends the run.
        if result.get("dispatched"):
            self._clear_route_refusal(route)
        elif result.get("refused") is True:
            self._count_route_refusal(index, route, thread, cid, result)
        self.store.log_event({
            "type": "route.decided", "at": _now(), "thread": thread,
            "comment_id": cid, "to": "project_pm", "project": target["project"],
            "verdict": payload["verdict"],
            "dispatched": bool(result.get("dispatched")),
            "refused": bool(result.get("refused")),
            "session_delivery": "ring" if result.get("dispatched") else "failed",
            "reason": result.get("reason") or "",
        })
        if not result.get("dispatched"):
            self._append_needs_person(index, thread, {
                "comment_id": cid, "reason": "route.undeliverable", "at": _now()})
            self.store.log_event({
                "type": "route.undeliverable", "at": _now(), "thread": thread,
                "comment_id": cid, "reason": result.get("reason") or "",
            })
        return result

    def _maybe_arbitrate_review(self, index: dict, thread: str, issue: int,
                                comment: dict, assignment: dict) -> None:
        """Second consecutive QA reject asks the supervisor to arbitrate once."""
        cid = int(comment["id"])
        streak = 0
        readings = assignment.get("review_readings") or {}
        for raw_cid, verdict in sorted(readings.items(), key=lambda item: int(item[0])):
            if verdict == "reject":
                streak += 1
            elif verdict == "pass":
                streak = 0
        if streak != 2:
            return
        action_key = f"qa_arbitration:{assignment.get('id')}:{cid}"
        text = (f"【QA 仲裁】#{int(issue)} 连续 2 轮退回，最新 QA cid {cid}，"
                "请 supervisor 仲裁")
        self._route_pm_frame(
            index, thread, issue, comment,
            {"state": "blocked", "delivery": assignment.get("delivery_comment_id")},
            exception=True, reason="two consecutive QA rejects",
            action_key=action_key, body_text=text)

    def _on_review(self, index: dict, thread: str, issue: int, comment: dict,
                   review: dict, summary: dict) -> str:
        """Apply a verdict, then route it. Returns the triage state."""
        cid = int(comment["id"])
        assignment, requirement_error = self._review_target(
            index, thread, issue, comment)
        if assignment is None:
            reason = requirement_error or "no active worker assignment"
            self.store.log_event({"type": "review.unmatched", "at": _now(),
                                  "thread": thread, "comment_id": cid,
                                  "reason": reason})
            return "needs_person", reason
        verified = self._review_verified(thread, comment)
        moved = asg_mod.on_review(assignment, review, review_cid=cid,
                                  verified=verified)
        if moved["changed"]:
            self.store.log_event({
                "type": "assignment.qa_review", "at": _now(),
                "assignment": assignment["id"], "thread": thread,
                "comment_id": cid, "status": moved.get("status"),
                "verdict": str(review.get("verdict") or ""),
                "verified": verified,
            })
            # A verdict that was refused earlier and is applied now settles its
            # own refusal. Leaving the row in place would keep telling a person
            # to decide something the engine has just decided - and #65's
            # repaired comment is exactly that case.
            self._clear_refused(index, thread, comment_id=cid)
            self._maybe_arbitrate_review(index, thread, issue, comment, assignment)
        elif moved.get("refused"):
            # A verdict that landed and was **not** applied. Three things are
            # needed here, and none existed before this issue:
            #
            # * the ``review.stale`` reason keeps its own event (doctor reads
            #   it) *and* lands in the refused bucket. Making it an either/or
            #   was how a stale pass stayed half-invisible: a doctor row is not
            #   a person's queue.
            # * the refusal becomes a ``needs_person`` row. A ``consumers`` key
            #   is a routing hint, not a queue - ``brief``/``status`` read
            #   ``index.needs_person``, so a ``[user]`` event with no row was
            #   readable only in the raw log.
            # * the PM hears about it, as a **refusal**. The supervisor does
            #   not: the bell stays silent, which is the part that must not
            #   regress.
            #
            # The row and the event are deduped per ``(thread, cid, reason)``:
            # a refused verdict is re-read every round while the comment stays
            # in the window, and N identical rows is noise, not N problems -
            # but two *different* reasons are two problems.
            reason = str(moved.get("reason") or "")
            if reason == "review.stale":
                self.store.log_event({"type": "review.stale", "at": _now(),
                                      "assignment": assignment["id"],
                                      "thread": thread, "comment_id": cid})
            self._note_refused_after_hold(index, assignment, thread, cid,
                                          review, moved)
        if not moved.get("refused"):
            # Routing a refused verdict is what made the deadlock complete:
            # the frame said "ready to release" while ``qa_gate`` still had no
            # pass, so the bell rang for a thread that could never complete.
            # Only an **applied** verdict speaks for the thread.
            #
            # A replay speaks for nothing either: same comment, same reading,
            # already answered. Routing it again would ring the PM once per
            # round for a batch that was read before.
            if moved.get("reason") == "same review already recorded":
                return "review", ""
            self._route_review(index, thread, issue, comment, review)
        return "review", ""

    def _route_refused_review(self, index: dict, thread: str, issue: int,
                              assignment: dict, cid: int, review: dict,
                              reason: str) -> dict:
        """Tell the project PM that a verdict landed and was not applied.

        Goes to ``event.agent.project_pm`` with ``refused: true`` under its own
        action key, so it can never be mistaken for - or suppress - the release
        route of the same comment. ``release_frames`` is untouched by design:
        that ledger answers "has this release rung", and a refusal is the
        opposite answer.
        """
        key = f"refused_route:{thread}:{cid}"
        routes = index.setdefault("refused_routes", {})
        if not isinstance(routes, dict):
            routes = index["refused_routes"] = {}
        route = routes.get(key)
        if isinstance(route, dict) and route.get("ok"):
            return {"dispatched": False, "reason": "already sent"}
        base = action_id(key, "bus_inject", str(cid))
        if self.store.action_attempted(base) > 0:
            return {"dispatched": False, "reason": "already attempted",
                    "action_id": base}
        target = self._pm_target(thread, issue)
        payload = {"thread": thread, "issue": int(issue),
                   "review_comment_id": int(cid),
                   "project": target["project"],
                   "verdict": str(review.get("verdict") or ""),
                   "sid": target["pm_sid"], "pm_sid": target["pm_sid"],
                   "text": self._qa_notice_text(
                       target["project"], issue, cid,
                       str(review.get("verdict") or "")),
                   "refused": True, "reason": reason,
                   "assignment": str(assignment.get("id") or ""),
                   "status": str(assignment.get("status") or ""),
                   "at": _now()}
        if not target["pm_sid"]:
            result = {"dispatched": False, "refused": True,
                      "reason": "no project pm session resolved", "action_id": base}
            self._route_pm_delivery_failure(index, thread, issue, cid, target)
            self._append_needs_person(index, thread, {
                "comment_id": cid, "reason": "route.undeliverable", "at": _now()})
        else:
            result = self._dispatch(self._route_action(
                thread, issue, cid, payload, "event.agent.project_pm",
                target_role=target["target_role"], origin_role=markers.QA.lower(),
                assignment_id=key))
        routes[key] = {"thread": thread, "comment_id": int(cid), "at": _now(),
                       "verdict": payload["verdict"], "reason": reason,
                       "ok": (bool(result.get("ok"))
                              if result.get("dispatched") else False),
                       "action_id": result.get("action_id") or base}
        self.store.log_event({
            "type": "route.refused_decided", "at": _now(), "thread": thread,
            "comment_id": int(cid), "to": "project_pm",
            "project": target["project"], "verdict": payload["verdict"],
            "reason": reason,
            "dispatched": bool(result.get("dispatched")),
            "refused": bool(result.get("refused")),
        })
        return result

    def _note_refused_after_hold(self, index: dict, assignment: dict,
                                 thread: str, cid: int, review: dict,
                                 moved: dict) -> None:
        """Record one refused verdict: log, queue row, PM frame, dedupe.

        The identity is ``(thread, cid, reason)``. Reason is part of it on
        purpose: one comment can be refused for two different reasons on
        different rounds ("not newer", then "stale"), and swallowing the second
        is how a real problem stays invisible. The ``needs`` dismissal key
        stays ``(thread, cid)`` - that is how a person dismisses a *comment*,
        not a diagnosis - so a dismissal still suppresses every row for it.
        """
        cid = int(cid)
        reason = str(moved.get("reason") or "")
        key = f"refused:{thread}:{cid}:{reason}"
        seen = index.setdefault("review_refused", {})
        if not isinstance(seen, dict):
            seen = index["review_refused"] = {}
        if seen.get(key):
            return
        seen[key] = {"thread": thread, "comment_id": cid,
                     "assignment": str(assignment.get("id") or ""),
                     "status": str(assignment.get("status") or ""),
                     "verdict": str(review.get("verdict") or ""),
                     "reason": reason, "at": _now()}
        self.store.log_event({
            "type": "assignment.qa_refused_after_hold", "at": _now(),
            "assignment": assignment.get("id"), "thread": thread,
            "comment_id": cid, "status": str(assignment.get("status") or ""),
            "verdict": str(review.get("verdict") or ""),
            "reason": reason, "level": "warn", "consumers": ["user"],
        })
        numbers = index.setdefault("issues", {}).setdefault(
            thread, {"state": "idle", "comments_seen": 0, "needs_person": []})
        numbers.setdefault("needs_person", []).append({
            "comment_id": cid,
            # ``brief`` files an entry under "4. 等用户的事项" only when the
            # reason matches its ``[user]`` markers. The old row carried no
            # marker, so it landed in "3. 等 supervisor 裁决" - the opposite of
            # what the code comment claimed.
            "reason": "review.refused_after_hold [user]",
            "detail": reason, "at": _now()})
        issue = assignment.get("issue")
        if issue is not None:
            self._route_refused_review(index, thread, int(issue), assignment,
                                       cid, review, reason)

    def _clear_refused(self, index: dict, thread: str,
                       *, comment_id=None) -> int:
        """Drop refused-verdict bookkeeping for a thread, or for one comment.

        ``review_refused`` and its ``needs_person`` rows describe work still
        waiting on a person. Two things end that wait, and both used to leave
        the rows behind:

        * the **comment itself** is applied later (``comment_id`` given) - the
          refusal was about a reading that has since been corrected, so the row
          now asks a person to decide something the engine already decided;
        * the **thread completes** (no ``comment_id``) - nothing on it waits for
          a person any more.

        The original ``needs_person`` rows stay append-only; supersession marks
        hide them from the open queue. Returns cleared ``review_refused`` entries.
        """
        def wanted(rec) -> bool:
            if not isinstance(rec, dict):
                return False
            if str(rec.get("thread") or "") != thread:
                return False
            if comment_id is None:
                return True
            try:
                return int(rec.get("comment_id")) == int(comment_id)
            except (TypeError, ValueError):
                return False

        seen = index.get("review_refused")
        cleared = 0
        if isinstance(seen, dict):
            for key in [k for k, rec in seen.items() if wanted(rec)]:
                del seen[key]
                cleared += 1
        numbers = (index.get("issues") or {}).get(thread)
        if isinstance(numbers, dict):
            from .. import needs as needs_mod
            rows = numbers.get("needs_person")
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict) or not str(
                            row.get("reason") or "").startswith("review.refused_after_hold"):
                        continue
                    if comment_id is not None and str(row.get("comment_id")) != str(comment_id):
                        continue
                    cid = needs_mod.cid_of(row)
                    if cid is not None:
                        needs_mod.mark(index, thread, cid, state=needs_mod.SUPERSEDED,
                                       by="engine", reason="review refusal cleared",
                                       by_comment=comment_id)
        if cleared:
            self.store.log_event({
                "type": "review.refused_cleared", "at": _now(),
                "thread": thread, "entries": cleared,
                "comment_id": comment_id,
            })
        return cleared
    @classmethod
    def is_permanent_refusal(cls, reason: str) -> bool:
        """Is this refusal permanent for *this comment*, whatever happens next?

        Only one answer qualifies: the creation mode forbids session creation.
        Everything else - an issue or a project that is not listed yet, an
        action that is not listed yet, a network error, ``target session is
        archived`` - can clear on its own or by a one-line edit, so those keep
        retrying. The text is read as a prefix only because that is the shape
        the engine itself emits for the creation mode; no other wording is
        load-bearing.
        """
        text = str(reason or "").strip()
        return any(text.startswith(p) for p in cls.PERMANENT_REFUSAL_PREFIXES)

    @staticmethod
    def _round_token(index: dict) -> int:
        """The round a write belongs to.

        ``run_count`` is bumped at the end of the round, after the retry pass,
        so every write inside one round (the inline triage call and the retry
        call) reads the same value. A crash before ``save_index`` leaves the
        token unchanged, which merges that refusal into the next round instead
        of counting it twice - late, never early.
        """
        return int((index.get("meta") or {}).get("run_count") or 0)

    def _count_route_refusal(self, index: dict, route: dict, thread: str,
                             cid, result: dict | None = None) -> None:
        """One increment per round for a route whose dispatch was refused.

        The criterion is the returned ``refused`` flag, never the reason text:
        the wording belongs to ``allowlist.check`` and may change without
        changing what happened. Nothing here is kept in memory - the streak
        lives in the route record so a restart resumes it.

        The alert says what was observed (a run of refusals, this comment) and
        carries the refusal's own reason for evidence. It deliberately does not
        guess a cause: three of the four refusal shapes are unrelated to the
        allowlist, and an alert that names the wrong file is worse than none.
        """
        streak = route.get("refusal_streak")
        if not isinstance(streak, dict):
            streak = route["refusal_streak"] = {"rounds": 0, "last_round": None}
        token = self._round_token(index)
        if streak.get("last_round") == token:
            return
        streak["last_round"] = token
        streak["rounds"] = int(streak.get("rounds") or 0) + 1
        if streak["rounds"] < self.REFUSED_RETRY_ROUNDS:
            return
        if streak.get("alerted_at"):
            return
        streak["alerted_at"] = _now()
        reason = (f"{self.REFUSED_RETRY_ROW}[user]: verdict on cid {cid} of "
                  f"{thread} was refused {streak['rounds']} rounds in a row; "
                  "see route.decided for what each round answered")
        self._append_needs_person(index, thread, {
            "comment_id": cid, "reason": reason, "at": _now()})
        self.store.log_event({
            "type": self.REFUSED_RETRY_ROW, "at": _now(), "thread": thread,
            "comment_id": cid, "rounds": streak["rounds"],
            "reason": str((result or {}).get("reason") or ""),
            "level": "warn", "consumers": ["doctor"],
        })

    @staticmethod
    def _clear_route_refusal(route: dict) -> None:
        """A landed dispatch ends the streak.

        ``alerted_at`` stays as the one-shot proof, so the same comment can
        never alert twice even if it is refused again afterwards.
        """
        streak = route.get("refusal_streak")
        if isinstance(streak, dict):
            streak["rounds"] = 0
