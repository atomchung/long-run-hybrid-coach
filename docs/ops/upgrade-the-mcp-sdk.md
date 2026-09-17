# Upgrade the MCP SDK

One pinned dependency owns the wire behind `/mcp`. Moving it changes what production says
to every connected client — which protocol revisions the handshake agrees to, what an
`initialize` result carries, which JSON-RPC error a refused batch returns — **with no diff
in this repository to review**. That is the whole reason this page exists: the upgrade has
to be a decision with evidence, not something that happens because a version was newer.

Nothing here asks anything of an athlete. A rollout of this kind needs no reconnection, no
OAuth prompt, no extra confirmation and no protocol setting in any client; if a step turns
out to need one, that is a product change and the owner decides it (AGENTS.md, "A step the
athlete has to take is the owner's decision").

## When to upgrade

Two reasons, and the list is closed:

1. **A protocol revision this product should serve that the pinned SDK does not.** The
   accepted set is the SDK's own registry (`mcp_sdk_transport.HTTP_PROTOCOL_VERSIONS`),
   never a list maintained here, so serving a new revision *is* an SDK upgrade and nothing
   else.
2. **A security fix, or a compatibility fix this product would otherwise be broken by** —
   in `mcp` itself or in any distribution `requirements.lock` resolves it to. That set
   includes `cryptography`, `PyJWT`, `starlette` and `pydantic-core`, inside the process
   that holds every connected athlete's Intervals credential.

**Not "latest".** A release with neither reason is not a reason. Tracking latest would
spend the dual-era acceptance run on every upstream patch and would still be answering the
question "was it new?" rather than "did we need it?". The owner can choose the other
policy, but it is a choice to make explicitly and write here, not a default to drift into.

**Who notices.** The owner, at two fixed moments — there is no watcher process, and
pretending otherwise would be the same "somebody notices" this page replaces:

- at every release-lane roll (`docs/ops/roll-with-railway-cli.md` sends you here), one line:

  ```bash
  python3 -m pip index versions mcp
  ```

- whenever an advisory names `mcp` or any distribution in `requirements.lock`.

## The upgrade, in five steps

Each step produces evidence the next one needs. Run them in order; the gate output in step
0 is what says whether the run is required at all.

**0. Classify the change.** From the branch with the moved pin:

```bash
python3 scripts/change_gates.py --base origin/main
```

A moved pin reports `protocol_acceptance: true` and names
`requirements.lock (pinned dependency set moved)`. It does **not** report `scan_tools` or
`plugin_resubmission`: the reviewed tool catalogue and `instructions` are this repository's
own bytes, an SDK cannot move them, and a resubmission triggered by a dependency would put
a release into OpenAI review for a change no reviewer can see. `dependency_pin_base` and
`dependency_pin_head` are printed either way, so a receipt can quote what the decision was
made from. A comment edit in `requirements.txt` moves neither and asks for nothing.

The one case that *does* reach Scan Tools is an upgrade whose SDK changes what it puts on
the wire for bytes this repository owns — a descriptor field dropped or renamed in
serialization, say. `change_gates.py` cannot see that (`tool_catalogue_sha256()` is built
from this repository's own constants, which such an upgrade leaves alone), and it does not
have to: `scripts/mcp_contract_equivalence.py` runs on every pull request, reads the served
surface *through the real transport* on both eras, and compares it with the base ref field
by field. A failure there is the evidence that the reviewed contract really moved, and then
the resubmission rules apply as they would for any catalogue change. A pass is the evidence
that it did not.

**1. Write the reason down** in the tracking issue before touching the pin: which of the
two triggers fired, and what evidence there is for it (the revision a client needs, or the
advisory). "2.3.0 is out" is not an entry.

**2. Regenerate the hashed lock.** Move the pin in `requirements.txt`, then:

```bash
uv pip compile requirements.txt --generate-hashes --universal --python-version 3.11 \
  -o requirements.lock
```

The lock's own header records that command, so the file says how to rebuild itself. Read
the diff: it is the full list of distributions and artifact hashes entering the gateway
image, and a transitive package appearing, disappearing or moving version is part of this
upgrade whether or not `mcp`'s own release notes mention it. `--universal` is what keeps
one lock usable on the image, on CI and on a developer machine; markers decide which lines
install where.

Nothing installs from `requirements.txt`. The Dockerfile and every workflow job install
`requirements.lock` with `--require-hashes`, so a substituted byte fails the build instead
of shipping, and there is deliberately no second unhashed path to fall back to.

**3. Diff the protocol envelope.** Install the candidate somewhere disposable and ask it
what it puts on the wire:

```bash
python3 -m venv /tmp/mcp-candidate
/tmp/mcp-candidate/bin/pip install --require-hashes -r requirements.lock
/tmp/mcp-candidate/bin/python scripts/mcp_protocol_envelope.py
```

It compares what that environment serves against
[`docs/mcp-protocol-envelope.json`](../mcp-protocol-envelope.json), the envelope the
pinned SDK serves today, and prints every difference: the negotiated revision per
requested revision, the `initialize` capabilities and `serverInfo` shape, the
`server/discover` envelope, and the error code and HTTP status for a batch, a malformed
body, an unknown method, an unknown protocol version and a routing-header mismatch. Tool
results and coaching content are deliberately not in it —
`scripts/mcp_contract_equivalence.py` owns that half, and a capture carrying the catalogue
would move on every ordinary release and stop reading as a protocol fact.

A difference is not a defect. It is the upgrade's behavioural delta, read before a client
meets it rather than after. Decide on each line, then record the new envelope in the same
pull request:

```bash
/tmp/mcp-candidate/bin/python scripts/mcp_protocol_envelope.py --update
```

`tests/test_protocol_envelope.py` compares the record against the installed SDK on every
CI run, so a pin that moves without this step fails the suite rather than reaching
production quietly.

Measured while this page was written, so the expected shapes are known: `mcp` 2.0.0, 2.1.1
and 2.2.0 serve an **identical** envelope — only `mcp_sdk_version` differs — and 1.30.0
cannot import `garmin_coach_loop/mcp_sdk_transport.py` at all, failing at the import rather
than on the wire.

**4. Run the dual-era acceptance.**
[`accept-both-protocol-eras.md`](accept-both-protocol-eras.md), in full, against a loopback
gateway on the branch. A 2026-07-28 client that is refused falls back to 2025 silently and
the conversation still works, so this is the only step that catches it. The envelope diff
does not replace it: the diff says what the server answers, the acceptance run says what a
real client does with that answer.

**5. Deploy, then read production back.** `/readyz` reports `mcp_sdk_version` — the release
the running container actually resolved, which is the deployment-side answer to what the
lock pinned. A merged pull request and a green CI run are still the plan;
[`verify-production-status.md`](verify-production-status.md) is the account. Then repeat
step 4 against the live domain, because the loopback run proved the branch, not the
deployment.

## One semantic this repository decided rather than implemented

The hand-written transport refused to agree to `2025-03-26`, because that revision permits
JSON-RPC batching and this server does not implement it: agreeing was judged a promise it
could not keep. The SDK agrees to the revision and refuses the batch itself, with
`INVALID_PARAMS` where the old code said `INVALID_REQUEST`. Both designs refuse the same
request, no client batches, and the alternative — re-adding a local rule the SDK does not
have — is exactly the ownership the migration moved away from (issue #489).

So it stays accepted, and the `jsonrpc_batch` row in the recorded envelope is where it
would show up moving again. If a client ever does batch, that row is the evidence for
reopening the decision.
