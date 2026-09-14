"""One place to inject the failure classes this product has actually hit.

The suite used to mock each class differently: `_held.clear()`,
`_forget_retained_contexts`, a new `CoachGateway` that left the HTTP server on the
old process, ad-hoc `bulk_upsert` wrappers, and a hand-made `.pending-*` directory
that never went through the writer. Those shapes still exist in older tests; new
regressions go through here so a restart, a mid-commit death, a half-applied
provider write, and a corrupted hold mean the same thing in every case.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest import mock

from garmin_coach_loop.delivery import DeliveryError
from garmin_coach_loop.gateway import CoachGateway, HELD_TRANSACTION, _proposal_key
from garmin_coach_loop.store import (
    StateStoreError,
    adopt_store,
    apply_decision,
    restore_snapshot,
    snapshot_store,
)


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "garmin-coach-loop-28-day"
HOME_ENV = "GARMIN_COACH_LOOP_HOME"
GATEWAY_URL_ENV = "GARMIN_COACH_LOOP_GATEWAY_URL"


class ProcessDeath(BaseException):
    """A process that vanished mid-write.

    ``except Exception`` cleanup in the store writer does not run. ``finally``
    handlers still do, which matches KeyboardInterrupt and not SIGKILL. SIGKILL
    also leaves ``.lock``; gateway startup already reclaims that, and
    ``test_gateway`` covers the reclaim.
    """


def _load_example(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


@contextmanager
def isolated_product_home(home: Path | None = None) -> Iterator[Path]:
    """Point every default-store lookup at a temp directory, never the dogfood store."""
    temporary = None
    if home is None:
        temporary = tempfile.TemporaryDirectory()
        home = Path(temporary.name)
    previous = {
        HOME_ENV: os.environ.get(HOME_ENV),
        GATEWAY_URL_ENV: os.environ.get(GATEWAY_URL_ENV),
    }
    os.environ[HOME_ENV] = str(home)
    os.environ.pop(GATEWAY_URL_ENV, None)
    try:
        yield Path(home)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if temporary is not None:
            temporary.cleanup()


def restart_gateway(case: Any) -> CoachGateway:
    """Replace the in-process gateway so held transactions vanish.

    Signed proposals still open: they are self-contained. The HTTP server is rebound
    when this case is serving one, so a later MCP call cannot spend a yes against
    the process that just died.
    """
    case.gateway = CoachGateway(
        case.gateway.config,
        fetch=case.gateway.fetch,
        now=lambda: case.now,
    )
    server = getattr(case, "server", None)
    if server is not None:
        server.gateway = case.gateway
    return case.gateway


def held_transaction_payload(gateway: CoachGateway, owner_id: str, proposal: str) -> dict[str, Any]:
    """The live retained record, not a copy — mutations are what a corrupt cache looks like."""
    key = _proposal_key(proposal)
    for item in gateway._held.get(owner_id, []):
        if item.kind == HELD_TRANSACTION and item.key == key:
            return item.payload
    raise AssertionError("no held transaction for that proposal")


def corrupt_held_transaction(
    gateway: CoachGateway,
    owner_id: str,
    proposal: str,
    mutate: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    payload = held_transaction_payload(gateway, owner_id, proposal)
    mutate(payload)
    return payload


@contextmanager
def crash_during_store_commit(*, after: str) -> Iterator[None]:
    """Kill the process at a named seam of ``_write_commit`` / ``_atomic_json``.

    ``pending-files``: the pending commit directory has been written; it has not
    been renamed into the append-only history.
    ``commit-dir-before-manifest``: the commit directory is in place; ``store.json``
    still names the previous head.
    ``handled-exception``: a recoverable ``Exception`` during the rename, which the
    writer is supposed to clean up.
    """
    from garmin_coach_loop import store as store_module

    real_replace = store_module.os.replace

    def replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        src_path, dst_path = Path(src), Path(dst)
        pending_rename = (
            src_path.name.startswith(".pending-") and src_path.parent.name == "commits"
        )
        if after == "pending-files" and pending_rename:
            raise ProcessDeath("killed after writing pending commit files")
        if after == "handled-exception" and pending_rename:
            raise RuntimeError("volume failed during commit rename")
        if after == "commit-dir-before-manifest" and dst_path.name == "store.json":
            raise ProcessDeath(
                "killed after commit directory rename, before the manifest"
            )
        return real_replace(src, dst, *args, **kwargs)

    with mock.patch.object(store_module.os, "replace", replace):
        yield


@contextmanager
def fail_nth_event_delete(n: int, *, exc: BaseException | None = None) -> Iterator[None]:
    """Let the first ``n - 1`` event deletes land, then fail. Gateway delivery uses this."""
    from garmin_coach_loop.delivery import IntervalsTransport

    original = IntervalsTransport.delete_event
    if exc is None:
        exc = DeliveryError("Intervals DELETE failed: a later withdrawal was refused")
    count = {"n": 0}

    def wrapped(transport: Any, event_id: Any) -> Any:
        count["n"] += 1
        if count["n"] >= n:
            raise exc
        return original(transport, event_id)

    with mock.patch.object(IntervalsTransport, "delete_event", wrapped):
        yield


def fail_nth_calendar_write(
    provider: Any, n: int, *, exc: BaseException | None = None
) -> Callable[[], None]:
    """Let the first ``n - 1`` calendar upserts land, then fail.

    Works on ``FakeTransport.bulk_upsert`` and ``FakeIntervals._bulk_upsert``.
    """
    if hasattr(provider, "_bulk_upsert") and callable(getattr(provider, "__call__", None)):
        original = provider._bulk_upsert
        attr = "_bulk_upsert"
        if exc is None:
            exc = _http_unavailable()
    else:
        original = provider.bulk_upsert
        attr = "bulk_upsert"
        if exc is None:
            exc = DeliveryError("Intervals POST failed: the second write was refused")

    count = {"n": 0}

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        count["n"] += 1
        if count["n"] >= n:
            raise exc
        return original(*args, **kwargs)

    setattr(provider, attr, wrapped)

    def restore() -> None:
        setattr(provider, attr, original)

    return restore


def _http_unavailable() -> Exception:
    import urllib.error

    return urllib.error.HTTPError(
        "https://intervals.icu/api/v1/events/bulk",
        503,
        "unavailable",
        None,
        None,
    )


def assert_unreconciled_delivery_fences(
    test: Any,
    state_dir: Path,
    *,
    snapshot_dir: Path,
    copy_destination: Path,
) -> None:
    """The four refusals CLAUDE.md names while a delivery reservation is open."""
    after = _load_example("plan-state-v2-day-4.json")
    for session in after["week"]["sessions"]:
        if session["session_id"] in {"run-quality-01", "run-long-01"}:
            session["execution"]["publish_supported"] = True

    with test.assertRaises(StateStoreError) as blocked:
        apply_decision(
            state_dir,
            context=_load_example("coach-context-day-4.json"),
            after=after,
            event=_load_example("decision-event-day-4.json"),
        )
    test.assertIn("in flight", str(blocked.exception))

    with test.assertRaises(StateStoreError) as blocked:
        snapshot_store(state_dir, reason="while-in-flight")
    test.assertIn("in flight", str(blocked.exception))
    test.assertIn("snapshot-store", str(blocked.exception))

    before = sorted(path.name for path in state_dir.parent.iterdir())
    with test.assertRaises(StateStoreError) as blocked:
        restore_snapshot(snapshot_dir, state_dir, confirm=True)
    test.assertIn("in flight", str(blocked.exception))
    test.assertIn("restore-store", str(blocked.exception))
    test.assertEqual(before, sorted(path.name for path in state_dir.parent.iterdir()))

    with test.assertRaises(StateStoreError) as blocked:
        adopt_store(state_dir, copy_destination, mode="copy", confirm=True)
    test.assertIn("in flight", str(blocked.exception))
    test.assertIn("adopt-owner-store --mode copy", str(blocked.exception))
    test.assertFalse(copy_destination.exists())


def dumped(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def assert_payload_hides(test: Any, payload: Any, *needles: str) -> None:
    blob = dumped(payload)
    for needle in needles:
        test.assertNotIn(needle, blob, payload)
