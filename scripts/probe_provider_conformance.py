#!/usr/bin/env python3
"""Check, against a live Intervals account, that the provider holds what we sent.

Development/operator tool, never imported by runtime code and never run in CI: it writes
to a real calendar, so it is manual and opt-in by construction.

Why it exists
-------------
Unit tests prove the product builds the payload it means to build. They cannot prove the
provider stores what that payload meant -- and twice it did not. A step named `門檻 1000m`
was parsed as 1000 minutes (2026-08-13), and a `77-83% HR` target was resolved against max
heart rate instead of threshold heart rate (2026-08-12). Both writes succeeded. Both were
caught by read-back, after the fact, on the athlete's own calendar.

This runs the same check on purpose, on a date nobody trains on, for every shape the
product can emit -- using the product's own payload builder and the product's own
`verify_readback`, not a hand-written copy of either. A probe that restated them would go
stale exactly when it mattered.

What it cannot check
--------------------
The hop after Intervals. Intervals accepting and storing a target is not Garmin exporting
it, and not a watch enforcing it: a pace range correctly stored has been reported arriving
on the watch as `No Target`, and a supplied `workout_doc` is stored verbatim without
Intervals processing it at all. Nothing in the API reports either outcome. So the last
thing this prints is a checklist for a human holding the watch, rather than a claim.

Usage
-----
    python3 scripts/probe_provider_conformance.py                 # show the probes only
    python3 scripts/probe_provider_conformance.py --write         # write, verify, report
    python3 scripts/probe_provider_conformance.py --write --keep  # leave them for a watch
    python3 scripts/probe_provider_conformance.py --clean         # remove them

Credentials come from the same place every other command reads them. The probe date
defaults to a far-future day and must be empty; probes carry a `gcl-probe:` marker, which
no delivery path can ever match, so this cannot touch a delivered workout.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from garmin_coach_loop.delivery import (  # noqa: E402
    DeliveryError,
    IntervalsTransport,
    _provider_payload,
    approve_delivery_set,
    prepare_delivery_set,
    verify_readback,
)
from garmin_coach_loop.prescription import render_prescription  # noqa: E402
from garmin_coach_loop.source_intervals import resolve_credentials  # noqa: E402

EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day" / "plan-state-v1.json"

# Never the product's own `gcl:` marker. A probe must be unable to collide with, update or
# be mistaken for a delivered workout, in either direction.
PROBE_PREFIX = "gcl-probe:"
DEFAULT_DATE = "2026-12-01"


def _steps(*steps: dict[str, Any]) -> list[dict[str, Any]]:
    return list(steps)


def _work(name: str, duration: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "work", "name": name, "duration": duration, "target": target}


TIME = lambda seconds: {"kind": "time", "seconds": seconds}  # noqa: E731
DISTANCE = lambda meters: {"kind": "distance", "meters": meters}  # noqa: E731
OPEN = {"kind": "open"}
PACE = {
    "kind": "pace",
    "unit": "sec_per_km",
    "low_seconds_per_km": 365,
    "high_seconds_per_km": 380,
}
CEILING = {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 140}


# One probe per shape the delivery boundary can emit. Adding an execution model to the
# product means adding it here, or this stops describing what is actually sent.
PROBES: list[tuple[str, str, dict[str, Any]]] = [
    (
        "open-time",
        "開放目標 時間軸",
        {"kind": "time_axis", "name": "輕鬆跑", "steps": _steps(
            _work("熱身", TIME(600), OPEN), _work("主段", TIME(1200), OPEN))},
    ),
    (
        "open-distance",
        "開放目標 距離步驟",
        {"kind": "time_axis", "name": "距離跑", "steps": _steps(
            _work("熱身", TIME(600), OPEN), _work("主段", DISTANCE(3000), OPEN))},
    ),
    (
        "pace-distance",
        "絕對配速 距離步驟",
        {"kind": "time_axis", "name": "節奏跑", "steps": _steps(
            _work("熱身", TIME(600), OPEN), _work("節奏段", DISTANCE(2000), PACE))},
    ),
    (
        "pace-in-repeat",
        "絕對配速 重複組",
        {"kind": "time_axis", "name": "間歇", "steps": _steps(
            _work("熱身", TIME(600), OPEN),
            {"kind": "repeat", "repetitions": 3, "steps": _steps(
                _work("快段", DISTANCE(800), PACE), _work("趟間", TIME(120), OPEN))},
            _work("收操", TIME(600), OPEN))},
    ),
    (
        "hr-ceiling",
        "絕對心率上限 文件路徑",
        {"kind": "time_axis", "name": "恢復跑", "steps": _steps(
            _work("輕鬆跑", TIME(1200), CEILING))},
    ),
]


def _plan_for(probe_kind: str, plan_body: dict[str, Any], day: str) -> dict[str, Any]:
    plan = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    # A plan only validates when its session falls inside its own week and cycle, so the
    # fixture's calendar moves to the probe date rather than the probe moving to 2026-08.
    probe_day = dt.date.fromisoformat(day)
    week_start = probe_day - dt.timedelta(days=probe_day.weekday())
    plan["week"]["start"] = week_start.isoformat()
    plan["cycle"]["start"] = week_start.isoformat()
    plan["cycle"]["end"] = (week_start + dt.timedelta(days=27)).isoformat()
    session = next(
        item for item in plan["week"]["sessions"] if item["session_id"] == "run-quality-01"
    )
    session["scheduled_date"] = day
    session["match_status"] = "planned"
    session["plan"] = copy.deepcopy(plan_body)
    session["prescription"] = render_prescription(session["plan"])
    session["execution"] = {
        "publish_supported": True,
        "external_id": None,
        "delivery_state": "not_published",
    }
    plan["week"]["sessions"] = [session]
    return plan


def _payloads(day: str) -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    """Every probe, as the exact payload the product would send for it."""
    built = []
    for probe_kind, label, plan_body in PROBES:
        plan = _plan_for(probe_kind, plan_body, day)
        proposal_set = prepare_delivery_set(plan, ["run-quality-01"])
        approve_delivery_set(proposal_set, approved_by="conformance-probe")
        proposal = proposal_set["items"][0]
        payload = _provider_payload(proposal)
        payload["external_id"] = f"{PROBE_PREFIX}{probe_kind}"
        payload["name"] = f"探測 {label}"
        built.append((probe_kind, label, proposal, payload))
    return built


def _report(rows: list[tuple[str, str, str]]) -> None:
    width = max(len(row[0]) for row in rows)
    for name, verdict, detail in rows:
        print(f"  {name:<{width}}  {verdict:<8} {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", default=DEFAULT_DATE, help="probe date; must be empty")
    parser.add_argument("--write", action="store_true", help="write the probes and verify")
    parser.add_argument("--keep", action="store_true", help="leave them for a watch check")
    parser.add_argument("--clean", action="store_true", help="remove probes from the date")
    args = parser.parse_args(argv)

    try:
        dt.date.fromisoformat(args.date)
    except ValueError:
        print(f"--date must be an ISO date, got {args.date!r}", file=sys.stderr)
        return 2

    built = _payloads(args.date)
    if not (args.write or args.clean):
        print(f"{len(built)} probes would be written to {args.date}:\n")
        _report([(kind, "", payload["description"].replace("\n", " | ")[:80])
                 for kind, _, _, payload in built])
        print("\nNothing was written. Pass --write to send them.")
        return 0

    credentials = resolve_credentials()
    if credentials is None:
        print("Intervals credentials are unavailable", file=sys.stderr)
        return 2
    transport = IntervalsTransport(credentials)

    if args.clean:
        removed = [
            event for event in transport.list_events(args.date)
            if str(event.get("external_id", "")).startswith(PROBE_PREFIX)
        ]
        for event in removed:
            transport.delete_event(str(event["id"]))
        print(f"removed {len(removed)} probe(s) from {args.date}")
        return 0

    existing = transport.list_events(args.date)
    foreign = [
        event for event in existing
        if not str(event.get("external_id", "")).startswith(PROBE_PREFIX)
    ]
    if foreign:
        print(
            f"{args.date} already holds {len(foreign)} event(s) this probe did not write; "
            "choose an empty date rather than writing beside real training.",
            file=sys.stderr,
        )
        return 2

    rows: list[tuple[str, str, str]] = []
    written: list[str] = []
    for probe_kind, label, proposal, payload in built:
        try:
            result = transport.bulk_upsert(payload)
            event_id = str(result[0]["id"])
            written.append(event_id)
            readback = transport.get_event(event_id)
        except DeliveryError as exc:
            rows.append((probe_kind, "ERROR", str(exc)))
            continue
        # The product's own verifier, against the product's own proposal: whatever it
        # accepts in production it must accept here, and the reverse.
        checked = copy.deepcopy(proposal)
        checked["owned_external_id"] = payload["external_id"]
        checked["workout"]["name"] = payload["name"]
        try:
            verify_readback(checked, readback, event_id)
        except DeliveryError as exc:
            rows.append((probe_kind, "MISMATCH", str(exc)))
            continue
        document = readback.get("workout_doc") or {}
        # Whether Intervals ran its own analysis over the workout, reported as the fact it
        # is rather than as a verdict: absent is correct for an open target, which has no
        # intensity to analyse, and is the known gap for the supplied-document path.
        analysed = "yes" if document.get("zoneTimes") else "no "
        rows.append((probe_kind, "exact", f"event {event_id}   provider analysis: {analysed}"))

    print(f"\nIntervals read-back for {args.date}:\n")
    _report(rows)
    print(
        "\n  exact             = the product's own read-back verifier accepted it\n"
        "  provider analysis = Intervals computed zone times for it. Expected `no` for an\n"
        "                      open target; `no` for hr-ceiling is the known limit of the\n"
        "                      supplied-document path, not a failure of this run."
    )

    if args.keep:
        print(
            "\nProbes were left in place. What Intervals holds is now known; what the "
            "athlete sees is not. On the watch, open each probe and record for every step "
            "whether a target is shown:\n"
            "  open-*         expect no target\n"
            "  pace-*         expect a pace range\n"
            "  hr-ceiling     expect a heart-rate ceiling\n"
            "A target that is exact here and absent there is a provider export gap, not a "
            "product bug -- record it in docs/workout-delivery-compatibility.md and run "
            "--clean when done."
        )
    else:
        for event_id in written:
            transport.delete_event(event_id)
        print(f"\nremoved {len(written)} probe(s); pass --keep to check them on a watch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
