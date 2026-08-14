"""Regression tests for the distribution safety gate.

The gate decides what may leave this repository, so a hole in it is not visible
until after the material is public. The self-reference allowance is the one
place it deliberately looks past a personal handle; these tests hold that
allowance to exactly the two published URLs and nothing wider.

Every sample below is assembled from fragments rather than written out, because
a test for a scanner necessarily contains what the scanner looks for, and this
file is itself a candidate the gate reads (see scripts/check_repo_safety.py).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

HANDLE = "atom" + "chung"
OWNER_NAME = "T" + "ING"
PUBLIC_REPO_URL = f"https://github.com/{HANDLE}/long-run-hybrid-coach"
PUBLIC_PAGES_URL = f"https://{HANDLE}.github.io/long-run-hybrid-coach"
OTHER_REPO_URL = f"https://github.com/{HANDLE}/garmin-coach-loop"
SECRET_ASSIGNMENT = "client_" + 'secret = "synthetic-not-real-value"'


def _load_gate():
    """Import the gate from scripts/, which is not an installed package."""
    spec = importlib.util.spec_from_file_location(
        "check_repo_safety", ROOT / "scripts" / "check_repo_safety.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


safety = _load_gate()


class RepoSafetyGateTest(unittest.TestCase):
    def run_gate(self, files: dict[str, str]) -> tuple[int, str]:
        """Run the gate over a synthetic candidate set and return its verdict."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(safety, "ROOT", root), mock.patch.object(
                safety, "git_candidates", lambda: sorted(files)
            ), contextlib.redirect_stdout(output):
                code = safety.main()
        return code, output.getvalue()

    def test_published_site_may_link_back_to_its_own_repository(self):
        """The site is served from the repository it links to; that is not a leak."""
        code, report = self.run_gate(
            {
                "docs/index.html": f'<a href="{PUBLIC_REPO_URL}">Source</a>',
                "docs/privacy.html": f"Published at {PUBLIC_PAGES_URL}/.",
            }
        )
        self.assertEqual(code, 0, report)

    def test_the_same_handle_anywhere_else_still_fails(self):
        """Neutralising the two published URLs must not neutralise the handle."""
        code, report = self.run_gate(
            {"docs/index.html": f"Written by {HANDLE}, in Taipei."}
        )
        self.assertEqual(code, 1)
        self.assertIn("personal GitHub/account handle", report)

    def test_a_different_repository_under_the_same_account_still_fails(self):
        """The allowance covers two exact URLs, not every URL carrying the handle."""
        code, report = self.run_gate({"README.md": f"See {OTHER_REPO_URL}."})
        self.assertEqual(code, 1)
        self.assertIn("personal GitHub/account handle", report)

    def test_the_owner_name_is_never_allowed(self):
        """The self-reference allowance touches no other personal pattern."""
        code, report = self.run_gate(
            {"docs/index.html": f"Built by {OWNER_NAME}. {PUBLIC_REPO_URL}"}
        )
        self.assertEqual(code, 1)
        self.assertIn("owner personal name", report)

    def test_secrets_are_scanned_on_the_unmodified_text(self):
        """Removing a URL for the personal scan must not hide a secret beside it."""
        code, report = self.run_gate(
            {"docs/index.html": f"{PUBLIC_REPO_URL}\n{SECRET_ASSIGNMENT}\n"}
        )
        self.assertEqual(code, 1)
        self.assertIn("assigned secret", report)


if __name__ == "__main__":
    unittest.main()
