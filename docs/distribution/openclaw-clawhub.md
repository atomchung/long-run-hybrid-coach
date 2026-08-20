# OpenClaw / ClawHub

The listing metadata for publishing the canonical Skill on ClawHub, pointed at the same
hosted endpoint. The shared facts — identity, URLs, OAuth, scopes, data flow, the tool table,
the logo, the reviewer path, the test cases — are in [`README.md`](README.md). The connection
mechanics and the Skill install are already written once, in
[`../../entrypoints/openclaw/README.md`](../../entrypoints/openclaw/README.md); this file is
only what a submission asks for that is not there. Issue #133 tracks the work.

Requirements below are from ClawHub's own publishing, skill-format and CLI reference, read
2026-08-20.

## Shape

Unlike the other two, this listing is a **Skill** rather than a server: ClawHub skills are a
folder with `SKILL.md` plus optional supporting files, which is the format
[`../../.agents/skills/garmin-coach-loop/`](../../.agents/skills/garmin-coach-loop/) is
already published in. The Skill is referenced or copied at listing time and never forked, so
a later change here is picked up by re-syncing rather than by editing the published copy.

There is no server to submit and nothing to review by hand. Publishing is a CLI call against
a registry, and the gatekeeping is a GitHub account old enough to pass the upload gate plus
an automated security scan — not a human reviewer working through a form, which is what the
OpenAI path is. The MCP endpoint is the same `https://mcp.paceandstaystrong.com/mcp` every
other entry uses, and it is configured by the athlete on their own OpenClaw rather than
carried in the listing.

## Field mapping

| Field | Value | Source |
| --- | --- | --- |
| `--slug` | `long-run-hybrid-coach` | this file; see below |
| `--name` | `Long Run Hybrid Coach` | the packaging file's `display_name` |
| `--version` | the product version in `garmin_coach_loop/gateway.py` | one source, never retyped |
| `--owner` | the publishing handle | operator |
| `--categories` | `lifestyle` | the category both comparable listings use |
| `--topics` | `running`, `strength-training`, `training-plan`, `intervals-icu`, `garmin` | this file |
| `--changelog` | what changed in this version | operator |
| Skill payload | [`../../.agents/skills/garmin-coach-loop/`](../../.agents/skills/garmin-coach-loop/) | canonical |
| Description | the frontmatter `description`, which is also the trigger | canonical |
| Homepage | `https://paceandstaystrong.com/` | [`README.md`](README.md) |

**The slug is not the Skill's name, and that is deliberate.** The listing slug is public and
the product name does not contain Garmin; the frontmatter `name` is `garmin-coach-loop`
because it is the trigger every other entry already installs and invokes, and renaming it
would be a change to the canonical Skill made for one directory's URL. They are allowed to
differ, so they do.

**Topics** are free-form, at most five, at most 48 characters each, and reserved names
(`official`, `verified`, `trusted-publisher`) are refused. **Categories** come from a slug
list ClawHub owns and does not publish in its reference, so it is read off the shelf instead:
the two closest listings on it — an adaptive plan generator built on one provider's data, and
an endurance coaching skill — are both filed under Lifestyle. At most three are accepted, and
a first publish naming none defaults to `other`, which is a worse listing than a category
corrected on the next version. Confirm the exact slug with the dry run.

Versions are semantic. A first publish starts at `1.0.0` and later ones auto-increment the
patch unless a version is named, so passing the product version explicitly is what keeps the
listing and the release the same number.

## The version question, and the rule that settles it

One rule covers this and every platform after it: **a directory's requirement is met from
what the product already declares, or it is met on the command line — never by adding a
field to the canonical Skill for one directory.** That file is installed by every entry, so
a field added for one of them is carried by all of them, and the second directory that wants
something slightly different is how a canonical file becomes a per-platform file.

So the release number reaches ClawHub through `--version`, read from the same constant the
gateway serves, and the frontmatter stays `name` and `description`. ClawHub's skill-format
page lists `version` among its required fields while its own quickstart publishes without
one; `--dry-run` says which reading is enforced, and costs nothing to ask. If the frontmatter
field does turn out to be mandatory, it goes in as *the product's* version — one line, wired
to that same constant, true for every entry that reads the file — and not as a ClawHub field.

## The licence is not ours to choose here

`All skills published on ClawHub are licensed under MIT-0`, and the registry does not support
per-skill overrides — a licence line in `SKILL.md` claiming otherwise is a conflict rather
than an exception. Keeping this repository MIT does not resolve it: the repository stays MIT,
and the published copy of the Skill is MIT-0. The difference is one clause. MIT permits reuse
on the condition that the copyright notice travels with the copy; MIT-0 deletes the
condition, so a copier may ship the file with no attribution at all.

What that exposes is bounded, and worth knowing before deciding: the Skill carries the
trigger and the loop, and deliberately nothing else. The sequencing, the field meanings and
the training judgment are served over the connection at run time, which is why the file is
installed rather than forked. MIT-0 on it hands a copier the skeleton, not the coach.

