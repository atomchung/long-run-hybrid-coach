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
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .prescription import duration_text as _duration_text
from .prescription import pace_text as _pace_text
from .prescription import language_of as _language_of
from .prescription import strength_title as _strength_title
from .source_intervals import (
    BASE_URL,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    IntervalsCredentials,
    authorization_header,
)
from .store import (
    StateStoreError,
    apply_delivery_observations,
    apply_delivery_withdrawals,
    canonical_hash,
    close_delivery_attempt,
    delivery_session_content_hash,
    mark_delivery_attempt_recorded,
    open_delivery_attempt,
    pending_delivery_attempt,
    read_current_plan,
    record_delivery_attempt_operation,
    unresolved_delivery_operations,
)
from .validation import validate_plan_state


PROPOSAL_SCHEMA_VERSION = "1.0"
APPROVAL_SCHEMA_VERSION = "1.0"
DELIVERY_SET_SCHEMA_VERSION = "1.0"

# Which way a confirmed set moves the calendar. Two directions reach the provider through
# one confirmation boundary -- publishing a workout and removing a superseded one -- and
# the athlete confirms one of them, never "a delivery". So the direction is a *hashed
# field of the set*, written before `proposal_hash` is computed and checked by the
# validator that every apply path already runs.
#
# It was briefly a label carried beside the set instead, outside the hash, defended by
# the observation that a delivery item and a withdrawal item share no fields and so
# cannot validate as each other. That defence is real but incidental: it holds only while
# the two item shapes stay disjoint, it is not what AGENTS.md 7 asks for ("approval bound
# to the exact proposed delivery"), and nothing fails when a later field makes the shapes
# overlap. Inside the hash, flipping the direction breaks the approval itself, which is
# the same mechanism that already protects every other claim in the set.
DELIVER_DIRECTION = "deliver"
WITHDRAW_DIRECTION = "withdraw"


# Tokens the Intervals workout-text grammar reads as executable meaning. The emitted line
# is `- {name} {duration}{target}`, so the parser sees the name and the duration as one
# stream: a name carrying one of these rewrites the step the watch enforces.
#
# Blocking validator, per AGENTS.md 6:
#   invariant/harm -- the structured content the watch enforces must be exactly the
#     approved content. Live, 2026-08-13 (issue #75): the step named `門檻 1000m` had its
#     `1000m` read as 1000 *minutes*, producing 60000s per rep and dropping the real 1 km,
#     so the provider recorded 805695 m / 301440 s for a 5x1000 m threshold session.
#   why a warning is insufficient -- the corruption happens inside the provider's parse,
#     after approval and before read-back. The only point at which it can be prevented is
#     refusing the name before the provider write.
#   valid workflows kept -- purpose-first Chinese step names pass untouched; a digit with
#     no unit (`第 3 趟`) passes. Every run, strength and mobility name is expressible
#     without a digit+unit token, because the numbers belong in the plan, not the name.
#   false-positive cost -- one rename at prepare time, named exactly. Nothing else blocks,
#     and nothing is silently rewritten: a sanitised name is a name the athlete never
#     approved.
#
# The unit ends in `(?![A-Za-z])` rather than `\b`, for the same reason `intent_text`
# gives: the athlete names steps in Chinese and CJK is a word character to `re`, so `\b`
# would catch `間歇 400m` and let `間歇400m快` through -- the same token, in the naming
# style this product actually produces. What the lookahead still excludes is a longer
# English word, so a step called `5 minutes easy` is not a duration token.
#
# Every branch here is one token the provider's published syntax guide defines, checked
# against that guide rather than against memory of an incident (issue #129): the unit
# letters and their `mtr`-not-`m` rule for metres, the `5'`/`30"` short forms, the `mm:ss`
# absolute-pace form, the `Nx` repeat, the `N%` intensity and the `ZN` zone. The short
# forms were the gap that audit found -- they are durations exactly as `5m` is, and a step
# named `節奏 5'` would have been read as five minutes, which is the 2026-08-13 failure
# again under a different spelling.
_STEP_NAME_GRAMMAR_COLLISION = re.compile(
    r"""
    \d+\s*(?:km|mi|mtr|yd|min|mins|sec|secs|hrs|hr|[mshd])(?![A-Za-z])  # 1000m, 5km, 30s
    | \d+\s*['’\"”]                                          # 5' and 30" short forms
    | \d+\s*:\s*\d{2}                                                  # 5:30 clock form
    | \d+\s*[xX](?![A-Za-z])                                            # 5x repeat count
    | \d+\s*%                                                          # 85% intensity
    | (?<![A-Za-z0-9])[zZ]\d(?![0-9A-Za-z])                              # Z2 zone
    """,
    re.VERBOSE,
)


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


# The non-binding lower edge of a delivered ceiling, as a percentage of threshold HR.
#
# The plan states a ceiling and no floor, but the only encoding that reaches the watch is
# a range, so one has to be chosen. 50% of threshold is below any running heart rate the
# athlete can produce, which is the point: it occupies the slot the grammar requires
# without adding an instruction the plan never gave.
HR_CEILING_FLOOR_PERCENT_LTHR = 50


def _resolved_ceiling_bpm(percent: int, run_threshold_hr: int) -> int:
    """The highest bpm `percent% LTHR` can resolve to against this threshold.

    Live 2026-08-14, threshold 163: `50-86% LTHR` reached the watch as `81-140 bpm`, so
    the provider truncates (86% of 163 is 140.18, and 50% is 81.5 arriving as 81). This
    rounds half up instead, which is never lower than truncation. Modelling the provider
    as the *looser* of the two is what makes the guarantee hold if it ever rounds: an
    encoding this function calls safe is safe under either rule, and the number reported
    to the athlete can only overstate the ceiling by one bpm, never understate it.
    """
    return int(run_threshold_hr * percent / 100 + 0.5)


def hr_ceiling_percent_lthr(ceiling_bpm: int, run_threshold_hr: int) -> tuple[int, int]:
    """The `% LTHR` band whose upper edge resolves at or below the plan's bpm ceiling.

    Absolute bpm is not expressible in the provider's workout text at all, and the
    structured `workout_doc` that could carry it reached the watch as 1-252 bpm -- a
    target that displays and constrains nothing (issue #22, device-verified 2026-08-14).
    `% LTHR` is the one encoding the provider parses, analyses and exports intact, so a
    ceiling is delivered as the largest whole percent that still resolves under it. The
    percent is never rounded up to the nearer value: an encoding that resolved one bpm
    above the plan would be the same silent loosening this replaces.
    """
    if isinstance(ceiling_bpm, bool) or not isinstance(ceiling_bpm, int) or ceiling_bpm < 1:
        raise DeliveryError("hr_ceiling target requires a positive whole ceiling_bpm")
    if (
        isinstance(run_threshold_hr, bool)
        or not isinstance(run_threshold_hr, int)
        or run_threshold_hr < 1
    ):
        raise DeliveryError("resolving an hr_ceiling requires a positive Run threshold HR")
    low = HR_CEILING_FLOOR_PERCENT_LTHR
    # Capped at 100 rather than extrapolated past threshold: a ceiling at or above
    # threshold is not binding anyway, and 100% is the highest edge this has evidence for.
    candidates = [
        percent
        for percent in range(low + 1, 101)
        if _resolved_ceiling_bpm(percent, run_threshold_hr) <= ceiling_bpm
    ]
    if not candidates:
        raise DeliveryError(
            f"an hr_ceiling of {ceiling_bpm}bpm cannot be delivered against this "
            f"Intervals Run threshold HR of {run_threshold_hr}bpm: every percentage of "
            f"threshold above the {low}% floor resolves above the ceiling. Either the "
            "ceiling or the account's Run threshold HR (Intervals -> Settings -> Sport "
            "Settings -> Run) is wrong; correct one of them and preview again."
        )
    return (low, max(candidates))


def _work_line(step: dict[str, Any], run_threshold_hr: int | None) -> str:
    target = step["target"]
    target_text = ""
    if target["kind"] == "pace":
        target_text = (
            f" {_pace_text(target['low_seconds_per_km'])}-"
            f"{_pace_text(target['high_seconds_per_km'])}/km Pace"
        )
    elif target["kind"] == "hr_ceiling":
        if run_threshold_hr is None:
            raise DeliveryError(_MISSING_RUN_THRESHOLD_HR)
        low, high = hr_ceiling_percent_lthr(target["ceiling_bpm"], run_threshold_hr)
        target_text = f" {low}-{high}% LTHR"
    return f"- {step['name']} {_duration_text(step['duration'])}{target_text}"


def intervals_description(
    steps: list[dict[str, Any]], run_threshold_hr: int | None = None
) -> str:
    lines: list[str] = []
    for step in steps:
        if step["kind"] == "work":
            lines.append(_work_line(step, run_threshold_hr))
            continue
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"{step['repetitions']}x")
        lines.extend(_work_line(child, run_threshold_hr) for child in step["steps"])
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


