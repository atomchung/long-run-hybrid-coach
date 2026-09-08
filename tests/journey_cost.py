"""What a whole coaching conversation costs the model, call by call.

Every other measurement in this repository bounds one thing: a field
(`test_context_budget.py`), one read (`test_context_view.py`), one served prompt
(`test_orchestration_prompt.py`). None of them answers the question issue #15 makes the
release gate -- *does a common journey cost less than it did*, end to end -- because a
journey is several calls and a mechanism that halves one of them can add a call.

So this walks whole journeys through the real gateway, over the real fake provider the
gateway tests use, and reports characters of compact JSON per call. Two arms:

``reference``  what a model following the pre-1.4 descriptions emitted and received:
               every group in every read, the CoachContext and the change request
               authored twice, the training judgment on every turn.
``candidate``  what this checkout's descriptions ask for: a declared read, expansion
               when the answer turns on something else, the proposal alone to confirm,
               and the judgment named after the first turn.

Both arms answer the same questions of the same account. What is *not* claimed here is
coaching quality: bytes are not answers, and the reference arm is deliberately the
expensive one, so a smaller number here is a cost result and nothing else. The quality
question has its own gate, and its own failure mode -- an arm that reads less and
answers worse is the finding `docs/decision-evidence.md` records.

Run it:

    python3 -m tests.journey_cost

`test_journey_cost.py` asserts the ceilings so a regression fails in CI rather than in
the next release note.

## The result, on the fixture account, 2026-09-08

| journey | reference | candidate | delta |
| --- | ---: | ---: | ---: |
| what should I do today | 27,390 | 25,404 | -1,986 |
| correct a goal, then move Thursday | 54,780 | 29,451 | **-25,329** |
| how did Thursday's session go | 27,390 | 29,030 | **+1,640** |
| review the week and roll it | 74,821 | 49,552 | **-25,269** |
| **all four** | **184,381** | **133,437** | **-50,944 (-28%)** |

The loss is reported rather than dropped. A day question expanded to one session makes
two calls where reading everything makes one, and this account is small enough that the
second call's fixed cost is not repaid. On a heavy account the same two calls read 6,060
characters where every group is 59,744 -- which is the account the mechanism exists for,
and is measured in `test_context_view.py` rather than claimed here.

Served before any of it: the tool catalogue is 73,889 characters, the orchestration
prompt 7,598, the training judgment 9,265. The judgment is the one of those three a
conversation can stop paying for after its first turn.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Callable

from garmin_coach_loop import orchestration


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


class Journey:
    """One conversation, recorded call by call.

    ``sent`` is what the model had to emit, ``received`` what came back. Both matter and
    they fail differently: a large result can exceed a client's per-result cap and stop
    the turn happening at all, while a large argument is a thing the model has to author
    from evidence it may not be able to hold.
    """

    def __init__(self, name: str, arm: str) -> None:
        self.name = name
        self.arm = arm
        self.calls: list[dict[str, Any]] = []

    def record(self, tool: str, sent: Any, received: Any) -> Any:
        self.calls.append(
            {"tool": tool, "sent": _size(sent), "received": _size(received)}
        )
        return received

    @property
    def sent(self) -> int:
        return sum(call["sent"] for call in self.calls)

    @property
    def received(self) -> int:
        return sum(call["received"] for call in self.calls)

    @property
    def total(self) -> int:
        return self.sent + self.received

    def as_row(self) -> dict[str, Any]:
        return {
            "journey": self.name,
            "arm": self.arm,
            "calls": len(self.calls),
            "sent": self.sent,
            "received": self.received,
            "total": self.total,
            "detail": self.calls,
        }


# The journeys, named for what the athlete asked rather than for the mechanism under
# test. Each is a function of one driver, so the same definition runs under the test
# suite's fake provider and under the report below.
JOURNEY_NAMES: tuple[str, ...] = (
    "what_should_i_do_today",
    "correct_a_goal_then_move_thursday",
    "how_did_thursdays_session_go",
    "review_the_week_and_roll_it",
)


def run_all(driver: "JourneyDriver") -> list[Journey]:
    return [
        journey
        for name in JOURNEY_NAMES
        for journey in (driver.run(name, "reference"), driver.run(name, "candidate"))
    ]


class JourneyDriver:
    """Drives one account through a named journey in one arm.

    ``call`` is the seam: ``(tool, arguments) -> result``. The test harness passes its
    own gateway route, so nothing here knows about HTTP, tokens or fixtures.
    """

    def __init__(
        self,
        call: Callable[[str, dict[str, Any]], dict[str, Any]],
        *,
        plan_id: str,
        plan_version: int,
        weekly_change: dict[str, Any],
        reset: Callable[[], None] | None = None,
    ) -> None:
        self.call = call
        self.plan_id = plan_id
        self.plan_version = plan_version
        self.weekly_change = weekly_change
        # Every journey starts from the same account. Without this, the arms drift:
        # `startCoachSession` reconciles, and reconciling commits, so the second arm of
        # the second journey is answering about a plan the first one already moved --
        # which reads as a difference between the arms and is a difference between the
        # accounts.
        self.reset = reset or (lambda: None)

    # -- the arms ----------------------------------------------------------------------

    def _open(self, journey: Journey, read: Any, *, guidance: dict[str, Any]) -> Any:
        sent = {"all_clear": True, **guidance}
        if read is not None:
            sent["read"] = read
        return journey.record("startCoachSession", sent, self.call("session", sent))

    def run(self, name: str, arm: str) -> Journey:
        self.reset()
        journey = Journey(name, arm)
        getattr(self, f"_{name}")(journey, arm)
        return journey

    # -- the journeys ------------------------------------------------------------------

    def _what_should_i_do_today(self, journey: Journey, arm: str) -> None:
        """The commonest turn there is, and the one a narrow read should win."""
        if arm == "reference":
            self._open(journey, "all", guidance={})
        else:
            self._open(journey, ["today"], guidance={})

    def _correct_a_goal_then_move_thursday(self, journey: Journey, arm: str) -> None:
        """A stored-record turn whose next sentence is a training question.

        The reference arm reads everything twice, because before the expansion route
        there was no way back to the plan except a second session read -- which is also
        a second provider read of an account nothing changed in between.
        """
        if arm == "reference":
            self._open(journey, "all", guidance={})
            self._open(journey, "all", guidance={})
            return
        first = self._open(journey, ["records"], guidance={})
        expansion = {
            "context_id": first["context"]["context_id"],
            "read": ["week"],
        }
        journey.record(
            "readCoachEvidence", expansion, self.call("evidence_read", expansion)
        )

    def _how_did_thursdays_session_go(self, journey: Journey, arm: str) -> None:
        """One session, completely: prescription, execution, comparison, source."""
        if arm == "reference":
            self._open(journey, "all", guidance={})
            return
        first = self._open(journey, ["today"], guidance={})
        context_id = first["context"]["context_id"]
        # Off the read the coach already has: `current_calendar` is in the `today` group,
        # so naming the session costs nothing. A helper call here would be a call the
        # real journey does not make.
        session_id = first["context"]["current_calendar"][0]["session_id"]
        focused = {
            "context_id": context_id,
            "read": ["week", "cycle", "strength", "session_detail"],
            "focus": {"sessions": [session_id]},
        }
        journey.record(
            "readCoachEvidence", focused, self.call("evidence_read", focused)
        )

    def _review_the_week_and_roll_it(self, journey: Journey, arm: str) -> None:
        """A review, then the change it leads to, previewed and confirmed."""
        digest_arm = arm == "candidate"
        opened = self._open(
            journey, "all" if arm == "reference" else ["today", "week"], guidance={}
        )
        context = opened["context"]
        # Read off the context rather than the constructor or `plan_state`. This journey
        # commits, so the arms cannot share a version; and the context is what a preview
        # is validated against, which on the first read of an account with work to
        # reconcile is one version behind what `plan_state` reports.
        plan_id = context["goal_context"]["plan_id"]
        plan_version = context["goal_context"]["plan_version"]
        prepare_sent: dict[str, Any] = {
            "plan_id": plan_id,
            "plan_version": plan_version,
            "context": (
                context if arm == "reference" else {"context_id": context["context_id"]}
            ),
            # Deep-copied because a request object is the model's own statement and the
            # boundary is free to normalise it; two arms sharing one dict would make the
            # second arm's request whatever the first one left behind.
            "change_request": copy.deepcopy(self.weekly_change),
        }
        prepared = journey.record(
            "prepareCoachDecision", prepare_sent, self.call("decision_prepare", prepare_sent)
        )
        apply_sent: dict[str, Any] = (
            {
                "plan_id": plan_id,
                "plan_version": plan_version,
                "context": context,
                "change_request": copy.deepcopy(self.weekly_change),
                "proposal": prepared["proposal"],
                "confirmed": True,
            }
            if arm == "reference"
            else {"proposal": prepared["proposal"], "confirmed": True}
        )
        journey.record(
            "applyCoachDecision", apply_sent, self.call("decision_apply", apply_sent)
        )
        # And the turn after the change, which is where the judgment stops repeating.
        self._open(
            journey,
            "all" if arm == "reference" else ["today"],
            guidance=(
                {}
                if not digest_arm
                else {
                    "guidance_received": True,
                    "guidance_digest": opened["guidance_digest"],
                }
            ),
        )

    # -- helpers -----------------------------------------------------------------------



def report(journeys: list[Journey]) -> str:
    """The table, plus what the model is handed before any of it happens."""
    catalogue = _catalogue_size()
    lines = [
        "One conversation's characters of compact JSON, per journey and arm.",
        "",
        f"Served before the first turn: tool catalogue {catalogue:,}, "
        f"orchestration prompt {len(orchestration.instructions()):,}, "
        f"training judgment {len(orchestration.training_judgment()):,}.",
        "",
        f"{'journey':38s} {'arm':10s} {'calls':>5s} {'sent':>9s} {'received':>9s} "
        f"{'total':>9s}  {'delta':>9s}",
    ]
    by_name: dict[str, dict[str, Journey]] = {}
    for journey in journeys:
        by_name.setdefault(journey.name, {})[journey.arm] = journey
    for name, arms in by_name.items():
        reference = arms.get("reference")
        for arm in ("reference", "candidate"):
            journey = arms.get(arm)
            if journey is None:
                continue
            delta = (
                f"{journey.total - reference.total:+,}"
                if arm == "candidate" and reference is not None
                else ""
            )
            lines.append(
                f"{name:38s} {arm:10s} {len(journey.calls):5d} {journey.sent:9,} "
                f"{journey.received:9,} {journey.total:9,}  {delta:>9s}"
            )
    paired = [item for item in by_name.values() if len(item) == 2]
    if len(paired) > 1:
        total_reference = sum(item["reference"].total for item in paired)
        total_candidate = sum(item["candidate"].total for item in paired)
        lines += [
            "",
            f"{'every journey above':38s} {'reference':10s} {'':5s} {'':9s} {'':9s} "
            f"{total_reference:9,}",
            f"{'':38s} {'candidate':10s} {'':5s} {'':9s} {'':9s} {total_candidate:9,}  "
            f"{total_candidate - total_reference:+,}",
        ]
    return "\n".join(lines)


def _catalogue_size() -> int:
    from garmin_coach_loop.mcp_transport import TOOLS

    return _size([tool.descriptor() for tool in TOOLS])
