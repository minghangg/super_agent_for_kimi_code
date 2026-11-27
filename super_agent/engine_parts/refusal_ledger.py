"""Refusal bookkeeping: what the engine remembers about a verdict it could not send.

Why this module exists (§3.1.1, #130 QA P1-2): ``review_routes`` was 548 lines
and carried two responsibilities that change for different reasons. Routing a
verdict asks *who should hear this*; refusal bookkeeping asks *what do we do
about the fact that nobody could be told*. They share nothing but the caller.

The rules that live here, and why each one exists:

* a refusal goes to the PM **under its own action key**, so it can never be
  mistaken for - or suppress - the release route of the same comment;
* ``permanent`` is decided by the creation mode, not by wording: the wording
  belongs to ``allowlist.check`` and may be rephrased without changing what
  happened;
* a streak counts **rounds**, not attempts - one round can retry a route more
  than once, and a person should hear about a stuck verdict on the same rhythm
  they hear about a stuck session;
* ``alerted_at`` is the one-shot proof, so the same comment never alerts twice
  even if it is refused again afterwards.

A mixin, like its parent: it reads ``self.store`` and the routers on ``Engine``.
"""
from __future__ import annotations

from .. import markers
from ..store import action_id
from . import frame_text as frame_text_mod


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RefusedRoutes:
    """Mixin: the refused-verdict ledger, the retry streak and its alert."""

    # A refusal is permanent only when no retry can ever succeed (the creation
    # mode forbids the session), so asking again is asking the same question.
    PERMANENT_REFUSAL_PREFIXES = ("creation_mode_forbidden",)

    # How many refused rounds in a row earn one visible alert, and the event
    # type it carries. Three matches the E61 liveness window, so a person hears
    # about a stuck verdict on the same rhythm they hear about a stuck session.
    REFUSED_RETRY_ROUNDS = 3
    # Re-alert cadence after the first: once ever is amnesia, every round is noise.
    REFUSED_REALERT_ROUNDS = 30
    REFUSED_RETRY_ROW = "route.refused_retryable"

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
                   "text": frame_text_mod.qa_notice_text(
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
        # One alert is a moment, not a report (#122/#123/#129: 4243/4240/2839
        # refused rounds, one alert each). First at 3, then every 30.
        if not streak.get("alerted_at"):
            streak["alerted_at"] = _now()
        elif streak["rounds"] % self.REFUSED_REALERT_ROUNDS:
            return
        else:
            streak["realerted_at"] = _now()
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
        """A landed dispatch ends the streak - and the alert stamps with it.

        Keeping ``alerted_at`` became a trap once re-alerting existed: a route
        that succeeded and was later refused for a new cause could never
        announce it. A new streak earns its own first alert.
        """
        streak = route.get("refusal_streak")
        if isinstance(streak, dict):
            streak["rounds"] = 0
            streak.pop("alerted_at", None)
            streak.pop("realerted_at", None)
