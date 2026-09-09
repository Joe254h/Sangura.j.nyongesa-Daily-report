"""Operational concerns: logging, alerting, health. The only layer that reads a wall clock."""

from fxbot.ops.health import utc_now
from fxbot.ops.logging import configure_logging

__all__ = ["configure_logging", "utc_now"]