# Blocking validator, per AGENTS.md 6:
#   invariant/harm -- a recovery run *is* its ceiling. Threshold HR is the denominator
#     the provider resolves `% LTHR` against, and it is the only encoding of a ceiling
#     that reaches the watch. Without it there is no correct number to send, and the two
#     ways of continuing are both the failure this replaces: an open target the athlete
#     confirmed as a ceiling, or a guessed denominator resolving somewhere unknown.
#   why a warning is insufficient -- the athlete confirms an exact preview. A preview
#     that names a ceiling it could not resolve is the 2026-08-14 device failure again,
#     where the watch showed a heart-rate target that permitted everything.
#   valid workflows kept -- open-target runs, pace runs and strength entries never reach
#     this. A ceiling run reaches it once, and only while the account setting is missing.
#   false-positive cost -- one setting the athlete fills in once, named exactly, at
#     preview rather than after a provider write.
_MISSING_RUN_THRESHOLD_HR = (
    "this workout prescribes a heart-rate ceiling, and this Intervals account's Run "
    "threshold HR could not be read. Intervals resolves a ceiling against that number, "
    "and absolute bpm does not survive the export to the watch, so there is no correct "
    "workout to send without it. Set the Run threshold HR in Intervals (Settings -> "
    "Sport Settings -> Run), then preview again."
)


