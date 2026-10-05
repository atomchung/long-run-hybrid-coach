# Independent coach products

One athlete may connect to two independent coaches. Each coach owns its identity
registry, records, current PlanState, confirmations and delivery history. Shared
code continues to own training contracts, validation, approval and provider
delivery. A client name never determines the product; deployment configuration
does. There is no automatic data or plan synchronization.

## Foundation and release boundary

This foundation establishes storage and authorization isolation. Both identities
still serve the existing full coaching catalogue, instructions and provider scopes.
**The training identity is not yet the restricted OpenAI product and must not be
submitted as one.** Its allowed evidence, tools, OAuth scopes, public descriptions
and independent user journey must be implemented and verified in the next task.
A separate URL or name is not evidence of OpenAI acceptance. Track the work in
[issue #519](https://github.com/atomchung/long-run-hybrid-coach/issues/519).

## Deployment configuration

`GARMIN_COACH_LOOP_PRODUCT` accepts exactly `hybrid` or `training`:

| Configuration | Effective state root | Existing connections |
| --- | --- | --- |
| absent or `hybrid` | `GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT` | Existing layout and grants remain valid |
| `training` | `<configured base>/products/training` | Fresh independent registry and connection |

Startup binds the effective root using `product-identity.json` before it opens the
registry or reclaims owner locks. Hybrid can bind an existing unmarked legacy
root. Training requires an empty root on its first startup; it will not silently
adopt copied history. A mismatched or unreadable marker refuses startup. A
symlink cannot redirect the training namespace to another store.

Deploy the products as separate services with separate HTTPS domains, private
volumes and signing keys, one replica per service. Do not change the existing
production service's product identity or move its store. No DNS, deployment or
submission is performed by this foundation change. `/healthz` and `/readyz`
include `deployment_product`; normal release identity and readiness evidence
remain necessary.

OAuth state, registration, authorization codes and access tokens are product
bound, including when deployments accidentally share a signing key and audience.
Legacy unlabelled envelopes belong only to hybrid. Confirmation bindings are
also product specific. A second coach does not require revoking the first coach's
provider grant.

## Plans and provider delivery

New training plans include the product namespace when deriving their plan ID.
Two coaches creating the same prescription at the same instant therefore own
different provider event IDs. They cannot update the other's events through a
matching generated ID. Both can nevertheless put workouts on the same athlete's
calendar: independent event ownership does not coordinate two training plans.

The first public experience should describe choosing a primary coach for calendar
delivery. Any extra selector, confirmation or migration step needs an owner
choice about the concrete client experience before implementation. There is no
new athlete step in this foundation.

## Operator commands

Athlete-scoped maintenance commands (`adopt-owner-store`, `import-store`,
`archive-store` and `privacy-request-*`) interpret `--state-root` and the state-root
environment variable as the configured **base**. They use the same
`GARMIN_COACH_LOOP_PRODUCT` as the gateway and verify the root's product marker
before resolving the athlete. Keep the deployment's product and signing-key
configuration together when running these commands. Training exports and
account-deletion receipts use the same account reference as its gateway.

Explicit `--state-dir` commands and low-level `delete-owner` remain literal path
operations; the latter also requires its explicit identity database. They do not
infer a deployment from the environment. These are operator tools, not an
athlete-facing migration or synchronization route.

## Next task

Define and implement the training product's accepted evidence and capability
boundary using the Review Team's clarification. Share acquisition and coaching
machinery where their behavior is common, while each product exposes only its
own complete contract. Verify the real client flow and provider authorization,
then prepare its own release, public information, demo and submission evidence.
