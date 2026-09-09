"""Configuration: one pydantic v2 model tree, loaded once, frozen, injected downward."""

from fxbot.config.loader import load_config
from fxbot.config.schema import AppConfig

__all__ = ["AppConfig", "load_config"]
