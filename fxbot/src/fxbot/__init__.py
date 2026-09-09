"""fxbot - trend-following FX bot for MetaTrader 5.

The package is layered (§2): ``core`` and ``indicators`` are pure and depend on nothing
in the project; ``strategy`` and ``risk.sizing``/``risk.exposure`` are pure trading logic;
``data``/``execution`` are adapters onto MetaTrader 5; ``runtime`` orchestrates.
"""

__version__ = "1.0.0"
