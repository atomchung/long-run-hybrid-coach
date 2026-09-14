"""The provider conformance probe has to still run when someone reaches for it.

The probe is the only thing that checks what Intervals *stored* against what the product
*sent*, and it is opt-in and manual by construction: it writes to a real calendar, so it
never runs in CI. That is exactly why it rots unnoticed. It did: the outlook validation
added in #127 made the rebased fixture plan invalid, and from then on every invocation --
including the read-only listing -- died in `prepare_delivery_set` before a single payload
was built. The live-provider-smoke gate had no working tool behind it and nothing said so.

These tests build the probe payloads the way the operator's first command does, with no
credentials and no network, and assert the two properties the script cannot check about
itself: that each probe plan is a plan the product would accept, and that no probe can be
mistaken for a delivered workout in either direction.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from garmin_coach_loop.validation import validate_plan_state


ROOT = Path(__file__).resolve().parents[1]


def _load_probe():
    """Import the probe from scripts/, which is not an installed package."""
    spec = importlib.util.spec_from_file_location(
        "probe_provider_conformance", ROOT / "scripts" / "probe_provider_conformance.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


class ProbePayloadsBuildOffline(unittest.TestCase):
    """`python3 scripts/probe_provider_conformance.py` with no arguments must work.

    That invocation writes nothing and reads no credentials, so a failure here is the
    script being broken rather than the environment lacking an account.
    """

    def setUp(self) -> None:
        # The live account's Run threshold HR is read only when the probe writes. Stubbing
        # it keeps these tests off the network without changing the payloads under test.
        patcher = mock.patch.object(probe, "_read_run_threshold_hr", lambda: 168)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_probe_shape_builds_a_payload(self) -> None:
        built = probe._payloads(probe.DEFAULT_DATE)
        self.assertEqual(len(built), len(probe.PROBES))
        self.assertEqual(
            [kind for kind, _, _, _ in built], [kind for kind, _, _ in probe.PROBES]
        )
        for kind, _, _, payload in built:
            with self.subTest(probe=kind):
                self.assertTrue(payload.get("description"), "payload carries no workout text")

    def test_each_probe_plan_is_one_the_product_would_accept(self) -> None:
        """The regression. An invalid plan stops the probe before it reaches Intervals."""
        for kind, _, plan_body in probe.PROBES:
            with self.subTest(probe=kind):
                plan = probe._plan_for(kind, plan_body, probe.DEFAULT_DATE)
                report = validate_plan_state(plan)
                self.assertEqual(report["errors"], [])

    def test_the_rebased_outlook_follows_the_rebased_week(self) -> None:
        """What #127 started checking, and what the rebase had stopped doing."""
        kind, _, plan_body = probe.PROBES[0]
        plan = probe._plan_for(kind, plan_body, probe.DEFAULT_DATE)
        week_start = dt.date.fromisoformat(plan["week"]["start"])
        self.assertEqual(
            [entry["week_start"] for entry in plan["cycle"]["outlook"]],
            [
                (week_start + dt.timedelta(days=7 * (index + 1))).isoformat()
                for index in range(len(plan["cycle"]["outlook"]))
            ],
        )

    def test_the_probe_date_falls_in_the_week_the_probe_built(self) -> None:
        kind, _, plan_body = probe.PROBES[0]
        plan = probe._plan_for(kind, plan_body, probe.DEFAULT_DATE)
        week_start = dt.date.fromisoformat(plan["week"]["start"])
        probe_day = dt.date.fromisoformat(probe.DEFAULT_DATE)
        self.assertLessEqual(week_start, probe_day)
        self.assertLess((probe_day - week_start).days, 7)

    def test_no_probe_can_be_mistaken_for_a_delivered_workout(self) -> None:
        """`gcl-probe:` is not `gcl:`, in both directions.

        The product owns an event by its `gcl:` external id, and `--clean` deletes every
        event whose id starts with `PROBE_PREFIX`. A prefix that was a prefix of the other
        would make one of those two operations reach the wrong events.
        """
        self.assertFalse(probe.PROBE_PREFIX.startswith("gcl:"))
        self.assertFalse("gcl:".startswith(probe.PROBE_PREFIX))
        built = probe._payloads(probe.DEFAULT_DATE)
        for kind, _, _, payload in built:
            with self.subTest(probe=kind):
                self.assertTrue(payload["external_id"].startswith(probe.PROBE_PREFIX))

    def test_each_probe_writes_its_own_event(self) -> None:
        built = probe._payloads(probe.DEFAULT_DATE)
        ids = [payload["external_id"] for _, _, _, payload in built]
        self.assertEqual(len(set(ids)), len(ids))


if __name__ == "__main__":
    unittest.main()
