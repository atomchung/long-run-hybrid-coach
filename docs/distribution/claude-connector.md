# Claude: a custom connector, and no directory submission

**Owner decision: the Connectors Directory is out of scope.** Nothing here is submission
material, and there is no checklist to work through — this file records the decision, why
it costs nothing, and what would have to change to revisit it.

## Claude users can use this today

Any claude.ai account, **including Free**, adds `https://mcp.paceandstaystrong.com/mcp` as a
custom connector and authorizes with Intervals. That is the whole setup. A directory listing
and a custom connector run on the same infrastructure and expose the same tools with the
same access; listing changes only whether a stranger can *find* it.

So the honest answer to "can Claude users use this" is yes, now, with no listing and no
waiting. The claude.ai connector is in fact the one entry already verified end to end
against production — a real authorization, a real coaching turn, a real Intervals delivery.
The setup steps are in [`../../entrypoints/claude/README.md`](../../entrypoints/claude/README.md);
they are not repeated here.

## Why the Directory is skipped

The submission portal lives under an organization's admin settings. A personal Pro or Max
account has no entry to it at all — it needs a Team or Enterprise organization, and Team has
a two-seat minimum, which puts a floor of roughly 40–50 USD per month on being listed.

That is a real price for discovery alone, on a product whose Claude entry already works for
free. The owner's decision is to skip it.

## What would have to change to revisit it

Only the account gate. Nothing about the server would need work: it already speaks
streamable HTTP over one hosted URL, authorizes with OAuth and dynamic client registration,
separates read tools from write tools, and gives every tool a title and accurate
annotations — which is the substance of what a review checks.

The two prerequisites that are not about the server, and would have to be met first, are
shared with any public listing and are written down once in [`README.md`](README.md): the
Intervals.icu application has to be visible to all users before a reviewer can authorize at
all, and the canonical site URLs have to answer.

## Not a uniqueness claim

Other Garmin-related MCP connectors exist in this ecosystem, at least one of which writes
structured workouts to a calendar. Issue #98 left the survey of them unfinished and the
owner recorded that it no longer gates anything, so nothing this product publishes claims to
be the only or the first anything.
