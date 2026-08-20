# Official MCP registry

`registry.modelcontextprotocol.io`, the protocol's own registry. It matters more than its
size suggests: the other directories consume it, so one entry here is the upstream several
listings are built from rather than a fourth place to keep in step. The shared facts —
identity, URLs, OAuth, scopes, the tool table, the reviewer path — are in
[`README.md`](README.md).

Requirements below are from the registry's own `server.json` specification and its official
requirements document, read 2026-08-20.

## Shape

One file, [`../../server.json`](../../server.json) at the repository root, published by a
CLI. There is no form and no human review: the registry validates the document, checks that
the publisher owns the namespace, and stores it.

**This product declares a remote and no package.** That removes the heaviest part of the
requirements: package-ownership verification, and the rule restricting packages to trusted
public registries, both apply only to entries that ship one. A remote entry states its
transport and its URL, and the authorization is discovered from the endpoint the way every
other client discovers it — the registry document carries no credential and no header.

## The namespace decision

The registry requires proof that the publisher owns the namespace, and there are two this
product could claim:

| Namespace | Proof | Cost |
| --- | --- | --- |
| `io.github.atomchung/long-run-hybrid-coach` | signing in as that GitHub account | one command |
| `com.paceandstaystrong/long-run-hybrid-coach` | a DNS record on the domain | a registrar change, plus a key to keep |

The first is what [`../../server.json`](../../server.json) declares. The domain form reads
better next to the brand and is the right move later; it is not worth blocking a first
listing on a DNS change, and a namespace can be added rather than migrated.

## Two fields that get an entry rejected or, worse, quietly stale

- **The description is capped at 100 characters**, and so is the title. The description
  field carries the settled public one-liner, which fits; anything longer is refused at
  submit time rather than truncated.
- **The version and the endpoint are read from the product, not typed here.**
  `RegistryEntryTests` in `tests/test_distribution_surface.py` asserts the published version
  against the running one and the published URL against the gateway's own path. A registry
  keeps whatever it was last given, so a stale entry is not a failing build anywhere — it is
  a listing that describes a release nobody runs. That is the failure this test exists to
  make loud.

---

## Operator checklist

Publishing is automated, and that is the point rather than a convenience: the registry
keeps whatever it was last given, so anything a person has to remember to re-run is a
listing that eventually describes a release nobody is running.

`.github/workflows/publish-mcp-registry.yml` publishes when `server.json` changes on `main`,
and authenticates with the workflow's own OIDC token — **no interactive sign-in, no stored
credential, no personal access token**. GitHub asserting which repository the job runs in is
what proves the namespace, which is the same claim signing in by hand would make.

So the standing operator job is two reads, not a publish:

1. **After the first run**, read the entry back from the registry rather than trusting the
   workflow's output, and confirm the version it shows is the one `/readyz` reports on the
   live domain. Verification is against the live service, here as everywhere else.
2. **After any release**, the same read. The version moves with `PRODUCT_VERSION`, so a
   release that changed it should show the new number within a run.

Publishing by hand is the fallback when the workflow cannot run — `mcp-publisher login github`
opens a browser device flow, then `mcp-publisher publish` from the repository root. Prefer the
workflow: a hand-published entry is one nobody will remember to update.
