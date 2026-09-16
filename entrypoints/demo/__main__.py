"""``python3 -m entrypoints.demo`` -- how the Railway service starts.

A deployment that cannot answer honestly says so in one line and exits non-zero, rather
than printing a traceback into a platform log or, worse, starting and refusing every
request one visitor at a time.
"""

from __future__ import annotations

import sys

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
