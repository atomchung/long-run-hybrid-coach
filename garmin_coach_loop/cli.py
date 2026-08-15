"""CLI for validating and continuously using the current Coach Loop plan."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .athlete_evidence import AthleteEvidenceError, record_availability
from .context_builder import (
    DEFAULT_SESSION_MINUTES,
    DEFAULT_SOURCE,
    DEFAULT_TIMEZONE,
    VALID_SOURCES,
    ContextBuildError,
    ContextRequest,
    build_context,
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
from .identity import IdentityError, owner_for_provider_athlete
from .reconcile import apply_reconciliation
from .source_intervals import resolve_credentials
from .store import (
    ADOPTION_MODES,
    StateStoreError,
    adopt_store,
    apply_decision,
    close_delivery_attempt,
    default_state_dir,
    doctor_store,
    history_store,
    init_store,
    resolve_state_dir,
    resolve_state_root,
    restore_snapshot,
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
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
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


def _context_request(args: argparse.Namespace) -> ContextRequest:
    return ContextRequest(
        as_of_raw=args.as_of,
        timezone_name=args.timezone,
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
            return None
        statement = days
    if args.week_start is not None:
        statement["week_start"] = args.week_start
    return statement


def _write_object(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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
        "--timezone", default=DEFAULT_TIMEZONE,
        help="IANA timezone used to resolve 'today' when --today is omitted",
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

    baseline = subparsers.add_parser(
        "set-baseline",
        help="record a measured athlete baseline as a new current plan version",
    )
    baseline.add_argument("--state-dir", type=Path, default=default_state_dir())
    baseline.add_argument("--context", required=True, type=Path)
    baseline.add_argument("--baseline", required=True, type=Path)
    baseline.add_argument("--event", required=True, type=Path)

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
        "--timezone", default=DEFAULT_TIMEZONE,
        help="IANA timezone deciding which week is the current one",
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

    serve = subparsers.add_parser(
        "serve-gateway",
        help="serve the agent-neutral coach gateway for OAuth-connected athletes",
    )
    serve.add_argument("--host", default=DEFAULT_HOST,
                       help="bind address; loopback by default, TLS belongs to the tunnel")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
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
            report = status_store(args.state_dir, today=args.today, timezone=args.timezone)
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
        elif args.command == "record-availability":
            report = {
                "status": "passed",
                **record_availability(
                    args.state_dir,
                    recurring=_availability_days(
                        args.recurring_available, args.recurring_unavailable
                    ),
                    week=_week_statement(args),
                    timezone_name=args.timezone,
                ),
            }
        elif args.command in {"build-context", "refresh-context"}:
            request = _context_request(args)
            report = build_context(
                request,
                state_dir=args.state_dir,
                source=args.source,
                db_path=args.db,
                health_db=args.health_db,
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
                        report = build_context(
                            request,
                            state_dir=args.state_dir,
                            source=args.source,
                            db_path=args.db,
                            health_db=args.health_db,
                        )
                    report["reconciliation"] = reconciliation
            _write_context_output(args.out, report)
        elif args.command == "prepare-delivery":
            proposal = prepare_delivery_set(
                status_store(args.state_dir)["current_plan"],
                args.session_ids,
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
            proposal = prepare_withdrawal_set(
                status_store(args.state_dir)["current_plan"], args.sessions
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
                # same place `status` answers "today" from.
                today=args.today or status_store(args.state_dir)["as_of_date"],
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
    except (AthleteEvidenceError, DeliveryError, GatewayConfigError, IdentityError) as exc:
        print(
            json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"passed", "initialized", "preview", "adopted", "restored"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
