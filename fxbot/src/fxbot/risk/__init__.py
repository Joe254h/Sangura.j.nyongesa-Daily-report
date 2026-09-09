"""Risk: how much, and the veto over everything.

``sizing`` and ``exposure`` are pure. ``governor`` and ``state`` touch the filesystem
because the kill switch has to survive a restart (§0.5), and nothing else.
"""

from fxbot.risk.exposure import check_exposure
from fxbot.risk.sizing import SizingResult, position_size

__all__ = ["SizingResult", "check_exposure", "position_size"]
