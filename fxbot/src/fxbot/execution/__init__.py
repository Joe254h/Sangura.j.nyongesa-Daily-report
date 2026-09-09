"""Execution adapters. The engine holds a :class:`~fxbot.execution.broker.Broker`, never
``MetaTrader5`` itself.
"""

from fxbot.execution.broker import Broker
from fxbot.execution.filling import filling_candidates, negotiate_filling_mode

__all__ = ["Broker", "filling_candidates", "negotiate_filling_mode"]
