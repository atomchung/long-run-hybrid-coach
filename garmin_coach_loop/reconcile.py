"""Reconcile actionable sessions against matched actuals -- mechanically, not by asking.

Marking a completed session "completed" has no trade-off, no athlete-only preference and
no outward risk, so it must never surface as a question or cost anyone sixty lines of
hand-written JSON (both happened; see issue #22). The context builder already does the
hard part -- ``recent_actuals`` carries deterministic planned<->actual matches -- but
nothing wrote the result back, which is exactly how a plan went two days stale and why
``match_status`` could not be trusted by the next decision.

This module closes that gap deterministically and narrowly:

- only an attached actual (``match_confidence`` in ``ATTACHED_MATCH_CONFIDENCES`` -- the
  provider's own pairing, or the product's ownership of an unambiguous day) with
  ``completion == "completed"`` is written;
- ``probable`` matches and non-completed completions are reported, never guessed;
- unmatched actionable sessions keep their current status and are listed;
- every write is one ``planned_actual_reconciled`` DecisionEvent per session, riding the
  same review_week/adjust shape a human reconciliation already used (store commit 7),
  fenced by ``validation._check_reconcile_semantics`` so it can record the one fact and
  do nothing else;
- partial-versus-completed judgment stays absent on purpose. ``completed`` here means
  "this session was trained, and this is the activity"; how well it went is read from the
  activity's own numbers by the coach, not decided by a duration ratio in this module
  (issue #22's scope line, reaffirmed by issue #51).

Re-running is naturally idempotent: a reconciled session is no longer actionable, so
the second pass proposes nothing. A *validation*
failure mid-run raises after earlier commits have landed (append-only); the rerun
continues with whatever is still actionable. A process kill inside one commit has the
store's normal consequence -- doctor reports the torn commit and blocks until repaired;
this module adds no crash-safety beyond what ``apply_decision`` provides.

No CLI wiring here on purpose -- a parallel branch owns ``cli.py`` edits right now; the
subcommand lands separately once that merges.
"""

from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
from typing import Any

from .context_core import MATCH_STATUS_TO_CALENDAR_STATUS
from .store import apply_decision, status_store
from .validation import ACTIONABLE_MATCH_STATUSES, ATTACHED_MATCH_CONFIDENCES, validate_bundle


AUTHORED_BY = {"model": "deterministic", "skill_version": None, "harness": "garmin_coach_loop.reconcile"}


def _project_calendar(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "session_id": session["session_id"],
            "date": session["scheduled_date"],
            "sport": session["sport"],
            "cost": session["cost"],
            "status": MATCH_STATUS_TO_CALENDAR_STATUS[session["match_status"]],
        }
        for session in (plan.get("week") or {}).get("sessions", [])
    ]


