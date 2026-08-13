"""Approved, deterministic delivery of current PlanState workouts to Intervals.icu.

The model chooses the workout once, as canonical executable content inside PlanState.
This module derives the exact preview and provider payload from that session, writes one
product-owned event after approval, reads it back, and returns an observation only when
all three representations match. It does not claim Garmin Connect or device delivery.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .source_intervals import (
    BASE_URL,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    IntervalsCredentials,
    authorization_header,
)
from .store import canonical_hash, delivery_session_content_hash
from .validation import validate_plan_state


PROPOSAL_SCHEMA_VERSION = "1.0"
APPROVAL_SCHEMA_VERSION = "1.0"
DELIVERY_SET_SCHEMA_VERSION = "1.0"


class DeliveryError(RuntimeError):
    """A delivery boundary was blocked before observable state could advance."""


def _utc_iso(moment: dt.datetime | None = None) -> str:
    value = moment or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        raise DeliveryError("delivery timestamp must include a timezone")
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _exact_keys(value: dict[str, Any], required: set[str], optional: set[str], field: str) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing or extra:
        raise DeliveryError(
            f"{field} fields do not match the contract"
            f"; missing={sorted(missing)}; extra={sorted(extra)}"
        )


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeliveryError(f"{field} must be an object")
    return value


def _whole(value: float, field: str) -> int:
    rounded = round(value)
    if abs(value - rounded) > 1e-9:
        raise DeliveryError(f"{field} must resolve to a whole device unit")
    return int(rounded)


def session_content_hash(session: dict[str, Any]) -> str:
    """Compatibility entry point for the store-owned compare-and-commit hash."""
    return delivery_session_content_hash(session)


def _contains_pace_target(steps: list[dict[str, Any]]) -> bool:
    return any(
        step["target"]["kind"] == "pace"
        if step["kind"] == "work"
        else _contains_pace_target(step["steps"])
        for step in steps
    )


def _contains_hr_ceiling(steps: list[dict[str, Any]]) -> bool:
    return any(
        step["target"]["kind"] == "hr_ceiling"
        if step["kind"] == "work"
        else _contains_hr_ceiling(step["steps"])
        for step in steps
    )


def _pace_text(seconds: int) -> str:
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}:{remainder:02d}"


def _duration_text(duration: dict[str, Any]) -> str:
    if duration["kind"] == "distance":
        meters = duration["meters"]
        return f"{meters // 1000}km" if meters % 1000 == 0 else f"{meters}mtr"
    seconds = duration["seconds"]
    minutes, remainder = divmod(seconds, 60)
    if minutes and remainder:
        return f"{minutes}m{remainder}s"
    return f"{minutes}m" if minutes else f"{remainder}s"


def _work_line(step: dict[str, Any]) -> str:
    target = step["target"]
    target_text = ""
    if target["kind"] == "pace":
        target_text = (
            f" {_pace_text(target['low_seconds_per_km'])}-"
            f"{_pace_text(target['high_seconds_per_km'])}/km Pace"
        )
    return f"- {step['name']} {_duration_text(step['duration'])}{target_text}"


def intervals_description(steps: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for step in steps:
        if step["kind"] == "work":
            lines.append(_work_line(step))
            continue
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"{step['repetitions']}x")
        lines.extend(_work_line(child) for child in step["steps"])
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _hr_ceiling_description(steps: list[dict[str, Any]]) -> str:
    """Human-readable prose for a workout carrying an hr_ceiling target.

    Deliberately not `- `-prefixed intervals workout-text syntax: that syntax
    silently drops every absolute-bpm variant (verified 2026-08-12), so a line
    that looks parseable here would promise a target the provider cannot keep.
    Distance-duration steps and repeats are rejected rather than guessed at --
    the doc-JSON shape carrying the ceiling is only verified for time-based,
    non-repeating work steps.
    """
    lines: list[str] = []
    for step in steps:
        if step["kind"] == "repeat":
            raise DeliveryError("hr_ceiling workout must not contain a repeat step")
        if step["duration"]["kind"] == "distance":
            raise DeliveryError("hr_ceiling workout step must use a time duration")
        line = f"{step['name']} {_duration_text(step['duration'])}"
        if step["target"]["kind"] == "hr_ceiling":
            line += f" 心率上限 {step['target']['ceiling_bpm']} bpm"
        lines.append(line)
    return "\n".join(lines)


def _plan_session(plan: dict[str, Any], session_id: str) -> dict[str, Any]:
    if not isinstance(session_id, str) or not session_id.strip():
        raise DeliveryError("delivery session_id must be a non-empty string")
    matches = [
        session for session in (plan.get("week") or {}).get("sessions", [])
        if isinstance(session, dict) and session.get("session_id") == session_id
    ]
    if len(matches) != 1:
        raise DeliveryError(f"current plan must contain exactly one session {session_id!r}")
    return matches[0]


def _current_plan_is_valid(plan: dict[str, Any]) -> None:
    report = validate_plan_state(plan)
    if report["status"] != "passed":
        raise DeliveryError(f"current PlanState is invalid: {report['errors']}")


def _calendar_entry_from_session(session: dict[str, Any]) -> dict[str, Any]:
    """Deliver a strength session as a titled calendar entry, not as a workout.

    The plan owns the sets, reps and load; the watch has no structured strength
    target it could execute from them, and inventing one would be the structured
    strength delivery this product deliberately defers. Publishing the title puts
    the day on the calendar so planned and actual line up on one surface, while the
    prescription rides along as text the athlete reads rather than the device follows.
    """
    purpose = session.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        raise DeliveryError("strength delivery requires a purpose to title the calendar entry")
    prescription = session.get("prescription")
    return {
        "sport": "strength",
        "name": purpose.strip(),
        "scheduled_date": session["scheduled_date"],
        "description": prescription.strip() if isinstance(prescription, str) else "",
    }


def _workout_from_session(session: dict[str, Any]) -> dict[str, Any]:
    if session.get("sport") == "strength":
        return _calendar_entry_from_session(session)
    structured = session.get("structured_workout")
    if not isinstance(structured, dict):
        raise DeliveryError(
            "selected session has no canonical structured_workout in current PlanState"
        )
    steps = structured.get("steps")
    if not isinstance(steps, list) or not steps:
        raise DeliveryError("selected current PlanState workout has no executable steps")
    canonical = copy.deepcopy(structured)
    canonical["sport"] = "running"
    canonical["scheduled_date"] = session["scheduled_date"]
    if _contains_hr_ceiling(canonical["steps"]):
        canonical["description"] = _hr_ceiling_description(canonical["steps"])
    else:
        canonical["description"] = intervals_description(canonical["steps"])
    return canonical


def _proposal_hash(proposal: dict[str, Any]) -> str:
    material = {key: value for key, value in proposal.items() if key != "proposal_hash"}
    return canonical_hash(material)


def prepare_delivery_proposal(
    current_plan: dict[str, Any],
    session_id: str,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    _current_plan_is_valid(current_plan)
    if current_plan.get("status") != "active":
        raise DeliveryError("only an active current plan may publish workouts")
    session = _plan_session(current_plan, session_id)
    if (
        session.get("sport") not in {"running", "strength"}
        or session.get("match_status") not in {"planned", "moved", "replaced"}
    ):
        raise DeliveryError("delivery session must be an executable running or strength session")
    execution = session.get("execution") if isinstance(session.get("execution"), dict) else {}
    if execution.get("publish_supported") is not True:
        raise DeliveryError("selected session does not support structured publishing")
    if execution.get("delivery_state") != "not_published" or execution.get("external_id") is not None:
        raise DeliveryError("selected session already has delivery state")

    workout = _workout_from_session(session)
    threshold_pace = (current_plan.get("athlete_baseline") or {}).get(
        "threshold_pace_sec_per_km"
    )
    if _contains_pace_target(workout.get("steps") or []) and (
        isinstance(threshold_pace, bool)
        or not isinstance(threshold_pace, int)
        or threshold_pace < 1
    ):
        raise DeliveryError(
            "absolute pace delivery requires measured "
            "athlete_baseline.threshold_pace_sec_per_km"
        )

    selected_session_hash = session_content_hash(session)
    delivery_instance = {
        "plan_id": current_plan["plan_id"],
        "week_start": (current_plan.get("week") or {}).get("start"),
        "session_id": session["session_id"],
    }
    owned_external_id = "gcl:" + canonical_hash(delivery_instance)[:32]
    proposal: dict[str, Any] = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_id": f"delivery-{canonical_hash({'session': selected_session_hash, 'workout': workout})[:20]}",
        "plan_id": current_plan["plan_id"],
        "plan_version": current_plan["version"],
        "session_id": session["session_id"],
        "session_content_hash": selected_session_hash,
        "owned_external_id": owned_external_id,
        "workout": workout,
        "preview": {
            "plan_prescription": session.get("prescription"),
            "delivered_description": workout["description"],
            "normalizations": [],
        },
        "created_at": _utc_iso(now),
        "state": "AWAITING_CONFIRMATION",
    }
    proposal["proposal_hash"] = _proposal_hash(proposal)
    return proposal


def _validate_proposal(proposal: dict[str, Any]) -> None:
    _exact_keys(
        proposal,
        {
            "schema_version",
            "proposal_id",
            "proposal_hash",
            "plan_id",
            "plan_version",
            "session_id",
            "session_content_hash",
            "owned_external_id",
            "workout",
            "preview",
            "created_at",
            "state",
        },
        set(),
        "proposal",
    )
    if proposal.get("schema_version") != PROPOSAL_SCHEMA_VERSION:
        raise DeliveryError("proposal schema_version is unsupported")
    if proposal.get("state") != "AWAITING_CONFIRMATION":
        raise DeliveryError("proposal is not awaiting confirmation")
    if proposal.get("proposal_hash") != _proposal_hash(proposal):
        raise DeliveryError("proposal content changed after hashing")


def approve_delivery_proposal(
    proposal: dict[str, Any],
    *,
    approved_by: str,
    approved_at: dt.datetime | None = None,
) -> dict[str, Any]:
    _validate_proposal(proposal)
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise DeliveryError("approved_by must be non-empty")
    return {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "approval_id": f"approval-{proposal['proposal_hash'][:20]}",
        "proposal_id": proposal["proposal_id"],
        "proposal_hash": proposal["proposal_hash"],
        "plan_id": proposal["plan_id"],
        "plan_version": proposal["plan_version"],
        "session_id": proposal["session_id"],
        "session_content_hash": proposal["session_content_hash"],
        "status": "APPROVED",
        "approved_by": approved_by.strip(),
        "approved_at": _utc_iso(approved_at),
    }


def _validate_approval(proposal: dict[str, Any], approval: dict[str, Any]) -> None:
    _validate_proposal(proposal)
    expected = {
        "proposal_id": proposal.get("proposal_id"),
        "proposal_hash": proposal.get("proposal_hash"),
        "plan_id": proposal.get("plan_id"),
        "plan_version": proposal.get("plan_version"),
        "session_id": proposal.get("session_id"),
        "session_content_hash": proposal.get("session_content_hash"),
    }
    if approval.get("schema_version") != APPROVAL_SCHEMA_VERSION or approval.get("status") != "APPROVED":
        raise DeliveryError("approval is not an APPROVED delivery receipt")
    for field, value in expected.items():
        if approval.get(field) != value:
            raise DeliveryError(f"approval {field} does not match the proposal")


def _set_hash(value: dict[str, Any]) -> str:
    return canonical_hash({key: item for key, item in value.items() if key != "proposal_hash"})


def prepare_delivery_set(
    current_plan: dict[str, Any],
    selected_session_ids: list[str],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Derive selected current-plan workouts into one athlete-confirmation boundary."""
    if not isinstance(selected_session_ids, list) or not selected_session_ids:
        raise DeliveryError("delivery set must contain at least one session_id")
    created_at = now or dt.datetime.now(dt.timezone.utc)
    proposals = [
        prepare_delivery_proposal(current_plan, session_id, now=created_at)
        for session_id in selected_session_ids
    ]
    proposals.sort(key=lambda item: (item["workout"]["scheduled_date"], item["session_id"]))
    session_ids = [item["session_id"] for item in proposals]
    if len(session_ids) != len(set(session_ids)):
        raise DeliveryError("delivery set contains the same session_id more than once")
    proposal_set: dict[str, Any] = {
        "schema_version": DELIVERY_SET_SCHEMA_VERSION,
        "proposal_id": "delivery-set-" + canonical_hash(
            [item["proposal_hash"] for item in proposals]
        )[:20],
        "plan_id": current_plan["plan_id"],
        "plan_version": current_plan["version"],
        "items": proposals,
        "created_at": _utc_iso(created_at),
        "state": "AWAITING_CONFIRMATION",
    }
    proposal_set["proposal_hash"] = _set_hash(proposal_set)
    return proposal_set


