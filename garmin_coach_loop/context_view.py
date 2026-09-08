"""What one read hands the model, out of everything the build assembled (issue #250).

A CoachContext is built whole and stays whole: `validate_bundle` reads all of it, the
proposal binds its bytes, and the retained copy a confirmation names is the same object
it always was. What changed is that the *response* no longer has to be that object.

**What this buys, measured rather than assumed.** A heavy account's whole
`startCoachSession` result is 81,143 characters. A day-and-week read is 53,641 and a
stored-record correction is 15,326. That peak is the thing that has actually broken:
issue #233 recorded a 77,166-character result past a client's own per-result limit, and
#355 a 38.2 KB context the model could not echo back to author against. Neither failure
is visible in an average -- the turn simply does not happen.

**What it does not buy.** Seven blind coaching turns were asked the same questions of the
same evidence, once with everything and once with a declared read plus the freedom to
expand. The whole journey came out **1% larger**, not smaller, because five of the seven
ended up reading every group -- and a journey that reads everything pays the compact read
*plus* the rest, so it can only exceed reading everything once. Two rounds of repair took
that from +16% to +1% and stopped there, because the remainder is arithmetic rather than
wording. The turns that stayed narrow did save: 35% on one, and 17x on a goal correction.

So this is a ceiling, and a cheap narrow turn. It is not a smaller conversation, and
nothing here should be described as one.

## The three parts of a read

**The core** is what orients any coaching turn: which plan and cycle this is, what the
athlete has said they want and how they like to train, what limits this week, what the
provider covered and how fresh it is. It is eighteen small fields, ~3,500 characters on
the heavy fixture, and it is in every read because a coach that cannot see it cannot
safely answer anything.

**The groups** are the evidence that grows with how much the athlete trains. A group is
a set of whole fields -- never a slice of one. Issue #249 is why: when old prescriptions
were dropped from half a field's rows, the coach compared whole-session averages against
threshold anchors that no longer applied and proposed the wrong change. A field is
comparable with itself; half of one is not.

**The index** names what this read did *not* load, with row counts and spans, so an
unexpected interaction is discoverable without downloading everything. It is emitted
only when something was withheld -- a read that loaded everything has nothing to index.

## What this is not

Not an authorization boundary. A read purpose says what to hand back first; it never
limits what the coach may consider, raise, or recommend, and it grants nothing. The
model may name several groups, ask for more mid-conversation, or find its first choice
was wrong and expand -- which is an ordinary second call, not a restart.

Not a checklist either, and that half had to be measured before it was written down.
Expanding every group the index names costs the whole payload *plus* the compact one,
which is worse than never projecting -- so the index says when to reach for a group,
not merely that the group exists.

Not an intent classifier. Nothing here reads the athlete's sentence. The model declares
what it is reading for, because the model is the only party that has the sentence.
"""

from __future__ import annotations

from typing import Any


# Every field a build can put on a CoachContext that is small and orienting rather than
# proportional to how much the athlete trains. Each is one row per thing that is true
# right now -- the goal, the frame, the constraints, the coverage table -- so carrying
# all of them always costs a fixed ~3,500 characters and buys a coach that can tell what
# it is looking at. `test_context_view.py` fails if a field arrives in neither this list
# nor a group, because silently dropping evidence is the failure this whole mechanism
# could most easily become.
CORE_FIELDS: tuple[str, ...] = (
    "schema_version",
    "context_id",
    "as_of",
    "timezone",
    "athlete_profile",
    "goal_context",
    "measurement_evidence",
    "review_frame",
    "constraints",
    "athlete_baseline",
    "coverage",
    "freshness",
    "sources",
    "privacy",
    "unknowns",
    "recovery_trends",
    "long_term_goals",
    "training_preferences",
    "evidence_expectations",
)

