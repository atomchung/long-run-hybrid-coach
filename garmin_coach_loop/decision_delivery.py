"""Exact future calendar effects approved with one plan decision (issue #272).

Each effect keeps an existing one-session delivery/withdrawal set. This permits
skipping an expired item without changing another approved workout's content. The
receipt binds these sets to the committed plan; the existing journal still fences
provider mutations. A removed future session is withdrawn from that receipt's
historical before/after evidence, never reintroduced into the current week.
"""
from __future__ import annotations

import copy
import datetime as dt
from typing import Any

from .delivery import (
    DELIVER_DIRECTION, WITHDRAW_DIRECTION, DeliveryError, IntervalsTransport,
    _AttemptJournal, _confirmation_still_describes_the_event, _observe_superseded_event,
    _open_attempt, _owned_matches, _proposal_hash, _release_if_untouched, _set_hash,
    approve_delivery_set, approve_withdrawal_set, deliver_approved_set,
    prepare_delivery_set, prepare_withdrawal_set, withdraw_approved_set,
)
from .store import (
    StateStoreError, canonical_hash, close_delivery_attempt, mark_delivery_attempt_recorded,
    pending_delivery_attempt, read_current_plan,
)
from .validation import ACTIONABLE_MATCH_STATUSES, ATTACHED_MATCH_CONFIDENCES


def _sessions(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["session_id"]: s for s in plan.get("week", {}).get("sessions", [])}


