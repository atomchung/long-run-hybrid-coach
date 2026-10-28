"""The one-command health data switch stages, redeploys and reads back, in that order.

Railway, Git and the network are replaced by recorders: what is under test is the order of
the acts and what each one is handed, which is where a flip goes wrong silently -- a
switch staged without its release identity, a deploy triggered per variable, or a success
reported before `/readyz` says so.
"""

from __future__ import annotations

import unittest

from garmin_coach_loop.gateway import RELEASE_ID_ENV_VAR, RELEASE_TOOL_CATALOGUE_SHA_ENV_VAR
from garmin_coach_loop.product_scope import HEALTH_DATA_ENV_VAR
from scripts import switch_health_data as switch_script

DOMAIN = "https://coach-plugin.example"
COMMIT = "a" * 40
BUNDLE = {
    "release_id": "gclr-new",
    "git_commit": COMMIT,
    "instructions_sha256": "1" * 64,
    "tool_catalogue_sha256": "2" * 64,
    "skill_sha256": "3" * 64,
    "gateway_domain": DOMAIN,
    "gateway_artifact_sha256": "4" * 64,
}


def _reading(health_data: str, release_id: str, status: str = "ok") -> dict:
    return {
        "status": status,
        "health_data": health_data,
        "release_identity": {"release_id": release_id, "git_commit": COMMIT},
    }


class SwitchTests(unittest.TestCase):
    def _switch(self, readings: list[dict], **extra):
        self.commands: list[list[str]] = []
        self.built: list[tuple] = []
        queue = list(readings)

        def read(domain: str) -> dict:
            return queue.pop(0) if len(queue) > 1 else queue[0]

        def build(commit: str, domain: str, health_data: bool) -> dict:
            self.built.append((commit, domain, health_data))
            return BUNDLE

        return switch_script.switch(
            True,
            service="coach-plugin",
            domain=DOMAIN,
            scope=[],
            read=read,
            build=build,
            execute=self.commands.append,
            commit_present=lambda commit: True,
            sleep=lambda seconds: None,
            **extra,
        )

    def test_stages_everything_once_then_redeploys_then_reads_back(self):
        confirmed = self._switch(
            [_reading("off", "gclr-old"), _reading("off", "gclr-old", "blocked"), _reading("on", "gclr-new")]
        )
        self.assertEqual("on", confirmed["health_data"])
        self.assertEqual([(COMMIT, DOMAIN, True)], self.built)
        stage, redeploy = self.commands
        self.assertEqual(["railway", "variables", "--service", "coach-plugin", "--skip-deploys"], stage[:5])
        staged = dict(arg.split("=", 1) for arg in stage[5:] if arg != "--set")
        self.assertEqual("on", staged[HEALTH_DATA_ENV_VAR])
        self.assertEqual("gclr-new", staged[RELEASE_ID_ENV_VAR])
        self.assertEqual("2" * 64, staged[RELEASE_TOOL_CATALOGUE_SHA_ENV_VAR])
        self.assertEqual(8, len(staged))
        self.assertEqual(["railway", "redeploy", "--service", "coach-plugin", "--yes"], redeploy)

    def test_a_deployment_already_on_the_setting_is_left_alone(self):
        self._switch([_reading("on", "gclr-old")])
        self.assertEqual([], self.commands)
        self.assertEqual([], self.built)

    def test_a_flip_that_never_reads_back_is_reported_not_assumed(self):
        with self.assertRaises(switch_script.SwitchError) as caught:
            self._switch([_reading("off", "gclr-old")], wait_seconds=0)
        self.assertIn("previous deployment", str(caught.exception))
        self.assertEqual(2, len(self.commands))

    def test_no_release_identity_stages_nothing(self):
        with self.assertRaises(switch_script.SwitchError):
            self._switch([{"status": "blocked", "health_data": "off", "release_identity": None}])
        self.assertEqual([], self.commands)

    def test_project_and_environment_reach_both_commands(self):
        commands = switch_script.railway_commands(
            "coach-plugin", {HEALTH_DATA_ENV_VAR: "off"}, ["--project", "p", "--environment", "e"]
        )
        for command in commands:
            self.assertEqual(["--project", "p", "--environment", "e"], command[4:8])


if __name__ == "__main__":
    unittest.main()
