"""Tests for chronicler.dirs module."""

import os
from pathlib import Path
from unittest.mock import patch

from chronicler.dirs import cache_dir, config_dir, config_path, data_dir


class TestXDGOverride:
    """XDG env vars take precedence on all platforms."""

    def test_config_dir_respects_xdg(self, tmp_path):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
            assert config_dir() == tmp_path / "chronicler"

    def test_data_dir_respects_xdg(self, tmp_path):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(tmp_path)}):
            assert data_dir() == tmp_path / "chronicler"

    def test_cache_dir_respects_xdg(self, tmp_path):
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(tmp_path)}):
            assert cache_dir() == tmp_path / "chronicler"


class TestPlatformdirsFallback:
    """Without XDG vars, platformdirs provides the base path."""

    def test_config_dir_falls_back_to_platformdirs(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
        with patch.dict(os.environ, env, clear=True):
            result = config_dir()
            assert result.name == "chronicler"
            assert isinstance(result, Path)

    def test_data_dir_falls_back_to_platformdirs(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_DATA_HOME"}
        with patch.dict(os.environ, env, clear=True):
            result = data_dir()
            assert result.name == "chronicler"
            assert isinstance(result, Path)

    def test_cache_dir_falls_back_to_platformdirs(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_CACHE_HOME"}
        with patch.dict(os.environ, env, clear=True):
            result = cache_dir()
            assert result.name == "chronicler"
            assert isinstance(result, Path)


class TestConfigPath:
    """config_path() returns the full path to config.toml."""

    def test_config_path_is_toml_inside_config_dir(self, tmp_path):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
            assert config_path() == tmp_path / "chronicler" / "config.toml"
