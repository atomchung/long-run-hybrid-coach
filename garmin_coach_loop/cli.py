"""CLI for validating and continuously using the current Coach Loop plan."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

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
    prepare_delivery_set,
    publish_delivery_set,
)
from .gateway import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    GatewayConfigError,
    load_config,
    run_gateway,
)
from .reconcile import apply_reconciliation
from .source_intervals import resolve_credentials
from .store import (
    StateStoreError,
    apply_decision,
    apply_delivery_observations,
    default_state_dir,
    doctor_store,
    history_store,
    init_store,
    set_baseline,
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


def _write_object(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garmin-coach-loop",
        description="Maintain and deliver one current Garmin Coach Loop plan",
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

    status = subparsers.add_parser(
        "status",
        help="report store health, what comes next from today, and the current plan",
    )
    status.add_argument("--state-dir", type=Path, default=default_state_dir())
    status.add_argument(
        "--today", default=None,
        help="ISO date to answer 'next' from; defaults to the athlete's own date",
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
        help="bind one structured running-workout set to exact current sessions",
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
        elif args.command == "status":
            report = status_store(args.state_dir, today=args.today)
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
            proposal = _read_object(args.proposal)
            receipt = publish_delivery_set(
                proposal,
                _read_object(args.approval),
                load_current_plan=lambda: status_store(args.state_dir)["current_plan"],
                transport=IntervalsTransport(credentials),
            )
            state_update = apply_delivery_observations(
                args.state_dir,
                observations=receipt["observations"],
            )
            report = {**receipt, "state_update": state_update}
            _write_object(args.receipt_out, report)
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
    except (DeliveryError, GatewayConfigError) as exc:
        print(
            json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"passed", "initialized"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
