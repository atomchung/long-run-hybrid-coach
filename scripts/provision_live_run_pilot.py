"""Prepare a private FR570 watch bundle; does not connect to or reconfigure the gateway.

The owner is resolved from a hybrid state root and an athlete ID, or named directly with
``--owner-id`` when the registry lives on a host this machine should not copy from. The
device token is generated here either way; the gateway only ever receives its digest.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from garmin_coach_loop import identity, product_identity
from garmin_coach_loop.gateway import (
    LIVE_RUN_DIGEST_ENV,
    LIVE_RUN_OWNER_ENV,
    LIVE_RUN_PATH,
    PROVIDER,
    identity_db_path,
)


class ProvisionError(ValueError):
    """An actionable message safe to display without including any input values."""


# The form the gateway itself accepts for its live-run owner.
OWNER_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def _endpoint(origin: str) -> str:
    error = ProvisionError("Supply a bare HTTPS gateway origin, optionally with a port.")
    # urlsplit strips some controls; reject them before it can reinterpret the input.
    if not origin.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in origin):
        raise error
    try:
        url = urlsplit(origin)
        port = url.port
    except ValueError:
        raise error from None
    if (
        url.scheme != "https" or not url.hostname or url.path not in ("", "/")
        or any(c in origin for c in "@?#\\%") or port == 0
        or not re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?", url.netloc)
    ):
        raise error
    if ":" not in url.hostname:
        hostname = url.hostname.removesuffix(".")
        if len(hostname) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in hostname.split(".")
        ):
            raise error
    return "https://" + url.netloc + LIVE_RUN_PATH


def _private_destination(path: Path) -> Path:
    path = path.expanduser()
    parent = path.parent.resolve(strict=True)
    if not path.name or path.name in {"..", ".git"} or not parent.is_dir():
        raise ProvisionError("Specify a new output directory under an existing directory.")
    for directory in (parent, *parent.parents):
        if (
            directory.name == ".git" or os.path.lexists(directory / ".git")
            or (directory / "HEAD").is_file() and (directory / "objects").is_dir()
            and (directory / "refs").is_dir()
        ):
            raise ProvisionError("The output directory must be outside Git.")
    destination = parent / path.name
    if os.path.lexists(destination):
        raise ProvisionError("The output directory already exists; nothing was replaced.")
    return destination


def _write_env(path: Path, variables: dict[str, str]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        for name, value in variables.items():
            stream.write(f"{name}={shlex.quote(value)}\n")


def _owner(state_root: Path | None, athlete_id: str | None, owner_id: str | None) -> str:
    if owner_id is not None:
        if state_root is not None or athlete_id is not None:
            raise ProvisionError("Name either --owner-id, or --state-root with --athlete-id.")
        if not OWNER_ID.fullmatch(owner_id):
            raise ProvisionError("--owner-id must be a registered owner's lowercase UUID.")
        return owner_id
    if state_root is None or athlete_id is None:
        raise ProvisionError("Name either --owner-id, or --state-root with --athlete-id.")
    root = product_identity.product_state_root(state_root.expanduser().resolve(), "hybrid")
    product_identity.verify_product_state(root, "hybrid")
    owner = identity.owner_for_provider_athlete(identity_db_path(root), PROVIDER, athlete_id)
    if not owner:
        raise ProvisionError("No registered athlete was found in the supplied hybrid state root.")
    return owner


def provision(*, gateway_url: str, output_dir: Path, state_root: Path | None = None,
              athlete_id: str | None = None, owner_id: str | None = None) -> None:
    endpoint = _endpoint(gateway_url)
    destination = _private_destination(output_dir)
    owner = _owner(state_root, athlete_id, owner_id)
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    # Exclusive mkdir is also the no-rotation guard when two operators race.
    destination.mkdir(mode=0o700)
    try:
        destination.chmod(0o700)
        _write_env(destination / "watch.env", {
            "LIVE_COACH_ENDPOINT": endpoint,
            "LIVE_COACH_DEVICE_TOKEN": token,
        })
        _write_env(destination / "gateway.env", {
            LIVE_RUN_OWNER_ENV: owner,
            LIVE_RUN_DIGEST_ENV: digest,
        })
    except BaseException:
        shutil.rmtree(destination)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, help="Gateway base state root")
    parser.add_argument("--athlete-id", help="Already connected Intervals athlete ID")
    parser.add_argument("--owner-id", help="Registered owner UUID, instead of a state root and athlete ID")
    parser.add_argument("--gateway-url", required=True, help="Bare HTTPS gateway origin")
    parser.add_argument("--output-dir", type=Path, required=True, help="New private directory outside Git")
    args = parser.parse_args()
    try:
        provision(**vars(args))
    except ProvisionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError):
        print("Could not read the registered hybrid owner or write the private files.", file=sys.stderr)
        return 1
    print("Created private watch.env and gateway.env. No gateway settings were changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
