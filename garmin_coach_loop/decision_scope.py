"""Declared plan-authoring scope shared by the projector and the tool catalogue.

Scope is intent supplied by the coach and shown for the athlete to confirm. It is
never inferred from an adaptation, a symptom, or a training threshold. The legacy
omission path is kept in plan_change for clients holding the previous catalogue.
"""

DECISION_SCOPE_MODES = {"week": "review_week", "cycle": "review_cycle"}

DECISION_SCOPE_SCHEMA = {
    "type": "string",
    "enum": list(DECISION_SCOPE_MODES),
    "description": (
        "The scope the athlete is deciding, shown in the confirmation preview. week "
        "keeps the goal and cycle direction fixed; it may update the remaining outlook. "
        "cycle reassesses the goal or 28-day direction and may update this week's "
        "sessions in the same decision. A first plan uses cycle. Declare intent, not "
        "a scope chosen merely to make a proposed diff pass validation."
    ),
}
