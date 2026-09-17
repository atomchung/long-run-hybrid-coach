"""``python3 -m entrypoints.demo`` -- how the Railway service starts.

A deployment that cannot answer honestly says so in one line and exits non-zero, rather
than printing a traceback into a platform log or, worse, starting and refusing every
request one visitor at a time.
"""

from __future__ import annotations

from pathlib import Path
import sys

# Railway may invoke a custom start command from a platform-owned working directory.
# Make direct-file execution as independent of that directory as module execution is.
if __package__ in {None, ""}:  # pragma: no cover - exercised by the container command
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "entrypoints.demo"

from .config import ConfigError
from .server import StartupError, serve


def main() -> int:
    try:
        serve()
    except (ConfigError, StartupError) as error:
        print(f"the demo service cannot start: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