## An install is two parts, and the listing carries one

ClawHub skill metadata has no field for an MCP server. `metadata.openclaw` declares
environment variables, binaries, config file paths and dependency installs — none of them
names an endpoint. Neither does the Skill, on purpose: where the plan lives is settled by the
athlete's setup rather than by the file. So a listing installs the trigger and the loop, and
the connection is a second step:

```bash
clawhub install @<owner>/long-run-hybrid-coach
```

```bash
openclaw mcp add garmin-coach-loop \
  --url https://mcp.paceandstaystrong.com/mcp \
  --transport streamable-http \
  --auth oauth
openclaw mcp login garmin-coach-loop
```

Three consequences, all of them now handled:

- **The Skill says so when it arrives alone.** Installed-and-unconnected is a state it names
  and declines to coach from, instead of reading as a broken install or — the worse
  outcome — being answered out of the file. That is not a ClawHub fix: the same state exists
  on every path that installs the Skill before the connection, and it had no answer until
  this one.
- **The homepage field is load-bearing.** It is the only route from a listing page to those
  connection steps, which is why the Skill declares one. The spec's own optional field, a
  fact about the product, not a field invented for this directory.
- **A directory that lists servers fits this product better than one that lists skills.**
  That is why [`hermes-agent.md`](hermes-agent.md) sits beside this file rather than
  replacing it.

Collapsing it to one step would mean shipping an OpenClaw plugin package — an npm artifact,
built and maintained for one platform, to save one command. That is the platform-specific
adaptation this repository does not do.

## What has to be true before an OpenClaw client can connect

One thing, and it is a configuration value rather than code: a hosted OpenClaw's callback
origin has to be trusted by the deployment, or dynamic client registration is refused with a
reason saying so. Loopback callbacks need no entry, so an OpenClaw instance running on the
athlete's own machine works as-is; a hosted OpenClaw deployment needs its origin added
through `GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` — "Admitting a new hosted client" in
[`../deploy-gateway.md`](../deploy-gateway.md).

The client-side settings that make that connection work — the OAuth key the gateway refuses
a connection without, and the identity setting that decides whether one instance means one
athlete — are in [`../../entrypoints/openclaw/README.md`](../../entrypoints/openclaw/README.md).

Nothing has been verified through a real OpenClaw client yet: no real OAuth authorization, no
real coaching turn, no real delivery. [`../../entrypoints/README.md`](../../entrypoints/README.md)
is the status table, and it says exactly that.

---

## Operator checklist

1. **Prove the domain answers**, with the loop in
   [`openai-plugin.md`](openai-plugin.md)'s first step. Four `200`s, or stop: the homepage
   above and everything a listing links to are on that domain.
2. **Confirm the upstream authorization is open.** Already proven: a second Intervals
   account completed the consent screen and reached a coaching turn on 2026-08-18, so the
   application is grantable beyond the owner. Re-verify only if the registration changed
   since — [`README.md`](README.md).
3. **Roll production to `main`** and confirm `/readyz` is `"status": "ok"` at `main`'s head.
4. **Connect once from a real OpenClaw client** before listing anything. If it runs on the
   athlete's own machine, nothing needs configuring. If it is hosted, find the callback
   origin its registration attempt is refused for, validate the flow, then add that origin to
   `GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS` in the service variables and redeploy. Worked
   when a coaching turn returns a plan through that client.
5. **Run the test cases** in [`README.md`](README.md) through that client. Case 5 is the one
   that proves the delivery path; do it against a test account, not a real athlete's calendar.
6. **Update the entry status table** in [`../../entrypoints/README.md`](../../entrypoints/README.md)
   from "packaged, awaiting real-connection verification" to verified — but only after a real
   authorization, a real coaching turn and a real delivery have all run.
7. **Accept the MIT-0 term above**, then sign in: `clawhub login`, `clawhub whoami`. The
   handle it reports is the `--owner` value, and the GitHub account behind it has to be old
   enough to pass the upload gate.
8. **Dry-run the publish.** Nothing is uploaded, and this is where an unknown category slug
   or a missing frontmatter field is reported:

   ```bash
   clawhub skill publish .agents/skills/garmin-coach-loop \
     --slug long-run-hybrid-coach \
     --name "Long Run Hybrid Coach" \
     --version "$(python3 -c 'from garmin_coach_loop.gateway import PRODUCT_VERSION; print(PRODUCT_VERSION)')" \
     --categories lifestyle \
     --topics "running,strength-training,training-plan,intervals-icu,garmin" \
     --changelog "Initial release" \
     --dry-run
   ```

9. **Publish** by re-running that command without `--dry-run`, correcting the category slug
   if step 8 refused it. Validation is all-or-nothing: if it fails, nothing was published
   and there is no half-listing to clean up.
10. **Expect the release to be held.** A new version stays out of the normal install surfaces
    until the automated scan finishes, and a held release is visible to its owner in the
    dashboard rather than to installers. Confirm it appears for a real install before saying
    the entry is listed — the calendar read-back rule, one layer out.
