"""Routing and safety contract for the production-release operator skill."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "custom-gpt-production-release" / "SKILL.md"
OPENAI_YAML = ROOT / ".agents" / "skills" / "custom-gpt-production-release" / "agents" / "openai.yaml"


class CustomGptProductionReleaseSkillTests(unittest.TestCase):
    def test_routing_and_release_boundary_are_explicit(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("name: custom-gpt-production-release", text)
        for trigger in ("deploy", "promote", "update", "repair", "rollback", "verify parity"):
            self.assertIn(trigger, text)
        for excluded in ("coaching", "PlanState", "workout operations", "ordinary pull-request review"):
            self.assertIn(excluded, text)
        self.assertIn("exactly one production Custom GPT", text)
        self.assertIn("GitHub `main`\ncommit with green CI is only a candidate", text)

    def test_operator_must_use_deterministic_contracts_and_manual_checkpoint(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("scripts/custom_gpt_release.py", text)
        self.assertIn("scripts/custom_gpt_deploy.py", text)
        self.assertIn("human manually copies", text)
        self.assertIn("explicit user confirmation", text)
        self.assertIn("external release home", text)
        self.assertIn("Gateway secret file", text)
        self.assertIn("production GPT Vercel or Builder permissions", text)
        self.assertIn("must not claim it made that edit", text)
        self.assertIn("Never roll back PlanState", text)
        self.assertIn("external receipt", text)

    def test_ui_metadata_names_the_same_skill(self):
        text = OPENAI_YAML.read_text(encoding="utf-8")
        self.assertIn('display_name: "Custom GPT Production Release"', text)
        self.assertIn("$custom-gpt-production-release", text)


if __name__ == "__main__":
    unittest.main()
