"""Dispatch layer: build and self-validate a sanitized CoachContext.

Everything about the CoachContext shape, the shared time window, and provider-agnostic
derivation formulas lives in ``context_core``. Everything about actually reaching an
athlete's data lives in exactly one of two parallel, unrelated modules -- neither built
on top of the other, and neither aware the other exists:

- ``source_intervals``: the athlete's own intervals.icu account. The product path and
  the default -- a fresh clone with no local infrastructure beyond one API key can use
  it end to end. Imported eagerly below because the default path must always be
  importable.
- ``source_personal_os``: the owner's local personal-os health.db snapshot. Owner-only,
  never the default, and imported lazily -- only inside the ``source == "personal-os"``
  branch of ``build_context`` -- so that a machine with no personal-os installation can
  import this module and run ``--source intervals`` without ever touching it.

This module is the only place that knows both sources exist; it re-exports the public
names both the CLI and tests need so callers do not have to know the internal package
layout.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from . import athlete_evidence, source_intervals
from .context_core import (
    ALL_DAYS,
    DEFAULT_SESSION_MINUTES,
    DEFAULT_TIMEZONE,
    RED_FLAG_FIELDS,
    BuildWindow,
    ContextBuildError,
    ContextRequest,
    SourceDomain,
    assemble_context,
    build_window,
    parse_available_days,
    parse_optional_bool,
    parse_red_flag_overrides,
)
from .store import cycle_sessions as store_cycle_sessions, status_store


__all__ = [
    "ALL_DAYS",
    "DEFAULT_SESSION_MINUTES",
    "DEFAULT_SOURCE",
    "DEFAULT_TIMEZONE",
    "RED_FLAG_FIELDS",
    "VALID_SOURCES",
    "BuildWindow",
    "ContextBuildError",
    "ContextRequest",
    "SourceDomain",
    "build_context",
    "parse_available_days",
    "parse_optional_bool",
    "parse_red_flag_overrides",
]


# No "auto": degrading away from the product path is always an explicit CLI choice
# (--source personal-os), never something this code decides silently on the caller's
# behalf. See build_context's docstring for what each value does.
VALID_SOURCES = ("intervals", "personal-os")
DEFAULT_SOURCE = "intervals"


def _strength_execution_group(
    window: BuildWindow,
    measured: list[dict[str, Any]],
    reported: list[dict[str, Any]],
    *,
    measured_group: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One ``strength_execution`` group from both places per-set truth can come from.

    A measured record is never displaced. Where a local strength log already holds a
    ``(date, exercise)``, that entry stands and the reported one is dropped -- the athlete
    recalling a session they also logged adds nothing, and preferring the recollection
    would quietly downgrade the better record. Everywhere else the report is the only
    thing there is, which is the entire point: before this, a machine with a health.db
    could not see a reported lift at all, so the same sentence to the same coach was kept
    or lost depending on which entry point the athlete happened to be using.

    ``None`` when neither side holds anything *and* no log was read, which the caller
    turns back into the ordinary "no strength evidence" unknown. An empty group from a log
    that was read is a different answer -- looked, nothing there -- and is kept.
    """
    taken = {
        (session.get("date"), athlete_evidence.exercise_key(str(session.get("exercise") or "")))
        for session in measured
    }
    merged = [
        *measured,
        *(
            session
            for session in reported
            if (session["date"], athlete_evidence.exercise_key(session["exercise"])) not in taken
        ),
    ]
    if not merged and measured_group is None:
        return None
    merged.sort(key=lambda item: (item["date"], item["exercise"]))
    merged.sort(key=lambda item: item["date"], reverse=True)
    return {
        "source": _strength_execution_source(measured_group, merged),
        "window_start": window.window42_start.isoformat(),
        "window_end": window.window42_end.isoformat(),
        "sessions": merged,
    }


def _strength_execution_source(
    measured_group: dict[str, Any] | None, merged: list[dict[str, Any]]
) -> str:
    """What the group as a whole came from, when it can come from two places at once.

    Read off the rows that survived the merge, never off what was offered to it: a report
    the local log displaced is not in the group, so naming it here would advertise
    evidence a reader cannot find. Each session already names its own ``source``, which is
    what weighs one row against another; this is only the summary above them.
    """
    measured_name = (
        str(measured_group.get("source")) if isinstance(measured_group, dict) else None
    )
    # Whatever the surviving rows actually say, in a stable order, rather than one name
    # this function decides on. There are two kinds of athlete statement now -- a set-by-set
    # report and a confirmed prescription (issue #76) -- and a summary that named only the
    # first would hide the second behind a label that does not describe it.
    stated = [
        name
        for name in (
            athlete_evidence.ATHLETE_REPORTED_SOURCE,
            athlete_evidence.PRESCRIBED_CONFIRMED_SOURCE,
        )
        if any(session.get("source") == name for session in merged)
    ]
    names = ([measured_name] if measured_name else []) + stated
    return "+".join(names) if names else athlete_evidence.ATHLETE_REPORTED_SOURCE