def _validate_delivery_set(proposal_set: dict[str, Any]) -> None:
    _exact_keys(
        proposal_set,
        {
            "schema_version", "proposal_id", "proposal_hash", "plan_id", "plan_version",
            "items", "created_at", "state",
        },
        set(),
        "delivery set",
    )
    if proposal_set.get("schema_version") != DELIVERY_SET_SCHEMA_VERSION:
        raise DeliveryError("delivery set schema_version is unsupported")
    if proposal_set.get("state") != "AWAITING_CONFIRMATION":
        raise DeliveryError("delivery set is not awaiting confirmation")
    items = proposal_set.get("items")
    if not isinstance(items, list) or not items:
        raise DeliveryError("delivery set must contain at least one proposal")
    for item in items:
        _validate_proposal(_mapping(item, "delivery set item"))
        if item.get("plan_id") != proposal_set.get("plan_id"):
            raise DeliveryError("delivery set item plan_id mismatch")
        if item.get("plan_version") != proposal_set.get("plan_version"):
            raise DeliveryError("delivery set item plan_version mismatch")
    if len({item["session_id"] for item in items}) != len(items):
        raise DeliveryError("delivery set contains duplicate sessions")
    if proposal_set.get("proposal_hash") != _set_hash(proposal_set):
        raise DeliveryError("delivery set content changed after hashing")