def _prescription(plan: dict[str, Any], approved: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(plan)
    value.pop("version", None)
    approved_sessions = _sessions(approved)
    for sid, session in _sessions(value).items():
        # A verified execution closes that item, without invalidating the unchanged
        # prescriptions still ahead. It is checked and skipped separately below.
        if session.get("match_status") in {"completed", "partial", "missed"} and sid in approved_sessions:
            session["match_status"] = approved_sessions[sid].get("match_status")
        execution = session.get("execution") or {}
        for key in ("delivery_state", "external_id", "superseded_external_id"):
            execution.pop(key, None)
    return value


def prepare_decision_delivery(
    before: dict[str, Any] | None, after: dict[str, Any], *, transport: IntervalsTransport,
    today: str, now: dt.datetime, publish_new_workouts: bool = False,
    read_run_threshold_hr: Any = None,
) -> dict[str, Any]:
    """Prepare only affected old deliveries, plus new ones when explicitly requested."""
    previous, current = _sessions(before or {}), _sessions(after)
    effects, unresolved = [], []
    threshold_read = False
    threshold = None

    def read_once():
        nonlocal threshold_read, threshold
        if not threshold_read:
            threshold = read_run_threshold_hr() if read_run_threshold_hr else None
            threshold_read = True
        return threshold

    for sid in sorted(set(previous) | set(current)):
        old, session = previous.get(sid), current.get(sid)
        old_execution = (old or {}).get("execution") or {}
        previous_id = old_execution.get("external_id") or old_execution.get("superseded_external_id")
        old_pending = bool(old and old.get("scheduled_date", "") >= today
                           and old.get("match_status") in ACTIONABLE_MATCH_STATUSES)
        if previous_id and not old_pending:
            continue  # A moved historical delivery remains history.
        execution = (session or {}).get("execution") or {}
        pending = bool(session and session.get("scheduled_date", "") >= today
                       and session.get("match_status") in ACTIONABLE_MATCH_STATUSES)
        changed = old != session
        replace_old = bool(previous_id and old_pending and changed)
        executable = bool(pending and session.get("sport") in {"running", "strength"}
                          and execution.get("publish_supported") is True)
        deliver = executable and execution.get("delivery_state") == "not_published" and (
            replace_old or publish_new_workouts
        )
        withdraw = replace_old and not executable and (
            session is None or execution.get("superseded_external_id")
        )
        if not deliver and not withdraw:
            continue
        try:
            retired = None
            if deliver:
                proposal_set = prepare_delivery_set(
                    after, [sid], now=now, read_run_threshold_hr=read_once,
                    read_run_sport_settings=transport.require_run_sport_settings,
                )
                # The recorded id is an exact target check, not a calendar sync.
                if previous_id:
                    observed = _observe_superseded_event(
                        transport.find_event, session_id=sid, event_id=previous_id,
                        owned_external_id=proposal_set["items"][0]["owned_external_id"],
                    )
                    if observed.get("present") and observed.get("scheduled_date", "") < today:
                        unresolved.append({"session_id": sid, "reason": "the delivered entry is past history; left unchanged"})
                        continue
            else:
                source = after
                if session is None:
                    source = copy.deepcopy(before)
                    retired = copy.deepcopy(old)
                    target = _sessions(source)[sid]
                    target["execution"] = {**target["execution"], "delivery_state": "not_published",
                                           "external_id": None, "superseded_external_id": previous_id}
                proposal_set = prepare_withdrawal_set(source, [sid], read_event=transport.find_event, now=now)
                proposal_set["plan_version"] = after["version"]
                proposal_set["proposal_hash"] = _set_hash(proposal_set)
                observed = proposal_set["items"][0]["observed_event"]
                if observed.get("present") and observed.get("scheduled_date", "") < today:
                    unresolved.append({"session_id": sid, "reason": "the delivered entry is past history; left unchanged"})
                    continue
            effects.append({"set": proposal_set, "previous_event_id": previous_id, "retired_session": retired})
        except DeliveryError:
            # An unavailable optional delivery read must not prevent saving a valid
            # plan. No exact set was approved for this item; do not fabricate one later.
            unresolved.append({"session_id": sid, "reason": "exact delivery preview unavailable; this calendar effect is not approved"})
    return {"effects": effects, "unresolved": unresolved}


def delivery_preview(prepared: dict[str, Any]) -> dict[str, Any]:
    workouts, withdrawals, settings = [], [], []
    for effect in prepared["effects"]:
        proposal_set = effect["set"]
        item = proposal_set["items"][0]
        if proposal_set["direction"] == DELIVER_DIRECTION:
            settings.extend(proposal_set.get("settings_changes", []))
            workouts.append({"session_id": item["session_id"], "workout": item["workout"],
                             "operation": "replace" if effect["previous_event_id"] else "publish",
                             **item["preview"]})
        else:
            observed = item["observed_event"]
            withdrawals.append({"session_id": item["session_id"], "event_present": observed["present"],
                                "event_date": observed.get("scheduled_date"), "event_name": observed.get("name")})
    return {"workouts": workouts, "withdrawals": withdrawals,
            "settings_changes": list({canonical_hash(s): s for s in settings}.values()),
            "unresolved": prepared["unresolved"]}


def _rebind_version(original: dict[str, Any], version: int) -> dict[str, Any]:
    result = copy.deepcopy(original)
    result["plan_version"] = version
    if result["direction"] == DELIVER_DIRECTION:
        for item in result["items"]:
            item["plan_version"] = version
            item["proposal_hash"] = _proposal_hash(item)
    result["proposal_hash"] = _set_hash(result)
    return result


class _FutureTransport:
    """The existing writer may only mutate this exact still-future owned target."""
    def __init__(self, transport: IntervalsTransport, previous_id: str | None, owned: str, today: str,
                 *, read_only: bool = False):
        self.transport, self.previous_id, self.owned, self.today = transport, previous_id, owned, today
        self.read_only = read_only

    def __getattr__(self, name: str):
        return getattr(self.transport, name)

    def bulk_upsert(self, payload: dict[str, Any]):
        if self.read_only:
            raise DeliveryError("this approval may only verify its earlier provider effect")
        if str(payload.get("start_date_local", ""))[:10] < self.today:
            raise DeliveryError("an expired approved workout is calendar history")
        if self.previous_id:
            event = self.transport.find_event(self.previous_id)
            if event is not None:
                if event.get("external_id") != self.owned:
                    raise DeliveryError("the recorded event is no longer product-owned")
                if str(event.get("start_date_local", ""))[:10] < self.today:
                    raise DeliveryError("the recorded event is now calendar history")
        return self.transport.bulk_upsert(payload)

    def update_run_sport_settings(self, payload: dict[str, Any]):
        if self.read_only:
            raise DeliveryError("an expired delivery may not change provider settings")
        return self.transport.update_run_sport_settings(payload)

    def delete_event(self, event_id: str):
        if self.read_only:
            raise DeliveryError("this approval may only verify its earlier provider effect")
        return self.transport.delete_event(event_id)


def _withdraw_retired(state_dir: Any, proposal_set: dict[str, Any], *, transport: IntervalsTransport,
                      today: str, now: dt.datetime) -> dict[str, Any]:
    """Remove a future delivery whose session this confirmed week roll retired.

    The caller has verified the immutable approved after-plan and its exact set.
    No PlanState field exists to clear: the journal records verified absence, then
    closes. A later retry proves absence again; it never assumes absence from a list.
    """
    item = proposal_set["items"][0]
    attempt = _open_attempt(state_dir, kind="withdrawal", proposal_set=proposal_set, operations=[{
        "session_id": item["session_id"], "operation": "delete", "owned_external_id": item["owned_external_id"],
        "scheduled_date": item["scheduled_date"], "external_id": item["superseded_external_id"],
    }])
    journal = _AttemptJournal(state_dir, attempt["attempt_id"])
    try:
        plan = read_current_plan(state_dir)["current_plan"]
        if item["session_id"] in _sessions(plan):
            raise DeliveryError("retired session is present again")
        if len(_owned_matches(transport, plan, item["owned_external_id"])) > 1:
            raise DeliveryError("multiple events carry this product-owned marker")
        event = transport.find_event(item["superseded_external_id"])
        _confirmation_still_describes_the_event(item, event)
        if event is not None:
            if event.get("external_id") != item["owned_external_id"]:
                raise DeliveryError("the retired event is no longer product-owned")
            if str(event.get("start_date_local", ""))[:10] < today:
                raise DeliveryError("the retired entry has become calendar history")
            journal.record(item["session_id"], "mutation_started")
            transport.delete_event(item["superseded_external_id"])
            journal.record(item["session_id"], "mutated_unverified")
            if transport.find_event(item["superseded_external_id"]) is not None:
                raise DeliveryError("the retired entry remains after deletion")
        journal.record(item["session_id"], "verified", result={"withdrawal": {
            "session_id": item["session_id"], "withdrawn_external_id": item["superseded_external_id"],
        }})
        mark_delivery_attempt_recorded(state_dir, attempt_id=attempt["attempt_id"],
                                      session_ids=[item["session_id"]], plan_version=plan["version"])
        close_delivery_attempt(state_dir, attempt_id=attempt["attempt_id"])
        return {"session_id": item["session_id"]}
    except Exception:
        _release_if_untouched(state_dir, attempt["attempt_id"])
        raise


def apply_decision_delivery(
    state_dir: Any, approved_plan: dict[str, Any], prepared: dict[str, Any], *,
    transport: IntervalsTransport, approved_by: str, today: str, now: dt.datetime,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    delivered, withdrawn, skipped, unresolved, settings = [], [], [], list(prepared["unresolved"]), []
    executed = {a.get("planned_session_id") for a in (context or {}).get("recent_actuals", [])
                if a.get("match_confidence") in ATTACHED_MATCH_CONFIDENCES
                and a.get("completion") in {"completed", "partial"}}
    effects = prepared["effects"]
    flags = ((context or {}).get("constraints") or {}).get("red_flags") or {}
    symptomatic = any(value is True for value in flags.values())
    for index, effect in enumerate(effects):
        original = effect["set"]
        item = original["items"][0]
        sid = item["session_id"]
        current = read_current_plan(state_dir)["current_plan"]
        if canonical_hash(_prescription(current, approved_plan)) != canonical_hash(_prescription(approved_plan, approved_plan)):
            unresolved.append({"session_id": sid, "reason": "current prescription differs from this approval"})
            break
        session = _sessions(current).get(sid) or effect["retired_session"]
        execution = (session or {}).get("execution") or {}
        is_delivery = original["direction"] == DELIVER_DIRECTION
        if is_delivery and execution.get("delivery_state") == "intervals_accepted":
            delivered.append({"session_id": sid, "delivery_state": "intervals_accepted"})
            continue
        if not is_delivery and not effect["retired_session"] and not execution.get("superseded_external_id"):
            withdrawn.append({"session_id": sid})
            continue
        attempt = pending_delivery_attempt(state_dir)
        expired = (session or {}).get("scheduled_date", "") < today
        completed = sid in executed or (session or {}).get("match_status") not in ACTIONABLE_MATCH_STATUSES
        symptom_today = is_delivery and symptomatic and (session or {}).get("scheduled_date") == today
        read_only = expired or completed or symptom_today
        if read_only:
            reason = ("today's explicitly reported symptom requires a human decision; calendar left unchanged"
                      if symptom_today else "past or already executed; calendar left unchanged")
            skipped.append({"session_id": sid, "reason": reason})
            if not attempt or sid not in attempt["session_ids"]:
                continue
            # An interrupted earlier mutation may still verify without another write.
            # If it does not, the existing recovery fence remains honest about that
            # unresolved effect; this path never abandons it to publish another item.
        version = attempt["plan_version"] if attempt else current["version"]
        rebound = _rebind_version(original, version)
        if attempt and attempt["proposal_hash"] != rebound["proposal_hash"]:
            unresolved.append({"session_id": sid, "reason": "another approved delivery must finish first"})
            break
        try:
            if is_delivery and effect["previous_event_id"] and not attempt:
                previous_event = transport.find_event(effect["previous_event_id"])
                if previous_event is not None:
                    if previous_event.get("external_id") != item["owned_external_id"]:
                        unresolved.append({"session_id": sid, "reason": "the recorded event is no longer product-owned"})
                        continue
                    if str(previous_event.get("start_date_local", ""))[:10] < today:
                        skipped.append({"session_id": sid, "reason": "the recorded event is now calendar history; left unchanged"})
                        continue
            protected = _FutureTransport(transport, effect["previous_event_id"], item["owned_external_id"],
                                         today, read_only=read_only)
            if is_delivery:
                result = deliver_approved_set(state_dir, rebound,
                    approve_delivery_set(rebound, approved_by=approved_by, approved_at=now), transport=protected, now=now)
                delivered.extend({"session_id": r["observation"]["session_id"], "delivery_state": "intervals_accepted"}
                                 for r in result["item_receipts"])
                settings.extend(result.get("settings_changes", []))
            elif effect["retired_session"]:
                approve_withdrawal_set(rebound, approved_by=approved_by, approved_at=now)
                withdrawn.append(_withdraw_retired(state_dir, rebound, transport=protected, today=today, now=now))
                result = {"status": "passed"}
            else:
                result = withdraw_approved_set(state_dir, rebound,
                    approve_withdrawal_set(rebound, approved_by=approved_by, approved_at=now),
                    transport=protected, now=now, today=today)
                withdrawn.extend({"session_id": r["session_id"]} for r in result["withdrawn"])
            unresolved.extend(result.get("unresolved", []))
            if result.get("status") != "passed" or result.get("attempt_open"):
                break
        except (DeliveryError, StateStoreError):
            unresolved.append({"session_id": sid, "reason": "approved calendar effect did not complete; retry this same proposal"})
            if pending_delivery_attempt(state_dir):
                break
    else:
        index = len(effects)
    for effect in effects[index + 1:]:
        unresolved.append({"session_id": effect["set"]["items"][0]["session_id"], "reason": "not attempted"})
    return {"status": "partial" if unresolved else "passed", "delivered": delivered,
            "withdrawn": withdrawn, "skipped": skipped, "unresolved": unresolved,
            "settings_changes": settings, "attempt_open": pending_delivery_attempt(state_dir) is not None,
            "plan_version": read_current_plan(state_dir)["current_version"]}
