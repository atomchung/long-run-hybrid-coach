"""Keep the public plugin package aligned with the canonical submission facts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "long-run-hybrid-coach"
MANIFEST = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
MCP_CONFIG = PLUGIN_ROOT / ".mcp.json"


class PublicPluginPackageTests(unittest.TestCase):
    def test_mcp_only_package_has_no_skill_or_registered_app_copy(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "long-run-hybrid-coach")
        self.assertEqual(manifest["interface"]["displayName"], "Long Run Hybrid Coach")
        self.assertEqual(
            manifest["interface"]["shortDescription"],
            "Adaptive run and strength plan",
        )
        self.assertNotIn("skills", manifest)
        self.assertNotIn("apps", manifest)
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")

    def test_mcp_config_points_at_the_universal_production_endpoint(self):
        config = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
        server = config["mcpServers"]["long-run-hybrid-coach"]

        self.assertEqual(server["type"], "http")
        self.assertEqual(server["url"], "https://mcp.paceandstaystrong.com/mcp")
        self.assertNotIn("token", json.dumps(config).lower())
        self.assertNotIn("secret", json.dumps(config).lower())


if __name__ == "__main__":
    unittest.main()
