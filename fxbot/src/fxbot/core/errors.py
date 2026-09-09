"""The full error hierarchy (§4).

A :class:`FatalError` always drives the governor to ``HALTED`` (§10.1); the non-fatal
errors are recoverable conditions the loop is expected to survive.
"""

from __future__ import annotations


class FxBotError(Exception):
    """Base class for every error raised by this package."""


class FatalError(FxBotError):
    """Unrecoverable: the engine halts and requires a manual reset."""


class ConfigError(FatalError):
    """Configuration is missing, malformed or contains an unrecognised key."""


class SymbolResolutionError(FatalError):
    """A configured symbol could not be resolved to exactly one broker symbol."""


class ClockError(FatalError):
    """The broker server offset could not be determined or is inconsistent."""


class SizingError(FatalError):
    """Position sizing was asked to guess a value it must never guess."""


class ReconciliationError(FatalError):
    """The bot's view of open positions disagrees with the broker's."""


class BrokerConnectionError(FxBotError):
    """Retryable connectivity failure; three consecutive occurrences halt the bot."""


class DataUnavailableError(FxBotError):
    """Per-symbol data problem: blocks new entries, never blocks position management."""
