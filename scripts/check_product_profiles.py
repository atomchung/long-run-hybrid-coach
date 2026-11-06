#!/usr/bin/env python3
"""Check each product's stated fingerprints and selected submission contract."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from garmin_coach_loop.product_profiles import PRODUCTS, ProductProfile, get_profile
from garmin_coach_loop.release_identity import sha256_text


def check_profile(profile: ProductProfile, root: Path = ROOT) -> list[str]:
    """Compare declared release pins and listing tools with the actual default surface."""
    problems = []
    surface = profile.surface()
    if len(surface.tools) != profile.tool_count:
        problems.append("tool count differs from the profile")
    if surface.tool_catalogue_sha256() != profile.tool_catalogue_sha256:
        problems.append("tool catalogue differs from the reviewed fingerprint")
    if sha256_text(surface.instructions()) != profile.instructions_sha256:
        problems.append("instructions differ from the reviewed fingerprint")
    packet_path = root / profile.submission_packet
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    declared = packet.get("tools", {})
    served = {tool.name: tool.descriptor() for tool in surface.tools}
    if set(declared) != set(served):
        problems.append("submission packet and served tool names differ")
    for name in sorted(set(declared) & set(served)):
        annotations = declared[name].get("annotations")
        expected = {key: served[name].get("annotations", {}).get(key)
                    for key in ("readOnlyHint", "openWorldHint", "destructiveHint")}
        if annotations != expected:
            problems.append(f"submission annotations differ for {name}")
    info = packet.get("app_info", {})
    for field in ("display_name", "subtitle", "description"):
        if not isinstance(info.get(field), str) or not info[field].strip():
            problems.append(f"submission {field} is absent")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", choices=PRODUCTS)
    args = parser.parse_args()
    failed = False
    for name in (args.product,) if args.product else PRODUCTS:
        profile = get_profile(name)
        try:
            problems = check_profile(profile)
        except (OSError, ValueError, TypeError) as exc:
            problems = [f"cannot read submission contract: {exc}"]
        failed |= bool(problems)
        print(json.dumps({"product": name, "version": profile.version,
                          "health_data": "on" if profile.health_data else "off",
                          "submission_packet": profile.submission_packet,
                          "passed": not problems, "problems": problems}))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
