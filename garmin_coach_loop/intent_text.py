"""An intent line may say what a session is for; it may not prescribe (issue #99).

`purpose` is the one field the Coach still writes in its own words. It carries intent
("hold aerobic base this week"), it titles the calendar entry a strength session reaches
the watch as, and nothing derives anything from it. Issue #93 deleted the layer that
did -- eleven regular expressions re-reading the plan's own numbers out of the sentence
reporting them -- and `FreeTextLayerCannotGrowBackTests` fails the build if any
expression in `validation` reads a free-text value again. That guard is right, and this
module does not loosen it: the read left the validator instead, the way the delivery
projection did, and the rule travels with it.

## Why this check exists at all (AGENTS.md 6)

**The invariant.** Every number the athlete is asked to execute has an anchor the
evidence gate can read. `session.plan` is where numbers live, and `validation` checks
each one against the baseline that vouches for it: a pace against a measured threshold,
a load against a measured lift. A number written into `purpose` has no such anchor and
never passes that gate, because nothing reads it.

**The harm, observed.** Issue #38: `5x1000m @5:50/km` -- 20 sec/km faster than the
measured threshold -- sat in `purpose` for two days and reached the athlete unexamined,
because the pace check of the day scanned `prescription` and this field was beside it.
Issue #93 removed the surface for `prescription` by generating it; `purpose` stayed
authored, so the same incident is reachable again through the same field.

**Why a warning is not enough.** The athlete reads `purpose` first and the watch shows
it as the calendar title. A warning is addressed to the Coach, which is the party that
wrote the number; the athlete never sees it and the watch cannot.

**Why model judgment is not enough.** SKILL.md has said "the numbers live in `plan`"
and "check every pace against the anchor it claims" for two issues now. Issue #93's
whole argument is that instructions about prose have already failed five times where a
mechanism would not have. This is the mechanism for the one field prose still is.

**Why not a narrower capability boundary.** Removing the field, or picking its wording
from a fixed vocabulary, is issue #99's option 2/3. It costs the Coach the only place it
speaks in its own words, and it costs the athlete the strength calendar title they
actually read -- "上拉為主的上肢課" is not expressible in a bounded vocabulary, and every
stored plan would have to be regenerated to adopt one.

**What stays possible.** Everything an intent line is for. A digit alone is legal:
"維持 Zone 2 有氧基礎", "本週第 3 次長跑", "第 2 組加重" all pass, and so does every
purpose this repository has ever stored. Refused is exactly one shape -- a number
wearing a prescription's unit, or the tail of a pace -- and the same number is fully
expressible one field over, in `plan`, where its anchor is checked.

**The false-positive cost.** An intent line that genuinely wants to name a distance or a
clock time is refused and has to be reworded; the wording is not sanitised for the
athlete behind their back, because a title they never approved is worse than a rewrite
the Coach is told about. A duration in minutes, a set count and a rep count are not
refused: they carry no baseline anchor to smuggle past, and drawing the line at the
units the evidence gate actually reads keeps the rule narrow enough to explain in one
sentence.

## What this module may do, and what pins it

It notices a token and hands the text back. It never turns one into a number, never
reaches a baseline, and never decides anything about training -- which is the whole
difference between refusing prose and the layer that read it.
`FreeTextLayerCannotGrowBackTests` walks this module's syntax tree too and holds it to
that: one pattern, no numeric conversion, nothing imported from the package it protects.
"""

from __future__ import annotations

import re
from typing import Any


#: A number wearing a prescription's unit, or the tail of a pace however it is spelled.
#: Deliberately not a parser: the alternatives are shapes, they capture no groups, and the
#: only thing done with a match is showing it back to whoever wrote it.
#:
#: The bare metre ends in `(?![A-Za-z])` rather than `\b`, because the athlete titles a
#: session in Chinese and CJK is a word character to `re`: `\b` would let `800m重複跑`
#: through while catching `800m 重複跑`, which is the same prescription with a space in it.
#: What the lookahead does exclude is a longer English word, so `5 minutes` stays intent.
_PRESCRIBED_TOKEN = re.compile(
    "|".join((
        r"\d+\s*:\s*\d{2}",
        r"\d+(?:\.\d+)?\s*(?:%|kg|km|mtr|bpm|公斤|公里|公尺|米|m(?![A-Za-z]))",
        r"/\s*(?:km|公里)",
    )),
    re.IGNORECASE,
)


def prescribed_token_in_intent(session: Any) -> str | None:
    """The prescription token a session's intent line carries, or ``None``.

    Takes the whole session rather than the value: the field name belongs to the
    validator's error message, and the validator is the one module that may not name it.
    """
    if not isinstance(session, dict):
        return None
    intent = session.get("purpose")
    if not isinstance(intent, str):
        return None
    found = _PRESCRIBED_TOKEN.search(intent)
    return found.group() if found else None
