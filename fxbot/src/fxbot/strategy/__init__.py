"""The single implementation of every trading rule (§0.1).

This package is pure: bars in, intents out. It imports ``core``, ``config`` and
``indicators`` and nothing else. The backtester and the live engine are thin adapters
that feed it bars and execute its intents; a rule that exists in only one of them is a
bug.
"""

from fxbot.strategy.manage import chandelier_stop, manage_position
from fxbot.strategy.regime import classify_regime, htf_bias
from fxbot.strategy.trend_donchian import generate_signal

__all__ = ["chandelier_stop", "classify_regime", "generate_signal", "htf_bias",
           "manage_position"]