# The groups, named for the question that needs them rather than for the field they
# hold: the model maps a sentence to one of these, and a field name would make that a
# question about this repository's vocabulary instead.
#
# A field appears in every group that genuinely needs it. `current_calendar` is in both
# `today` and `week` because both answers depend on what is on the calendar; naming it
# once and making the week question ask for two groups would be a puzzle, not a saving.
EVIDENCE_GROUPS: dict[str, tuple[str, ...]] = {
    # What to do today: what was actually trained lately, what is on the calendar, how
    # the athlete has been sleeping and feeling, and whether recent runs held together.
    "today": (
        "recent_actuals",
        "current_calendar",
        "run_drift",
        "subjective_states",
        "recovery_signals",
        # The athlete-stated half of the same reading. `recovery_signals` is what the
        # provider covered; `reported_recovery` is what they said on the days it did
        # not, and a today answer that loads one without the other is holding half a
        # field -- the #249 failure this module's own rule exists to prevent. It is
        # small: at most 28 days of four numbers.
        "reported_recovery",
    ),
    # Planning or reviewing a week: the cycle's own sessions against what happened, the
    # calendar, and the weekly volume those weeks sit in.
    "week": ("cycle_sessions", "current_calendar", "baseline_evidence"),
    # Reassessing the 28-day direction: the same sessions, plus what the athlete's
    # training looked like before this cycle and where it stopped.
    "cycle": (
        "cycle_sessions",
        "baseline_evidence",
        "training_breaks",
        "training_history",
    ),
    # Lifting: the sets as reported, the per-movement arithmetic, and how a session's
    # sets were actually structured.
    "strength": ("strength_execution", "movement_history", "set_structure"),
    # Why comparable sessions went the way they did: per-rep execution against what was
    # prescribed, and whether the run drifted inside itself.
    "session_detail": ("segment_execution", "run_drift"),
    # Sleep, HRV and resting heart rate, from the provider and from the athlete.
    "recovery": ("recovery_signals", "reported_recovery", "subjective_states"),
    # Months rather than weeks: the long evidence issue #222 asks for, delivered through
    # this mechanism rather than through a second reader.
    "history": (
        "training_history",
        "training_breaks",
        "reported_activities",
        "baseline_evidence",
    ),
    # What the athlete stated themselves. Their goals and habits are in the core already
    # -- a coach must not contradict a stated habit in any turn -- so this adds the
    # measurements and sessions they reported that no device recorded.
    "records": ("body_measurements", "reported_activities"),
}

ALL_GROUPS: tuple[str, ...] = tuple(EVIDENCE_GROUPS)

# What a read with no declared purpose gets. Day and week questions are what a coaching
# turn usually is, and both are one call away from anything else through the index.
DEFAULT_READ: tuple[str, ...] = ("today", "week")

# The whole PlanState rides along for a read that is about training. A read that is only
# correcting something the athlete stated does not need every session of the cycle to do
# it, and `plan_state` still names the plan, its version and this week.
PLAN_BEARING_GROUPS: frozenset[str] = frozenset(
    {"today", "week", "cycle", "strength", "session_detail", "recovery"}
)

# The one name that means "everything", so a caller that genuinely wants the whole
# build -- the committed scenario reads, the CLI, a client that would rather pay than
# choose -- says so rather than listing eight groups it would have to keep in step.
ALL = "all"


class EvidenceGroupError(ValueError):
    """A read naming something that is not a group."""


def parse_read(value: Any) -> tuple[str, ...]:
    """The groups one request asks for, or the default when it asks for nothing.

    ``None`` is the default rather than an error: a client that has never heard of this
    still gets a coaching turn's evidence, and the description tells the model to say
    what it is reading for. ``"all"`` (alone or in the list) is every group.
    """
    if value is None:
        return DEFAULT_READ
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise EvidenceGroupError("read must be an array of evidence group names")
    if ALL in value:
        return ALL_GROUPS
    unknown = sorted({item for item in value} - set(ALL_GROUPS))
    if unknown:
        raise EvidenceGroupError(
            f"read names no evidence group: {', '.join(unknown)}; it takes "
            f"{', '.join(ALL_GROUPS)} or {ALL!r}"
        )
    # Order by the canonical list rather than by how the request happened to spell it,
    # so two requests for the same evidence produce the same response bytes.
    return tuple(group for group in ALL_GROUPS if group in set(value))


def fields_for(groups: tuple[str, ...]) -> tuple[str, ...]:
    """Every context field the named groups carry, core included, in build order."""
    wanted = set(CORE_FIELDS)
    for group in groups:
        wanted |= set(EVIDENCE_GROUPS.get(group, ()))
    return tuple(wanted)


