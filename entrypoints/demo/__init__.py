"""The public Product Hunt demo: one synthetic athlete, no account, no writes.

This is a separate deployable from the Coach Gateway and shares no runtime with it -- its
own Railway service, its own process, its own secret, no volume, no provider credentials,
and a failure here cannot reach an athlete's plan. What it *does* share is the only thing
worth sharing: the canonical coaching surface. The context contract, the evidence
projection, the PlanState schema and the plan-change projector are imported from
``garmin_coach_loop`` and reused as they are. There is no second coaching engine here, no
second prompt-only coach, and no second set of decision rules.

The demo reads, reasons and previews. It cannot write: no provider call, no calendar
delivery, no OAuth, no account mutation, no deletion, and no path that reaches a real
owner store. ``boundary`` is where that is decided, and it refuses by name rather than by
having quietly omitted the code.
"""

from .config import DemoConfig
from .service import DemoService

__all__ = ["DemoConfig", "DemoService"]
