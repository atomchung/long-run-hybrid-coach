"""What a delivered session was sent as, projected for comparison.

One rule with three readers -- the writers' stale-delivery reset, validation's refusal to
carry a receipt across a content change, and the store's compare-and-commit hash -- so it
lives where all three import it and none of them restates it.

It is not in `validation` because it holds `purpose` and `coach_note`, and the free-text
guard (archived issue #93) forbids the validator from reading those values. The guard is right
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
    # The word a labelled session's title starts with, so changing it changes what the
    # calendar holds. Projected only when present, so a session with none carries no key.
    label = {"label": session["label"]} if "label" in session else {}
    # Delivered content `plan` does not carry, but only where it is still delivered: a
    # strength session with an unstructured plan reaches Intervals titled with its purpose,
    # so rewording it leaves the calendar holding a title the plan no longer says. Every
    # other session's title comes from its label or sport and its description from the
    # plan, so purpose reaches nothing there, and projecting it would make a reword
    # re-deliver an unchanged event the athlete then has to confirm.
    plan = session.get("plan") if isinstance(session.get("plan"), dict) else {}
    purpose: dict[str, Any] = {}
    if session.get("sport") == "strength" and plan.get("kind") == "unstructured":
        stored = session.get("purpose")
        # The value delivery writes as the title, which is the stored one without its outer
        # whitespace (`prescription.session_title`): a difference the calendar never shows
        # is not a reason to send the entry again.
        purpose["purpose"] = stored.strip() if isinstance(stored, str) else stored
    # A run's stored `plan.name` titled its calendar entry before titles were composed, and
    # reaches nothing now, so a restatement that only drops it is the same workout.
    projected_plan = session.get("plan")
    if session.get("sport") == "running" and plan.get("kind") == "time_axis":
        projected_plan = {key: value for key, value in plan.items() if key != "name"}
    return {
        field: session.get(field)
        for field in (
            "session_id",
            "sport",
            "scheduled_date",
            # The other authored field that reaches Intervals (issue #56). It is appended
            # to the delivered description for both sports, so changing or clearing it
            # changes what the calendar holds -- and a delivery reported verified under a
            # note that was since edited would be this projection's one job left undone.
            "coach_note",
            "adaptation",
            "planned_minutes",
            "hard",
        )
    } | {"plan": projected_plan} | purpose | label | {
        "publish_supported": execution.get("publish_supported")
    }
