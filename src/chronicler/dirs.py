"""Platform-aware directory resolution for Chronicler.

XDG environment variables are checked first on all platforms (including
Windows), falling back to platformdirs defaults.
"""

import os
from pathlib import Path

import platformdirs

_APP_NAME = "chronicler"


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / _APP_NAME
    return Path(platformdirs.user_config_dir(_APP_NAME, appauthor=False))


def data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / _APP_NAME
    return Path(platformdirs.user_data_dir(_APP_NAME, appauthor=False))


def cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / _APP_NAME
    return Path(platformdirs.user_cache_dir(_APP_NAME, appauthor=False))


def config_path() -> Path:
    return config_dir() / "config.toml"
