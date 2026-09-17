"""Hold every build to the hashed lock, and to only the hashed lock.

`requirements.txt` pins one version. A version number is not an artifact identity: it
survives a release re-published under the same version, a compromised index account and a
mirror substitution, and the build looks identical either way. `requirements.lock` is what
every install actually reads -- the distributions that pin resolves to, each with the
sha256 of the artifacts allowed to satisfy it -- and `--require-hashes` is what turns a
substituted byte into a failed build rather than a shipped one, in the process that holds
every connected athlete's Intervals credential.

What these tests refuse is the drift that would make that decorative: a requirement that
lost its hashes, an install site that forgot the flag, a second unhashed path kept for
convenience, and the possibility that `--require-hashes` does not fail closed at all.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock"
DOCKERFILE = ROOT / "Dockerfile"
WORKFLOWS = ROOT / ".github" / "workflows"
UPGRADE_RUNBOOK = ROOT / "docs" / "ops" / "upgrade-the-mcp-sdk.md"

PINNED = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;]+)")
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


def _requirements(text: str) -> dict[str, dict[str, object]]:
    """Every pinned requirement in a requirements file, with the hashes attached to it.

    A hand-rolled reader rather than a parser dependency: the file is generated, the shape
    is `name==version [; marker] \\` followed by indented `--hash=` continuations, and this
    repository does not acquire a second dependency to read its own lock (AGENTS.md,
    "Dependencies").
    """
    found: dict[str, dict[str, object]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if raw[:1].isspace() and current is not None:
            found[current]["hashes"].extend(HASH.findall(line))  # type: ignore[union-attr]
            continue
        match = PINNED.match(line)
        if match is None:
            current = None
            continue
        current = match.group("name").lower().replace("_", "-")
        found[current] = {
            "version": match.group("version"),
            "hashes": HASH.findall(line),
            "marker": line.split(";", 1)[1].split("\\")[0].strip() if ";" in line else None,
        }
    return found


def _install_lines(text: str) -> list[str]:
    """Lines that run an install, not lines that talk about one.

    A `#` comment is prose wherever it appears -- the Dockerfile explains its own install
    directly above it -- and a rule that read comments would be asserting about wording.
    """
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or "pip install" not in line:
            continue
        lines.append(line)
    return lines


def _tracked_text_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    paths = []
    for name in listed.decode("utf-8").split("\0"):
        if not name:
            continue
        path = ROOT / name
        if not path.is_file() or path.suffix in {".png", ".jpg", ".fit", ".ico"}:
            continue
        paths.append(path)
    return paths


class LockContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = _requirements(LOCK.read_text(encoding="utf-8"))

    def test_every_pinned_distribution_carries_at_least_one_hash(self):
        # `--require-hashes` refuses the whole file when one requirement has no hash, so a
        # missing one does not weaken the install quietly -- it breaks it. This is the
        # assertion that says so before a deploy finds out.
        self.assertTrue(self.lock, "requirements.lock pins nothing")
        missing = sorted(name for name, entry in self.lock.items() if not entry["hashes"])
        self.assertEqual([], missing, "requirements.lock entries without a sha256")

    def test_the_lock_pins_the_version_requirements_txt_asks_for(self):
        intent = _requirements(REQUIREMENTS.read_text(encoding="utf-8"))
        self.assertEqual({"mcp"}, set(intent), "requirements.txt is the one-line intent")
        self.assertEqual(intent["mcp"]["version"], self.lock["mcp"]["version"])

    def test_the_lock_records_the_command_that_regenerates_it(self):
        """A hash set nobody can rebuild is a hash set nobody will update.

        Issue #488's requirement is a *recorded* command rather than a maintained file.
        The generator writes it into the header; the upgrade runbook repeats it as step 2,
        and this holds the two to each other so a rewritten runbook cannot drift from what
        the file was actually made with.
        """
        header = "\n".join(LOCK.read_text(encoding="utf-8").splitlines()[:5])
        self.assertIn("uv pip compile", header)
        self.assertIn("--generate-hashes", header)
        self.assertIn("requirements.lock", header)

        runbook = UPGRADE_RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("uv pip compile requirements.txt --generate-hashes", runbook)
        self.assertIn("--require-hashes", runbook)

    def test_the_lock_covers_what_the_pinned_sdk_itself_requires(self):
        """The direct dependencies of `mcp`, as the installed distribution declares them.

        Transitive completeness is pip's own answer -- `--require-hashes` refuses an
        install whose dependency is not listed -- so this only catches the lock being
        regenerated from a different resolution than the one that is installed here.
        """
        declared = importlib.metadata.requires("mcp") or []
        required = set()
        for entry in declared:
            name = re.split(r"[\s;\[<>=!~()]", entry.strip(), maxsplit=1)[0]
            if not name:
                continue
            marker = entry.split(";", 1)[1] if ";" in entry else ""
            if "extra ==" in marker:
                # An optional extra this product does not install.
                continue
            required.add(name.lower().replace("_", "-"))
        self.assertTrue(required, "the installed mcp declares no requirements")
        self.assertEqual(set(), required - set(self.lock))


class InstallPathTests(unittest.TestCase):
    """Every place a build installs Python packages, and nowhere a second one hides."""

    def test_the_gateway_image_installs_the_lock_with_hashes(self):
        lines = _install_lines(DOCKERFILE.read_text(encoding="utf-8"))
        self.assertEqual(1, len(lines), f"the image should install once: {lines}")
        self.assertIn("--require-hashes", lines[0])
        self.assertIn("requirements.lock", lines[0])

    def test_every_workflow_install_verifies_hashes(self):
        checked = 0
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            for line in _install_lines(workflow.read_text(encoding="utf-8")):
                checked += 1
                self.assertIn("--require-hashes", line, f"{workflow.name}: {line}")
                self.assertIn("requirements.lock", line, f"{workflow.name}: {line}")
        # Four today: ci.yml's two jobs and publish-mcp-registry.yml's two. A workflow that
        # stopped installing at all would otherwise pass this by checking nothing.
        self.assertEqual(4, checked)

    def test_no_tracked_file_offers_an_unhashed_install_path(self):
        """The convenience path is the one an upgrade takes when hashes are inconvenient.

        Prose about `requirements.txt` is fine and this file is full of it. What is not
        fine is an *install command* naming it, or naming the lock without the flag --
        including in a runbook, a README or a comment somebody will copy.
        """
        offenders: list[str] = []
        for path in _tracked_text_files():
            if path == Path(__file__):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line in _install_lines(text):
                if "requirements.txt" in line:
                    offenders.append(f"{path.relative_to(ROOT)}: {line}")
                elif "requirements.lock" in line and "--require-hashes" not in line:
                    offenders.append(f"{path.relative_to(ROOT)}: {line}")
        self.assertEqual([], offenders)


class HashVerificationTests(unittest.TestCase):
    """Prove `--require-hashes` fails closed, rather than assuming pip does what it says.

    Offline and self-contained: a one-file wheel built here, a local `--find-links`
    directory, and no index. The point is the *mechanism* -- a lock entry whose sha256 does
    not match the artifact stops the install -- which is the whole basis for calling the
    gateway image's contents pinned.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._directory = tempfile.TemporaryDirectory()
        root = Path(cls._directory.name)
        cls.links = root / "links"
        cls.links.mkdir()
        cls.wheel = cls._build_wheel(cls.links)
        cls.digest = hashlib.sha256(cls.wheel.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._directory.cleanup()

    @staticmethod
    def _build_wheel(directory: Path) -> Path:
        name, version = "gclhashprobe", "1.0"
        wheel = directory / f"{name}-{version}-py3-none-any.whl"
        dist = f"{name}-{version}.dist-info"
        records: list[str] = []
        with zipfile.ZipFile(wheel, "w") as archive:
            def add(member: str, text: str) -> None:
                data = text.encode("utf-8")
                archive.writestr(member, data)
                digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
                records.append(f"{member},sha256={digest.rstrip(b'=').decode()},{len(data)}")

            add(f"{name}/__init__.py", "")
            add(f"{dist}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
            add(
                f"{dist}/WHEEL",
                "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            records.append(f"{dist}/RECORD,,")
            archive.writestr(f"{dist}/RECORD", "\n".join(records) + "\n")
        return wheel

    def _install(self, digest: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as workspace:
            lock = Path(workspace) / "probe.lock"
            lock.write_text(f"gclhashprobe==1.0 \\\n    --hash=sha256:{digest}\n", encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--no-index",
                    "--no-deps",
                    "--find-links",
                    str(self.links),
                    "--require-hashes",
                    "--target",
                    str(Path(workspace) / "site"),
                    "-r",
                    str(lock),
                ],
                capture_output=True,
                text=True,
                # The caller's pip configuration could name an index or a trusted host;
                # this run has to be the mechanism and nothing else.
                env={**os.environ, "PIP_CONFIG_FILE": os.devnull},
            )

    def test_the_declared_hash_installs(self):
        # The control. Without it, the refusal below could be pip failing for any reason.
        completed = self._install(self.digest)
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_a_mismatched_hash_fails_closed(self):
        completed = self._install("0" * 64)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("DO NOT MATCH THE HASHES", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
