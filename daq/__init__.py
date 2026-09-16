"""A configuration-driven data acquisition platform for liquid-cooling test rigs."""

from .config import ConfigError, RigConfig, load_config
from .server import create_app

__version__ = "1.0.0"
__all__ = ["ConfigError", "RigConfig", "create_app", "load_config"]