def propose_reconciliation(plan: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Pure planning step: decide what may be written and what may only be reported.

    Returns ``{"proposals": [...], "ambiguous": [...], "unmatched_planned": [...]}``.
    A proposal pairs one actionable session with the single attached completed actual
    that backs it. Ambiguous entries (probable matches, or attached-but-not-completed
    completions such as partial) carry a reason and are never written. A probable match
    also carries the session's own date, sport and planned minutes beside the actual's
    duration (issue #30) -- enough for a one-sentence question without a second lookup
    by id; the write it would take stays exactly as gated as before.
    """
    sessions = (plan.get("week") or {}).get("sessions") or []
    actuals = context.get("recent_actuals") or []
    # A session whose outcome is already recorded has nothing left for a human to
    # confirm. Reporting its probable actual anyway asks the athlete to settle a
    # question the plan already answers -- the mirror image of the failure the Skill
    # forbids, and it repeats on every refresh because the actual never stops being
    # probable. A "missed" session keeps its entry on purpose: "you recorded this as
    # missed, and here is an activity that looks like it" is exactly a human's call.
    settled = {
        session.get("session_id")
        for session in sessions
        if isinstance(session, dict) and session.get("match_status") in {"completed", "partial"}
    }
    # An actual may now name a session from an earlier week of the cycle: matching runs
    # over the whole cycle so the coach can read a prescription beside what came back for
    # it (context.cycle_sessions). Nothing outside the current week is reconcilable --
    # this module only ever writes into week.sessions -- and reporting one as "ambiguous,
    # a human confirms" would ask the athlete to settle a question no answer can act on.
    sessions_by_id = {
        session.get("session_id"): session for session in sessions if isinstance(session, dict)
    }
    week_session_ids = set(sessions_by_id)
    by_session: dict[str, list[dict[str, Any]]] = {}
    ambiguous: list[dict[str, Any]] = []
    for actual in actuals:
        if not isinstance(actual, dict):
            continue
        sid = actual.get("planned_session_id")
        confidence = actual.get("match_confidence")
        if not isinstance(sid, str) or not sid or sid not in week_session_ids:
            continue
        if confidence in ATTACHED_MATCH_CONFIDENCES:
            by_session.setdefault(sid, []).append(actual)
        elif confidence == "probable" and sid not in settled:
            session = sessions_by_id[sid]
            ambiguous.append(
                {
                    "session_id": sid,
                    "activity_id": actual.get("activity_id"),
                    "reason": "match_confidence is probable; a human confirms, this tool does not guess",
                    # What a one-sentence question needs and nothing more -- "Tuesday's
                    # 45-minute easy run, is this the 40-minute run that came back?" --
                    # without asking the reader to cross-reference session_id/activity_id
                    # against the rest of the context first.
                    "scheduled_date": session.get("scheduled_date"),
                    "sport": session.get("sport"),
                    "planned_minutes": session.get("planned_minutes"),
                    "actual_minutes": actual.get("duration_minutes"),
                }
            )

    proposals: list[dict[str, Any]] = []
    unmatched_planned: list[dict[str, Any]] = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        sid = session.get("session_id")
        if (
            not isinstance(sid, str)
            or session.get("match_status") not in ACTIONABLE_MATCH_STATUSES
        ):
            continue
        backing = by_session.get(sid, [])
        completed = [a for a in backing if a.get("completion") == "completed"]
        # Any multiplicity is a conflict, even "one completed plus one partial": the
        # matcher's one-to-one rule means a real context never produces two attached
        # actuals for one session, so seeing two is evidence the data is wrong, not a
        # tie to break in favour of the completed one.
        if len(backing) == 1 and len(completed) == 1:
            proposals.append({"session_id": sid, "actual": completed[0]})
        elif len(backing) > 1:
            ambiguous.append(
                {
                    "session_id": sid,
                    "activity_id": [a.get("activity_id") for a in backing],
                    "reason": "more than one attached actual claims this session",
                }
            )
        elif backing:
            ambiguous.append(
                {
                    "session_id": sid,
                    "activity_id": [a.get("activity_id") for a in backing],
                    "reason": "attached actual exists but its completion is not completed",
                }
            )
        elif any(entry.get("session_id") == sid for entry in ambiguous):
            # A probable match already put this session in ambiguous -- listing it as
            # "unmatched" too would claim two contradictory things about one session.
            continue
        else:
            unmatched_planned.append({"session_id": sid, "scheduled_date": session.get("scheduled_date")})

    return {"proposals": proposals, "ambiguous": ambiguous, "unmatched_planned": unmatched_planned}


def build_reconcile_bundle(
    plan: dict[str, Any],
    proposal: dict[str, Any],
    context: dict[str, Any],
    *,
    created_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the (after, event) pair recording one proposal against ``plan``."""
    session_id = proposal["session_id"]
    actual = proposal["actual"]
    activity_id = actual.get("activity_id", "unknown-activity")
    actual_date = actual.get("date", "unknown-date")
    duration = actual.get("duration_minutes")
    before_status = next(
        session.get("match_status")
        for session in plan["week"]["sessions"]
        if session.get("session_id") == session_id
    )

    after = copy.deepcopy(plan)
    after["version"] = plan["version"] + 1
    for session in after["week"]["sessions"]:
        if session.get("session_id") == session_id:
            session["match_status"] = "completed"

    provider_sources = [
        entry.get("source")
        for entry in (context.get("sources") or [])
        if isinstance(entry, dict) and entry.get("source") != "coach-loop-state-store"
    ]
    provider = provider_sources[0] if provider_sources else "unknown-source"

    duration_text = f"{duration} 分鐘" if duration is not None else "時長未回報"
    # The event says which evidence backed the write. "Reconciled" alone would read the
    # same for a provider pairing and for the product's own ownership evidence, and a
    # later review has to be able to tell those apart.
    basis_text = (
        "provider 已配對"
        if actual.get("match_confidence") == "matched"
        else "產品交付的課表,當天該類型唯一"
    )
    event = {
        "schema_version": "1.0",
        "event_id": f"evt-{str(actual_date).replace('-', '')}-reconcile-{session_id}",
        "mode": "review_week",
        "plan_id": plan["plan_id"],
        "plan_version_before": plan["version"],
        "plan_version_after": after["version"],
        "action": "adjust",
        "session_id": session_id,
        "inputs_used": [
            f"coach-loop-state-store: current PlanState v{plan['version']}",
            f"{provider}: recent_actuals planned-actual matching",
        ],
        "evidence": [
            {
                "field": f"recent_actuals.{session_id}",
                "observation": (
                    f"activity {activity_id} matched {session_id}:completed,"
                    f"{duration_text},日期 {actual_date},依據 {basis_text}"
                ),
            },
            {
                "field": f"current_plan.{session_id}.match_status",
                "observation": f"計畫仍記 {before_status},與已完成的事實不一致;對帳為 completed",
            },
        ],
        "unknowns": list(context.get("unknowns") or []),
        "reason_codes": ["planned_actual_reconciled"],
        "change": {
            "before": f"{session_id} match_status = {before_status}",
            "after": f"{session_id} match_status = completed",
            "summary": f"對帳:{session_id} 已由 matched 活動 {activity_id} 完成,僅記錄事實不改處方",
        },
        "goal_effect": {
            "week": f"{session_id} 記為已完成;本週處方與結構未變",
            "cycle": "僅記錄完成事實,不影響週期方向",
        },
        "next_review_condition": "下一次 review_week 比對本週 planned 與 actual 全貌",
        "created_at": created_at,
        "authored_by": dict(AUTHORED_BY),
        "initiative": "reactive",
        "trigger": f"matched activity {activity_id} completed actionable session {session_id}",
    }
    return after, event


def apply_reconciliation(
    state_dir: Path | str,
    context: dict[str, Any],
    *,
    now: dt.datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Propose and (unless ``dry_run``) commit one reconcile event per matched session.

    Sequential by design: each commit bumps the plan version, and the next proposal is
    rebuilt against the just-committed plan, so version numbers stay exact. A
    *validation* failure raises after earlier commits have landed -- append-only stores
    do not roll back -- and the rerun proposes only what is still actionable. A process
    kill in the middle of one commit has the same consequence it has for any
    ``apply_decision``: the store's doctor reports the torn commit and blocks until it
    is repaired; this module adds no crash-safety beyond what the store provides.

    ``dry_run`` validates every proposed bundle without writing, so its "would apply"
    answer is one an actual run can honor.
    """
    moment = now if now is not None else dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        # A naive datetime would isoformat() without an offset and fail event
        # validation downstream; treat it as UTC rather than producing a bad event.
        moment = moment.replace(tzinfo=dt.timezone.utc)
    created_at = moment.replace(microsecond=0).isoformat()

    plan = status_store(state_dir)["current_plan"]
    report = propose_reconciliation(plan, context)
    applied: list[dict[str, Any]] = []
    # The context is this run's input snapshot, and its goal_context.plan_version must
    # track the plan each sequential commit is validated against: aligned to the
    # store's current version up front (a rerun may carry a snapshot taken before
    # earlier commits landed -- the proposals are computed against the current plan
    # either way), then advanced after each commit, because the next event genuinely
    # faces the new version.
    rolling_context = copy.deepcopy(context)
    goal_context = rolling_context.get("goal_context")
    if isinstance(goal_context, dict):
        goal_context["plan_version"] = plan["version"]

    for proposal in report["proposals"]:
        after, event = build_reconcile_bundle(plan, proposal, rolling_context, created_at=created_at)
        if dry_run:
            validation = validate_bundle(rolling_context, plan, after, event)
            applied.append(
                {
                    "session_id": proposal["session_id"],
                    "event_id": event["event_id"],
                    "dry_run": True,
                    "validation": validation["status"],
                    "errors": validation["errors"],
                }
            )
            continue
        result = apply_decision(state_dir, context=rolling_context, after=after, event=event)
        applied.append(
            {
                "session_id": proposal["session_id"],
                "event_id": event["event_id"],
                "version": result["current_version"],
                "idempotent_replay": result["idempotent_replay"],
            }
        )
        plan = after
        goal_context = rolling_context.get("goal_context")
        if isinstance(goal_context, dict):
            goal_context["plan_version"] = after["version"]
        # The next sequential commit must be validated against the PlanState produced by
        # this one, not merely carry its version number. Exact projection validation
        # intentionally rejects a context whose calendar still says the reconciled
        # session is planned.
        rolling_context["current_calendar"] = _project_calendar(after)

    # A dry run whose bundles would be blocked must say so at the top level too --
    # "passed" over a list of blocked validations is exactly the layered dishonesty
    # this repo's delivery reporting forbids.
    status = "passed"
    if dry_run and any(entry.get("validation") == "blocked" for entry in applied):
        status = "blocked"
    return {
        "status": status,
        "dry_run": dry_run,
        "applied": applied,
        "ambiguous": report["ambiguous"],
        "unmatched_planned": report["unmatched_planned"],
    }
