"""Enforce the per-package coverage floors (§12.1).

``--cov-fail-under`` can only express one number, and §12.1 gives four:
``risk/`` 100%, ``strategy/`` 95%, ``execution/`` 90%, everything else 80%. This reads the
Cobertura XML pytest-cov writes and checks each floor separately, so a well-covered
indicator module cannot mask an untested branch in the governor.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

FLOORS: dict[str, float] = {
    "fxbot/risk": 100.0,
    "fxbot/strategy": 95.0,
    "fxbot/execution": 90.0,
    "": 80.0,
}
"""Longest matching prefix wins."""


def floor_for(path: str) -> tuple[str, float]:
    """Return the ``(prefix, floor)`` that applies to ``path``."""
    best = ("", FLOORS[""])
    for prefix, floor in FLOORS.items():
        if prefix and path.startswith(prefix) and len(prefix) > len(best[0]):
            best = (prefix, floor)
    return best


def main(argv: list[str]) -> int:
    """Check every floor and report each group's actual coverage."""
    report = Path(argv[1] if len(argv) > 1 else "coverage.xml")
    if not report.is_file():
        print(f"coverage report not found: {report}")
        return 2

    totals: dict[str, list[int]] = {prefix: [0, 0] for prefix in FLOORS}
    for cls in ET.parse(report).getroot().iter("class"):
        filename = str(cls.get("filename", "")).replace("\\", "/")
        if "src/" in filename:
            filename = filename.split("src/", 1)[1]
        prefix, _ = floor_for(filename)
        covered = sum(1 for line in cls.iter("line") if int(line.get("hits", "0")) > 0)
        total = sum(1 for _ in cls.iter("line"))
        totals[prefix][0] += covered
        totals[prefix][1] += total

    failed = False
    for prefix, floor in sorted(FLOORS.items(), key=lambda kv: -len(kv[0])):
        covered, total = totals[prefix]
        if total == 0:
            continue
        percent = 100.0 * covered / total
        label = prefix or "everything else"
        status = "ok " if percent >= floor else "FAIL"
        print(f"{status} {label:<20} {percent:6.2f}%  (floor {floor:.0f}%)")
        failed |= percent < floor
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
