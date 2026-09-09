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

Publishing uses the manually dispatched `.github/workflows/publish-mcp-registry.yml`.
A source merge never publishes. First deploy the accepted commit, verify its production
receipt, and dispatch the workflow on that exact commit's branch or tag:

```bash
gh workflow run publish-mcp-registry.yml --ref <accepted-release-ref>
```

Both the unprivileged verification job and the publish job read the fixed production
`/readyz` endpoint without redirects. They compare readiness, source commit, product
version, release identity and all four content digests against that checkout. A stale
production deployment refuses publication before authentication. Dispatching a newer docs
commit while production still serves the release also refuses; select the accepted ref.

Only the publish job has `id-token: write`; authentication remains GitHub OIDC, with no
long-lived credentials. Publisher v1.8.1's Linux amd64 archive is pinned by SHA-256 from
its upstream checksums and release asset digest, verified before extraction or execution.
Checkout and CI Actions are pinned to full commit SHAs. `tests/test_registry_release.py`
holds these boundaries, including stale version/source/digest refusal.

After a successful workflow, read the versioned Registry entry back and compare its
version and remote URL with production. The workflow's success and that read-back are
different receipts; record both in the release status and issue #283.
