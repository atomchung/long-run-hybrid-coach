"""Select existing product differences without selecting an athlete or moving state.

Each profile module owns its release settings and reviewed surface pins. Actual MCP
bytes still come from product_scope, and listing prose still comes from the referenced
submission packet. A profile does not grant a permission or change an existing token.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import profile_hybrid, profile_training
from .product_identity import validate_product


PRODUCTS = ("hybrid", "training")
_MODULES = {"hybrid": profile_hybrid, "training": profile_training}


@dataclass(frozen=True)
class ProductProfile:
    product: str
    version: str
    health_data: bool
    oauth_scopes_on: tuple[str, ...]
    oauth_scopes_off: tuple[str, ...]
    submission_packet: str
    tool_count: int
    tool_catalogue_sha256: str
    instructions_sha256: str

    def effective_health_data(self, override: bool | None = None) -> bool:
        return self.health_data if override is None else override

    def oauth_scopes(self, health_data: bool | None = None) -> tuple[str, ...]:
        return self.oauth_scopes_on if self.effective_health_data(health_data) else self.oauth_scopes_off

    def surface(self, health_data: bool | None = None):
        # Lazy import keeps profile configuration independent of the MCP implementation.
        from .product_scope import surface
        return surface(self.effective_health_data(health_data))


def get_profile(product: str) -> ProductProfile:
    """Read one immutable configuration; identity validation remains the existing rule."""
    name = validate_product(product)
    config = _MODULES[name]
    return ProductProfile(
        product=name, version=config.VERSION, health_data=config.HEALTH_DATA,
        oauth_scopes_on=config.OAUTH_SCOPES_ON, oauth_scopes_off=config.OAUTH_SCOPES_OFF,
        submission_packet=config.SUBMISSION_PACKET, tool_count=config.TOOL_COUNT,
        tool_catalogue_sha256=config.TOOL_CATALOGUE_SHA256,
        instructions_sha256=config.INSTRUCTIONS_SHA256,
    )