def approve_delivery_set(
    proposal_set: dict[str, Any],
    *,
    approved_by: str,
    approved_at: dt.datetime | None = None,
) -> dict[str, Any]:
    _validate_delivery_set(proposal_set)
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise DeliveryError("approved_by must be non-empty")
    return {
        "schema_version": DELIVERY_SET_SCHEMA_VERSION,
        "approval_id": f"approval-{proposal_set['proposal_hash'][:20]}",
        "proposal_id": proposal_set["proposal_id"],
        "proposal_hash": proposal_set["proposal_hash"],
        "plan_id": proposal_set["plan_id"],
        "plan_version": proposal_set["plan_version"],
        "status": "APPROVED",
        "approved_by": approved_by.strip(),
        "approved_at": _utc_iso(approved_at),
    }


def _validate_set_approval(proposal_set: dict[str, Any], approval: dict[str, Any]) -> None:
    _validate_delivery_set(proposal_set)
    expected = {
        "proposal_id": proposal_set["proposal_id"],
        "proposal_hash": proposal_set["proposal_hash"],
        "plan_id": proposal_set["plan_id"],
        "plan_version": proposal_set["plan_version"],
    }
    if approval.get("schema_version") != DELIVERY_SET_SCHEMA_VERSION:
        raise DeliveryError("delivery set approval schema_version is unsupported")
    if approval.get("status") != "APPROVED":
        raise DeliveryError("delivery set approval is not APPROVED")
    for field, value in expected.items():
        if approval.get(field) != value:
            raise DeliveryError(f"delivery set approval {field} mismatch")


