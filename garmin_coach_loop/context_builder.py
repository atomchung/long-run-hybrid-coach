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
    prescribed_reps_dates,
    review_horizon_start,
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
    "build_context_with_domain",
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


def _merged_recovery_signals(
    window: BuildWindow,
    domain: SourceDomain,
    provided: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Per-day recovery evidence from both origins at once, day by day, or ``None``.

    One group, because the coach reads one thing, and merged by *day* rather than by
    group because replacing one with the other cost the athlete evidence for speaking:
    an upload that named one morning displaced six weeks of provider rows, so stating a
    figure left the coach with less than stating nothing (issue #364). Neither does the
    reverse hold -- an upload carries readings the provider has no column for, and
    dropping it whenever the provider answered anything would take those away instead.

    **The upload wins the days it names.** Both describe the same morning, and one of them
    is the athlete looking at their own watch in this conversation while the other is what
    the provider had synced by the time this build read it. On every other day the
    provider's row stands, which is most of the window.

    The source is read off the rows that survived rather than decided here, the same rule
    ``_strength_execution_source`` follows: naming an origin whose rows are all displaced
    would advertise evidence a reader cannot find.
    """
    provided_days = {
        day["date"]: day
        for day in (provided or {}).get("days") or []
        if isinstance(day, dict) and isinstance(day.get("date"), str)
    }
    by_day = {
        day["date"]: day
        for day in domain.recovery_days or []
        if isinstance(day, dict) and isinstance(day.get("date"), str)
    }
    from_provider = [date for date in by_day if date not in provided_days]
    by_day.update(provided_days)
    if not by_day:
        # Neither origin has a row. An upload that stated none is still a group -- "the
        # client looked and found no values", which the boundary deliberately keeps apart
        # from null's "nobody looked" -- so it is handed back as it arrived rather than
        # collapsed into the second answer.
        return provided
    names = []
    if from_provider and domain.sources:
        names.append(str(domain.sources[0]["source"]))
    if provided_days and isinstance(provided, dict) and provided.get("source"):
        names.append(str(provided["source"]))
    return {
        "source": "+".join(names) if names else "provider",
        # One cycle: the span both origins are read over, and the one a review asks
        # about. Not the 7-day window the trend and coverage readings beside this are
        # still computed over, and not the upload's own, which covers part of it.
        "window_start": window.window28_start.isoformat(),
        "window_end": window.window28_end.isoformat(),
        "days": [by_day[date] for date in sorted(by_day, reverse=True)],
    }


def _reported_group(
    window: BuildWindow,
    rows: list[dict[str, Any]],
    *,
    key: str,
    window_start: dt.date | None = None,
) -> dict[str, Any] | None:
    """One conversational evidence group, or ``None`` when the athlete has stated nothing.

    Same envelope every standalone group carries -- source, the window it was read over,
    the rows -- so the coach reads it the way it reads the others. ``None`` rather than an
    empty group because there is no configuration step behind these: unlike a local health
    db, this file is always readable, so "nothing here" can only ever mean the athlete has
    not said anything, and an empty group would dress that up as a search that came back
    empty.

    ``window_start`` overrides the default 42-day span for a group read over a shorter
    one. It is the *stated* window rather than a second convention: whatever span the rows
    were selected over has to be the span the envelope names, or the coach reads "nothing
    since" over a period nobody looked at.
    """
    if not rows:
        return None
    return {
        "source": athlete_evidence.ATHLETE_REPORTED_SOURCE,
        "window_start": (window_start or window.window42_start).isoformat(),
        "window_end": window.window42_end.isoformat(),
        key: rows,
    }


def _recovery_dates(recovery_signals: dict[str, Any] | None) -> set[str]:
    """The days the device reading already answers, so the stated group need not repeat them."""
    if not isinstance(recovery_signals, dict):
        return set()
    return {
        day["date"]
        for day in recovery_signals.get("days") or []
        if isinstance(day, dict) and isinstance(day.get("date"), str)
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
    domain: SourceDomain | None = None,
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

    ``health_db`` (archived issue #37) is unrelated to ``source``: it opts a build into two
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
      - ``domain`` is an activity/recovery domain the caller already holds, and passing
        one replaces the provider read entirely rather than adding to it: no credential
        is resolved, no request is issued, and ``source``, ``credentials``, ``fetch``
        and ``db_path`` go unused. It is for the caller that has to build the same
        context twice in one request -- see ``build_context_with_domain``, which is
        where a domain comes from -- and never a way to supply a domain from anywhere
        but a real read of this athlete's own provider.

    The owner's ``athlete-evidence.json`` -- what they told the coach in an earlier
    conversation, which no provider holds -- is read alongside the plan and feeds eight
    fields. ``constraints`` gains the week's stored availability whenever this request
    does not state its own (issue #28). ``strength_execution`` falls back to reported
    lifts only when no local strength log resolved at all (issue #47), so a measured
    per-set record is never displaced by a recollection. ``athlete_profile`` carries the
    timezone and language the athlete stated, or ``None`` when they stated neither.
    ``body_measurements`` and ``reported_activities`` carry what the athlete weighed and
    the sessions no device recorded, each as its own labelled group -- never merged into
    ``recent_actuals``, never offered to the matcher, so neither can be read as a
    provider-backed actual. ``subjective_states`` carries the last fortnight of what the
    athlete said about how they felt, dated and unread by anything here (issue #188).
    ``training_history`` (issue #101) reads the same file's complete history rather than
    any windowed slice of it, and rolls it up into monthly buckets -- the coarse,
    honestly-labelled long-range view the 42-day cycle window cannot provide.
    ``evidence_expectations`` (issue #28) reads the same unwindowed history a third way,
    beside the provider's own read: per stream, the first and last day evidence arrived
    and how long the silence since has run, so a supply that worked and stopped stops
    looking like one that was never there. All eight are absent-by-default and never
    block: no file means nothing was reported, which is not an error.

    The calendar/goal/athlete_baseline domain always comes from the local state store's
    current PlanState regardless of source. Raises ``ContextBuildError`` when the
    selected provider cannot produce data -- never fabricates a context from a broken
    source. Lets ``StateStoreError`` (from ``garmin_coach_loop.store``) propagate
    unchanged when the state store's own doctor check fails. ``now`` is an optional
    injection point for deterministic tests; it must be UTC-aware when given.
    """
    report, _ = build_context_with_domain(
        request,
        state_dir=state_dir,
        source=source,
        db_path=db_path,
        health_db=health_db,
        now=now,
        credentials=credentials,
        fetch=fetch,
        use_local_health_db=use_local_health_db,
        provided_recovery_signals=provided_recovery_signals,
        domain=domain,
    )
    return report


def build_context_with_domain(
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
    domain: SourceDomain | None = None,
) -> tuple[dict[str, Any], SourceDomain]:
    """``build_context``, plus the provider domain it built, so a caller can build again
    without reading the provider a second time.

    Every parameter means exactly what it means to ``build_context``, whose docstring is
    the one to read; this exists only because the report is a JSON-serializable dict that
    a ``SourceDomain`` must not be smuggled into. The CLI prints that dict, so an extra
    key on it would both change the command's output and fail to serialize.

    Reconcile-then-rebuild is the caller this is for. Reconciliation deep-copies the
    plan, bumps its version and marks matched sessions completed, and none of that
    reaches either value the provider read takes from the plan -- the baseline threshold
    pace, and the days the plan prescribed more than one step on. So a second read would
    send byte-identical requests, and answering the rebuild from the first snapshot is
    not an approximation of it: it is the same rows, selected over the same window,
    which a re-read could only reproduce or -- if the athlete's account moved in
    between -- silently disagree with halfway through one response.

    Returns the domain unconditionally: this function raises rather than returning
    without one, so an optional value here would be a case no caller could ever hit and
    every caller would have to write code for.
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

    # The days this cycle prescribed reps on, read from the plan's own week plus every
    # earlier week still in the commit chain -- the segment read spans 14 days, and the
    # plan holds only the current 7 (issue #233). Both lists are full plan sessions
    # here; the context's own `cycle_sessions` projection drops `plan` and is built
    # later, so this is the only place both halves are in the shape the check needs.
    structured_dates = prescribed_reps_dates(
        list(plan.get("week", {}).get("sessions") or []) + list(cycle_sessions)
    )
    # The earliest day a review still reads session by session. Stated once here and
    # handed to every group that carries per-session rows, so two groups cannot end up
    # windowed differently for no reason a reader of either could see.
    horizon = review_horizon_start(plan, window.as_of.date(), window.window42_start)

    # A domain the caller handed in replaces this read outright rather than seeding it:
    # it came from this athlete's own provider, over this same window, earlier in this
    # same request. Everything after this block still runs, because the plan, the
    # athlete's own evidence and the optional local groups are the halves a
    # reconciliation between the two builds actually moves.
    if domain is None:
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
                structured_dates=structured_dates,
                # The plan's own max HR decides whether the provider's Run sport settings
                # are worth a request at all, because disagreeing with this figure is the
                # only thing that value ever does. Handed over rather than tested here, so
                # one predicate -- the divergence note's own -- answers both "is it worth
                # reading" and "is it worth reporting".
                baseline_max_hr=baseline.get("max_hr"),
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
    # fed by the same local file (archived issue #37), resolved independently of `source`
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
        recovery_signals = _merged_recovery_signals(
            window, domain, provided_recovery_signals
        )
        if recovery_signals is None:
            recovery_signals_unknown = (
                "recovery_signals: no local health db configured; recent recovery state "
                "unverified"
                if use_local_health_db
                else "recovery_signals: no client upload supplied; recent device-only "
                "recovery state unverified"
            )
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

    # Counted off the window's own end rather than from a second clock, so the span the
    # envelope names is the span the rows were selected over.
    subjective_states_start = window.window14_end - dt.timedelta(
        days=athlete_evidence.SUBJECTIVE_STATE_WINDOW_DAYS - 1
    )
    reported_recovery_start = window.window28_end - dt.timedelta(
        days=athlete_evidence.REPORTED_RECOVERY_WINDOW_DAYS - 1
    )
    report = assemble_context(
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
        # Windowed to the review horizon rather than the 42-day span beside it (issue
        # #233). This group is per-session rows for sessions no device recorded, and a
        # bulk import of a year of training lands hundreds of them inside six weeks --
        # 36 rows, 9.4 KB, on the day the owner's backfill landed. What those rows
        # answer past the horizon is how the athlete's volume has moved, and
        # `training_history` below answers it in monthly buckets built from the same
        # evidence, unwindowed. What they answer inside it is last week, and inside it
        # they are all still here.
        reported_activities=_reported_group(
            window,
            athlete_evidence.reported_activity_summaries(
                evidence, window, since=horizon
            ),
            key="activities",
            window_start=horizon,
        ),
        # How the athlete said they felt, over a shorter window than the rest of this file
        # is read on (issue #188). Two natural weeks, because these are statements about a
        # day rather than facts that keep standing: what was said six weeks ago has been
        # answered by six weeks of training since, and carrying it would spend the coach's
        # context on it every turn (AGENTS.md 13). Rows, dates, nothing else -- no run
        # length, no count, no comparison against the recovery readings sitting beside
        # them; "three weeks of this" is what the coach reads out of the rows.
        subjective_states=_reported_group(
            window,
            athlete_evidence.reported_subjective_states(
                evidence, subjective_states_start, window.window14_end
            ),
            key="states",
            window_start=subjective_states_start,
        ),
        # The readings the athlete stated or uploaded, over the cycle window rather than
        # the fortnight beside it: the value in these is the multi-week shape -- "in the
        # fifties all month" -- and a two-week read would cut the month in half. Beside
        # `recovery_signals` and never inside it, the boundary a reported session already
        # keeps from `recent_actuals` (docs/data-sources.md): a device reading and a
        # number the athlete typed are two facts, and merging them would make the coach
        # unable to tell which it was looking at (issue #358).
        reported_recovery=_reported_group(
            window,
            athlete_evidence.reported_recovery(
                evidence,
                reported_recovery_start,
                window.window28_end,
                answered_dates=_recovery_dates(recovery_signals),
            ),
            key="days",
            window_start=reported_recovery_start,
        ),
        # Standing statements, so no window applies: a target set six months ago and a
        # habit stated last week are equally current until the athlete changes them.
        # Handed across whole and uninterpreted -- nothing here compares a preference
        # against what was actually trained, and nothing promotes what was actually
        # trained into a preference (issue #164).
        long_term_goals=athlete_evidence.stated_long_term_goals(evidence),
        training_preferences=athlete_evidence.stated_training_preferences(evidence),
        # The complete, unwindowed evidence history -- not the 42-day slice the two reads
        # above are clipped to -- so ``training_history`` (issue #101's hosted half) can
        # answer "how has this changed over the past year", which the cycle-planning
        # window structurally cannot.
        training_history_activities=athlete_evidence.all_reported_activity_summaries(evidence),
        training_history_strength_reports=athlete_evidence.all_reported_strength_sessions(
            evidence
        ),
        # The third unwindowed read, and the one `evidence_expectations` needs that no
        # other group does: the windowed series above cannot tell a stream that stopped
        # before the window opened from one that never started (issue #28).
        body_measurement_history=athlete_evidence.all_body_measurements(evidence),
    )
    return report, domain
