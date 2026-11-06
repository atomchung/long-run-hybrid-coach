import hashlib
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from garmin_coach_loop import identity, product_identity
from garmin_coach_loop.gateway import LIVE_RUN_DIGEST_ENV, LIVE_RUN_OWNER_ENV, identity_db_path
from scripts import provision_live_run_pilot as pilot


class ProvisionLiveRunPilotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name)
        self.state = self.scratch / "synthetic-state"
        self.output = self.scratch / "private-config"
        self.athlete = "i-synthetic-operator-12345"
        self.origin = "https://pilot.example:8443/"
        self.db = identity_db_path(product_identity.product_state_root(self.state, "hybrid"))

    def run_cli(self, athlete=None):
        return subprocess.run(
            [sys.executable, pilot.__file__, "--state-root", str(self.state),
             "--athlete-id", athlete or self.athlete, "--gateway-url", self.origin,
             "--output-dir", str(self.output)],
            cwd=self.scratch, capture_output=True, text=True, timeout=10,
        )

    def test_cli_writes_only_separate_private_files_and_will_not_rotate(self):
        owner = identity.lookup_or_create_owner(self.db, "intervals", self.athlete)
        registry_digest = hashlib.sha256(self.db.read_bytes()).digest()
        first = self.run_cli()
        self.assertEqual(first.returncode, 0)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        self.assertEqual({p.name for p in self.output.iterdir()}, {"watch.env", "gateway.env"})
        def read_env(name):
            path = self.output / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            return dict(item.split("=", 1) for item in shlex.split(path.read_text()))
        watch = read_env("watch.env")
        gateway = read_env("gateway.env")
        self.assertEqual(set(watch), {"LIVE_COACH_ENDPOINT", "LIVE_COACH_DEVICE_TOKEN"})
        self.assertEqual(watch["LIVE_COACH_ENDPOINT"], "https://pilot.example:8443/v1/live-run/samples")
        token = watch["LIVE_COACH_DEVICE_TOKEN"]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.assertEqual(gateway, {LIVE_RUN_DIGEST_ENV: digest, LIVE_RUN_OWNER_ENV: owner})
        second = self.run_cli()
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(watch, read_env("watch.env"))
        self.assertEqual(gateway, read_env("gateway.env"))
        for value in (token, digest, owner, self.athlete, self.origin, str(self.output)):
            self.assertNotIn(value, first.stdout + first.stderr + second.stdout + second.stderr)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).digest(), registry_digest)

    def test_unknown_owner_and_wrong_product_leave_no_files(self):
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertFalse(self.state.exists())
        identity.lookup_or_create_owner(self.db, "intervals", self.athlete)
        self.assertNotEqual(self.run_cli("unregistered").returncode, 0)
        self.assertEqual(identity.owner_count(self.db), 1)
        self.assertFalse(self.output.exists())
        (self.state / product_identity.MARKER_NAME).write_text(
            '{"schema_version":1,"product":"training"}'
        )
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertFalse(self.output.exists())

    def test_only_https_origins_get_the_ingest_path(self):
        for bad in ("http://pilot.example", "https://pilot.example/prefix",
                    "https://pilot.example?", "https://pilot.example#",
                    "https://user:pass@pilot.example", "https://pilot.example:65536",
                    "https://pilot.exam\nple", "https://pilot..example",
                    "https://pilot.example\\@other.example"):
            with self.subTest(origin=bad), self.assertRaises(pilot.ProvisionError):
                pilot._endpoint(bad)
        self.assertEqual(pilot._endpoint("https://[::1]:8443"), "https://[::1]:8443/v1/live-run/samples")

    def test_output_must_be_new_and_outside_git_including_symlinks(self):
        git_workspace = self.scratch / "fake-worktree"
        git_workspace.mkdir()
        (git_workspace / ".git").write_text("gitdir: /synthetic-only")
        link = self.scratch / "alias"
        link.symlink_to(git_workspace, target_is_directory=True)
        dangling = self.scratch / "dangling"
        dangling.symlink_to(self.scratch / "missing")
        for path in (git_workspace / "secrets", link / "secrets", dangling):
            with self.subTest(path=path), self.assertRaises(pilot.ProvisionError):
                pilot._private_destination(path)
        self.assertFalse((git_workspace / "secrets").exists())

    def test_second_write_failure_removes_new_directory(self):
        identity.lookup_or_create_owner(self.db, "intervals", self.athlete)
        write = pilot._write_env
        def fail_second(path, values):
            if path.name == "gateway.env":
                raise OSError("simulated write failure")
            write(path, values)
        with patch.object(pilot, "_write_env", side_effect=fail_second), self.assertRaises(OSError):
            pilot.provision(state_root=self.state, athlete_id=self.athlete,
                            gateway_url=self.origin, output_dir=self.output)
        self.assertFalse(os.path.lexists(self.output))
