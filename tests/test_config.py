"""Tests for configuration loading and merging (SDD §3.8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hls_harvester.config import (
    ConfigError,
    ConfigFile,
    Settings,
    default_config_path,
    load_config,
    match_site,
    resolve_settings,
)
from hls_harvester.errors import ExitCode

SDD_EXAMPLE = """
[defaults]
output_dir = "~/Videos"
capture_timeout_s = 45
concurrency_per_host = 4
headless = true

[sites."example.com"]
headless = false
play_selector = "button.play"
cache_ttl_s = 600   # v2 only
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_default_path_linux_xdg(tmp_path: Path) -> None:
    path = default_config_path(platform="linux", env={"XDG_CONFIG_HOME": "/xdg"}, home=tmp_path)
    assert path == Path("/xdg/hls-harvester/config.toml")


@pytest.mark.parametrize("xdg", ["", "relative/dir"])
def test_default_path_linux_fallback(tmp_path: Path, xdg: str) -> None:
    path = default_config_path(platform="linux", env={"XDG_CONFIG_HOME": xdg}, home=tmp_path)
    assert path == tmp_path / ".config" / "hls-harvester" / "config.toml"


def test_default_path_macos(tmp_path: Path) -> None:
    path = default_config_path(platform="darwin", env={"XDG_CONFIG_HOME": "/xdg"}, home=tmp_path)
    assert path == tmp_path / "Library/Application Support/hls-harvester/config.toml"


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nope.toml")
    assert config.path is None
    assert resolve_settings(config) == Settings()


def test_built_in_defaults_match_sdd() -> None:
    settings = Settings()
    assert settings.capture_timeout_s == 45
    assert settings.concurrency_per_host == 4
    assert settings.headless is True


def test_sdd_example_loads(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SDD_EXAMPLE))
    settings = resolve_settings(config, "other.org")
    assert settings.output_dir == Path("~/Videos").expanduser()
    assert settings.headless is True
    assert settings.play_selector is None


def test_site_section_overrides_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SDD_EXAMPLE))
    settings = resolve_settings(config, "www.Example.com")
    assert settings.headless is False
    assert settings.play_selector == "button.play"
    assert settings.cache_ttl_s == 600
    # Untouched keys still come from [defaults].
    assert settings.output_dir == Path("~/Videos").expanduser()


def test_cli_overrides_win_and_none_is_ignored(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SDD_EXAMPLE))
    settings = resolve_settings(
        config, "example.com", overrides={"headless": True, "concurrency_per_host": None}
    )
    assert settings.headless is True
    assert settings.concurrency_per_host == 4


def test_invalid_cli_override_is_rejected() -> None:
    with pytest.raises(ConfigError, match="concurrency_per_host"):
        resolve_settings(ConfigFile(), overrides={"concurrency_per_host": 0})


def test_most_specific_site_wins() -> None:
    config = ConfigFile(
        sites={"example.com": {"headless": False}, "video.example.com": {"headless": True}}
    )
    assert match_site(config, "a.video.example.com") == "video.example.com"
    assert match_site(config, "www.example.com") == "example.com"
    assert match_site(config, "notexample.com") is None
    assert match_site(config, None) is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[defaults]\nheadless = 'yes'", "headless"),
        ("[defaults]\nconcurrency_per_host = 0", "concurrency_per_host"),
        ("[defaults]\nconcurrency_per_host = true", "concurrency_per_host"),
        ("[defaults]\ncapture_timeout_s = -1", "capture_timeout_s"),
        ("[defaults]\noutput_dir = ''", "output_dir"),
        ("[defaults]\nheadles = true", "unknown setting 'headles'"),
        ("[default]\nheadless = true", "unknown top-level"),
        ('[sites."https://example.com"]\nheadless = true', "bare domain"),
        ("sites = 3", r"\[sites\]"),
        ("[defaults\n", "invalid TOML"),
    ],
)
def test_invalid_config(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message) as info:
        load_config(write(tmp_path, text))
    assert info.value.exit_code == ExitCode.USAGE


def test_duplicate_site_after_normalisation(tmp_path: Path) -> None:
    text = '[sites."Example.com"]\nheadless = true\n[sites."example.com"]\nheadless = false\n'
    with pytest.raises(ConfigError, match="more than once"):
        load_config(write(tmp_path, text))


def test_float_and_int_durations(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "[defaults]\ncapture_timeout_s = 12.5\n"))
    assert resolve_settings(config).capture_timeout_s == 12.5
