"""Service entry point (§13.4). A thin wrapper over ``fxbot live``.

NSSM points at this via ``python -m fxbot.cli live --env live``; this file exists so a
human can start the same thing by hand without remembering the flags, and so the service
has a single obvious target to name in the runbook.
"""

from __future__ import annotations

import argparse

from fxbot.cli import live


def main() -> None:
    """Start the live loop for the given environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="live", choices=["demo", "live"])
    args = parser.parse_args()
    live(env=args.env, max_cycles=0)


if __name__ == "__main__":
    main()