def _reject_ambiguous_step_names(steps: list[dict[str, Any]], field: str = "steps") -> None:
    """Refuse a step name the provider's own grammar would read as a duration.

    See ``_STEP_NAME_GRAMMAR_COLLISION`` for the invariant, the live failure, and the
    false-positive cost. The error names the step and the token so the fix is one rename.
    """
    for index, step in enumerate(steps):
        where = f"{field}[{index}]"
        if step.get("kind") == "repeat":
            _reject_ambiguous_step_names(step.get("steps") or [], f"{where}.steps")
            continue
        name = step.get("name")
        if not isinstance(name, str):
            continue
        collision = _STEP_NAME_GRAMMAR_COLLISION.search(name)
        if collision is not None:
            raise DeliveryError(
                f"{where} name {name!r} contains {collision.group(0)!r}, which Intervals "
                "reads as a duration, distance, repeat count or intensity; rename the step "
                "in the plan (the numbers belong in the step's duration and target, not in "
                "its name)"
            )


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

    The athlete effectively sees only this title on the watch, so purpose alone is not
    enough to know what today asks for: the title appends the primary lift and its load,
    rendered from the plan's first movement exactly as `render_prescription` renders the
    full text -- the model writes no second copy of either. A plan with nothing to render
    (`unstructured`) leaves the bare purpose, which is what titled every strength entry
    before this.

    The title is written in whichever language the session's own prescription was, which
    is read off that sentence rather than passed in: the description below it *is* that
    sentence, and one calendar entry carrying a title and a description in two languages
    would be this product's own doing.
    """
    purpose = session.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        raise DeliveryError("strength delivery requires a purpose to title the calendar entry")
    prescription = session.get("prescription")
    name = _strength_title(
        purpose.strip(),
        session.get("plan"),
        _language_of(session.get("plan"), prescription),
    )
    return {
        "sport": "strength",
        "name": name,
        "scheduled_date": session["scheduled_date"],
        "description": prescription.strip() if isinstance(prescription, str) else "",
    }


def _workout_from_session(
    session: dict[str, Any], run_threshold_hr: int | None = None
) -> dict[str, Any]:
    if session.get("sport") == "strength":
        return _calendar_entry_from_session(session)
    if session.get("sport") != "running":
        # The guard prepare_delivery_proposal already applies, repeated here so this
        # function cannot silently label some other sport's payload "running" if that
        # outer gate ever loosens. A cross-training sport gets a real representation per
        # sport, or none.
        raise DeliveryError(
            f"no delivery representation exists for {session.get('sport')} sessions yet"
        )
    plan = session.get("plan")
    if not isinstance(plan, dict) or plan.get("kind") != "time_axis":
        raise DeliveryError(
            "selected session carries no time_axis plan in current PlanState"
        )
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise DeliveryError("selected current PlanState workout has no executable steps")
    # Checked here rather than only at prepare time, so a plan edited between the preview
    # and the write cannot carry an ambiguous name past the confirmation either.
    _reject_ambiguous_step_names(steps)
    # `kind` is how the plan says which model it is; the provider payload is built from
    # the name and the steps, exactly as before it had a discriminator to drop.
    canonical = {key: copy.deepcopy(value) for key, value in plan.items() if key != "kind"}
    canonical["sport"] = "running"
    canonical["scheduled_date"] = session["scheduled_date"]
    canonical["description"] = intervals_description(canonical["steps"], run_threshold_hr)
    return canonical


def owned_external_id_for(plan: dict[str, Any], session_id: str) -> str:
    """The deterministic marker identifying one product-owned provider event.

    Derived from the plan, the week and the session -- deliberately not from the date, so
    the same event is found again after the session moves, and a replacement written under
    this marker updates the event in place rather than leaving the old date populated.
    """
    return "gcl:" + canonical_hash(
        {
            "plan_id": plan["plan_id"],
            "week_start": (plan.get("week") or {}).get("start"),
            "session_id": session_id,
        }
    )[:32]


def _proposal_hash(proposal: dict[str, Any]) -> str:
    material = {key: value for key, value in proposal.items() if key != "proposal_hash"}
    return canonical_hash(material)


def _plan_ceiling_bpm(workout: dict[str, Any]) -> int | None:
    """The tightest heart-rate ceiling this workout binds, or ``None`` if it binds none."""
    ceilings = [
        step["target"]["ceiling_bpm"]
        for step in _iter_work_steps(workout.get("steps") or [])
        if step["target"]["kind"] == "hr_ceiling"
    ]
    return min(ceilings) if ceilings else None


def _hr_ceiling_resolution(
    plan_ceiling: int, run_threshold_hr: int | None
) -> dict[str, Any]:
    """What a delivered ceiling will actually resolve to, recorded for the athlete to confirm.

    Kept out of ``workout`` on purpose: PlanState owns the ceiling in bpm, and the percent
    band is one provider's way of expressing it. Kept inside the proposal because the
    approval binds the proposal hash, so the athlete confirms the resolved number and not
    only the sentence that carries it.
    """
    if run_threshold_hr is None:
        raise DeliveryError(_MISSING_RUN_THRESHOLD_HR)
    low, high = hr_ceiling_percent_lthr(plan_ceiling, run_threshold_hr)
    resolved = _resolved_ceiling_bpm(high, run_threshold_hr)
    if resolved > plan_ceiling:
        raise DeliveryError(
            f"resolved ceiling {resolved}bpm exceeds the plan ceiling {plan_ceiling}bpm"
        )
    return {
        "run_threshold_hr": run_threshold_hr,
        "percent_lthr_low": low,
        "percent_lthr_high": high,
        "resolved_ceiling_bpm": resolved,
        "plan_ceiling_bpm": plan_ceiling,
    }


def _iter_work_steps(steps: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for step in steps:
        if step["kind"] == "work":
            yield step
        else:
            yield from _iter_work_steps(step["steps"])


def prepare_delivery_proposal(
    current_plan: dict[str, Any],
    session_id: str,
    *,
    now: dt.datetime | None = None,
    run_threshold_hr: int | None = None,
    read_run_threshold_hr: Callable[[], int | None] | None = None,
) -> dict[str, Any]:
    """Derive one session into an exact, confirmable preview. Never writes to the provider.

    ``read_run_threshold_hr`` is called at most once, and only when the selected session
    actually binds a heart-rate ceiling: that is the one target whose delivered numbers
    are not derivable from PlanState alone, because Intervals resolves them against the
    account's own threshold. Every other workout previews without touching the provider.
    """
    _current_plan_is_valid(current_plan)
    if current_plan.get("status") != "active":
        raise DeliveryError("only an active current plan may publish workouts")
    session = _plan_session(current_plan, session_id)
    if session.get("sport") not in {"running", "strength"}:
        # Not the same refusal as an unexecutable session: a cycling time_axis session
        # is perfectly executable, this product just has no verified way to write it to
        # the provider yet. Saying which is which is what lets the athlete plan the
        # session anyway and execute it off-calendar.
        raise DeliveryError(
            f"delivery for {session.get('sport')} sessions is not supported yet; "
            "running and strength are the sports that deliver today"
        )
    if session.get("match_status") not in {"planned", "moved", "replaced"}:
        raise DeliveryError("delivery session must be an executable running or strength session")
    execution = session.get("execution") if isinstance(session.get("execution"), dict) else {}
    if execution.get("publish_supported") is not True:
        raise DeliveryError("selected session does not support structured publishing")
    if execution.get("delivery_state") != "not_published" or execution.get("external_id") is not None:
        # Named, because this is what a retry after a partly delivered set runs into: the
        # sessions that did land are recorded, and the retry has to select only the rest.
        raise DeliveryError(
            f"session {session['session_id']} is already "
            f"{execution.get('delivery_state')} as Intervals event "
            f"{execution.get('external_id')}; select only the sessions still to deliver"
        )

    plan_ceiling = _plan_ceiling_bpm(session.get("plan") or {})
    if plan_ceiling is not None and run_threshold_hr is None and read_run_threshold_hr is not None:
        run_threshold_hr = read_run_threshold_hr()
    workout = _workout_from_session(session, run_threshold_hr)
    hr_ceiling_resolution = (
        _hr_ceiling_resolution(plan_ceiling, run_threshold_hr)
        if plan_ceiling is not None
        else None
    )
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
    owned_external_id = owned_external_id_for(current_plan, session["session_id"])
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
            "hr_ceiling_resolution": hr_ceiling_resolution,
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


def _read_once(reader: Callable[[], int | None]) -> Callable[[], int | None]:
    """The same reader, answering from one call however many times it is asked."""
    cache: list[int | None] = []

    def read() -> int | None:
        if not cache:
            cache.append(reader())
        return cache[0]

    return read


def _set_hash(value: dict[str, Any]) -> str:
    return canonical_hash({key: item for key, item in value.items() if key != "proposal_hash"})


def prepare_delivery_set(
    current_plan: dict[str, Any],
    selected_session_ids: list[str],
    *,
    now: dt.datetime | None = None,
    run_threshold_hr: int | None = None,
    read_run_threshold_hr: Callable[[], int | None] | None = None,
) -> dict[str, Any]:
    """Derive selected current-plan workouts into one athlete-confirmation boundary."""
    if not isinstance(selected_session_ids, list) or not selected_session_ids:
        raise DeliveryError("delivery set must contain at least one session_id")
    created_at = now or dt.datetime.now(dt.timezone.utc)
    # Read once for the whole set, however many of its sessions carry a ceiling: two
    # sessions confirmed together must be resolved against one threshold, or the set
    # would bind two different accounts of the same account.
    read_once = _read_once(read_run_threshold_hr) if read_run_threshold_hr else None
    proposals = [
        prepare_delivery_proposal(
            current_plan,
            session_id,
            now=created_at,
            run_threshold_hr=run_threshold_hr,
            read_run_threshold_hr=read_once,
        )
        for session_id in selected_session_ids
    ]
    proposals.sort(key=lambda item: (item["workout"]["scheduled_date"], item["session_id"]))
    session_ids = [item["session_id"] for item in proposals]
    if len(session_ids) != len(set(session_ids)):
        raise DeliveryError("delivery set contains the same session_id more than once")
    proposal_set: dict[str, Any] = {
        "schema_version": DELIVERY_SET_SCHEMA_VERSION,
        "direction": DELIVER_DIRECTION,
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
            "schema_version", "direction", "proposal_id", "proposal_hash", "plan_id",
            "plan_version", "items", "created_at", "state",
        },
        set(),
        "delivery set",
    )
    if proposal_set.get("schema_version") != DELIVERY_SET_SCHEMA_VERSION:
        raise DeliveryError("delivery set schema_version is unsupported")
    if proposal_set.get("direction") != DELIVER_DIRECTION:
        raise DeliveryError("delivery set direction is not a delivery")
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
        "direction": DELIVER_DIRECTION,
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
        "direction": DELIVER_DIRECTION,
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
        if not body:
            # A delete answers with no body. Every other call checks the shape it needs,
            # so an unexpected empty body still fails at the caller rather than here.
            return None
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

    def list_events_range(self, oldest: str, newest: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {"oldest": oldest, "newest": newest, "category": "WORKOUT", "resolve": "false"}
        )
        result = self._call("GET", f"/events?{query}")
        if not isinstance(result, list):
            raise DeliveryError("Intervals event list is not an array")
        return [item for item in result if isinstance(item, dict)]

    def get_event(self, event_id: str) -> dict[str, Any]:
        result = self._call("GET", f"/events/{urllib.parse.quote(event_id, safe='')}?resolve=false")
        if not isinstance(result, dict):
            raise DeliveryError("Intervals event read-back is not an object")
        return result

    def find_event(self, event_id: str) -> dict[str, Any] | None:
        """The event with this id, or ``None`` only when the provider says it is not there.

        A 404 is the one answer that means absence. Every other failure -- a timeout, a
        403, a 500 -- stays an error, because "I could not look" must never be recorded as
        "it is gone" (AGENTS.md 3).
        """
        try:
            return self.get_event(event_id)
        except DeliveryError as exc:
            if isinstance(exc.__cause__, urllib.error.HTTPError) and exc.__cause__.code == 404:
                return None
            raise

    def delete_event(self, event_id: str) -> None:
        self._call("DELETE", f"/events/{urllib.parse.quote(event_id, safe='')}")

    def run_sport_settings(self) -> tuple[bool, dict[str, Any] | None]:
        """The athlete's Intervals Run sport settings, and whether they could be read.

        Two different answers, kept apart on purpose: ``(True, value)`` means the provider
        told us, ``(False, None)`` means it would not. Not being allowed to look is not
        evidence about what is there (AGENTS.md 3).

        Both entry points read this. The hosted OAuth token carries ``SETTINGS:READ`` --
        confirmed live on 2026-08-15 by a consent showing Settings -> Read, a token whose
        normalized scopes include it, and a `200` from this endpoint (issue #41).

        Every failure answers "could not read", including a timeout or a 500. What each
        caller does with that silence is its own decision, and they differ: a pace target
        is written anyway and claims no more than the provider observed, while a
        heart-rate ceiling has no correct number to send without it and blocks.
        """
        try:
            settings = self._call("GET", "/sport-settings")
        except DeliveryError:
            return (False, None)
        if not isinstance(settings, list):
            return (False, None)
        for entry in settings:
            if isinstance(entry, dict) and "Run" in (entry.get("types") or []):
                return (True, entry)
        # Read successfully, and the athlete has no Run sport settings at all -- which is
        # exactly the state that strips a target, so it is an answer, not a silence.
        return (True, None)

    def run_threshold_pace(self) -> tuple[bool, Any]:
        """The Run threshold pace in metres per second, and whether it could be read.

        This is the one field that decides whether Intervals keeps a pace target when it
        exports the workout onward.
        """
        observed, entry = self.run_sport_settings()
        return (observed, entry.get("threshold_pace") if entry else None)

    def run_threshold_hr(self) -> tuple[bool, Any]:
        """The Run threshold heart rate in bpm, and whether it could be read.

        Intervals calls it ``lthr`` on the Run sport-settings entry, and it is the
        denominator it resolves every ``% LTHR`` workout target against. Deliberately not
        ``max_hr``: resolving a ceiling against the wrong denominator is the 2026-08-12
        failure that put a 139-149 bpm floor on a recovery run meant to stay under 140.
        """
        observed, entry = self.run_sport_settings()
        return (observed, entry.get("lthr") if entry else None)


def _provider_payload(proposal: dict[str, Any]) -> dict[str, Any]:
    """The Intervals event this approved proposal writes.

    Workout text only. A supplied ``workout_doc`` is not a documented provider input, and
    the one path that used it -- an absolute-bpm ceiling -- was device-verified on
    2026-08-14 to arrive as 1-252 bpm, a target that displays and constrains nothing
    (issue #22). Nothing this product sends carries a supplied ``workout_doc`` any more;
    the field survives only as read-back, which is the provider's own parse of this text.
    """
    workout = proposal["workout"]
    return {
        "external_id": proposal["owned_external_id"],
        "category": "WORKOUT",
        "type": _provider_type(workout),
        "name": workout["name"],
        "start_date_local": workout["scheduled_date"] + "T00:00:00",
        "description": workout["description"],
    }


def _provider_type(workout: dict[str, Any]) -> str:
    return "WeightTraining" if workout.get("sport") == "strength" else "Run"


def _actual_number(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeliveryError(f"read-back {field} is not numeric")
    return _whole(float(value), f"read-back {field}")


def _verify_step(
    expected: dict[str, Any],
    actual: Any,
    field: str,
    resolution: dict[str, Any] | None = None,
) -> None:
    observed = _mapping(actual, f"read-back {field}")
    if expected["kind"] == "repeat":
        if _actual_number(observed.get("reps"), f"{field}.reps") != expected["repetitions"]:
            raise DeliveryError(f"read-back {field} repeat count mismatch")
        children = observed.get("steps")
        if not isinstance(children, list) or len(children) != len(expected["steps"]):
            raise DeliveryError(f"read-back {field} repeat steps mismatch")
        for index, child in enumerate(expected["steps"]):
            _verify_step(child, children[index], f"{field}.steps[{index}]", resolution)
        return

    duration = expected["duration"]
    key = "duration" if duration["kind"] == "time" else "distance"
    wanted = duration["seconds"] if key == "duration" else duration["meters"]
    if _actual_number(observed.get(key), f"{field}.{key}") != wanted:
        raise DeliveryError(f"read-back {field} {key} mismatch")
    target = expected["target"]

    if target["kind"] == "hr_ceiling":
        # Two failures are being held off here at once. 2026-08-12 (#38): `77-83% HR` was
        # resolved against max HR rather than threshold HR, putting a 139-149 bpm floor on
        # a recovery run meant to stay under 140 -- so the unit is checked to be `%lthr`
        # exactly, not merely a percentage. 2026-08-14 (#22): an absolute-bpm ceiling was
        # stored verbatim and reached the watch as 1-252 bpm -- so `bpm` is now a failure
        # here rather than the expected unit.
        #
        # The last check is the one that makes the claim: whatever percentage the provider
        # says it parsed is resolved back into bpm against the same threshold the athlete
        # confirmed, and must land at or under the plan's ceiling. Verifying the percent we
        # sent came back would only prove the provider echoes; this proves the number the
        # athlete was shown is the number that was delivered.
        if resolution is None:
            raise DeliveryError(f"read-back {field} carries an hr_ceiling with no resolved preview")
        forbidden = {name for name in ("pace", "power") if observed.get(name) is not None}
        if forbidden:
            raise DeliveryError(f"read-back {field} contains unsupported target {sorted(forbidden)}")
        hr = _mapping(observed.get("hr"), f"read-back {field}.hr")
        if hr.get("units") != "%lthr":
            raise DeliveryError(
                f"read-back {field}.hr must be resolved against threshold HR (%lthr), "
                f"not {hr.get('units')!r}"
            )
        if _actual_number(hr.get("start"), f"{field}.hr.start") != resolution["percent_lthr_low"]:
            raise DeliveryError(f"read-back {field}.hr.start is not the confirmed floor")
        end = _actual_number(hr.get("end"), f"{field}.hr.end")
        if end != resolution["percent_lthr_high"]:
            raise DeliveryError(f"read-back {field}.hr.end is not the confirmed ceiling")
        resolved = _resolved_ceiling_bpm(end, resolution["run_threshold_hr"])
        if resolved > target["ceiling_bpm"]:
            raise DeliveryError(
                f"read-back {field}.hr resolves to {resolved}bpm, above the plan ceiling "
                f"{target['ceiling_bpm']}bpm"
            )
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
    resolution = (proposal.get("preview") or {}).get("hr_ceiling_resolution")
    for index, expected in enumerate(workout["steps"]):
        _verify_step(expected, steps[index], f"workout_doc.steps[{index}]", resolution)


class _NullJournal:
    """What a delivery writes down when it has no store to write down to.

    ``publish_delivery`` is callable without a state directory (its own tests do it), and
    a caller with no store has nothing to lose or recover. Every caller that *can* reach a
    store passes ``_AttemptJournal`` instead, and that is the only path the product uses.
    """

    def record(self, session_id: str, state: str, **fields: Any) -> None:
        return None


class _AttemptJournal:
    """The durable record of what one attempt has already told Intervals.

    Written on both sides of every mutating call rather than after verification: the
    external effect happens at the write, so a journal that only knows about verified
    effects cannot describe the failure it exists for (issue #121).
    """

    def __init__(self, state_dir: Any, attempt_id: str):
        self.state_dir = state_dir
        self.attempt_id = attempt_id

    def record(self, session_id: str, state: str, **fields: Any) -> None:
        record_delivery_attempt_operation(
            self.state_dir,
            attempt_id=self.attempt_id,
            session_id=session_id,
            state=state,
            **fields,
        )


def _refuse_pace_the_provider_would_strip(
    proposal: dict[str, Any], transport: IntervalsTransport
) -> None:
    """Refuse a pace write Intervals would accept and then export without its target.

    Blocking validator, per AGENTS.md 6:
      invariant/harm -- a pace workout is the pace. Intervals parses an absolute pace
        range correctly and stores it, but drops it when it forwards the workout onward
        if the athlete's Run threshold pace is unset: the watch receives the right
        distances with `No Target`, and the athlete finds out mid-session. Reported live
        2026-07 (forum thread 130706). Read-back cannot see it -- the provider's own copy
        is correct -- so the export prerequisite is the only place it is visible.
      why a warning is insufficient -- the product would report `intervals_accepted`,
        which would be true, next to a plan whose whole point did not arrive. The fix is
        one setting the athlete makes once, after which every future delivery is right;
        a warning attached to a success is the shape of claim this product refuses.
      valid workflows kept -- open-target runs, heart-rate-ceiling runs and strength
        entries never reach this check. A pace run reaches it once, and only while the
        setting is missing.
      false-positive cost -- none that is silent: the check blocks only on a value the
        provider actually returned. When the provider will not answer -- the hosted OAuth
        path cannot read athlete settings at all -- nothing is blocked and nothing is
        claimed either.

    Deliberately not the same fact as ``athlete_baseline.threshold_pace_sec_per_km``.
    That is coaching evidence: it is what makes a prescribed pace defensible, and the
    preview already refuses an absolute pace without it. This is a provider export
    prerequisite living in the athlete's Intervals account, which the plan neither owns
    nor can set.
    """
    if not _contains_pace_target(proposal["workout"].get("steps") or []):
        return
    observed, threshold_pace = transport.run_threshold_pace()
    if not observed:
        return
    if (
        isinstance(threshold_pace, (int, float))
        and not isinstance(threshold_pace, bool)
        and threshold_pace > 0
    ):
        return
    raise DeliveryError(
        f"session {proposal['session_id']} prescribes a pace target, and this Intervals "
        "account has no Run threshold pace set. Intervals would accept the workout and "
        "then export it without its pace target, so the watch would show the distances "
        "with no target at all. Set the Run threshold pace in Intervals (Settings -> "
        "Sport Settings -> Run), then deliver again. This is the provider's own export "
        "prerequisite, not the plan's athlete_baseline.threshold_pace_sec_per_km, which "
        "is already recorded."
    )


def _confirmed_run_threshold_hr(
    proposal: dict[str, Any], transport: IntervalsTransport
) -> int | None:
    """Re-read the threshold HR the confirmed preview resolved against, and require it.

    The athlete confirmed a bpm ceiling, not a percentage. That number is only true while
    the account's Run threshold HR is the one the preview read: change it between preview
    and confirmation and the same `86% LTHR` resolves somewhere else. Rather than deliver
    a percentage whose meaning moved, this fails and asks for a fresh preview -- the same
    stance the plan-version and session-content checks above it take.

    Returns ``None`` when the proposal carries no ceiling, which is every other workout.
    """
    resolution = (proposal.get("preview") or {}).get("hr_ceiling_resolution")
    if not resolution:
        return None
    confirmed = resolution["run_threshold_hr"]
    observed, current = transport.run_threshold_hr()
    if not observed:
        raise DeliveryError(_MISSING_RUN_THRESHOLD_HR)
    if isinstance(current, bool) or not isinstance(current, int) or current < 1:
        raise DeliveryError(_MISSING_RUN_THRESHOLD_HR)
    if current != confirmed:
        raise DeliveryError(
            f"this Intervals account's Run threshold HR changed from {confirmed} to "
            f"{current} after the preview was confirmed, so the confirmed ceiling of "
            f"{resolution['resolved_ceiling_bpm']}bpm is no longer what would be "
            "delivered; preview and confirm this workout again"
        )
    return current


def _write_and_verify(
    proposal: dict[str, Any],
    *,
    transport: IntervalsTransport,
    journal: Any,
    event_id: str | None,
) -> tuple[str, dict[str, Any]]:
    """Write the approved payload to Intervals and read it back exactly.

    Every state this can stop in is journalled before the call that could stop there, so
    the store never has to infer from "no receipt" that nothing happened:
    ``mutation_started`` before the request (a process that dies inside it leaves that),
    ``mutated_unverified`` the moment Intervals answers with an id, and only then the
    read-back that may fail.
    """
    session_id = proposal["session_id"]
    journal.record(
        session_id,
        "mutation_started",
        external_id=event_id,
        detail="writing the approved workout to Intervals",
    )
    result = transport.bulk_upsert(_provider_payload(proposal))
    if len(result) != 1 or result[0].get("id") is None:
        # The provider answered something this code cannot read as one written event, so
        # whether it wrote one is unknown -- which is exactly what stays journalled.
        journal.record(
            session_id,
            "mutated_unverified",
            detail="Intervals did not return exactly one written event id",
        )
        raise DeliveryError("Intervals did not return exactly one written event id")
    written_id = str(result[0]["id"])
    journal.record(
        session_id,
        "mutated_unverified",
        external_id=written_id,
        detail="Intervals accepted the write; the read-back has not been verified",
    )
    if event_id is not None and written_id != event_id:
        raise DeliveryError("Intervals upsert changed the owned event id")
    readback = transport.get_event(written_id)
    try:
        verify_readback(proposal, readback, written_id)
    except DeliveryError as exc:
        # The write already happened. Failing closed keeps the state honest, but the
        # event stays on the athlete's calendar until something removes or corrects it
        # (issue #75), so the failure has to say which event that is -- and the journal
        # keeps saying it after this process is gone.
        journal.record(session_id, "mutated_unverified", external_id=written_id, detail=str(exc))
        raise DeliveryError(
            f"{exc}; Intervals event {written_id} was written on "
            f"{proposal['workout']['scheduled_date']} and does not match the approved "
            "workout -- retry this same approved delivery, which rewrites this same "
            "event, or withdraw it"
        ) from exc
    return written_id, readback


def publish_delivery(
    proposal: dict[str, Any],
    approval: dict[str, Any],
    *,
    load_current_plan: Callable[[], dict[str, Any]],
    transport: IntervalsTransport,
    now: dt.datetime | None = None,
    journal: Any | None = None,
    known_event_id: str | None = None,
    accept_plan_versions: set[int] | None = None,
) -> dict[str, Any]:
    """Write or reuse one owned event and return a receipt only after exact read-back.

    This is also the retry path. Nothing here assumes it runs first: it asks Intervals what
    it holds under this product's marker before writing anything, so an operation the
    journal left ``mutation_started`` or ``mutated_unverified`` converges here -- verified
    if the provider already holds the approved content, overwritten in place if it holds
    something else, written fresh if the earlier mutation never landed. The upsert is keyed
    on the owned marker, so no branch can produce a second event.

    ``known_event_id`` is what the journal recorded for an unresolved mutation. It is only
    consulted when the day's list shows no owned event -- the one case where "the write did
    not land" and "the event moved" look identical from the list alone.

    ``accept_plan_versions`` widens the stale-preview refusal to the versions this attempt
    itself committed. Nothing else can move the plan while the reservation is open, and the
    session's own content hash is still checked exactly.
    """
    journal = journal if journal is not None else _NullJournal()
    _validate_approval(proposal, approval)
    current = load_current_plan()
    _current_plan_is_valid(current)
    if current.get("plan_id") != proposal.get("plan_id"):
        raise DeliveryError("current plan_id changed after preview")
    accepted = accept_plan_versions or {proposal["plan_version"]}
    if current.get("version") not in accepted:
        raise DeliveryError("current PlanState version changed after preview")
    session = _plan_session(current, proposal["session_id"])
    if session_content_hash(session) != proposal.get("session_content_hash"):
        raise DeliveryError("selected current session changed after preview")
    execution = session.get("execution") if isinstance(session.get("execution"), dict) else {}
    if execution.get("publish_supported") is not True:
        raise DeliveryError("selected session no longer supports publishing")
    run_threshold_hr = _confirmed_run_threshold_hr(proposal, transport)
    if _workout_from_session(session, run_threshold_hr) != proposal.get("workout"):
        raise DeliveryError("approved workout is not the selected current PlanState workout")

    listed = transport.list_events(proposal["workout"]["scheduled_date"])
    owned = [event for event in listed if event.get("external_id") == proposal["owned_external_id"]]
    if len(owned) > 1:
        raise DeliveryError("multiple Intervals events carry this product-owned external_id")

    operation = "upserted"
    event_id: str | None = None
    existing: dict[str, Any] | None = None
    if owned:
        event_id = str(owned[0].get("id") or "") or None
        if event_id is None:
            raise DeliveryError("owned Intervals event has no id")
        if known_event_id is not None and known_event_id != event_id:
            # Two events carry this marker and the day's list shows only one of them: the
            # journal remembers an id this list does not. Overwriting the one in front of
            # us would leave the other on the calendar, which is the duplicate this whole
            # boundary exists to prevent -- so it refuses and names both.
            raise DeliveryError(
                f"this delivery already wrote Intervals event {known_event_id} for "
                f"{proposal['session_id']}, but {proposal['workout']['scheduled_date']} "
                f"now carries event {event_id} under the same product-owned marker; read "
                "the Intervals calendar and remove the one that should not be there"
            )
        existing = transport.get_event(event_id)
    elif known_event_id is not None:
        # The journal says a mutation may have landed under this id and the day's list does
        # not show it. Only a 404 for that exact id means it is gone (AGENTS.md 3); anything
        # else is still this product's event and is corrected in place, never duplicated.
        found = transport.find_event(known_event_id)
        if found is not None:
            if found.get("external_id") != proposal["owned_external_id"]:
                raise DeliveryError(
                    f"Intervals event {known_event_id} is no longer the product-owned event "
                    f"for {proposal['session_id']}; read the calendar before retrying"
                )
            event_id, existing = known_event_id, found
        else:
            journal.record(
                proposal["session_id"],
                "not_started",
                detail="Intervals does not hold the event this attempt may have written",
            )

    if existing is not None:
        assert event_id is not None
        try:
            verify_readback(proposal, existing, event_id)
        except DeliveryError:
            operation = "updated"
        else:
            operation = "deduplicated_existing"
            readback = existing

    if operation in {"upserted", "updated"}:
        # Checked here rather than at the top, so that it gates a mutation and only a
        # mutation. An event the provider already holds correctly still verifies and still
        # converges an unresolved attempt while the setting is missing; what is refused is
        # putting a new pace target somewhere it would arrive stripped.
        _refuse_pace_the_provider_would_strip(proposal, transport)
        event_id, readback = _write_and_verify(
            proposal, transport=transport, journal=journal, event_id=event_id
        )

    assert event_id is not None
    readback_hash = canonical_hash(readback)
    verified_at = _utc_iso(now)
    observation = {
        "plan_id": proposal["plan_id"],
        # The version this observation may be committed against is the one that is current
        # now, not the one the preview was taken under: a resumed attempt may have already
        # committed an earlier item of the same set. Every other binding -- plan, session,
        # exact content -- is unchanged and still checked above.
        "plan_version": current["version"],
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
    receipt = {
        "status": "passed",
        "delivery_state": "intervals_accepted",
        "operation": operation,
        "owned_external_id": proposal["owned_external_id"],
        "observation": observation,
        "receipt_id": f"delivery-receipt-{canonical_hash(receipt_material)[:24]}",
    }
    journal.record(
        proposal["session_id"],
        "verified",
        external_id=event_id,
        detail=None,
        result=receipt,
    )
    return receipt


def publish_delivery_set(
    proposal_set: dict[str, Any],
    approval: dict[str, Any],
    *,
    load_current_plan: Callable[[], dict[str, Any]],
    transport: IntervalsTransport,
    now: dt.datetime | None = None,
    journal: Any | None = None,
    resolved: set[str] | None = None,
    known_event_ids: dict[str, str] | None = None,
    accept_plan_versions: set[int] | None = None,
) -> dict[str, Any]:
    """Deliver one approved publish set, preserving whatever Intervals already accepted.

    Every item re-reads current state immediately before its own provider write, rather
    than trusting one read taken before the set began: a plan revision committed mid-set
    must fail before the provider is touched, not merely before state advances (#110).

    Intervals offers no transaction across two events, so this does not pretend to one.
    When a later item fails after an earlier one was already written and read back, the
    verified items are returned with ``status: partial`` and the unresolved item is named.
    Collapsing that into an exception would leave the athlete with an event the product
    reports as unpublished. Each item journals its own provider boundary as it crosses it,
    so the record survives this function returning at all.

    ``resolved`` names the sessions a resumed attempt has already recorded in PlanState;
    they are neither re-written nor reported as unresolved.
    """
    _validate_set_approval(proposal_set, approval)
    current = load_current_plan()
    if current.get("plan_id") != proposal_set.get("plan_id"):
        raise DeliveryError("current plan_id changed after publish-set preview")
    accepted_versions = accept_plan_versions or {proposal_set["plan_version"]}
    if current.get("version") not in accepted_versions:
        raise DeliveryError("current PlanState version changed after publish-set preview")

    approved_at = str(approval.get("approved_at", "")).replace("Z", "+00:00")
    try:
        approval_time = dt.datetime.fromisoformat(approved_at)
    except ValueError as exc:
        raise DeliveryError("delivery set approval approved_at is invalid") from exc
    receipts: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    already = resolved or set()
    items = [item for item in proposal_set["items"] if item["session_id"] not in already]
    for index, proposal in enumerate(items):
        derived_approval = approve_delivery_proposal(
            proposal,
            approved_by=approval.get("approved_by", ""),
            approved_at=approval_time,
        )
        try:
            receipt = publish_delivery(
                proposal,
                derived_approval,
                load_current_plan=load_current_plan,
                transport=transport,
                now=now,
                journal=journal,
                known_event_id=(known_event_ids or {}).get(proposal["session_id"]),
                accept_plan_versions=accepted_versions,
            )
        except DeliveryError as exc:
            if not receipts and not already:
                # Nothing has been accepted for this set at all -- not in this call, not in
                # an earlier one. There is nothing to preserve and nothing to report, so
                # this stays the all-or-nothing failure it has always been. Whatever the
                # provider may already hold is in the journal, not in this return value.
                raise
            unresolved = [
                {
                    "session_id": item["session_id"],
                    "scheduled_date": item["workout"]["scheduled_date"],
                    "error": str(exc) if position == index else "not attempted",
                }
                for position, item in enumerate(items)
                if position >= index
            ]
            break
        receipts.append(receipt)
    receipt_material = {
        "proposal_hash": proposal_set["proposal_hash"],
        "item_receipts": [receipt["receipt_id"] for receipt in receipts],
    }
    return {
        "status": "partial" if unresolved else "passed",
        "delivery_state": "intervals_accepted",
        "proposal_hash": proposal_set["proposal_hash"],
        "item_receipts": receipts,
        "observations": [receipt["observation"] for receipt in receipts],
        "unresolved": unresolved,
        "receipt_id": f"delivery-set-receipt-{canonical_hash(receipt_material)[:24]}",
    }


def _sessions_by_id(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        session["session_id"]: session
        for session in (plan.get("week") or {}).get("sessions", [])
        if isinstance(session, dict) and isinstance(session.get("session_id"), str)
    }


def _operation_is_recorded(
    sessions: dict[str, dict[str, Any]], operation: dict[str, Any]
) -> bool:
    """Does current PlanState already say what this journalled provider effect says?

    Both shapes an attempt journals: a delivery, whose session now carries the event id
    Intervals accepted, and a withdrawal, whose session no longer carries the superseded
    event at all.
    """
    execution = (sessions.get(operation["session_id"]) or {}).get("execution") or {}
    if operation["operation"] == "delete":
        return execution.get("superseded_external_id") != operation.get("external_id")
    return (
        execution.get("delivery_state") == "intervals_accepted"
        and execution.get("external_id") == operation.get("external_id")
    )


def _reconcile_attempt(state_dir: Any, attempt: dict[str, Any]) -> dict[str, Any]:
    """Re-derive each journalled operation's state from what PlanState actually records.

    Two directions, and both are real. An operation the journal calls ``verified`` may
    already be recorded, because a process can die between the commit and the mark -- that
    retry has nothing left to do and must not be told the plan moved (issue #121). An
    operation the journal calls ``recorded`` may not be, if the store it was recorded
    against is no longer the store in front of us -- so it goes back to being an
    unverified provider effect and is converged against Intervals again.

    Only the plan is consulted here. Nothing in this function talks to the provider.
    """
    operations = attempt["operations"]
    if all(operation["state"] in {"not_started"} for operation in operations):
        return attempt
    sessions = _sessions_by_id(read_current_plan(state_dir)["current_plan"])
    for operation in operations:
        if operation["state"] == "verified" and _operation_is_recorded(sessions, operation):
            attempt = record_delivery_attempt_operation(
                state_dir,
                attempt_id=attempt["attempt_id"],
                session_id=operation["session_id"],
                state="recorded",
                detail="the plan already records this delivery",
            )
        elif operation["state"] == "recorded" and not _operation_is_recorded(sessions, operation):
            attempt = record_delivery_attempt_operation(
                state_dir,
                attempt_id=attempt["attempt_id"],
                session_id=operation["session_id"],
                state="mutated_unverified",
                detail="the plan no longer records this delivery; verifying it again",
            )
    return attempt


def _accept_plan_versions(attempt: dict[str, Any]) -> set[int]:
    """Which current plan versions this attempt may still publish against.

    The version it was bound to, plus whatever version its own recording produced. Nothing
    else can move the plan while the reservation is open, so this stays exact rather than
    becoming "any version at least as new as the preview".
    """
    versions = {attempt["plan_version"]}
    if attempt["recorded_plan_version"] is not None:
        versions.add(attempt["recorded_plan_version"])
    return versions


def _open_attempt(
    state_dir: Any,
    *,
    kind: str,
    proposal_set: dict[str, Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        return open_delivery_attempt(
            state_dir,
            kind=kind,
            plan_id=proposal_set["plan_id"],
            plan_version=proposal_set["plan_version"],
            proposal_hash=proposal_set["proposal_hash"],
            operations=operations,
        )
    except StateStoreError as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        if details.get("kind") != "stale_plan_binding":
            raise
        # The plan moved between the preview and this confirmation. That is a delivery
        # boundary refusal -- re-preview against the current plan -- not a broken store.
        raise DeliveryError(str(exc)) from exc


def _release_if_untouched(state_dir: Any, attempt_id: str) -> None:
    """Release a reservation an interrupted run left protecting nothing.

    Reached only from an exception that is not a delivery refusal -- a crash, a killed
    transport, a bug. The journal decides, not the exception: an operation that reached
    the provider keeps the reservation, and one that never did must not fence the
    athlete's next plan change behind a delivery that did not happen.
    """
    try:
        attempt = pending_delivery_attempt(state_dir)
    except StateStoreError:
        # Never mask the exception that brought us here with one about the journal.
        return
    if (
        attempt is not None
        and attempt["attempt_id"] == attempt_id
        and not unresolved_delivery_operations(attempt)
    ):
        close_delivery_attempt(state_dir, attempt_id=attempt_id)


def _settle_attempt(
    state_dir: Any,
    attempt: dict[str, Any],
    *,
    failure: DeliveryError | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Close the reservation only when nothing about it is outstanding, and say why not.

    This is the fix issue #121 asks for, in one place: the reservation is released on the
    journal's own account of what Intervals may hold, never on "this run produced no
    verified receipt". A run that wrote an event and failed its read-back produces no
    receipt and must keep the reservation; a run that never reached the provider produces
    no receipt and must release it.
    """
    outstanding = unresolved_delivery_operations(attempt)
    if not outstanding:
        close_delivery_attempt(state_dir, attempt_id=attempt["attempt_id"])
    if failure is not None and outstanding:
        raise DeliveryError(
            f"{failure}; delivery attempt {attempt['attempt_id']} stays open and holds "
            "unreconciled Intervals effects -- retry this same approved set to converge "
            "them, or read the Intervals calendar and run clear-delivery-attempt"
        ) from failure
    if failure is not None:
        raise failure
    return attempt, outstanding


def _recorded_results(
    attempt: dict[str, Any], session_ids: list[str]
) -> list[dict[str, Any]]:
    """The journalled result of every recorded operation, in the set's own order.

    The journal is keyed by session, so it has no opinion about order. The receipt does:
    the athlete reads it as the set they confirmed, which is ordered by day.
    """
    operations = {operation["session_id"]: operation for operation in attempt["operations"]}
    return [
        operations[session_id]["result"]
        for session_id in session_ids
        if operations[session_id]["state"] == "recorded" and operations[session_id]["result"]
    ]


def _committable(
    state_dir: Any, attempt: dict[str, Any], *, field: str
) -> list[dict[str, Any]]:
    """The verified results this store may commit right now.

    A result verified against a plan version that is no longer current cannot be committed
    -- the store refuses it, correctly. Rather than letting that refusal fail the whole
    call, such an operation stays unresolved, keeps the reservation open, and is verified
    again against the current version on the next retry.
    """
    current_version = read_current_plan(state_dir)["current_version"]
    return [
        operation["result"][field]
        for operation in attempt["operations"]
        if operation["state"] == "verified"
        and operation["result"]
        and operation["result"][field]["plan_version"] == current_version
    ]


def _derived_state_update(
    state_dir: Any, attempt: dict[str, Any], *, policy: str, key: str
) -> dict[str, Any]:
    """What the store would have said, for an attempt whose commit already happened.

    The interruption this answers is narrow and real: `apply_delivery_observations`
    committed, the process died before the reservation was released, and the retry arrives
    with everything already true. Re-running the provider write would be wrong and
    re-committing is impossible, so the retry reports the success that exists instead of a
    stale-version failure it would have to invent.
    """
    current = read_current_plan(state_dir)
    recorded = [
        operation for operation in attempt["operations"] if operation["state"] == "recorded"
    ]
    return {
        "status": "passed",
        "idempotent_replay": True,
        "plan_id": current["plan_id"],
        "current_version": current["current_version"],
        "event_count": current["event_count"],
        "session_ids": [operation["session_id"] for operation in recorded],
        key: [operation["external_id"] for operation in recorded],
        "policy": policy,
    }


def deliver_approved_set(
    state_dir: Any,
    proposal_set: dict[str, Any],
    approval: dict[str, Any],
    *,
    transport: IntervalsTransport,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Reserve, publish, record: the one delivery boundary the CLI and the gateway share.

    The reservation is taken before the first provider write and released only once every
    operation it journalled is settled, so a plan change cannot land in the middle of a
    set, an older checkout meeting a newer writer contract writes nothing to Intervals at
    all, and an interruption anywhere leaves the operation, the product-owned marker and
    the provider event id on disk.

    Retrying is the same call with the same approved set. It converges rather than
    repeats: operations the plan already records are skipped, operations Intervals may
    hold are re-read and either verified or overwritten in place, and operations that
    never reached the provider are written for the first time.
    """
    items = proposal_set.get("items")
    if not isinstance(items, list) or not items:
        raise DeliveryError("delivery set must contain at least one proposal")
    attempt = _open_attempt(
        state_dir,
        kind="delivery",
        proposal_set=proposal_set,
        operations=[
            {
                "session_id": str(item["session_id"]),
                "operation": "upsert",
                "owned_external_id": item["owned_external_id"],
                "scheduled_date": item["workout"]["scheduled_date"],
            }
            for item in items
        ],
    )
    attempt_id = attempt["attempt_id"]
    attempt = _reconcile_attempt(state_dir, attempt)
    journal = _AttemptJournal(state_dir, attempt_id)

    failure: DeliveryError | None = None
    result: dict[str, Any] | None = None
    try:
        result = publish_delivery_set(
            proposal_set,
            approval,
            load_current_plan=lambda: read_current_plan(state_dir)["current_plan"],
            transport=transport,
            now=now,
            journal=journal,
            resolved={
                operation["session_id"]
                for operation in attempt["operations"]
                if operation["state"] == "recorded"
            },
            known_event_ids={
                operation["session_id"]: operation["external_id"]
                for operation in attempt["operations"]
                if operation["state"] in {"mutation_started", "mutated_unverified"}
                and operation["external_id"]
            },
            accept_plan_versions=_accept_plan_versions(attempt),
        )
    except DeliveryError as exc:
        failure = exc
    except Exception:
        _release_if_untouched(state_dir, attempt_id)
        raise
    attempt = pending_delivery_attempt(state_dir) or attempt

    observations = _committable(state_dir, attempt, field="observation")
    state_update: dict[str, Any] | None = None
    if observations:
        state_update = apply_delivery_observations(
            state_dir, observations=observations, attempt_id=attempt_id
        )
        attempt = mark_delivery_attempt_recorded(
            state_dir,
            attempt_id=attempt_id,
            session_ids=[observation["session_id"] for observation in observations],
            plan_version=state_update["current_version"],
        )

    attempt, outstanding = _settle_attempt(state_dir, attempt, failure=failure)
    # What this delivery has achieved is what the journal says it recorded -- across every
    # run of this attempt, not only this one. A retry that finished the set reports the
    # whole set, and a retry that had nothing left to do reports the same success the
    # interrupted run would have reported.
    receipts = _recorded_results(attempt, [item["session_id"] for item in items])
    unresolved = (result or {}).get("unresolved") or []
    if state_update is None:
        state_update = _derived_state_update(
            state_dir, attempt, policy="verified_intervals_delivery", key="external_ids"
        )
    return {
        "status": "partial" if unresolved else "passed",
        "delivery_state": "intervals_accepted",
        "proposal_hash": proposal_set["proposal_hash"],
        "item_receipts": receipts,
        "observations": [receipt["observation"] for receipt in receipts],
        "unresolved": unresolved,
        "receipt_id": "delivery-set-receipt-"
        + canonical_hash(
            {
                "proposal_hash": proposal_set["proposal_hash"],
                "item_receipts": [receipt["receipt_id"] for receipt in receipts],
            }
        )[:24],
        "attempt_id": attempt_id,
        "attempt_open": bool(outstanding),
        "state_update": state_update,
    }


# --------------------------------------------------------------------------------------
# Withdrawing a superseded event (issue #113)
# --------------------------------------------------------------------------------------


WITHDRAWAL_SET_SCHEMA_VERSION = "1.0"

# How far either side of the plan's own week to look for a product-owned event. The
# marker does not encode a date, so a session that moved is still found; the window is
# bounded rather than open-ended, and only events carrying this product's marker are ever
# touched inside it.
_WITHDRAWAL_SEARCH_MARGIN_DAYS = 7


def _week_search_range(plan: dict[str, Any]) -> tuple[str, str]:
    start = (plan.get("week") or {}).get("start")
    try:
        first = dt.date.fromisoformat(str(start))
    except ValueError as exc:
        raise DeliveryError("current plan has no usable week start") from exc
    return (
        (first - dt.timedelta(days=_WITHDRAWAL_SEARCH_MARGIN_DAYS)).isoformat(),
        (first + dt.timedelta(days=6 + _WITHDRAWAL_SEARCH_MARGIN_DAYS)).isoformat(),
    )


def prepare_withdrawal_set(
    current_plan: dict[str, Any],
    selected_session_ids: list[str],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Bind the exact provider events a confirmed change left contradicting the plan.

    Withdrawal is only ever offered for an event PlanState already records as superseded.
    A session whose current content is still the delivered content is not withdrawn; it is
    changed first, which is what makes the event superseded in the first place.
    """
    _current_plan_is_valid(current_plan)
    if not isinstance(selected_session_ids, list) or not selected_session_ids:
        raise DeliveryError("withdrawal set must contain at least one session_id")
    created_at = now or dt.datetime.now(dt.timezone.utc)
    items: list[dict[str, Any]] = []
    for session_id in selected_session_ids:
        session = _plan_session(current_plan, session_id)
        execution = session.get("execution") if isinstance(session.get("execution"), dict) else {}
        superseded = execution.get("superseded_external_id")
        if not isinstance(superseded, str) or not superseded.strip():
            raise DeliveryError(
                f"session {session_id} holds no superseded Intervals event to withdraw"
            )
        items.append(
            {
                "session_id": session["session_id"],
                "scheduled_date": session["scheduled_date"],
                "superseded_external_id": superseded,
                "owned_external_id": owned_external_id_for(current_plan, session["session_id"]),
            }
        )
    items.sort(key=lambda item: (item["scheduled_date"], item["session_id"]))
    if len({item["session_id"] for item in items}) != len(items):
        raise DeliveryError("withdrawal set contains the same session_id more than once")
    proposal_set: dict[str, Any] = {
        "schema_version": WITHDRAWAL_SET_SCHEMA_VERSION,
        "direction": WITHDRAW_DIRECTION,
        "proposal_id": "withdrawal-set-"
        + canonical_hash([item["superseded_external_id"] for item in items])[:20],
        "plan_id": current_plan["plan_id"],
        "plan_version": current_plan["version"],
        "items": items,
        "created_at": _utc_iso(created_at),
        "state": "AWAITING_CONFIRMATION",
    }
    proposal_set["proposal_hash"] = _set_hash(proposal_set)
    return proposal_set


def _validate_withdrawal_set(proposal_set: dict[str, Any]) -> None:
    _exact_keys(
        proposal_set,
        {
            "schema_version", "direction", "proposal_id", "proposal_hash", "plan_id",
            "plan_version", "items", "created_at", "state",
        },
        set(),
        "withdrawal set",
    )
    if proposal_set.get("schema_version") != WITHDRAWAL_SET_SCHEMA_VERSION:
        raise DeliveryError("withdrawal set schema_version is unsupported")
    if proposal_set.get("direction") != WITHDRAW_DIRECTION:
        raise DeliveryError("withdrawal set direction is not a withdrawal")
    if proposal_set.get("state") != "AWAITING_CONFIRMATION":
        raise DeliveryError("withdrawal set is not awaiting confirmation")
    items = proposal_set.get("items")
    if not isinstance(items, list) or not items:
        raise DeliveryError("withdrawal set must contain at least one item")
    for item in items:
        _exact_keys(
            _mapping(item, "withdrawal set item"),
            {"session_id", "scheduled_date", "superseded_external_id", "owned_external_id"},
            set(),
            "withdrawal set item",
        )
    if len({item["session_id"] for item in items}) != len(items):
        raise DeliveryError("withdrawal set contains duplicate sessions")
    if proposal_set.get("proposal_hash") != _set_hash(proposal_set):
        raise DeliveryError("withdrawal set content changed after hashing")


def approve_withdrawal_set(
    proposal_set: dict[str, Any],
    *,
    approved_by: str,
    approved_at: dt.datetime | None = None,
) -> dict[str, Any]:
    _validate_withdrawal_set(proposal_set)
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise DeliveryError("approved_by must be non-empty")
    return {
        "schema_version": WITHDRAWAL_SET_SCHEMA_VERSION,
        "direction": WITHDRAW_DIRECTION,
        "approval_id": f"approval-{proposal_set['proposal_hash'][:20]}",
        "proposal_id": proposal_set["proposal_id"],
        "proposal_hash": proposal_set["proposal_hash"],
        "plan_id": proposal_set["plan_id"],
        "plan_version": proposal_set["plan_version"],
        "status": "APPROVED",
        "approved_by": approved_by.strip(),
        "approved_at": _utc_iso(approved_at),
    }


def _validate_withdrawal_approval(proposal_set: dict[str, Any], approval: dict[str, Any]) -> None:
    _validate_withdrawal_set(proposal_set)
    expected = {
        "direction": WITHDRAW_DIRECTION,
        "proposal_id": proposal_set["proposal_id"],
        "proposal_hash": proposal_set["proposal_hash"],
        "plan_id": proposal_set["plan_id"],
        "plan_version": proposal_set["plan_version"],
    }
    if approval.get("schema_version") != WITHDRAWAL_SET_SCHEMA_VERSION:
        raise DeliveryError("withdrawal approval schema_version is unsupported")
    if approval.get("status") != "APPROVED":
        raise DeliveryError("withdrawal approval is not APPROVED")
    for field, value in expected.items():
        if approval.get(field) != value:
            raise DeliveryError(f"withdrawal approval {field} mismatch")


def _owned_matches(
    transport: IntervalsTransport,
    plan: dict[str, Any],
    owned_external_id: str,
) -> list[dict[str, Any]]:
    oldest, newest = _week_search_range(plan)
    listed = transport.list_events_range(oldest, newest)
    return [event for event in listed if event.get("external_id") == owned_external_id]


def withdraw_approved_set(
    state_dir: Any,
    proposal_set: dict[str, Any],
    approval: dict[str, Any],
    *,
    transport: IntervalsTransport,
    now: dt.datetime | None = None,
    today: str | None = None,
) -> dict[str, Any]:
    """Remove the superseded events the athlete confirmed, and verify they are gone.

    Ownership is decided by the product's own marker, never by the recorded numeric id
    alone: an event this product did not write is never deleted, and two events carrying
    the same marker are refused rather than guessed between. A session whose date has
    already passed is not touched at all -- editing a future plan must not delete the
    record of what was actually done.
    """
    _validate_withdrawal_approval(proposal_set, approval)
    items = proposal_set["items"]
    as_of = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    attempt = _open_attempt(
        state_dir,
        kind="withdrawal",
        proposal_set=proposal_set,
        operations=[
            {
                "session_id": str(item["session_id"]),
                "operation": "delete",
                "owned_external_id": item["owned_external_id"],
                "scheduled_date": item["scheduled_date"],
                "external_id": item["superseded_external_id"],
            }
            for item in items
        ],
    )
    attempt_id = attempt["attempt_id"]
    attempt = _reconcile_attempt(state_dir, attempt)
    journal = _AttemptJournal(state_dir, attempt_id)
    accepted_versions = _accept_plan_versions(attempt)
    already = {
        operation["session_id"]
        for operation in attempt["operations"]
        if operation["state"] == "recorded"
    }

    failure: DeliveryError | None = None
    unresolved: list[dict[str, Any]] = []
    pending = [item for item in items if item["session_id"] not in already]
    for index, item in enumerate(pending):
        try:
            withdrawal = _withdraw_one(
                state_dir,
                item,
                proposal_set=proposal_set,
                transport=transport,
                journal=journal,
                accepted_versions=accepted_versions,
                as_of=as_of,
                now=now,
            )
        except Exception as exc:
            if not isinstance(exc, DeliveryError):
                _release_if_untouched(state_dir, attempt_id)
                raise
            if not already and index == 0:
                # Nothing has been withdrawn for this set at all; the same all-or-nothing
                # failure as before. What the provider may already hold is in the journal.
                failure = exc
                break
            unresolved = [
                {
                    "session_id": remaining["session_id"],
                    "error": str(exc) if position == index else "not attempted",
                }
                for position, remaining in enumerate(pending)
                if position >= index
            ]
            break
        journal.record(
            item["session_id"],
            "verified",
            external_id=withdrawal["withdrawn_external_id"],
            detail=None,
            result={"withdrawal": withdrawal},
        )

    attempt = pending_delivery_attempt(state_dir) or attempt
    withdrawals = _committable(state_dir, attempt, field="withdrawal")
    state_update: dict[str, Any] | None = None
    if withdrawals:
        state_update = apply_delivery_withdrawals(
            state_dir, withdrawals=withdrawals, attempt_id=attempt_id
        )
        attempt = mark_delivery_attempt_recorded(
            state_dir,
            attempt_id=attempt_id,
            session_ids=[withdrawal["session_id"] for withdrawal in withdrawals],
            plan_version=state_update["current_version"],
        )

    attempt, outstanding = _settle_attempt(state_dir, attempt, failure=failure)
    recorded = [
        result["withdrawal"]
        for result in _recorded_results(attempt, [item["session_id"] for item in items])
    ]
    if state_update is None:
        state_update = _derived_state_update(
            state_dir,
            attempt,
            policy="verified_intervals_withdrawal",
            key="withdrawn_external_ids",
        )
    return {
        "status": "partial" if unresolved else "passed",
        "proposal_hash": proposal_set["proposal_hash"],
        "withdrawn": recorded,
        "unresolved": unresolved,
        "attempt_id": attempt_id,
        "attempt_open": bool(outstanding),
        "state_update": state_update,
        "receipt_id": "withdrawal-receipt-"
        + canonical_hash(
            {
                "proposal_hash": proposal_set["proposal_hash"],
                "withdrawn": [item["withdrawn_external_id"] for item in recorded],
            }
        )[:24],
    }


def _withdraw_one(
    state_dir: Any,
    item: dict[str, Any],
    *,
    proposal_set: dict[str, Any],
    transport: IntervalsTransport,
    journal: Any,
    accepted_versions: set[int],
    as_of: str,
    now: dt.datetime | None,
) -> dict[str, Any]:
    """Remove one superseded event and prove it is gone, journalling the delete boundary.

    Idempotent by construction, which is what makes the retry safe: an event Intervals no
    longer holds is already withdrawn, so a delete this attempt performed but could not
    confirm converges on the next run without a second delete.
    """
    plan = read_current_plan(state_dir)["current_plan"]
    if plan.get("plan_id") != proposal_set["plan_id"]:
        raise DeliveryError("current plan_id changed after withdrawal preview")
    if plan.get("version") not in accepted_versions:
        raise DeliveryError("current PlanState version changed after withdrawal preview")
    session = _plan_session(plan, item["session_id"])
    execution = session.get("execution") if isinstance(session.get("execution"), dict) else {}
    if execution.get("superseded_external_id") != item["superseded_external_id"]:
        raise DeliveryError(
            f"session {item['session_id']} no longer holds "
            f"{item['superseded_external_id']} as a superseded event"
        )

    if len(_owned_matches(transport, plan, item["owned_external_id"])) > 1:
        raise DeliveryError("multiple Intervals events carry this product-owned external_id")
    # Keyed on the exact event id, not on whether a list happened to mention it: an empty
    # list is also what a provider returns when it cannot answer, and recording that as
    # "the event is gone" is the one thing a withdrawal must never do. Only a 404 for this
    # id is absence.
    event_id = item["superseded_external_id"]
    existing = transport.find_event(event_id)
    if existing is not None:
        if existing.get("external_id") != item["owned_external_id"]:
            raise DeliveryError(
                f"Intervals event {event_id} is not the product-owned event "
                f"for {item['session_id']}; preview the withdrawal again"
            )
        event_day = str(existing.get("start_date_local", ""))[:10]
        if event_day and event_day < as_of:
            # The event, not the session, is what gets deleted -- and after a move the
            # session's own date is the new one. Checking the session would let a
            # future-dated plan edit remove a past day's record.
            raise DeliveryError(
                f"Intervals event {event_id} is dated {event_day}, which has passed; a "
                "past day's record is never removed by editing a future plan"
            )
        journal.record(
            item["session_id"],
            "mutation_started",
            detail="deleting the superseded Intervals event",
        )
        transport.delete_event(event_id)
        journal.record(
            item["session_id"],
            "mutated_unverified",
            detail="Intervals acknowledged the delete; its absence is not yet verified",
        )
        if transport.find_event(event_id) is not None:
            raise DeliveryError(f"Intervals still holds event {event_id} after the delete")
    return {
        "plan_id": proposal_set["plan_id"],
        "plan_version": plan["version"],
        "session_id": item["session_id"],
        "withdrawn_external_id": item["superseded_external_id"],
        "owned_external_id": item["owned_external_id"],
        "verified_at": _utc_iso(now),
    }