def _row_count(value: Any) -> int | None:
    """How many rows a withheld field holds, without knowing which field it is.

    Two shapes cover every group field: a list of rows, and a dict holding its rows
    beside a window and a source. A dict with more than one list -- ``training_history``
    carries months and movement longevity together -- is counted by its longest, because
    what "this is big" means for that field is how far back it goes. This is a size hint
    for a model deciding whether to expand, never a figure to answer a question with:
    the rows themselves are one call away and are what an answer cites.
    """
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        lists = [item for item in value.values() if isinstance(item, list)]
        if lists:
            return max(len(item) for item in lists)
    return None


def _span(value: Any) -> str | None:
    """The dates a withheld field covers, read off the field's own window or rows."""
    if isinstance(value, dict):
        start, end = value.get("window_start"), value.get("window_end")
        if isinstance(start, str) and isinstance(end, str):
            return f"{start}..{end}"
        months = value.get("months")
        if isinstance(months, list) and months:
            named = [
                month.get("month")
                for month in months
                if isinstance(month, dict) and isinstance(month.get("month"), str)
            ]
            if named:
                return f"{min(named)}..{max(named)}"
    if isinstance(value, list):
        dates = sorted(
            row["date"]
            for row in value
            if isinstance(row, dict) and isinstance(row.get("date"), str)
        )
        if dates:
            return f"{dates[0]}..{dates[-1]}"
    return None


def evidence_index(
    context: dict[str, Any], groups: tuple[str, ...]
) -> dict[str, Any] | None:
    """What this read did not load, and what is in it.

    Returned only when something was withheld. A read that loaded every group has
    nothing to index, and emitting an empty one would put a key in every response for
    the sake of symmetry -- which is the cost this file exists to stop.

    A group whose every field is empty or absent on this account is reported as holding
    nothing, not omitted: "there is no strength evidence" and "strength evidence was not
    loaded" are different answers, and a coach that cannot tell them apart will ask the
    athlete for something the product already knows is not there.
    """
    withheld = [group for group in ALL_GROUPS if group not in set(groups)]
    if not withheld:
        return None
    rows = []
    for group in withheld:
        holdings = []
        for field in EVIDENCE_GROUPS[group]:
            value = context.get(field)
            if value is None:
                continue
            count = _row_count(value)
            if count == 0:
                continue
            entry: dict[str, Any] = {"field": field}
            if count is not None:
                entry["rows"] = count
            span = _span(value)
            if span is not None:
                entry["spans"] = span
            holdings.append(entry)
        rows.append({"group": group, "holds": holdings})
    return {
        "loaded": list(groups),
        "not_loaded": rows,
        # What this line is for, measured: without it, six of seven blind coaching turns
        # answered the index by fetching *every* group named in it, which is a compact
        # first page followed by the whole payload and costs more than not projecting at
        # all. The row counts and spans are what make a needed group visible; this
        # sentence is what stops the list reading as a collection to complete.
        "read_more": (
            "readCoachEvidence with this context_id, for a group this answer turns on. "
            "A group listed here and not read is not a gap in the answer -- it is "
            "evidence this question does not rest on."
        ),
    }


def project_context(
    context: dict[str, Any], groups: tuple[str, ...]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The context this read hands back, and the index of what it did not.

    Field order follows the build's own, so a projected read reads like a shorter
    version of the whole one rather than like a differently-ordered object.
    """
    wanted = set(fields_for(groups))
    view = {key: value for key, value in context.items() if key in wanted}
    return view, evidence_index(context, groups)


def group_slice(
    context: dict[str, Any], groups: tuple[str, ...]
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """The named groups' evidence, one copy of each field, and which group holds what.

    The obvious shape -- one object per group, each carrying its own fields -- was tried
    and measured. Groups share fields on purpose (``current_calendar`` answers both today
    and the week; ``baseline_evidence`` answers the week, the cycle and months), so an
    expansion naming several groups sent some fields twice: on one measured turn, 9,790 of
    19,583 characters were a second copy of something already in the same response.

    So the fields come back once, and the map says which group each answers. Nothing is
    lost -- a model that asked two questions can still see which field answers which -- and
    the second copy is not paid for.
    """
    fields: dict[str, Any] = {}
    holds: dict[str, list[str]] = {}
    for group in groups:
        names = [field for field in EVIDENCE_GROUPS[group] if field in context]
        holds[group] = names
        for field in names:
            fields.setdefault(field, context[field])
    return fields, holds


def plan_is_read(groups: tuple[str, ...]) -> bool:
    """Whether this read is about training, and therefore needs the plan itself."""
    return bool(PLAN_BEARING_GROUPS & set(groups))
