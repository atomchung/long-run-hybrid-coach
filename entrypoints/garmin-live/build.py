#!/usr/bin/env python3
"""Build a Garmin sideload binary in a private directory with an existing local signer."""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
DEVICES = ("fr57042mm", "fr57047mm")
SAMPLES_PATH = "/v1/live-run/samples"


def validate_pair(endpoint: str, token: str) -> None:
    """Reject partial configurations and ambiguous URLs before writing a secret."""
    if not endpoint or not token:
        raise ValueError("Provisioning needs both LIVE_COACH_ENDPOINT and LIVE_COACH_DEVICE_TOKEN")
    try:
        url = urlsplit(endpoint)
        port = url.port
    except ValueError:
        raise ValueError("Invalid HTTPS endpoint") from None
    if (not endpoint.startswith("https://") or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment
            or not url.path.endswith(SAMPLES_PATH) or port == 0
            or "\\" in endpoint or any(ord(c) <= 32 or ord(c) == 127 for c in endpoint)):
        raise ValueError("Use an HTTPS endpoint ending in /v1/live-run/samples, without credentials, query or fragment")
    if not re.fullmatch(r"[a-zA-Z0-9._~+/-]+=*", token):
        raise ValueError("Device token contains an invalid bearer-token character")


def under_git_directory(path: Path) -> bool:
    return any((directory / ".git").exists() for directory in (path, *path.parents))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=DEVICES, action="append", help="default: build both FR570 sizes")
    parser.add_argument("--output-dir", required=True, type=Path, help="private output directory outside a Git checkout")
    parser.add_argument("--developer-key", type=Path, default=os.environ.get("CIQ_SIGNING_KEY"),
                        help="path to an existing private Garmin DER signing key, or set CIQ_SIGNING_KEY")
    parser.add_argument("--monkeyc", default="monkeyc", help="Connect IQ compiler path")
    parser.add_argument("--provision", action="store_true",
                        help="embed the HTTPS endpoint and a dedicated device write token from the environment")
    parser.add_argument("--test", action="store_true", help="compile the on-device unit tests with the uploader disabled")
    args = parser.parse_args()
    if args.provision and args.test:
        parser.error("Tests use the disabled configuration")
    signer = args.developer_key.expanduser().resolve() if args.developer_key else None
    if not signer or not signer.is_file() or under_git_directory(signer):
        parser.error("An existing DER signing key outside a Git checkout is required")
    compiler = shutil.which(args.monkeyc)
    if not compiler:
        parser.error("Connect IQ compiler unavailable; set --monkeyc or add monkeyc to PATH")
    output = args.output_dir.expanduser().resolve()
    if under_git_directory(output):
        parser.error("Choose an output directory outside a Git checkout")
    endpoint = os.environ.get("LIVE_COACH_ENDPOINT", "")
    token = os.environ.get("LIVE_COACH_DEVICE_TOKEN", "")
    if args.provision:
        try:
            validate_pair(endpoint, token)
        except ValueError as error:
            parser.error(str(error))
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.stat().st_mode & 0o077:
        parser.error("Use a private output directory (mode 0700)")
    compiler_env = {key: value for key, value in os.environ.items()
                    if key not in {"LIVE_COACH_DEVICE_TOKEN", "LIVE_COACH_ENDPOINT"}}
    with tempfile.TemporaryDirectory(prefix="live-coach-ciq-") as tmp:
        project = Path(tmp)
        for name in ("source", "resources"):
            shutil.copytree(ROOT / name, project / name)
        for name in ("manifest.xml", "monkey.jungle"):
            shutil.copyfile(ROOT / name, project / name)
        if args.test:
            shutil.copytree(ROOT / "tests", project / "tests")
            with (project / "monkey.jungle").open("a", encoding="utf-8") as jungle:
                jungle.write("base.sourcePath = source;tests\n")
        if args.provision:
            properties = project / "resources" / "properties" / "properties.xml"
            tree = ET.parse(properties)
            tree.find("property[@id='Endpoint']").text = endpoint
            tree.find("property[@id='DeviceToken']").text = token
            tree.write(properties, encoding="utf-8", xml_declaration=True)
        for device in dict.fromkeys(args.device or DEVICES):
            # The filename is also the name the Garmin simulator/sideload process sees.
            partial = project / ("LiveTest.prg" if args.test else "LiveCoach.prg")
            cmd = [str(Path(compiler).resolve()), "-f", "monkey.jungle", "-d", device, "-y", str(signer),
                   "-o", str(partial), "-l", "1", "-w", "-t" if args.test else "-r"]
            if args.provision:
                # Compiler diagnostics can include the source line of an invalid resource.
                result = subprocess.run(cmd, cwd=project, env=compiler_env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                result = subprocess.run(cmd, cwd=project, env=compiler_env)
            if result.returncode or not partial.is_file():
                raise SystemExit(f"Build failed for {device}. Run without --provision for non-secret diagnostics.")
            device_dir = output / device
            device_dir.mkdir(exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(dir=device_dir, delete=False) as result_file:
                temporary_result = Path(result_file.name)
                try:
                    with partial.open("rb") as source:
                        shutil.copyfileobj(source, result_file)
                    result_file.flush()
                    os.replace(temporary_result, device_dir / partial.name)
                finally:
                    temporary_result.unlink(missing_ok=True)
            print(f"Built {device_dir / partial.name}")


if __name__ == "__main__":
    main()