Fetcher = Callable[[urllib.request.Request], bytes]


@dataclass
class IntervalsTransport:
    credentials: IntervalsCredentials
    fetch: Fetcher | None = None

    def _call(self, method: str, path: str, payload: Any | None = None) -> Any:
        url = BASE_URL.format(athlete_id=self.credentials.athlete_id) + path
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", authorization_header(self.credentials))
        request.add_header("User-Agent", USER_AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            if self.fetch is None:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    body = response.read()
            else:
                body = self.fetch(request)
        except urllib.error.HTTPError as exc:
            raise DeliveryError(f"Intervals {method} failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise DeliveryError(f"Intervals {method} failed: {exc.reason}") from exc
        try:
            return json.loads(body)
        except (json.JSONDecodeError, TypeError) as exc:
            raise DeliveryError(f"Intervals {method} returned invalid JSON") from exc

    def list_events(self, day: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {"oldest": day, "newest": day, "category": "WORKOUT", "resolve": "false"}
        )
        result = self._call("GET", f"/events?{query}")
        if not isinstance(result, list):
            raise DeliveryError("Intervals event list is not an array")
        return [item for item in result if isinstance(item, dict)]

    def bulk_upsert(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        result = self._call("POST", "/events/bulk?upsert=true", [event])
        if not isinstance(result, list):
            raise DeliveryError("Intervals bulk write response is not an array")
        return [item for item in result if isinstance(item, dict)]

    def get_event(self, event_id: str) -> dict[str, Any]:
        result = self._call("GET", f"/events/{urllib.parse.quote(event_id, safe='')}?resolve=false")
        if not isinstance(result, dict):
            raise DeliveryError("Intervals event read-back is not an object")
        return result


def _hr_ceiling_workout_doc(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the provider workout_doc carrying the absolute-bpm ceiling.

    Only the bulk-upsert workout_doc JSON field survives read-back byte-exact,
    including start: 0 for a ceiling with no floor (verified 2026-08-12); the
    text description path cannot carry it at all.
    """
    provider_steps: list[dict[str, Any]] = []
    for step in steps:
        provider_step: dict[str, Any] = {
            "text": step["name"],
            "duration": step["duration"]["seconds"],
        }
        if step["target"]["kind"] == "hr_ceiling":
            provider_step["hr"] = {"units": "bpm", "start": 0, "end": step["target"]["ceiling_bpm"]}
        provider_steps.append(provider_step)
    return {
        "steps": provider_steps,
        "duration": sum(step["duration"]["seconds"] for step in steps),
    }


def _provider_payload(proposal: dict[str, Any]) -> dict[str, Any]:
    workout = proposal["workout"]
    payload = {
        "external_id": proposal["owned_external_id"],
        "category": "WORKOUT",
        "type": _provider_type(workout),
        "name": workout["name"],
        "start_date_local": workout["scheduled_date"] + "T00:00:00",
        "description": workout["description"],
    }
    if _contains_hr_ceiling(workout.get("steps") or []):
        payload["workout_doc"] = _hr_ceiling_workout_doc(workout["steps"])
    return payload


def _provider_type(workout: dict[str, Any]) -> str:
    return "WeightTraining" if workout.get("sport") == "strength" else "Run"


def _actual_number(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeliveryError(f"read-back {field} is not numeric")
    return _whole(float(value), f"read-back {field}")


def _verify_step(expected: dict[str, Any], actual: Any, field: str) -> None:
    observed = _mapping(actual, f"read-back {field}")
    if expected["kind"] == "repeat":
        if _actual_number(observed.get("reps"), f"{field}.reps") != expected["repetitions"]:
            raise DeliveryError(f"read-back {field} repeat count mismatch")
        children = observed.get("steps")
        if not isinstance(children, list) or len(children) != len(expected["steps"]):
            raise DeliveryError(f"read-back {field} repeat steps mismatch")
        for index, child in enumerate(expected["steps"]):
            _verify_step(child, children[index], f"{field}.steps[{index}]")
        return

    duration = expected["duration"]
    key = "duration" if duration["kind"] == "time" else "distance"
    wanted = duration["seconds"] if key == "duration" else duration["meters"]
    if _actual_number(observed.get(key), f"{field}.{key}") != wanted:
        raise DeliveryError(f"read-back {field} {key} mismatch")
    target = expected["target"]

    if target["kind"] == "hr_ceiling":
        # The dogfood failure (#38): 77-83 %hr was silently reinterpreted against
        # max HR instead of threshold HR, enforcing a 139-149 bpm floor during a
        # recovery run meant to stay under 140. Every field is checked explicitly
        # rather than trusting provider echo: a %hr/%lthr unit, a provider-added
        # floor, or a shifted ceiling must all fail closed here.
        forbidden = {name for name in ("pace", "power") if observed.get(name) is not None}
        if forbidden:
            raise DeliveryError(f"read-back {field} contains unsupported target {sorted(forbidden)}")
        hr = _mapping(observed.get("hr"), f"read-back {field}.hr")
        if hr.get("units") != "bpm":
            raise DeliveryError(f"read-back {field}.hr must remain absolute bpm")
        if _actual_number(hr.get("start"), f"{field}.hr.start") != 0:
            raise DeliveryError(f"read-back {field}.hr.start must remain 0 (no provider-added floor)")
        if _actual_number(hr.get("end"), f"{field}.hr.end") != target["ceiling_bpm"]:
            raise DeliveryError(f"read-back {field}.hr.end ceiling mismatch")
        return

    forbidden = {name for name in ("hr", "power") if observed.get(name) is not None}
    if forbidden:
        raise DeliveryError(f"read-back {field} contains unsupported target {sorted(forbidden)}")
    if target["kind"] == "open":
        if observed.get("pace") is not None:
            raise DeliveryError(f"read-back {field} unexpectedly contains a pace target")
        return
    pace = _mapping(observed.get("pace"), f"read-back {field}.pace")
    if pace.get("units") != "secs/km":
        raise DeliveryError(f"read-back {field}.pace must remain absolute secs/km")
    observed_bounds = sorted(
        (_actual_number(pace.get("start"), f"{field}.pace.start"),
         _actual_number(pace.get("end"), f"{field}.pace.end"))
    )
    expected_bounds = [target["low_seconds_per_km"], target["high_seconds_per_km"]]
    if observed_bounds != expected_bounds:
        raise DeliveryError(f"read-back {field}.pace target mismatch")


def verify_readback(proposal: dict[str, Any], event: dict[str, Any], event_id: str) -> None:
    workout = proposal["workout"]
    if str(event.get("id")) != event_id:
        raise DeliveryError("read-back event id mismatch")
    if event.get("external_id") != proposal["owned_external_id"]:
        raise DeliveryError("read-back owned external_id mismatch")
    if event.get("category") != "WORKOUT" or event.get("type") != _provider_type(workout):
        raise DeliveryError("read-back is not the delivered workout type")
    if str(event.get("start_date_local", ""))[:10] != workout["scheduled_date"]:
        raise DeliveryError("read-back scheduled date mismatch")
    if event.get("name") != workout["name"]:
        raise DeliveryError("read-back workout name mismatch")
    if str(event.get("description", "")).strip() != workout["description"].strip():
        raise DeliveryError("read-back workout description mismatch")
    if workout.get("sport") == "strength":
        # Intervals echoes a workout_doc container for every calendar entry, carrying
        # the description and no steps. The container is not the claim; steps are.
        # Steps coming back would mean the provider synthesised a structure this path
        # never delivered, which is the structured strength delivery it must not imply.
        document = event.get("workout_doc")
        steps = document.get("steps") if isinstance(document, dict) else None
        if steps:
            raise DeliveryError("read-back strength entry unexpectedly carries workout steps")
        return
    document = _mapping(event.get("workout_doc"), "read-back workout_doc")
    steps = document.get("steps")
    if not isinstance(steps, list) or len(steps) != len(workout["steps"]):
        raise DeliveryError("read-back workout step count mismatch")
    for index, expected in enumerate(workout["steps"]):
        _verify_step(expected, steps[index], f"workout_doc.steps[{index}]")


def publish_delivery(
    proposal: dict[str, Any],
    approval: dict[str, Any],
    *,
    load_current_plan: Callable[[], dict[str, Any]],
    transport: IntervalsTransport,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Write or reuse one owned event and return a receipt only after exact read-back."""
    _validate_approval(proposal, approval)
    current = load_current_plan()
    _current_plan_is_valid(current)
    if current.get("plan_id") != proposal.get("plan_id"):
        raise DeliveryError("current plan_id changed after preview")
    if current.get("version") != proposal.get("plan_version"):
        raise DeliveryError("current PlanState version changed after preview")
    session = _plan_session(current, proposal["session_id"])
    if session_content_hash(session) != proposal.get("session_content_hash"):
        raise DeliveryError("selected current session changed after preview")
    execution = session.get("execution") if isinstance(session.get("execution"), dict) else {}
    if execution.get("publish_supported") is not True:
        raise DeliveryError("selected session no longer supports publishing")
    if _workout_from_session(session) != proposal.get("workout"):
        raise DeliveryError("approved workout is not the selected current PlanState workout")

    listed = transport.list_events(proposal["workout"]["scheduled_date"])
    owned = [event for event in listed if event.get("external_id") == proposal["owned_external_id"]]
    if len(owned) > 1:
        raise DeliveryError("multiple Intervals events carry this product-owned external_id")

    operation = "upserted"
    event_id: str | None = None
    if owned:
        event_id = str(owned[0].get("id") or "") or None
        if event_id is None:
            raise DeliveryError("owned Intervals event has no id")
        existing = transport.get_event(event_id)
        try:
            verify_readback(proposal, existing, event_id)
        except DeliveryError:
            operation = "updated"
        else:
            operation = "deduplicated_existing"
            readback = existing

    if operation in {"upserted", "updated"}:
        result = transport.bulk_upsert(_provider_payload(proposal))
        if len(result) != 1 or result[0].get("id") is None:
            raise DeliveryError("Intervals did not return exactly one written event id")
        written_id = str(result[0]["id"])
        if event_id is not None and written_id != event_id:
            raise DeliveryError("Intervals upsert changed the owned event id")
        event_id = written_id
        readback = transport.get_event(event_id)
        verify_readback(proposal, readback, event_id)

    assert event_id is not None
    readback_hash = canonical_hash(readback)
    verified_at = _utc_iso(now)
    observation = {
        "plan_id": proposal["plan_id"],
        "plan_version": proposal["plan_version"],
        "session_id": proposal["session_id"],
        "session_content_hash": proposal["session_content_hash"],
        "external_id": event_id,
        "proposal_hash": proposal["proposal_hash"],
        "readback_hash": readback_hash,
        "verified_at": verified_at,
    }
    receipt_material = {
        "proposal_hash": proposal["proposal_hash"],
        "external_id": event_id,
        "readback_hash": readback_hash,
    }
    return {
        "status": "passed",
        "delivery_state": "intervals_accepted",
        "operation": operation,
        "owned_external_id": proposal["owned_external_id"],
        "observation": observation,
        "receipt_id": f"delivery-receipt-{canonical_hash(receipt_material)[:24]}",
    }


def publish_delivery_set(
    proposal_set: dict[str, Any],
    approval: dict[str, Any],
    *,
    load_current_plan: Callable[[], dict[str, Any]],
    transport: IntervalsTransport,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Deliver one approved publish set without advancing state between its items."""
    _validate_set_approval(proposal_set, approval)
    current = load_current_plan()
    if current.get("plan_id") != proposal_set.get("plan_id"):
        raise DeliveryError("current plan_id changed after publish-set preview")
    if current.get("version") != proposal_set.get("plan_version"):
        raise DeliveryError("current PlanState version changed after publish-set preview")

    approved_at = str(approval.get("approved_at", "")).replace("Z", "+00:00")
    try:
        approval_time = dt.datetime.fromisoformat(approved_at)
    except ValueError as exc:
        raise DeliveryError("delivery set approval approved_at is invalid") from exc
    receipts: list[dict[str, Any]] = []
    for proposal in proposal_set["items"]:
        derived_approval = approve_delivery_proposal(
            proposal,
            approved_by=approval.get("approved_by", ""),
            approved_at=approval_time,
        )
        receipts.append(
            publish_delivery(
                proposal,
                derived_approval,
                load_current_plan=lambda: current,
                transport=transport,
                now=now,
            )
        )
    receipt_material = {
        "proposal_hash": proposal_set["proposal_hash"],
        "item_receipts": [receipt["receipt_id"] for receipt in receipts],
    }
    return {
        "status": "passed",
        "delivery_state": "intervals_accepted",
        "proposal_hash": proposal_set["proposal_hash"],
        "item_receipts": receipts,
        "observations": [receipt["observation"] for receipt in receipts],
        "receipt_id": f"delivery-set-receipt-{canonical_hash(receipt_material)[:24]}",
    }
