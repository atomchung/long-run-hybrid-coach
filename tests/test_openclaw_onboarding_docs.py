"""Copyable onboarding examples must not silently change whose account is used."""

from __future__ import annotations

import json
import re
import shlex
import unittest
from pathlib import Path
from urllib.parse import urlsplit


README = Path(__file__).resolve().parents[1] / "entrypoints" / "openclaw" / "README.md"
SERVER_NAME = "garmin-coach-loop"


def examples(text: str) -> tuple[dict, dict]:
    configs = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", text, re.S)]
    requester = next(config for config in configs if "mcp" in config)
    for block in re.findall(r"```bash\n(.*?)\n```", text, re.S):
        for line in block.replace("\\\n", " ").splitlines():
            args = shlex.split(line)
            if args[:4] == ["openclaw", "mcp", "set", SERVER_NAME]:
                return requester, json.loads(args[4])
    raise AssertionError("No explicit single-user MCP configuration example")


class OpenClawOnboardingDocsTests(unittest.TestCase):
    def setUp(self):
        self.text = README.read_text(encoding="utf-8")
        self.requester_config, self.shared = examples(self.text)
        self.requester = self.requester_config["mcp"]["servers"][SERVER_NAME]

    def test_copyable_examples_declare_different_identities(self):
        self.assertEqual("per-requester", self.requester["oauth"]["identity"])
        self.assertEqual("shared", self.shared["oauth"]["identity"])

    def test_both_examples_use_the_same_oauth_streamable_http_service(self):
        for server in (self.requester, self.shared):
            with self.subTest(identity=server["oauth"]["identity"]):
                self.assertEqual("https://mcp.paceandstaystrong.com/mcp", server["url"])
                self.assertEqual("streamable-http", server["transport"])
                self.assertEqual("oauth", server["auth"])
                self.assertNotIn("headers", server)
                self.assertNotIn("authProfileId", server["oauth"])

    def test_requester_example_declares_a_separate_https_callback_origin(self):
        callback = urlsplit(self.requester_config["gateway"]["publicOrigin"])
        self.assertEqual("https", callback.scheme)
        self.assertTrue(callback.hostname)
        self.assertEqual("", callback.path)
        self.assertEqual("", callback.query)
        self.assertEqual("", callback.fragment)
        self.assertIsNone(callback.username)
        self.assertNotEqual(urlsplit(self.requester["url"]).netloc, callback.netloc)

    def test_operator_login_is_only_in_the_single_user_section(self):
        requester_section = self.text.split("### Separate accounts for channel senders", 1)[1].split(
            "### One person's private instance", 1
        )[0]
        self.assertNotIn("openclaw mcp login", requester_section)
        single_user_section = self.text.split("### One person's private instance", 1)[1].split(
            "### Connection checks and shared protocol rules", 1
        )[0]
        self.assertIn("openclaw mcp login garmin-coach-loop", single_user_section)


if __name__ == "__main__":
    unittest.main()
