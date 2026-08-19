"""What a delivered session was sent as, projected for comparison.

One rule with three readers -- the writers' stale-delivery reset, validation's refusal to
carry a receipt across a content change, and the store's compare-and-commit hash -- so it
lives where all three import it and none of them restates it.

It is not in `validation` because it holds `purpose` and `coach_note`, and the free-text
guard (issue #93) forbids the validator from reading those values. The guard is right
about the validator: eleven regular expressions once re-derived the plan's own numbers out
of the sentence reporting them. It is the wrong rule for this projection, which copies the value to
compare it byte for byte and interprets nothing -- so the projection moved rather than the
guard loosening. The guard follows it here in the only shape that stays honest: a
free-text value may be stored and nothing else.
"""

from __future__ import annotations

from typing import Any


def delivery_session_content(session: dict[str, Any]) -> dict[str, Any]:
    """Project only the session content that makes a delivered workout stale.

    Match/completion state and provider observations do not alter what was sent. Date and
    executable training content do. This projection is public to the store so validation
    and proposal/read-back identity cannot drift into two rules.

    `plan` carries both halves of what was delivered now: the steps the watch executes,
    and -- through the rendering -- the text a calendar entry describes itself with.
    `prescription` is not listed beside it because it is that rendering, and a projection
    that lists a value and the thing it is derived from is one rule written twice.
    """
    execution = session.get("execution") if isinstance(session.get("execution"), dict) else {}
    return {
        field: session.get(field)
        for field in (
            "session_id",
            "sport",
            "scheduled_date",
            # The delivered content `plan` does not carry: a movement_list session reaches
            # Intervals as a titled calendar entry whose name is *built from* purpose (with
            # the plan's primary movement and load, when the plan renders one), so
            # rewording purpose alone leaves the calendar holding a title the plan no
            # longer says. Held for every sport rather than for strength alone -- a
            # time_axis reword pays one redelivery it did not need, which is the cheaper of
            # the two errors; the other reports a verified delivery under a title that was
            # never sent.
            "purpose",
            # The other authored field that reaches Intervals (issue #56). It is appended
            # to the delivered description for both sports, so changing or clearing it
            # changes what the calendar holds exactly as rewording purpose does -- and a
            # delivery reported verified under a note that was since edited would be this
            # projection's one job left undone.
            "coach_note",
            "adaptation",
            "planned_minutes",
            "hard",
            "plan",
        )
    } | {"publish_supported": execution.get("publish_supported")}