def _reported_group(
    window: BuildWindow, rows: list[dict[str, Any]], *, key: str
) -> dict[str, Any] | None:
    """One conversational evidence group, or ``None`` when the athlete has stated nothing.

    Same envelope every standalone group carries -- source, the window it was read over,
    the rows -- so the coach reads it the way it reads the others. ``None`` rather than an
    empty group because there is no configuration step behind these: unlike a local health
    db, this file is always readable, so "nothing here" can only ever mean the athlete has
    not said anything, and an empty group would dress that up as a search that came back
    empty.
    """
    if not rows:
        return None
    return {
        "source": athlete_evidence.ATHLETE_REPORTED_SOURCE,
        "window_start": window.window42_start.isoformat(),
        "window_end": window.window42_end.isoformat(),
        key: rows,
    }


def build_context(
    request: ContextRequest,
    *,
    state_dir: Path | str,
    source: str = DEFAULT_SOURCE,
    db_path: Path | None = None,
    health_db: Path | None = None,
    now: dt.datetime | None = None,
    credentials: "source_intervals.IntervalsCredentials | None" = None,
    fetch: "source_intervals.Fetcher | None" = None,
    use_local_health_db: bool = True,
    provided_recovery_signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and self-validate a sanitized CoachContext from a live provider and the
    local state store.

    ``source`` selects the activity/recovery provider. There is no "auto": whichever
    source is selected is the only one attempted, and a failure there is always a
    blocked build -- this module never silently substitutes one source for another.

      - "intervals" (default): the athlete's own intervals.icu REST API, read-only,
        near-real-time. Any user with an API key can use this; a fresh clone needs
        nothing else. Raises ``ContextBuildError`` when credentials cannot be resolved
        or the read fails.
      - "personal-os": the owner's local read-only health.db sync snapshot -- an
        owner-only transitional patch, not the product path. Requires an explicit
        ``--db``, or ``HEALTH_DB_PATH`` / ``GARMIN_COACH_LOOP_HEALTH_DB``; there is no
        default path, so it is unavailable on any machine that has not set one. Every
        CoachContext this
        produces carries an unknowns note saying it did not use the product path (see
        ``source_personal_os.PERSONAL_OS_SOURCE_NOTE``).

    ``health_db`` (issue #37) is unrelated to ``source``: it opts a build into two
    standalone, optional evidence groups read from the same local file --
    ``strength_execution`` (per-set weight/reps truth from personal-os's
    ``strength_log`` table) and ``recovery_signals`` (readiness/HRV-status/acute-load
    and Body-Battery/stress truth from ``recovery_daily`` + ``daily_metrics``) --
    independent of which provider supplies activities/recovery. Resolution mirrors
    ``db_path``'s own precedence -- an explicit path, else the shared env vars via
    ``source_personal_os.resolve_health_db_path`` -- and when neither resolves, both
    groups are simply unconfigured (each ``None`` plus its own unknowns note), never
    a blocked build. A *configured* path that cannot be read still raises
    ``ContextBuildError`` for whichever group fails first, same as any other
    configured-but-broken source.

    ``credentials``, ``fetch``, ``use_local_health_db`` and
    ``provided_recovery_signals`` exist for a server that builds a context on behalf of
    a specific athlete rather than for whoever owns this machine. They default to
    today's local behavior exactly.

      - ``credentials`` supplies the intervals.icu credentials directly instead of
        resolving them from the environment. A multi-athlete caller holds one live token
        per request and must never read a machine-wide API key.
      - ``fetch`` is passed straight through to the provider's injectable fetcher.
      - ``use_local_health_db=False`` declares that the two optional local evidence groups
        do not apply to this build, and suppresses the ``HEALTH_DB_PATH`` /
        ``GARMIN_COACH_LOOP_HEALTH_DB`` fallback with it. Those variables name one
        machine-local database belonging to one person; consulting them while serving
        somebody else would attach a stranger's strength log and recovery signals to their
        context. Both groups then read as unconfigured -- ``None`` plus their unknowns
        note -- which is the honest answer, not a degraded one.
      - ``provided_recovery_signals`` is the sanitized evidence a client already read on
        the athlete's own machine. It is accepted only with ``use_local_health_db=False``:
        the server consumes the values, never a path or credential, and never opens the
        local database that produced them. The request boundary validates its provenance,
        exact seven-day window and per-day observations before it reaches this builder.

    The owner's ``athlete-evidence.json`` -- what they told the coach in an earlier
    conversation, which no provider holds -- is read alongside the plan and feeds five
    fields. ``constraints`` gains the week's stored availability whenever this request
    does not state its own (issue #28). ``strength_execution`` falls back to reported
    lifts only when no local strength log resolved at all (issue #47), so a measured
    per-set record is never displaced by a recollection. ``athlete_profile`` carries the
    timezone and language the athlete stated, or ``None`` when they stated neither.
    ``body_measurements`` and ``reported_activities`` carry what the athlete weighed and
    the sessions no device recorded, each as its own labelled group -- never merged into
    ``recent_actuals``, never offered to the matcher, so neither can be read as a
    provider-backed actual. All five are absent-by-default and never block: no file means
    nothing was reported, which is not an error.

    The calendar/goal/athlete_baseline domain always comes from the local state store's
    current PlanState regardless of source. Raises ``ContextBuildError`` when the
    selected provider cannot produce data -- never fabricates a context from a broken
    source. Lets ``StateStoreError`` (from ``garmin_coach_loop.store``) propagate
    unchanged when the state store's own doctor check fails. ``now`` is an optional
    injection point for deterministic tests; it must be UTC-aware when given.
    """
    if source not in VALID_SOURCES:
        raise ContextBuildError(f"unknown --source: {source!r}; expected one of {VALID_SOURCES}")
    if (credentials is not None or fetch is not None) and source != "intervals":
        # Silently ignoring them would let a caller believe it had pinned the provider it
        # is reading from while a completely different source answered.
        raise ContextBuildError("credentials and fetch apply only to --source intervals")
    if health_db is not None and not use_local_health_db:
        raise ContextBuildError(
            "health_db and use_local_health_db=False contradict each other"
        )
    if provided_recovery_signals is not None and use_local_health_db:
        raise ContextBuildError(
            "provided_recovery_signals and use_local_health_db=True contradict each other"
        )

    resolved_now = now if now is not None else dt.datetime.now(dt.timezone.utc)
    window = build_window(request, resolved_now)

    # Raises StateStoreError (unmodified) when the store's own doctor check fails. The
    # plan is always the local source of truth for the calendar/goal/baseline domain,
    # regardless of where the activity/recovery domain comes from.
    status = status_store(state_dir)
    plan = status["current_plan"]

    # What the athlete told the coach in an earlier conversation and no device can know
    # (issues #28 and #47). Read once here and used twice below: for the week's
    # availability, and -- only where no local strength log exists -- for reported lifts.
    # A file that cannot be read raises ``StateStoreError``, same as a broken store: the
    # alternative is dropping statements the athlete believes are still on record.
    evidence = athlete_evidence.load_evidence(state_dir)
    availability = athlete_evidence.effective_availability(
        evidence, week_start=athlete_evidence.week_start_for(window.as_of.date())
    )
    # Carried into the context rather than consumed here: whichever timezone this build
    # ran under is already settled by the time it reaches this function, and what the
    # coach still needs is whether the athlete ever stated one.
    profile = athlete_evidence.stored_profile(evidence)

    # Read from the commit chain rather than the plan: the week the plan holds is the only
    # one still in it, so every earlier session of this cycle lives in history alone. The
    # window is the cycle, which is the span the coach is actually reassessing. It ends at
    # today rather than including it: a day still running has not passed, and a session
    # scheduled for this afternoon is not a record of anything yet.
    cycle_sessions = store_cycle_sessions(
        state_dir,
        since=(plan.get("cycle") or {}).get("start") or "",
        before=window.as_of.date().isoformat(),
    )

    # Unmatched-run intensity is classified relative to the athlete's own threshold
    # pace (context_core._classify_running); the current PlanState's baseline is the
    # only place that number lives, so it is resolved here and handed to whichever
    # provider runs. None simply leaves unmatched runs unclassified at the easy floor.
    baseline = plan.get("athlete_baseline") or {}
    raw_threshold = baseline.get("threshold_pace_sec_per_km")
    # bool is an int subclass, and a hand-edited plan may carry 370.0 -- accept real
    # positive numbers only, never True/False.
    threshold_sec_per_km = (
        raw_threshold
        if isinstance(raw_threshold, (int, float))
        and not isinstance(raw_threshold, bool)
        and raw_threshold > 0
        else None
    )

    if source == "intervals":
        resolved_credentials = (
            credentials if credentials is not None else source_intervals.resolve_credentials()
        )
        if resolved_credentials is None:
            raise ContextBuildError(
                "intervals credentials not configured; set INTERVALS_ICU_API_KEY and "
                "INTERVALS_ICU_ATHLETE_ID (process env, ~/.config/garmin-coach-loop/.env, "
                "or repo-root .env)"
            )
        domain = source_intervals.fetch_domain(
            resolved_credentials,
            window,
            fetch=fetch,
            threshold_sec_per_km=threshold_sec_per_km,
        )
    else:  # "personal-os" -- the only other member of VALID_SOURCES, checked above
        # Lazy on purpose -- see module docstring. A machine with no personal-os
        # installation must be able to import this module and run --source intervals
        # without this line ever executing.
        from . import source_personal_os

        resolved_db_path = source_personal_os.resolve_health_db_path(db_path)
        if resolved_db_path is None:
            raise ContextBuildError(
                "personal-os source unavailable: pass --db or set HEALTH_DB_PATH "
                "(or GARMIN_COACH_LOOP_HEALTH_DB) -- there is no default path"
            )
        domain = source_personal_os.fetch_domain(
            resolved_db_path, window, threshold_sec_per_km=threshold_sec_per_km
        )

    # strength_execution + recovery_signals: two standalone optional evidence groups
    # fed by the same local file (issue #37), resolved independently of `source`
    # above -- including under --source intervals, since the whole point is layering
    # local evidence on top of the required base source. resolve_health_db_path is
    # pure (env/CLI lookup only, no file I/O), so this lazy import is safe to run
    # unconditionally; only the two fetchers below (the actual file reads) are gated
    # on a path having resolved. Env-configured means the owner opted in once; a
    # fresh clone with no --health-db and no env var never reads any personal-os file.
    strength_execution: dict[str, Any] | None = None
    strength_execution_unknown: str | None = None
    recovery_signals: dict[str, Any] | None = None
    recovery_signals_unknown: str | None = None
    resolved_health_db = health_db if use_local_health_db else None
    if resolved_health_db is None and use_local_health_db:
        from . import source_personal_os

        resolved_health_db = source_personal_os.resolve_health_db_path(None)
    reported_sessions = athlete_evidence.reported_strength_sessions(evidence, window)
    if resolved_health_db is None:
        # No local strength log at all -- notably every hosted build, where
        # ``use_local_health_db=False`` is permanent rather than a configuration step
        # someone skipped. What the athlete said they lifted is then the only per-set
        # record there is (issue #47).
        strength_execution = _strength_execution_group(window, [], reported_sessions)
        if strength_execution is None:
            strength_execution_unknown = (
                "strength_execution: no local strength log configured; recent lift "
                "execution unverified"
            )
        if provided_recovery_signals is None:
            recovery_signals_unknown = (
                "recovery_signals: no local health db configured; recent recovery state "
                "unverified"
                if use_local_health_db
                else "recovery_signals: no client upload supplied; recent device-only "
                "recovery state unverified"
            )
        else:
            # Already normalized and validated against this build's exact window by the
            # hosted request boundary. It is request-scoped evidence: the gateway does
            # not persist the group or the database material that produced it.
            recovery_signals = provided_recovery_signals
    else:
        from . import source_personal_os

        # Order matters only for which error surfaces first when the configured file
        # is missing a table: strength runs first, so a db missing strength_log fails
        # there even though it might carry perfectly good recovery_daily/daily_metrics.
        measured = source_personal_os.fetch_strength_execution(resolved_health_db, window)
        strength_execution = _strength_execution_group(
            window, measured.get("sessions") or [], reported_sessions, measured_group=measured
        )
        recovery_signals = source_personal_os.fetch_recovery_signals(resolved_health_db, window)

    return assemble_context(
        request,
        plan,
        window,
        domain,
        strength_execution=strength_execution,
        strength_execution_unknown=strength_execution_unknown,
        recovery_signals=recovery_signals,
        recovery_signals_unknown=recovery_signals_unknown,
        cycle_sessions=cycle_sessions,
        athlete_availability=availability,
        athlete_profile=profile,
        # Read from the same file, over the same window, and handed across untouched.
        # Nothing joins either of them to `domain` on the way -- a reported session has no
        # activity id to attach with and is never offered to the matcher, which is what
        # keeps it out of `recent_actuals` and out of reconciliation entirely.
        body_measurements=_reported_group(
            window,
            athlete_evidence.body_measurement_series(evidence, window),
            key="measurements",
        ),
        reported_activities=_reported_group(
            window,
            athlete_evidence.reported_activity_summaries(evidence, window),
            key="activities",
        ),
        # Standing statements, so no window applies: a target set six months ago and a
        # habit stated last week are equally current until the athlete changes them.
        # Handed across whole and uninterpreted -- nothing here compares a preference
        # against what was actually trained, and nothing promotes what was actually
        # trained into a preference (issue #164).
        long_term_goals=athlete_evidence.stated_long_term_goals(evidence),
        training_preferences=athlete_evidence.stated_training_preferences(evidence),
    )
