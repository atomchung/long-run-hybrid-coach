# Independent coach products

One athlete may connect to two independent coaches. Each coach owns its identity
registry, records, current PlanState, confirmations and delivery history. Shared
code continues to own training contracts, validation, approval and provider
delivery. A client name never determines the product; deployment configuration
does. There is no automatic data or plan synchronization.

## The health data switch

Whether a deployment reads health data is a setting of its own,
`GARMIN_COACH_LOOP_HEALTH_DATA=on|off`, separate from the product that decides whose
records it holds. Unset, it follows the product: `on` for `hybrid`, `off` for `training`.
Any other value refuses startup. `/readyz` reports it as `health_data`.

Authorization does not follow the switch. Every deployment asks Intervals for the same
four scopes, `WELLNESS:READ` included, so turning health data on later needs no athlete
to consent again. The switch decides what the server reads, keeps and hands the model.

With health data off a deployment never reads, accepts or keeps the categories a
directory review named as health information: sleep, HRV, resting heart rate, body
measurements and reported symptoms. Exercise heart rate, pace, load and how the athlete
says they feel stay, read the same way as with health data on.
`garmin_coach_loop/product_scope.py` owns the boundary, at every layer that could
otherwise reach the data:

| Layer | Health data off |
| --- | --- |
| Provider read | Never requests `/wellness`. The context carries `recovery_not_collected` in `unknowns` instead of a permission or outage note. |
| Catalogue | No `recordBodyMeasurement`; `startCoachSession` has no `red_flags`, `all_clear` or `recovery_signals`; `prepareCoachDecision` has no `red_flags` and no recovery or illness reason codes; retraction has no body or recovery kinds. |
| Direct calls | The gateway refuses those fields, tools and codes with `outside_product_scope` before any handler or provider request, so a cached full catalogue cannot reach them. |
| Uploads | An Apple Health export keeps its workouts; its body, recovery, HRV and sleep records are counted under one heading in `not_kept` before any value is parsed. |
| What the model reads | The context is shown without the recovery and symptom fields the contract still requires internally; the instructions and coaching guidance drop the body-composition section and the wearable-recovery sentences. |

Without a symptom field there is no deterministic same-day symptom limit while the switch
is off.
The instructions and tool descriptions still say that pain, illness, chest pain, dizziness
or unusual symptoms need a human decision before training, and that nothing about them is
recorded.

The full surface's served bytes are unchanged; `scripts/mcp_contract_equivalence.py`
holds them. The surface without health data is derived from them by named edits that must
each match the full text exactly once, so a wording change fails the build at startup
rather than shipping an unreviewed sentence. A separate URL or name is not
evidence of directory acceptance; track the submission in
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

## Deploying the training product

1. A separate Railway service from this repository, one replica, with its own volume
   mounted at the state root and its own `GARMIN_COACH_LOOP_TOKEN_HMAC_KEY`.
2. `GARMIN_COACH_LOOP_PRODUCT=training` and `GARMIN_COACH_LOOP_HEALTH_DATA=off`, plus the
   same Intervals client id and secret the hybrid service uses. A second coach does not
   need a second Intervals application. The switch is set explicitly even though `off` is
   the training default, so the deployment's variables show where it stands.
3. Register `https://<training domain>/oauth/callback` as a redirect URI on that Intervals
   application, beside the hybrid one.
4. Build its release identity with
   `python3 scripts/release_bundle.py build --health-data off --gateway-domain https://<training domain> ...`:
   readiness compares the catalogue the switch serves, which is not the full digest.
5. Read `/readyz` back: `deployment_product` is `training`, `health_data` is `off`, and
   `status` is `ok`.

The catalogue and instructions without health data are a new reviewed surface. Before a
submission they need a real client acceptance, Scan Tools and their own plugin version, as
`scripts/change_gates.py` reports for `product_scope.py`.

## Flipping the switch

No code change and no athlete action: every connection already holds the full scopes, and
the plan, records and connections stay where the product put them.

1. Set `GARMIN_COACH_LOOP_HEALTH_DATA` to the new value with `--skip-deploys`.
2. Rebuild the release identity with the matching `--health-data` and stage its
   `GARMIN_COACH_LOOP_RELEASE_*` variables. The catalogue digest moves with the switch, so
   a flip without a new identity reports `blocked`, `/readyz` answers 503, and Railway's
   health check keeps the previous deployment serving.
3. Redeploy and read `/readyz` back: `health_data` is the new value and `status` is `ok`.

Turning it off takes effect for every client of that URL once the deployment is live: a
client still holding the full catalogue is refused with `outside_product_scope`. Turning
it on serves the full catalogue to every client of that URL at once, so a directory
listing reviewed without health data needs a new submission before its reviewed surface
matches what the URL serves; until then, do not turn it on for a listed URL.
