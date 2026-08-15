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

    def test_operator_must_use_deploy_state_checkpoints_and_confirmed_authority(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("scripts/custom_gpt_deploy.py", text)
        for contract in (
            "status|prepare|repair-proxy|adopt-active|run-deployment-adapter|record-deployment|record-builder|verify|activate|rollback",
            "`init-production-target`",
            "`prepare` requires `--production-target`",
            "directly through `gh api`",
            "`record-deployment --provider-evidence`",
            "`scripts/custom_gpt_vercel_create.py`",
            "same revision is never\nsubmitted twice",
            "stable-alias read-back remains the production authority",
            "GET the deployment, project, deployment aliases, and production project\ndomains directly from Vercel",
            "fresh Vercel and public `/healthz`\nread-backs immediately before changing the active pointer",
            "`--expected-deployment-identity`",
            "`record-builder` with `--builder-evidence`",
            "both `--smoke-evidence` and the corresponding `--browser-evidence`",
            "Do not substitute the older release-verification script",
        ):
            self.assertIn(contract, text)
        self.assertIn("explicit user confirmation", text)
        self.assertIn("external release home", text)
        self.assertIn("~/.config/garmin-coach-loop/gateway.env", text)
        self.assertIn("~/.config/garmin-coach-loop/vercel.env", text)
        self.assertIn("mode `0600`", text)
        self.assertIn("may Codex/operator use an authorized Gateway or\n   Vercel connector", text)
        self.assertIn("browser-assisted editing of that same production GPT", text)
        self.assertIn("production GPT must never deploy itself", text)
        self.assertIn("human/browser Builder attestation, not deterministic proof", text)
        self.assertIn("Matching\ninstructions/OpenAPI hashes do not automatically prove the selected Builder\nmodel, authentication settings", text)
        self.assertIn("Never roll back PlanState", text)

    def test_production_proxy_and_external_evidence_boundaries_are_explicit(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("venture repository may link GTM, application, and\nmilestone material, but must not contain deploy code, receipts, secrets", text)
        self.assertIn("direct GitHub provider read-back proves candidate\nchecks", text)
        self.assertIn("Vercel deployment ID plus deployment/project read-back", text)
        self.assertIn("stable alias", (ROOT / "entrypoints" / "custom-gpt" / "README.md").read_text(encoding="utf-8"))
        self.assertIn("browser/user-visible\nsmoke", text)
        self.assertIn("do not change the production Builder schema or\nOAuth token URL", text)
        self.assertIn("direct development tunnel", text)
        self.assertIn("route-only revision may explicitly reuse\nthe already recorded Builder evidence", text)
        self.assertIn("restore does not reuse Builder evidence", text)

    def test_legacy_adoption_is_a_bounded_bootstrap_not_modern_proof(self):
        text = SKILL.read_text(encoding="utf-8")
        for contract in (
            "Use `adopt-active` only once",
            "`--current-proxy-upstream`",
            "`--current-proxy-config`",
            "`--expected-deployment-identity`",
            "without\n`--confirm-live-check` it is plan-only",
            "not proof of a modern Vercel deployment identity",
        ):
            self.assertIn(contract, text)

    def test_entrypoint_keeps_production_and_direct_development_tunnels_separate(self):
        text = (ROOT / "entrypoints" / "custom-gpt" / "README.md").read_text(encoding="utf-8")
        self.assertIn("~/.config/garmin-coach-loop/gateway.env", text)
        self.assertIn("mode `0600`", text)
        self.assertNotIn("a local, gitignored `.env`", text)
        self.assertIn("Do not use the older release-artifact verifier\nas the final production-state command", text)
        self.assertIn("do not edit the production\nGPT Builder schema or OAuth token URL", text)
        self.assertIn("Direct development tunnel changed", text)
        for flag in (
            "--production-target",
            "--provider-evidence",
            "--expected-deployment-identity",
            "--current-proxy-upstream",
            "--current-proxy-config",
            "--builder-evidence",
            "--smoke-evidence",
            "--browser-evidence",
        ):
            self.assertIn(flag, text)
        self.assertIn("`repair-proxy --run-id RUN_ID --proxy-upstream NEW_UPSTREAM`", text)
        self.assertIn("`rollback --run-id ACTIVE_RUN`", text)
        self.assertNotIn("repair-or-restore", text)
        self.assertNotIn("rollback-plan", text)

    def test_ui_metadata_names_the_same_skill(self):
        text = OPENAI_YAML.read_text(encoding="utf-8")
        self.assertIn('display_name: "Custom GPT Production Release"', text)
        self.assertIn("$custom-gpt-production-release", text)


if __name__ == "__main__":
    unittest.main()
