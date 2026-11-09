#!/usr/bin/env python3
"""Build the Coach data field: sideload binaries, the Connect IQ Store package, or tests.

Nothing secret goes into a build. The field pairs with an account by showing a code that
the athlete says in their Coach conversation, so a build carries only the server's public
address, which ``--server`` replaces for a test deployment.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
NAMESPACE = {"iq": "http://www.garmin.com/xml/connectiq"}
# Watches only: bike computers and handhelds also run data fields, but not a run.
NOT_WATCHES = ("edge", "etrextouch", "gpsmap")
MIN_API = (5, 0, 0)
DEFAULT_DEVICES = ("fr57042mm", "fr57047mm")
DEFAULT_SERVER = '"https://mcp.paceandstaystrong.com"'


def manifest_products(manifest: Path = ROOT / "manifest.xml") -> list[str]:
    tree = ET.parse(manifest)
    return [node.get("id") for node in tree.findall(".//iq:products/iq:product", NAMESPACE)]


def qualifying_devices(devices_dir: Path) -> dict[str, int]:
    """Watch profiles that run data fields at API 5.0+, with their data field memory."""
    found = {}
    for profile in sorted(devices_dir.glob("*/compiler.json")):
        device = profile.parent.name
        if device.startswith(NOT_WATCHES):
            continue
        data = json.loads(profile.read_text(encoding="utf-8"))
        levels = [tuple(int(part) for part in number["connectIQVersion"].split("."))
                  for number in data.get("partNumbers", []) if number.get("connectIQVersion")]
        memory = {kind["type"]: kind["memoryLimit"] for kind in data.get("appTypes", [])}.get("datafield")
        if levels and min(levels) >= MIN_API and memory:
            found[device] = memory
    return found


def check_devices(devices_dir: Path) -> list[str]:
    """Differences between the manifest's products and the SDK's qualifying watches."""
    listed = set(manifest_products())
    qualifying = qualifying_devices(devices_dir)
    problems = [f"listed but not an API 5.0+ watch profile here: {name}" for name in sorted(listed - set(qualifying))]
    problems += [f"qualifies but is not listed: {name}" for name in sorted(set(qualifying) - listed)]
    return problems


def validate_server(server: str) -> str:
    """An https origin with nothing after it, which the field appends its paths to."""
    try:
        url = urlsplit(server)
        url.port
    except ValueError:
        raise ValueError("Invalid server URL") from None
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.path not in ("", "/") or url.query or url.fragment
            or any(ord(c) <= 32 or ord(c) == 127 or c in '\\<>"' for c in server)):
        raise ValueError("Use an https origin such as https://mcp.example.com, with no path, query or credentials")
    return f"https://{url.netloc}"


def under_git_directory(path: Path) -> bool:
    return any((directory / ".git").exists() for directory in (path, *path.parents))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", action="append", help=f"device id, repeatable; default: {' and '.join(DEFAULT_DEVICES)}")
    parser.add_argument("--output-dir", required=True, type=Path, help="private output directory outside a Git checkout")
    parser.add_argument("--developer-key", type=Path, default=os.environ.get("CIQ_SIGNING_KEY"),
                        help="path to a Garmin DER signing key outside any Git checkout, or set CIQ_SIGNING_KEY")
    parser.add_argument("--monkeyc", default="monkeyc", help="Connect IQ compiler path")
    parser.add_argument("--server", help="https origin of a test deployment; default: the one in source/Server.mc")
    parser.add_argument("--test", action="store_true", help="compile the on-device unit tests")
    parser.add_argument("--package", action="store_true",
                        help="build the Connect IQ Store package (.iq) for every listed watch")
    parser.add_argument("--check-devices", type=Path, metavar="DEVICES_DIR",
                        help="compare the manifest with the SDK's device profiles and exit")
    args = parser.parse_args()

    if args.check_devices:
        problems = check_devices(args.check_devices)
        for problem in problems:
            print(problem)
        print(f"{len(manifest_products())} listed; {'differences above' if problems else 'matches the SDK profiles'}")
        raise SystemExit(1 if problems else 0)
    if args.package and (args.test or args.device):
        parser.error("--package builds every listed watch, without tests")
    signer = args.developer_key.expanduser().resolve() if args.developer_key else None
    if not signer or not signer.is_file() or under_git_directory(signer):
        parser.error("An existing DER signing key outside a Git checkout is required")
    compiler = shutil.which(args.monkeyc)
    if not compiler:
        parser.error("Connect IQ compiler unavailable; set --monkeyc or add monkeyc to PATH")
    output = args.output_dir.expanduser().resolve()
    if under_git_directory(output):
        parser.error("Choose an output directory outside a Git checkout")
    server = None
    if args.server:
        try:
            server = validate_server(args.server)
        except ValueError as error:
            parser.error(str(error))
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.stat().st_mode & 0o077:
        parser.error("Use a private output directory (mode 0700)")

    with tempfile.TemporaryDirectory(prefix="coach-ciq-") as tmp:
        project = Path(tmp)
        for name in ("source", "resources", "resources-zht"):
            shutil.copytree(ROOT / name, project / name)
        for name in ("manifest.xml", "monkey.jungle"):
            shutil.copyfile(ROOT / name, project / name)
        if args.test:
            shutil.copytree(ROOT / "tests", project / "tests")
            with (project / "monkey.jungle").open("a", encoding="utf-8") as jungle:
                jungle.write("base.sourcePath = source;tests\n")
        if server:
            constant = project / "source" / "Server.mc"
            text = constant.read_text(encoding="utf-8")
            if text.count(DEFAULT_SERVER) != 1:
                raise SystemExit("source/Server.mc no longer names the default server once")
            constant.write_text(text.replace(DEFAULT_SERVER, f'"{server}"'), encoding="utf-8")
        base = [str(Path(compiler).resolve()), "-f", "monkey.jungle", "-y", str(signer), "-l", "1", "-w"]
        if args.package:
            built = project / "Coach.iq"
            builds = [("store", base + ["-e", "-r", "-o", str(built)], built)]
        else:
            name = "CoachTest.prg" if args.test else "Coach.prg"
            builds = [(device, base + ["-d", device, "-o", str(project / device / name), "-t" if args.test else "-r"],
                       project / device / name)
                      for device in dict.fromkeys(args.device or DEFAULT_DEVICES)]
        for label, command, built in builds:
            built.parent.mkdir(exist_ok=True)
            if subprocess.run(command, cwd=project).returncode or not built.is_file():
                raise SystemExit(f"Build failed for {label}.")
            target = output / label
            target.mkdir(exist_ok=True, mode=0o700)
            shutil.copyfile(built, target / built.name)
            print(f"Built {target / built.name}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
