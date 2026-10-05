"""Deployment-owned product identity; client names never select an athlete's store.

The hybrid product keeps its existing layout. The training product starts in an
independent namespace, including its identity registry, records and PlanState. This
module does not select coaching capabilities or assert distribution eligibility.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


PRODUCT_ENV_VAR = "GARMIN_COACH_LOOP_PRODUCT"
DEFAULT_PRODUCT = "hybrid"
PRODUCTS = frozenset({DEFAULT_PRODUCT, "training"})
MARKER_NAME = "product-identity.json"


class ProductIdentityError(ValueError):
    """A deployment must not use an unknown product or another product's data."""


def validate_product(product: str) -> str:
    """Accept only explicitly supported identities, never arbitrary path components."""
    if product not in PRODUCTS:
        raise ProductIdentityError("product must be hybrid or training")
    return product


def product_state_root(base: Path, product: str) -> Path:
    """Return a product's resolved storage root without creating or migrating data."""
    validate_product(product)
    if product == DEFAULT_PRODUCT:
        return base
    target = base / "products" / product
    # A mounted volume is trusted, but a reused/symlinked namespace must not silently
    # point the new product at the legacy root or at a sibling product's files.
    if target.resolve() != base.resolve() / "products" / product:
        raise ProductIdentityError("product namespace must not redirect to another path")
    return target


def bind_product_state(root: Path, product: str) -> None:
    """Bind a storage root before opening its registry or reclaiming owner locks.

    Only the legacy hybrid product may adopt an unmarked existing store. A new
    product must start empty; copying another coach's registry/history is not a
    migration this operation authorizes. The exclusive marker creation also makes
    two simultaneous starts agree on one identity or refuse.
    """
    validate_product(product)
    marker = root / MARKER_NAME
    expected = {"schema_version": 1, "product": product}
    if not marker.exists():
        if product != DEFAULT_PRODUCT and any(root.iterdir()):
            raise ProductIdentityError("new product requires an empty, unbound storage root")
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(expected, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
    verify_product_state(root, product)


def verify_product_state(root: Path, product: str) -> None:
    """Read an existing binding; only legacy hybrid may be unmarked."""
    validate_product(product)
    marker = root / MARKER_NAME
    expected = {"schema_version": 1, "product": product}
    if product == DEFAULT_PRODUCT and not marker.exists():
        return
    try:
        stored = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProductIdentityError("product storage binding is unreadable") from exc
    if stored != expected:
        raise ProductIdentityError("storage root belongs to a different product")


def token_product(payload: dict, product: str) -> bool:
    """Legacy envelopes belong to hybrid; training must carry its explicit identity."""
    return payload.get("product", DEFAULT_PRODUCT) == validate_product(product)


def product_claims(payload: dict, product: str) -> dict:
    """Seal the configured identity while preserving legacy hybrid envelope semantics."""
    validate_product(product)
    if product == DEFAULT_PRODUCT:
        return dict(payload)
    return {**payload, "product": product}


def owner_binding_subject(owner_id: str, product: str) -> str:
    """Keep legacy references stable and bind new references to their product."""
    validate_product(product)
    return owner_id if product == DEFAULT_PRODUCT else f"{product}:{owner_id}"
