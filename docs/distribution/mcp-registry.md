# Official MCP registry

Latest recorded publication: 1.4.6, published 2026-09-13 by manual dispatch after the
entry had sat at 1.4.3 through three production rolls. That gap is why the workflow now
starts on its own once Railway reports a production deployment successful (below). The [1.4.1 release receipts](../releases/1.4.1.md)
show the hardened gate's first verified run.

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

Publishing is `.github/workflows/publish-mcp-registry.yml`, and it starts on its own,
**after** the deployment rather than with it. Railway posts a GitHub deployment status for
every production deployment (`in_progress`, then `success` once the container is up), and
the workflow runs on that `success`, checks out the deployed commit, and asks the fixed
production `/readyz` -- up to ten times, thirty seconds apart -- to confirm it serves that
commit, version and digests before the second job rechecks and publishes. A source merge to
`main` never publishes. Neither does the push that moves `production`: Railway's **Wait for
CI** holds a deployment until every workflow on that commit has finished, so a workflow that
waited for the deployment would hold it forever and then fail it. That is why the trigger
is the deployment's own receipt and not the push.

The manual dispatch remains for a retry: a Registry outage, a status Railway never posted,
or an entry found stale later. Run it on the promoted ref, never on a newer `main`:

```bash
gh workflow run publish-mcp-registry.yml --ref production
```

Both the unprivileged verification job and the publish job read the fixed production
`/readyz` endpoint without redirects. They compare readiness, source commit, product
version, release identity and all four content digests against that checkout. A stale
production deployment refuses publication before authentication. Dispatching a newer docs
commit while production still serves the release also refuses; select the promoted ref.
A rollback that moves `production` to an earlier commit deploys that commit, Railway
reports success, and -- if that commit already carries this trigger -- this publishes what
production then serves, the entry the Registry should carry. A rollback to a commit older
than the trigger gets a successful deployment and no automatic run; dispatch by hand on that
ref, which runs the dispatch-only workflow that commit has. Which revision of a workflow file
GitHub uses for a `deployment_status` run was not verified here, so nothing is claimed about
it; the retry loop lives in the workflow rather than in the gate script so that whichever
commit's gate is checked out, it is called without options it may not know.

Only the publish job has `id-token: write`; authentication remains GitHub OIDC, with no
long-lived credentials. Publisher v1.8.1's Linux amd64 archive is pinned by SHA-256 from
its upstream checksums and release asset digest, verified before extraction or execution.
Checkout and CI Actions are pinned to full commit SHAs. `tests/test_registry_release.py`
holds these boundaries, including stale version/source/digest refusal.

After a successful workflow, read the versioned Registry entry back and compare its
version and remote URL with production. The workflow's success and that read-back are
different receipts; record both in the release status and issue #283.
