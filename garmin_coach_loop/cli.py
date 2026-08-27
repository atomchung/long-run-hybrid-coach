"""CLI for validating and continuously using the current Coach Loop plan."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .athlete_evidence import (
    AthleteEvidenceError,
    record_availability,
    record_profile,
    resolve_settings,
)
from .context_builder import (
    DEFAULT_SESSION_MINUTES,
    DEFAULT_SOURCE,
    DEFAULT_TIMEZONE,
    VALID_SOURCES,
    ContextBuildError,
    ContextRequest,
    build_context,
    build_context_with_domain,
    parse_available_days,
    parse_optional_bool,
    parse_red_flag_overrides,
)
from .delivery import (
    DeliveryError,
    IntervalsTransport,
    approve_delivery_set,
    approve_withdrawal_set,
    deliver_approved_set,
    prepare_delivery_set,
    prepare_withdrawal_set,
    withdraw_approved_set,
)
from .gateway import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    PROVIDER,
    STATE_ROOT_ENV_VAR,
    GatewayConfigError,
    identity_db_path,
    load_config,
    run_gateway,
)
from .hosted import (
    GATEWAY_URL_ENV_VAR,
    HOSTED_TOKEN_ENV_VAR,
    HostedEntryError,
    configured_gateway,
    hosted_connection,
    require_local_store_write,
)
from .identity import (
    IdentityError,
    activity_report,
    delete_owner_identity,
    owner_for_provider_athlete,
    owner_identity_row_counts,
    revoke_owner_connections,
)
from .prescription import LANGUAGES
from .reconcile import apply_reconciliation
from .source_intervals import resolve_credentials
from .store import (
    ADOPTION_MODES,
    StateStoreError,
    adopt_store,
    apply_decision,
    archive_store,
    assert_outside_repository,
    close_delivery_attempt,
    default_state_dir,
    delete_owner_store,
    doctor_store,
    export_bundle,
    history_store,
    import_bundle,
    init_store,
    resolve_state_dir,
    resolve_state_root,
    restore_snapshot,
    seal_store,
    set_baseline,
    snapshot_store,
    status_store,
)
from .validation import validate_bundle


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


# Which commands write a local store. One list, checked once in `main`, rather than a
# check inside each handler: a command added later is caught by being absent from here,
# which is a review question, instead of by remembering to repeat a guard.
LOCAL_STORE_WRITERS = frozenset(
    {
        "init-store",
        "apply-decision",
        "set-baseline",
        "record-profile",
        "record-availability",
        "refresh-context",
        "publish-delivery",
        "withdraw-delivery",
        "restore-store",
        # Installs a whole store at a local path, which on a machine with a hosted coach
        # is a second plan appearing without anybody saying so.
        "import-store",
        # Changes what this machine believes about a delivery to Intervals. Recovery, and
        # still a local write: on a hosted machine it needs the same sentence said.
        "clear-delivery-attempt",
    }
)


def _add_offline_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "work on a local store even though a hosted coach is configured; what is "
            "written here is deliberately not the athlete's current plan"
        ),
    )


def _announce_authorization(url: str) -> None:
    """Say where the browser is going, on stderr, so stdout stays one JSON report.

    The URL carries a client id, a state and a PKCE challenge -- nothing that is worth
    anything to whoever reads the terminal, and printing it is what makes the flow usable
    on a machine where a browser cannot be opened for you.
    """
    print(f"Authorize this machine at:\n  {url}", file=sys.stderr)


def _reconciliation_statement(payload: dict[str, Any]) -> str:
    """One sentence: did this ``startCoachSession`` call commit a new PlanState version.

    ``startCoachSession`` is ``refresh-context`` under another name: it fetches provider
    evidence and can commit a reconciled version before this command prints anything. A
    raw ``reconciliation`` object buried in a full reply, or dropped entirely from a
    compact one, is exactly how a status-shaped read hid a write -- so every render path
    folds this sentence in instead of leaving the object to speak for itself.

    ``new_versions`` counts only the applied entries that are not ``idempotent_replay``:
    a replay is a commit that already happened before this call started (most often a
    retry after a prior run died mid-batch), so it contributes nothing to *this* call's
    own before/after delta even when the batch also contains a genuinely new commit.
    """
    if payload.get("status") == "no_plan_state":
        return "reconciliation: not applicable, no PlanState exists yet"
    reconciliation = payload.get("reconciliation")
    if not isinstance(reconciliation, dict):
        return "reconciliation: unknown, the reply carried no reconciliation object"
    if reconciliation.get("status") == "deferred":
        return (
            "reconciliation: deferred, an unresolved delivery attempt is blocking it "
            f"(attempt {reconciliation.get('attempt_id')!r})"
        )
    applied = reconciliation.get("applied")
    if not isinstance(applied, list) or not applied:
        return "reconciliation: no change"
    new_versions = sum(
        1 for entry in applied
        if isinstance(entry, dict) and not entry.get("idempotent_replay")
    )
    if new_versions == 0:
        return "reconciliation: no change"
    plan_state = payload.get("plan_state")
    after_version = plan_state.get("plan_version") if isinstance(plan_state, dict) else None
    if isinstance(after_version, int) and not isinstance(after_version, bool):
        before_version = after_version - new_versions
        return f"reconciliation applied: version {before_version} -> {after_version}"
    return f"reconciliation applied: {new_versions} session(s) reconciled"


def _hosted_session_summary(gateway: str, payload: dict[str, Any]) -> dict[str, Any]:
    """What the hosted coach currently holds, small enough to read in a terminal.

    The whole reply carries the CoachContext as well, which is the model's input rather
    than the athlete's answer. ``--full`` prints it; this is the shape that answers the
    question the command is actually asked -- which plan is current, and where.

    ``reconciliation`` and ``reconciliation_statement`` are never omitted here, on either
    path: this call can commit a new PlanState version, and a summary that dropped the
    one field saying so is exactly the honesty gap this function closes.
    """
    plan_state = payload.get("plan_state")
    if not isinstance(plan_state, dict):
        # A reply this summary cannot read is not a reply saying there is no plan. Hand
        # the whole thing back rather than rendering an absence the gateway never stated
        # (AGENTS.md invariant 3).
        return {
            **payload,
            "entry": "hosted",
            "gateway": gateway,
            "reconciliation_statement": _reconciliation_statement(payload),
        }
    plan = plan_state.get("current_plan")
    plan = plan if isinstance(plan, dict) else None
    week = (plan or {}).get("week")
    week = week if isinstance(week, dict) else None
    cycle = (plan or {}).get("cycle")
    cycle = cycle if isinstance(cycle, dict) else None
    sessions = (week or {}).get("sessions")
    sessions = sessions if isinstance(sessions, list) else None
    return {
        "status": payload.get("status"),
        "entry": "hosted",
        "gateway": gateway,
        # `present` is the gateway's own statement. Absent means it did not say, which is
        # not the same as it saying no.
        "plan_present": plan_state.get("present"),
        "plan_id": plan_state.get("plan_id"),
        "plan_version": plan_state.get("plan_version"),
        "cycle": None if cycle is None else {"start": cycle.get("start"), "end": cycle.get("end")},
        "week_start": None if week is None else week.get("start"),
        "session_count": None if sessions is None else len(sessions),
        "sessions": None if sessions is None else [
            {
                "session_id": session.get("session_id"),
                "scheduled_date": session.get("scheduled_date"),
                "sport": session.get("sport"),
                "purpose": session.get("purpose"),
                "match_status": session.get("match_status"),
                "delivery_state": (session.get("execution") or {}).get("delivery_state"),
            }
            for session in sessions
            if isinstance(session, dict)
        ],
        "delivery": payload.get("delivery"),
        "unknowns": payload.get("unknowns"),
        "reconciliation": payload.get("reconciliation"),
        "reconciliation_statement": _reconciliation_statement(payload),
    }


def _hosted_state_summary(gateway: str, payload: dict[str, Any]) -> dict[str, Any]:
    """``getCoachState``'s reply, tagged with where it came from.

    ``getCoachState`` already returns a status-sized summary rather than a full
    CoachContext, so unlike ``_hosted_session_summary`` there is no second narrowing step
    here -- only the same ``entry``/``gateway`` framing every hosted render carries.
    """
    return {**payload, "entry": "hosted", "gateway": gateway}


def _add_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--source", default=DEFAULT_SOURCE, choices=VALID_SOURCES)
    parser.add_argument(
        "--db", type=Path, default=None,
        help="personal-os health.db path (only used with --source personal-os)",
    )
    parser.add_argument(
        "--health-db", type=Path, default=None,
        help=(
            "local personal-os health.db path enabling the optional evidence groups "
            "(strength_execution, recovery_signals); env fallback HEALTH_DB_PATH / "
            "GARMIN_COACH_LOOP_HEALTH_DB"
        ),
    )
    parser.add_argument("--as-of", default=None, help="ISO-8601 timestamp; defaults to now")
    parser.add_argument(
        "--timezone", default=None,
        help="IANA timezone for this build only; defaults to the athlete's stored "
             f"profile, then {DEFAULT_TIMEZONE}",
    )
    parser.add_argument(
        "--available-days", default=None,
        help="comma-separated mon,tue,...; omit when availability is not confirmed",
    )
    parser.add_argument(
        "--session-minutes", type=int, default=DEFAULT_SESSION_MINUTES,
        help="confirmed time available for one session; omit when unknown",
    )
    parser.add_argument("--red-flag", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--all-clear", action="store_true")
    parser.add_argument(
        "--leg-fatigue", default="unknown", choices=["normal", "elevated", "severe", "unknown"]
    )
    parser.add_argument(
        "--soreness", default="unknown", choices=["normal", "elevated", "severe", "unknown"]
    )
    parser.add_argument("--schedule-changed", default=None, help="true|false|null")
    parser.add_argument("--equipment-changed", default=None, help="true|false|null")
    parser.add_argument(
        "--unknown", action="append", default=[], dest="extra_unknowns", metavar="NOTE"
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="write the resulting CoachContext JSON to this path on success",
    )


def _context_request(args: argparse.Namespace, timezone_name: str) -> ContextRequest:
    return ContextRequest(
        as_of_raw=args.as_of,
        timezone_name=timezone_name,
        available_days=parse_available_days(args.available_days),
        session_minutes=args.session_minutes,
        red_flags=parse_red_flag_overrides(args.red_flag, all_clear=args.all_clear),
        leg_fatigue=args.leg_fatigue,
        soreness=args.soreness,
        schedule_changed=parse_optional_bool(args.schedule_changed),
        equipment_changed=parse_optional_bool(args.equipment_changed),
        extra_unknowns=args.extra_unknowns,
    )


def _write_context_output(path: Path | None, report: dict[str, Any]) -> None:
    if path is not None and report.get("status") == "passed" and isinstance(report.get("context"), dict):
        path.write_text(
            json.dumps(report["context"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _availability_days(available: str | None, unavailable: str | None) -> dict[str, Any] | None:
    """One availability statement from two comma-separated flags, or None if neither was given.

    Distinguishing "not mentioned" from "mentioned as empty" is the whole reason this
    returns None rather than a pair of empty lists: an omitted flag must not overwrite a
    stored statement with silence.
    """
    if available is None and unavailable is None:
        return None
    return {
        "available_days": parse_available_days(available),
        "unavailable_days": parse_available_days(unavailable),
    }


def _week_statement(args: argparse.Namespace) -> dict[str, Any] | None:
    """One week's statement from the --week-* flags, or None when none was given.

    ``--week-start`` is no longer what makes a week statement exist: the common case is
    about the week the athlete is standing in, and requiring its Monday on the command
    line would be asking the operator to compute what the code already knows.
    """
    if args.week_only is not None:
        statement: dict[str, Any] = {"only_days": parse_available_days(args.week_only)}
    else:
        days = _availability_days(args.week_available, args.week_unavailable)
        if days is None:
            # A note alone is a whole statement about this week -- a trip, a hotel gym --
            # and one that costs no training day. Requiring a day to carry it would make
            # the operator invent one.
            statement = {} if args.week_note is not None else None
            if statement is None:
                return None
        else:
            statement = days
    if args.week_note is not None:
        statement["note"] = args.week_note
    if args.week_start is not None:
        statement["week_start"] = args.week_start
    return statement


def _write_object(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _refuse_existing_export_destination(path: Path) -> None:
    """Refuse a destination that already names something, symlink or not.

    Called on the path as the operator gave it, before anything resolves a symlink away,
    and again on the resolved destination right before it is installed: `lstat` never
    follows the final path component, so a symlink is refused for what it is rather than
    for whatever it currently points to (or fails to) -- and `os.replace` would otherwise
    install over an existing file, or replace a symlink entry, without a sound.
    """
    if path.is_symlink():
        raise StateStoreError(
            f"refusing to write an exported store bundle through a symlink: {path}"
        )
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise StateStoreError(f"refusing to overwrite an existing exported store bundle: {path}")


def _write_private_bundle(path: Path, value: dict[str, Any]) -> None:
    """Write an exported store bundle so it is never observable as more than 0600.

    The bundle is the athlete's whole plan history in one file, so its mode has to be
    exactly 0600 from the first byte on disk, not chmod-ed there after the fact: a umask
    only ever narrows an explicit `os.open` mode, never widens it, so opening the temporary
    file with `O_EXCL` and 0o600 is private no matter the caller's umask. The temporary
    file sits next to the destination so installing it is one same-filesystem
    `os.replace`, and the destination is checked again immediately before that call --
    `O_EXCL` on the *temporary* file says nothing about what already sits at the
    destination itself. Any failure removes the temporary file; nothing partial is ever
    left under the destination's name.
    """
    _refuse_existing_export_destination(path)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _refuse_existing_export_destination(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _run_threshold_hr() -> int | None:
    """The account's Run threshold HR, or ``None`` when it cannot be read.

    Read at preview because that is where a heart-rate ceiling becomes an exact number
    the athlete confirms. ``None`` is not a silent downgrade: `prepare_delivery_set`
    turns it into one blocking, actionable message, and only for a workout that carries
    a ceiling. Every other workout previews without ever touching the provider.
    """
    credentials = resolve_credentials()
    if credentials is None:
        return None
    observed, value = IntervalsTransport(credentials).run_threshold_hr()
    if not observed or isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _run_sport_settings() -> dict[str, Any] | None:
    """Read Run settings strictly when a pace delivery may need a confirmed correction."""
    credentials = resolve_credentials()
    if credentials is None:
        raise DeliveryError(
            "Intervals credentials are unavailable; a pace delivery must read its Run "
            "threshold pace before preview"
        )
    return IntervalsTransport(credentials).require_run_sport_settings()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garmin-coach-loop",
        description="Maintain and deliver one current Long Run Hybrid Coach plan",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate-bundle",
        help="check one context/before/after/event bundle without touching the store",
    )
    validate.add_argument("--context", required=True, type=Path)
    validate.add_argument("--before", required=True, type=Path)
    validate.add_argument("--after", required=True, type=Path)
    validate.add_argument("--event", required=True, type=Path)

    initialize = subparsers.add_parser(
        "init-store",
        help="create the state store from an initial PlanState",
    )
    initialize.add_argument("--state-dir", type=Path, default=default_state_dir())
    initialize.add_argument("--plan", required=True, type=Path)
    _add_offline_flag(initialize)

    doctor = subparsers.add_parser(
        "doctor-store",
        help="revalidate the whole commit history and report whether the store opens",
    )
    doctor.add_argument("--state-dir", type=Path, default=default_state_dir())

    clear_attempt = subparsers.add_parser(
        "clear-delivery-attempt",
        help="release a delivery reservation left behind by an interrupted publish",
    )
    clear_attempt.add_argument("--state-dir", type=Path, default=default_state_dir())
    clear_attempt.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="confirm the Intervals calendar has been read back first; the report lists "
        "every operation the reservation still held",
    )
    _add_offline_flag(clear_attempt)

    snapshot = subparsers.add_parser(
        "snapshot-store",
        help="take and verify an atomic backup of the current store",
    )
    snapshot.add_argument("--state-dir", type=Path, default=default_state_dir())
    snapshot.add_argument(
        "--reason", default="manual",
        help="short label recorded in the snapshot directory name",
    )

    restore = subparsers.add_parser(
        "restore-store",
        help="restore a store from a snapshot produced by snapshot-store",
    )
    restore.add_argument(
        "--snapshot", required=True, type=Path,
        help="snapshot directory produced by snapshot-store",
    )
    restore.add_argument("--state-dir", type=Path, default=default_state_dir())
    restore.add_argument(
        "--confirm", action="store_true",
        help="perform the restore; without it the plan is only shown",
    )
    _add_offline_flag(restore)

    status = subparsers.add_parser(
        "status",
        help="report store health, what comes next from today, and the current plan",
    )
    status.add_argument("--state-dir", type=Path, default=default_state_dir())
    status.add_argument(
        "--today", default=None,
        help="ISO date to answer 'next' from; defaults to today in --timezone",
    )
    status.add_argument(
        "--timezone", default=None,
        help="IANA timezone used to resolve 'today' when --today is omitted; defaults "
             f"to the athlete's stored profile, then {DEFAULT_TIMEZONE}",
    )

    history = subparsers.add_parser(
        "history",
        help="list applied decisions, or follow one session across every revision",
    )
    history.add_argument("--state-dir", type=Path, default=default_state_dir())
    history.add_argument("--session", default=None,
                         help="follow one session_id across every revision")

    apply = subparsers.add_parser(
        "apply-decision",
        help="validate one DecisionEvent and persist its result as the current plan",
    )
    apply.add_argument("--state-dir", type=Path, default=default_state_dir())
    apply.add_argument("--context", required=True, type=Path)
    apply.add_argument("--after", required=True, type=Path)
    apply.add_argument("--event", required=True, type=Path)
    _add_offline_flag(apply)

    baseline = subparsers.add_parser(
        "set-baseline",
        help="record a measured athlete baseline as a new current plan version",
    )
    baseline.add_argument("--state-dir", type=Path, default=default_state_dir())
    baseline.add_argument("--context", required=True, type=Path)
    baseline.add_argument("--baseline", required=True, type=Path)
    baseline.add_argument("--event", required=True, type=Path)
    _add_offline_flag(baseline)

    # Where the athlete is and what they read, said once and standing until restated. Not
    # a per-command flag anywhere else: every other command reads this instead of asking
    # again, which is the whole point of storing it.
    profile = subparsers.add_parser(
        "record-profile",
        help="record the athlete's own timezone and the language their plan is written in",
    )
    profile.add_argument("--state-dir", type=Path, default=default_state_dir())
    profile.add_argument(
        "--timezone", default=None,
        help="IANA timezone the athlete lives in, which decides what 'today' means",
    )
    profile.add_argument(
        "--language", default=None, choices=list(LANGUAGES),
        help="the language prescriptions are written in",
    )
    _add_offline_flag(profile)

    # Availability has a command; athlete-reported strength deliberately does not. On this
    # machine the per-set record already arrives through health.db (--health-db), which is
    # measured rather than recalled, and a second local way in would only create a way for
    # the two to disagree. The hosted route exists because a hosted athlete has no
    # health.db at all.
    availability = subparsers.add_parser(
        "record-availability",
        help="record which weekdays the athlete can train, as a standing default or for one week",
    )
    availability.add_argument("--state-dir", type=Path, default=default_state_dir())
    availability.add_argument(
        "--timezone", default=None,
        help="IANA timezone deciding which week is the current one; defaults to the "
             f"athlete's stored profile, then {DEFAULT_TIMEZONE}",
    )
    availability.add_argument(
        "--recurring-available", default=None,
        help="comma-separated mon,tue,... the athlete can normally train",
    )
    availability.add_argument(
        "--recurring-unavailable", default=None,
        help="comma-separated weekdays the athlete normally cannot train",
    )
    availability.add_argument(
        "--week-start", default=None,
        help="ISO date inside one week, to state that week instead of the normal one "
             "(default: the current week, when any --week-* flag is given)",
    )
    availability.add_argument(
        "--week-available", default=None,
        help="comma-separated weekdays gained in that week, on top of the normal week",
    )
    availability.add_argument(
        "--week-unavailable", default=None,
        help="comma-separated weekdays lost in that week, out of the normal week",
    )
    availability.add_argument(
        "--week-only", default=None,
        help="comma-separated weekdays that are the whole of that week, replacing the normal one",
    )
    availability.add_argument(
        "--week-note", default=None,
        help="what that week is beyond which days -- travel, equipment, a week running "
             "late; may stand alone, and expires with the week",
    )
    _add_offline_flag(availability)

    build_context_parser = subparsers.add_parser(
        "build-context",
        help="read latest evidence and build a CoachContext, leaving the store unchanged",
    )
    _add_context_arguments(build_context_parser)

    refresh_context_parser = subparsers.add_parser(
        "refresh-context",
        help="read latest evidence, reconcile the completions it can attach, and rebuild context",
    )
    _add_context_arguments(refresh_context_parser)
    # Only on this one of the two: `build-context` reads and reports, `refresh-context`
    # commits whatever reconciliation found.
    _add_offline_flag(refresh_context_parser)

    prepare_delivery = subparsers.add_parser(
        "prepare-delivery",
        help="bind one publishable session set to exact current sessions",
    )
    prepare_delivery.add_argument("--state-dir", type=Path, default=default_state_dir())
    prepare_delivery.add_argument(
        "--session", required=True, action="append", dest="session_ids",
        help="current PlanState session_id; repeat for the exact publish set",
    )
    prepare_delivery.add_argument("--out", required=True, type=Path)

    approve_delivery = subparsers.add_parser(
        "approve-delivery",
        help="record the athlete's one confirmation for an unchanged delivery preview",
    )
    approve_delivery.add_argument("--proposal", required=True, type=Path)
    approve_delivery.add_argument("--approved-by", required=True)
    approve_delivery.add_argument("--out", required=True, type=Path)

    publish_delivery_parser = subparsers.add_parser(
        "publish-delivery",
        help="deduplicate, write, read back, and record observable delivery state",
    )
    publish_delivery_parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    publish_delivery_parser.add_argument("--proposal", required=True, type=Path)
    publish_delivery_parser.add_argument("--approval", required=True, type=Path)
    publish_delivery_parser.add_argument("--receipt-out", required=True, type=Path)
    _add_offline_flag(publish_delivery_parser)

    prepare_withdrawal = subparsers.add_parser(
        "prepare-withdrawal",
        help="bind the superseded Intervals events a confirmed change left outstanding",
    )
    prepare_withdrawal.add_argument("--state-dir", type=Path, default=default_state_dir())
    prepare_withdrawal.add_argument(
        "--session", action="append", dest="sessions", required=True,
        help="current PlanState session_id holding a superseded event; repeat for a set",
    )
    prepare_withdrawal.add_argument("--out", required=True, type=Path)

    approve_withdrawal = subparsers.add_parser(
        "approve-withdrawal",
        help="record the athlete's one confirmation for an unchanged withdrawal preview",
    )
    approve_withdrawal.add_argument("--proposal", required=True, type=Path)
    approve_withdrawal.add_argument("--approved-by", required=True)
    approve_withdrawal.add_argument("--out", required=True, type=Path)

    withdraw = subparsers.add_parser(
        "withdraw-delivery",
        help="delete the confirmed superseded events and record that they are gone",
    )
    withdraw.add_argument("--state-dir", type=Path, default=default_state_dir())
    withdraw.add_argument("--proposal", required=True, type=Path)
    withdraw.add_argument("--approval", required=True, type=Path)
    withdraw.add_argument("--receipt-out", required=True, type=Path)
    withdraw.add_argument(
        "--today",
        help="athlete-local ISO date deciding what counts as past; defaults to the "
        "same day `status` answers from",
    )
    withdraw.add_argument(
        "--timezone", default=None,
        help="IANA timezone used to resolve 'today' when --today is omitted; defaults "
             f"to the athlete's stored profile, then {DEFAULT_TIMEZONE}",
    )
    _add_offline_flag(withdraw)

    adopt = subparsers.add_parser(
        "adopt-owner-store",
        help="give an existing local store to the gateway owner who already signed in",
    )
    adopt.add_argument(
        "--athlete-id", required=True,
        help=f"the {PROVIDER} athlete id whose OAuth sign-in already created the owner",
    )
    adopt.add_argument(
        "--from", required=True, type=Path, dest="source",
        help="the existing state directory to adopt; it is never modified",
    )
    adopt.add_argument(
        "--state-root", type=Path, default=None,
        help=f"gateway state root; defaults to {STATE_ROOT_ENV_VAR}",
    )
    adopt.add_argument(
        "--mode", default="link", choices=list(ADOPTION_MODES),
        help="link: both paths keep one plan; copy: a second plan that diverges from now on",
    )
    adopt.add_argument(
        "--confirm", action="store_true",
        help="perform the adoption; without it the exact source and destination are only shown",
    )

    delete_owner = subparsers.add_parser(
        "delete-owner",
        help="operator deletion: permanently remove one owner's identity rows and state",
    )
    delete_owner.add_argument(
        "--identity-db", required=True, type=Path,
        help="the gateway's identity registry (see serve-gateway's identity.db)",
    )
    delete_owner.add_argument(
        "--state-root", required=True, type=Path,
        help="gateway state root holding the owner's state directory",
    )
    delete_owner.add_argument(
        "--owner-id", required=True,
        help="the product-owned owner id (a UUID) to delete -- never a provider athlete id",
    )
    delete_owner.add_argument(
        "--confirm", action="store_true",
        help="perform the deletion; without it nothing is removed and only a preview is shown",
    )

    # -- hosted-first: the migration, and reading the canonical plan from here ---------

    export_store = subparsers.add_parser(
        "export-store",
        help="write one local store out as a portable bundle, changing nothing",
    )
    export_store.add_argument("--state-dir", type=Path, default=default_state_dir())
    export_store.add_argument(
        "--out", required=True, type=Path,
        help="where to write the bundle; it holds the athlete's whole plan history, so "
             "it must be outside this repository and is worth deleting afterwards",
    )

    import_store = subparsers.add_parser(
        "import-store",
        help="open an exported bundle at an owner store that holds nothing",
    )
    import_store.add_argument("--bundle", required=True, type=Path)
    import_store.add_argument(
        "--state-dir", type=Path, default=None,
        help="the destination store directory; or name the athlete with --athlete-id",
    )
    import_store.add_argument(
        "--athlete-id", default=None,
        help=f"resolve the destination from the {PROVIDER} athlete who already signed in",
    )
    import_store.add_argument(
        "--state-root", type=Path, default=None,
        help=f"gateway state root; defaults to {STATE_ROOT_ENV_VAR}",
    )
    import_store.add_argument(
        "--confirm", action="store_true",
        help="perform the import; without it the exact destination and plan are only shown",
    )
    _add_offline_flag(import_store)

    archive = subparsers.add_parser(
        "archive-store",
        help="move one store aside, whole and openable, so another can be imported",
    )
    archive.add_argument(
        "--state-dir", type=Path, default=None,
        help="the store to archive; or name the athlete with --athlete-id",
    )
    archive.add_argument(
        "--athlete-id", default=None,
        help=f"resolve the store from the {PROVIDER} athlete who already signed in",
    )
    archive.add_argument(
        "--state-root", type=Path, default=None,
        help=f"gateway state root; defaults to {STATE_ROOT_ENV_VAR}",
    )
    archive.add_argument(
        "--reason", default="superseded",
        help="short label recorded in the archive directory name",
    )
    archive.add_argument(
        "--confirm", action="store_true",
        help="perform the move; without it the exact source and destination are only shown",
    )

    seal = subparsers.add_parser(
        "seal-local-store",
        help="record that this local store's plan now lives on the hosted coach, and stop writing here",
    )
    seal.add_argument("--state-dir", type=Path, default=default_state_dir())
    seal.add_argument(
        "--hosted-entry", default=None,
        help=f"the hosted coach this store was handed to; defaults to {GATEWAY_URL_ENV_VAR}",
    )
    seal.add_argument(
        "--release", action="store_true",
        help="undo the seal, knowing that writing here again forks the athlete's plan in two",
    )
    seal.add_argument(
        "--confirm", action="store_true",
        help="perform it; without it only what would change is shown",
    )

    hosted_session = subparsers.add_parser(
        "hosted-session",
        help=(
            "refresh provider evidence and reconcile the hosted plan -- this can commit "
            "a new PlanState version; see hosted-status for a read-only check"
        ),
    )
    hosted_session.add_argument(
        "--gateway", default=None,
        help=f"the hosted coach's URL; defaults to {GATEWAY_URL_ENV_VAR}",
    )
    hosted_session.add_argument(
        "--full", action="store_true",
        help="print the whole reply, including the CoachContext, rather than a summary",
    )
    hosted_session.epilog = (
        "This calls startCoachSession, which is refresh-context under another name: it "
        "fetches provider evidence and applies deterministic reconciliation, which can "
        "commit a new PlanState version. The report always states whether it did, under "
        "`reconciliation_statement`, on every output path -- summary and --full alike. "
        "For a status check that is guaranteed not to write, use hosted-status instead. "
        "Authorizes through the browser once per run and keeps the token in memory only; "
        f"set {HOSTED_TOKEN_ENV_VAR} to reuse one this gateway already issued. Nothing "
        "here writes a credential to disk."
    )

    hosted_status = subparsers.add_parser(
        "hosted-status",
        help=(
            "read the hosted coach's current plan id/version/summary -- read-only: "
            "no provider call, no reconciliation, no store write"
        ),
    )
    hosted_status.add_argument(
        "--gateway", default=None,
        help=f"the hosted coach's URL; defaults to {GATEWAY_URL_ENV_VAR}",
    )
    hosted_status.epilog = (
        "Calls getCoachState: genuinely read-only, proven by a test that hashes the "
        "owner's store directory before and after the call. It cannot tell "
        "you whether Intervals holds anything new -- that needs hosted-session, which can "
        "write. Authorizes through the browser once per run and keeps the token in "
        f"memory only; set {HOSTED_TOKEN_ENV_VAR} to reuse one this gateway already "
        "issued. Nothing here writes a credential to disk."
    )

    revoke = subparsers.add_parser(
        "revoke-connections",
        help="sign every client out of one owner's account, leaving their plan untouched",
    )
    revoke.add_argument(
        "--identity-db", required=True, type=Path,
        help="the gateway's identity registry (see serve-gateway's identity.db)",
    )
    revoke.add_argument(
        "--owner-id", default=None,
        help="the product-owned owner id (a UUID); or name the athlete with --athlete-id",
    )
    revoke.add_argument(
        "--athlete-id", default=None,
        help=f"the {PROVIDER} athlete whose connections are being revoked",
    )
    revoke.add_argument(
        "--confirm", action="store_true",
        help="perform the revocation; without it only what would be removed is shown",
    )

    usage = subparsers.add_parser(
        "usage-report",
        help="how many accounts exist, how many are active, and how often each one calls",
    )
    usage.add_argument(
        "--identity-db", required=True, type=Path,
        help="the gateway's identity registry (see serve-gateway's identity.db)",
    )
    usage.add_argument(
        "--since", default=None,
        help=(
            "count activity on or after this UTC date (YYYY-MM-DD); the registered total "
            "always covers every account, so a window narrows who was active, not who exists"
        ),
    )

    serve = subparsers.add_parser(
        "serve-gateway",
        help="serve the agent-neutral coach gateway for OAuth-connected athletes",
    )
    serve.add_argument(
        "--host", default=None,
        help=(
            "bind address; unset falls back to GARMIN_COACH_LOOP_GATEWAY_HOST, then "
            f"{DEFAULT_HOST!r}. TLS belongs to the platform or tunnel in front, never "
            "this process."
        ),
    )
    serve.add_argument(
        "--port", type=int, default=None,
        help=(
            "unset falls back to GARMIN_COACH_LOOP_GATEWAY_PORT, then "
            f"{DEFAULT_PORT}"
        ),
    )
    return parser


def _owner_state_dir(args: argparse.Namespace) -> Path:
    """The store an operator command acts on: named directly, or resolved from an athlete.

    Resolving never creates an owner, for `adopt-owner-store`'s reason: an athlete id typed
    at a terminal is not an authorization, and an owner minted from one would name a
    directory no token can ever reach.
    """
    if (args.state_dir is None) == (args.athlete_id is None):
        raise ValueError("name exactly one of --state-dir or --athlete-id")
    if args.state_dir is not None:
        return args.state_dir
    configured_root = args.state_root or os.environ.get(STATE_ROOT_ENV_VAR)
    if not configured_root:
        raise ValueError(
            f"no gateway state root; pass --state-root or set {STATE_ROOT_ENV_VAR}"
        )
    state_root = resolve_state_root(configured_root)
    owner_id = owner_for_provider_athlete(
        identity_db_path(state_root), PROVIDER, args.athlete_id
    )
    if owner_id is None:
        raise ValueError(
            f"no owner has connected as {PROVIDER} athlete {args.athlete_id}; "
            "complete the OAuth sign-in once first"
        )
    return resolve_state_dir(owner_id, state_root=state_root)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in LOCAL_STORE_WRITERS:
            # Before anything is read, resolved or fetched: on a machine whose plan lives
            # on the hosted coach, a local write is the divergence this product exists to
            # not have, and the athlete has to say --offline to mean a different store.
            require_local_store_write(
                args.command, offline=getattr(args, "offline", False)
            )
        if args.command == "validate-bundle":
            report = validate_bundle(
                _read_object(args.context),
                _read_object(args.before),
                _read_object(args.after),
                _read_object(args.event),
            )
        elif args.command == "init-store":
            report = init_store(args.state_dir, _read_object(args.plan))
        elif args.command == "doctor-store":
            report = doctor_store(args.state_dir)
        elif args.command == "clear-delivery-attempt":
            # Deliberately manual and confirmed: the reservation exists because Intervals
            # may hold an event this store never recorded, and only a human who has looked
            # at the calendar can say the divergence is resolved (AGENTS.md 8). It is also
            # the only way past a reservation file this code cannot parse, so it reports
            # exactly which operations the athlete has just taken responsibility for.
            report = close_delivery_attempt(args.state_dir)
        elif args.command == "snapshot-store":
            report = snapshot_store(args.state_dir, reason=args.reason)
        elif args.command == "restore-store":
            report = restore_snapshot(args.snapshot, args.state_dir, confirm=args.confirm)
        elif args.command == "status":
            # Resolved only when it is going to be used. An explicit --today is already
            # the answer, and `status_store` never consults a timezone beside one -- so
            # reading the athlete's profile, or refusing their typo, would both be work
            # done about a question nobody asked.
            timezone_name = (
                None
                if args.today is not None
                else resolve_settings(args.state_dir, timezone_override=args.timezone)[0]
            )
            report = status_store(args.state_dir, today=args.today, timezone=timezone_name)
        elif args.command == "history":
            report = history_store(args.state_dir, session_id=args.session)
        elif args.command == "apply-decision":
            report = apply_decision(
                args.state_dir,
                context=_read_object(args.context),
                after=_read_object(args.after),
                event=_read_object(args.event),
            )
        elif args.command == "set-baseline":
            report = set_baseline(
                args.state_dir,
                context=_read_object(args.context),
                baseline=_read_object(args.baseline),
                event=_read_object(args.event),
            )
        elif args.command == "record-profile":
            report = {
                "status": "passed",
                **record_profile(
                    args.state_dir, timezone=args.timezone, language=args.language
                ),
            }
        elif args.command == "record-availability":
            timezone_name, _ = resolve_settings(
                args.state_dir, timezone_override=args.timezone
            )
            report = {
                "status": "passed",
                **record_availability(
                    args.state_dir,
                    recurring=_availability_days(
                        args.recurring_available, args.recurring_unavailable
                    ),
                    week=_week_statement(args),
                    timezone_name=timezone_name,
                ),
            }
        elif args.command in {"build-context", "refresh-context"}:
            timezone_name, _ = resolve_settings(
                args.state_dir, timezone_override=args.timezone
            )
            request = _context_request(args, timezone_name)
            # One instant for both builds below. The rebuild reports the window it read
            # over, and letting it resolve its own clock would name a window the rows it
            # is describing were never selected against.
            now = dt.datetime.now(dt.timezone.utc)
            report, domain = build_context_with_domain(
                request,
                state_dir=args.state_dir,
                source=args.source,
                db_path=args.db,
                health_db=args.health_db,
                now=now,
            )
            if args.command == "refresh-context" and report["status"] == "passed":
                reconciliation = apply_reconciliation(args.state_dir, report["context"])
                if reconciliation["status"] != "passed":
                    report = {
                        "status": "blocked",
                        "error": "planned-to-actual reconciliation failed",
                        "reconciliation": reconciliation,
                    }
                else:
                    if reconciliation["applied"]:
                        # Rebuilt against the moved plan from the snapshot already read.
                        # Reconciliation marks matched sessions completed and bumps the
                        # version; neither reaches anything the provider read depends on,
                        # so the second read would only re-ask the same questions.
                        report = build_context(
                            request,
                            state_dir=args.state_dir,
                            source=args.source,
                            db_path=args.db,
                            health_db=args.health_db,
                            now=now,
                            domain=domain,
                        )
                    report["reconciliation"] = reconciliation
            _write_context_output(args.out, report)
        elif args.command == "prepare-delivery":
            proposal = prepare_delivery_set(
                status_store(args.state_dir)["current_plan"],
                args.session_ids,
                read_run_threshold_hr=_run_threshold_hr,
                read_run_sport_settings=_run_sport_settings,
            )
            _write_object(args.out, proposal)
            report = {"status": "passed", "proposal": proposal, "out": str(args.out)}
        elif args.command == "approve-delivery":
            approval = approve_delivery_set(
                _read_object(args.proposal),
                approved_by=args.approved_by,
            )
            _write_object(args.out, approval)
            report = {"status": "passed", "approval": approval, "out": str(args.out)}
        elif args.command == "publish-delivery":
            credentials = resolve_credentials()
            if credentials is None:
                raise DeliveryError(
                    "Intervals credentials are unavailable; set INTERVALS_ICU_API_KEY "
                    "and INTERVALS_ICU_ATHLETE_ID"
                )
            report = deliver_approved_set(
                args.state_dir,
                _read_object(args.proposal),
                _read_object(args.approval),
                transport=IntervalsTransport(credentials),
            )
            _write_object(args.receipt_out, report)
        elif args.command == "prepare-withdrawal":
            # The same authenticated read the hosted entry makes: the athlete confirming
            # a deletion here is shown the same calendar entry, from the same lookup.
            credentials = resolve_credentials()
            if credentials is None:
                raise DeliveryError(
                    "Intervals credentials are unavailable; set INTERVALS_ICU_API_KEY "
                    "and INTERVALS_ICU_ATHLETE_ID"
                )
            proposal = prepare_withdrawal_set(
                status_store(args.state_dir)["current_plan"],
                args.sessions,
                read_event=IntervalsTransport(credentials).find_event,
            )
            _write_object(args.out, proposal)
            report = {"status": "passed", "withdrawal_set": proposal, "out": str(args.out)}
        elif args.command == "approve-withdrawal":
            approval = approve_withdrawal_set(
                _read_object(args.proposal), approved_by=args.approved_by
            )
            _write_object(args.out, approval)
            report = {"status": "passed", "approval": approval, "out": str(args.out)}
        elif args.command == "withdraw-delivery":
            credentials = resolve_credentials()
            if credentials is None:
                raise DeliveryError(
                    "Intervals credentials are unavailable; set INTERVALS_ICU_API_KEY "
                    "and INTERVALS_ICU_ATHLETE_ID"
                )
            report = withdraw_approved_set(
                args.state_dir,
                _read_object(args.proposal),
                _read_object(args.approval),
                transport=IntervalsTransport(credentials),
                # The athlete's own day decides what counts as past, so it comes from the
                # same place `status` answers "today" from -- including the timezone,
                # which is resolved through the same precedence: this request's
                # --timezone, then the athlete's stored profile, then the default.
                today=args.today
                or status_store(
                    args.state_dir,
                    timezone=resolve_settings(
                        args.state_dir, timezone_override=args.timezone
                    )[0],
                )["as_of_date"],
            )
            _write_object(args.receipt_out, report)
        elif args.command == "adopt-owner-store":
            # The owner has to already exist: an athlete id typed at a terminal is not an
            # authorization, so this resolves an owner and never creates one.
            configured_root = args.state_root or os.environ.get(STATE_ROOT_ENV_VAR)
            if not configured_root:
                raise ValueError(
                    f"no gateway state root; pass --state-root or set {STATE_ROOT_ENV_VAR}"
                )
            state_root = resolve_state_root(configured_root)
            owner_id = owner_for_provider_athlete(
                identity_db_path(state_root), PROVIDER, args.athlete_id
            )
            if owner_id is None:
                raise ValueError(
                    f"no owner has connected as {PROVIDER} athlete {args.athlete_id}; "
                    "complete the OAuth sign-in once first"
                )
            report = {
                "owner_id": owner_id,
                "athlete_id": args.athlete_id,
                **adopt_store(
                    args.source,
                    resolve_state_dir(owner_id, state_root=state_root),
                    mode=args.mode,
                    confirm=args.confirm,
                ),
            }
        elif args.command == "delete-owner":
            # owner_id never becomes a path except through resolve_state_dir: its
            # canonical-UUID check is what stands between a typo or an injected value and
            # another owner's directory. Everything below reads that resolved Path;
            # nothing re-derives one from args.owner_id again.
            state_root = resolve_state_root(args.state_root)
            state_dir = resolve_state_dir(args.owner_id, state_root=state_root)
            identity_rows = owner_identity_row_counts(args.identity_db, args.owner_id)
            # Store first, identity second: if this is interrupted between the two, the
            # bulk of what a deletion request is actually about -- plans, decisions,
            # reported evidence -- is already gone, and only a small identity mapping can
            # be left to a retry. The reverse order risks the opposite: a live identity
            # link with no store behind it, re-attachable to real training history.
            store_report = delete_owner_store(state_dir, confirm=args.confirm)
            owner_known = (
                any(identity_rows.values())
                or store_report["state_dir_existed"]
                or store_report["snapshots_dir_existed"]
            )
            if not owner_known:
                report = {
                    "status": "absent",
                    "owner_id": args.owner_id,
                    "message": "no such owner; nothing to delete",
                }
            elif not args.confirm:
                report = {
                    "status": "preview",
                    "owner_id": args.owner_id,
                    "identity_rows": identity_rows,
                    "state_dir": store_report["state_dir"],
                    "state_dir_exists": store_report["state_dir_existed"],
                    "state_dir_is_link": store_report["state_dir_is_link"],
                    "snapshots_dir": store_report["snapshots_dir"],
                    "snapshots_dir_exists": store_report["snapshots_dir_existed"],
                }
            else:
                identity_deleted = delete_owner_identity(args.identity_db, args.owner_id)
                report = {
                    "status": "deleted",
                    "owner_id": args.owner_id,
                    "identity_rows_deleted": identity_deleted,
                    "state_dir": store_report["state_dir"],
                    "state_dir_removed": store_report["state_dir_removed"],
                    "state_dir_was_link": store_report["state_dir_is_link"],
                    "snapshots_dir": store_report["snapshots_dir"],
                    "snapshots_dir_removed": store_report["snapshots_dir_removed"],
                    "note": (
                        "this does not remove any workout already written to the "
                        "athlete's Intervals.icu calendar; see docs/account-lifecycle.md"
                    ),
                }
        elif args.command == "export-store":
            # Checked here, before `assert_outside_repository` resolves the path: that
            # resolution follows symlinks, so it is the only point where "the destination
            # itself is a symlink" is still a fact about the path rather than about
            # whatever the symlink happened to point to (or fail to).
            destination = args.out.expanduser()
            _refuse_existing_export_destination(destination)
            out = assert_outside_repository(destination, what="an exported store bundle")
            bundle = export_bundle(args.state_dir)
            _write_private_bundle(out, bundle)
            report = {
                "status": "passed",
                "state_dir": str(args.state_dir),
                "out": str(out),
                **{
                    key: bundle[key]
                    for key in (
                        "plan_id",
                        "current_version",
                        "event_count",
                        "writer_contract_version",
                        "bundle_digest",
                        "exported_at",
                    )
                },
                "file_count": len(bundle["files"]),
                "note": (
                    "this file holds the athlete's whole plan history; keep it out of "
                    "any repository and delete it once the import is verified"
                ),
            }
        elif args.command == "import-store":
            report = import_bundle(
                _owner_state_dir(args), _read_object(args.bundle), confirm=args.confirm
            )
        elif args.command == "archive-store":
            report = archive_store(
                _owner_state_dir(args), reason=args.reason, confirm=args.confirm
            )
        elif args.command == "seal-local-store":
            hosted_entry = args.hosted_entry or configured_gateway() or ""
            report = seal_store(
                args.state_dir,
                hosted_entry=hosted_entry,
                release=args.release,
                confirm=args.confirm,
            )
        elif args.command == "hosted-session":
            gateway = args.gateway or configured_gateway()
            if gateway is None:
                raise ValueError(
                    f"no hosted coach; pass --gateway or set {GATEWAY_URL_ENV_VAR}"
                )
            # The token exists for this process. It is not written down, and the report
            # below never carries it -- see `hosted.hosted_connection`.
            live = hosted_connection(gateway, announce=_announce_authorization)
            refused, payload = live.call_tool("startCoachSession", {})
            if refused or args.full:
                # Raw either way, but never silently: whether this call reconciled is
                # stated on this path too, not only in the summary.
                report = {
                    **payload,
                    "reconciliation_statement": _reconciliation_statement(payload),
                }
            else:
                report = _hosted_session_summary(gateway, payload)
        elif args.command == "hosted-status":
            gateway = args.gateway or configured_gateway()
            if gateway is None:
                raise ValueError(
                    f"no hosted coach; pass --gateway or set {GATEWAY_URL_ENV_VAR}"
                )
            live = hosted_connection(gateway, announce=_announce_authorization)
            refused, payload = live.call_tool("getCoachState", {})
            report = payload if refused else _hosted_state_summary(gateway, payload)
        elif args.command == "revoke-connections":
            if (args.owner_id is None) == (args.athlete_id is None):
                raise ValueError("name exactly one of --owner-id or --athlete-id")
            owner_id = args.owner_id or owner_for_provider_athlete(
                args.identity_db, PROVIDER, args.athlete_id
            )
            if owner_id is None:
                report = {
                    "status": "absent",
                    "message": f"no owner has connected as {PROVIDER} athlete {args.athlete_id}",
                }
            else:
                rows = owner_identity_row_counts(args.identity_db, owner_id)
                report = {
                    "status": "revoked" if args.confirm else "preview",
                    "owner_id": owner_id,
                    "connections": rows["token_fingerprints"],
                    "note": (
                        "this ends every client's access through this gateway, including "
                        "tokens it already issued, and leaves PlanState untouched; the "
                        "athlete signs in again from any client. It does not revoke the "
                        "Intervals tokens themselves -- that is done at intervals.icu"
                    ),
                }
                if args.confirm:
                    report["revoked"] = revoke_owner_connections(args.identity_db, owner_id)
        elif args.command == "usage-report":
            report = {
                "status": "passed",
                **activity_report(args.identity_db, since=args.since),
            }
        elif args.command == "serve-gateway":
            # Configuration is read from the environment only, and a missing variable is
            # named -- never printed with its value.
            config = load_config(host=args.host, port=args.port)
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s %(message)s",
                stream=sys.stderr,
            )
            run_gateway(config)
            report = {"status": "passed", "command": "serve-gateway"}
        else:  # pragma: no cover - argparse enforces known commands.
            raise ValueError(f"unsupported command: {args.command}")
    except (StateStoreError, ContextBuildError) as exc:
        payload: dict[str, Any] = {"status": "blocked", "error": str(exc)}
        if exc.details is not None:
            payload["details"] = exc.details
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (
        AthleteEvidenceError,
        DeliveryError,
        GatewayConfigError,
        HostedEntryError,
        IdentityError,
    ) as exc:
        print(
            json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {
        "passed", "initialized", "preview", "adopted", "restored", "deleted", "absent",
        "imported", "archived", "sealed", "already_sealed", "released", "no_plan_state",
        "revoked",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
