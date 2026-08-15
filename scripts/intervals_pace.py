#!/usr/bin/env python3
"""Convert between running pace and the unit intervals.icu actually stores.

Sport settings hold the run threshold pace in **metres per second** -- `threshold_pace`
on the API entry whose `types` contain `Run` (`pace_threshold` as the MCP tool names
the same field). Both the tool's parameter description and its response string present
it as `M:SS /km`, so a value written as if it were min/km is silently accepted and reads
back looking correct. That trap has been walked into three times; this script exists so
the conversion is never done by hand again.

Development/operator tool. Nothing at runtime imports it, and it writes nothing --
it prints the number a human then writes through the provider's own interface.

    $ python3 scripts/intervals_pace.py 6:10      -> 2.7027 (write this to the API)
    $ python3 scripts/intervals_pace.py 2.7027    -> 6:10 /km (what that value means)

After writing, verify with a temporary calendar event containing `- 10m 100% pace`:
its `distance_meters` must equal the pace's distance over 10 minutes (6:10/km ->
1622m). Reading the settings back is NOT a verification -- the display always looks
right. Delete the event afterwards.
"""

from __future__ import annotations

import sys


def pace_to_mps(pace: str) -> float:
    """'6:10' (min:sec per km) -> metres per second."""
    minutes, _, seconds = pace.partition(":")
    total_seconds = int(minutes) * 60 + int(seconds or 0)
    return 1000 / total_seconds


def mps_to_pace(mps: float) -> str:
    """Metres per second -> 'M:SS /km'."""
    seconds_per_km = round(1000 / mps)
    return f"{seconds_per_km // 60}:{seconds_per_km % 60:02d} /km"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    value = argv[1]
    if ":" in value:
        mps = pace_to_mps(value)
        print(f"{mps:.4f}  # m/s -- write this to pace_threshold; 10 min covers {mps * 600:.0f}m")
    else:
        print(mps_to_pace(float(value)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
