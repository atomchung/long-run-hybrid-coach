"""The copyable onboarding example must stay the single-athlete one.

The entry documents one supported path -- one athlete, their own OpenClaw, the hosted
coach -- and records the multi-sender shape only as out of scope. Someone copying the
first configuration they meet must therefore land on the single-athlete one, with the
credentials owned by that athlete and no deployment change implied.
"""

from __future__ import annotations

import json
import re
import shlex
import unittest
from pathlib import Path


README = Path(__file__).resolve().parents[1] / "entrypoints" / "openclaw" / "README.md"
SERVER_NAME = "garmin-coach-loop"
SINGLE_USER_HEADING = "## Connecting one athlete's own OpenClaw"
OUT_OF_SCOPE_HEADING = "### One OpenClaw serving several people is not this round"


def copyable_examples(text: str) -> tuple[dict, dict]:
    """The first copyable JSON server entry, and the one the CLI example writes."""

    configs = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", text, re.S)]
    config = next(entry for entry in configs if "mcp" in entry)
    for block in re.findall(r"```bash\n(.*?)\n```", text, re.S):
        for line in block.replace("\\\n", " ").splitlines():
            args = shlex.split(line)
            if args[:4] == ["openclaw", "mcp", "set", SERVER_NAME]:
                return config, json.loads(args[4])
    raise AssertionError("No copyable `openclaw mcp set` configuration example")


class OpenClawOnboardingDocsTests(unittest.TestCase):
    def setUp(self):
        self.text = README.read_text(encoding="utf-8")
        self.config, self.cli = copyable_examples(self.text)
        self.server = self.config["mcp"]["servers"][SERVER_NAME]

    def test_the_single_athlete_path_is_documented_first(self):
        self.assertLess(
            self.text.index(SINGLE_USER_HEADING),
            self.text.index(OUT_OF_SCOPE_HEADING),
            "the out-of-scope multi-sender shape must not precede the supported path",
        )

    def test_both_copyable_examples_authorize_the_one_athlete(self):
        for server in (self.server, self.cli):
            with self.subTest(source=server):
                self.assertEqual("shared", server["oauth"]["identity"])

    def test_both_copyable_examples_name_the_same_oauth_streamable_http_service(self):
        for server in (self.server, self.cli):
            with self.subTest(source=server):
                self.assertEqual("https://mcp.paceandstaystrong.com/mcp", server["url"])
                self.assertEqual("streamable-http", server["transport"])
                self.assertEqual("oauth", server["auth"])
                self.assertNotIn("headers", server)
                self.assertNotIn("authProfileId", server["oauth"])

    def test_the_copyable_example_implies_no_deployment_change(self):
        # `gateway.publicOrigin` is the multi-sender callback key, and pasting it into a
        # single-athlete install would ask the operator to trust an origin needlessly.
        self.assertNotIn("gateway", self.config)

    def test_loopback_login_is_only_offered_on_the_supported_path(self):
        # The out-of-scope section may name operator login to say it does *not* connect
        # senders; what it must never do is hand over the runnable command as the way in.
        supported, out_of_scope = self.text.split(OUT_OF_SCOPE_HEADING, 1)
        self.assertIn(f"openclaw mcp login {SERVER_NAME}", supported)
        self.assertNotIn(f"openclaw mcp login {SERVER_NAME}", out_of_scope)

    def test_the_out_of_scope_section_never_offers_a_shared_token_workaround(self):
        out_of_scope = self.text.split(OUT_OF_SCOPE_HEADING, 1)[1]
        self.assertIn("per-requester", out_of_scope)


if __name__ == "__main__":
    unittest.main()
