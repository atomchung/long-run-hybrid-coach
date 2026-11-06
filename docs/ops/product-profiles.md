# Product release profiles

`GARMIN_COACH_LOOP_PRODUCT` selects one existing product and its release profile:

| Product | Configuration | Default health data | Submission and listing text |
| --- | --- | --- | --- |
| `hybrid` | `garmin_coach_loop/profile_hybrid.py` | on | `chatgpt-app-submission.json` |
| `training` | `garmin_coach_loop/profile_training.py` | off | `chatgpt-app-submission.training.json` |

Each configuration owns its version, default health-data setting, OAuth scopes for each
health setting, reviewed catalogue and instruction fingerprints, and submission packet
reference. The packet owns the listing description; the profile does not duplicate it.
`product_profiles.get_profile` reads these settings, and `product_scope` continues to
build and enforce the actual evidence boundary and MCP surface.

The existing explicit health-data override remains independent of product identity:

| Product | Health data | Requested scopes | Served tools |
| --- | --- | --- | --- |
| hybrid | on | activity, wellness, calendar, settings | 23 |
| hybrid | off | activity, wellness, calendar, settings | 22 |
| training | off | activity, calendar, settings | 22 |
| training | on | activity, wellness, calendar, settings | 23 |

Activity and wellness permissions are read-only; calendar and settings are read/write.
A health override never changes the product's version, registry, store or plan identity.
It does not change an existing provider grant. The restrictions on changing a reviewed
Plugin deployment remain in [independent-coach-products.md](independent-coach-products.md).

## Validate a candidate

```bash
python3 scripts/check_product_profiles.py
python3 scripts/mcp_contract_equivalence.py --base origin/main --product hybrid
python3 scripts/mcp_contract_equivalence.py --base origin/main --product training
python3 scripts/change_gates.py --base origin/main
```

The first command checks each profile's pinned fingerprints, tool count and selected
submission packet's tool names and annotations. The next two read the actual SDK surface
through both protocol eras and compare every tool descriptor, instruction and prompt
against the base commit. CI runs both products separately. Neither a matching tool count
nor a green full-product check substitutes for the training check.

An intentional surface change updates the corresponding profile pin and follows the
normal release gates. Do not change a frozen Plugin deployment or its submitted metadata
to make a current branch's checks pass. An unchanged profile extraction still needs its
OAuth/gateway smoke. The release gate compares the actual tools and prompts for both
products, the health-data filtering code, and the Plugin version, packet selection and
requested permissions. A change to Plugin release metadata requires review even when
the MCP tool descriptions match.

## Build from the selected commit

```bash
python3 scripts/release_bundle.py build --product training \
  --gateway-domain https://your-staging-domain.example \
  --output /tmp/training-release.json
```

`--product` reads the profile from the target Git commit, including its default health
setting, version, scopes and packet. `--health-data on|off` overrides only health data.
The bundle includes this selection and the selected packet's digest for operator review;
release identity retains its existing schema and binds the profile source as part of the
package artifact. The gateway reports the selected version through MCP and `/readyz`.

For a commit before profiles existed, omit `--product` and pass `--health-data` explicitly.
This legacy path keeps the original release identity; it never substitutes this checkout's
new profile into an older release. Deployment configuration must select the same product
and health setting as the bundle. For profile bundles, `verify` compares the observed
product, version and health setting before writing a verification receipt.
