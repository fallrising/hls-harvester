"""Configuration file loading and per-site merging (SDD §3.8 "設定檔").

The file lives at ``$XDG_CONFIG_HOME/hls-harvester/config.toml`` (``~/.config`` when the
variable is unset) or, on macOS, ``~/Library/Application Support/hls-harvester/config.toml``.

Precedence, lowest to highest: built-in defaults, ``[defaults]``, the most specific
matching ``[sites."<domain>"]`` section, command-line flags.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hls_harvester.errors import UsageError

APP_NAME = "hls-harvester"
CONFIG_FILENAME = "config.toml"


class ConfigError(UsageError):
    """The configuration file is unreadable or invalid (exit code 2)."""


@dataclass(frozen=True)
class Settings:
    """Effective settings for one run, after all layers are merged."""

    output_dir: Path = Path()
    """Directory for finished downloads; ``~`` is expanded."""
    capture_timeout_s: float = 45.0
    """Capture deadline (SDD §3.2 item 2)."""
    concurrency_per_host: int = 4
    """Concurrent connections per upstream host (SDD §3.5 item 3)."""
    headless: bool = True
    """Run the capture browser without a window (SDD §3.2 item 1)."""
    play_selector: str | None = None
    """Site-specific selector of the play button (SDD §3.2 item 3, §3.7)."""
    cache_ttl_s: float = 600.0
    """Capture cache lifetime; used only by the v2 resident service (SDD §3.3)."""
    request_timeout_s: float = 30.0
    """Per-attempt upstream request timeout (SDD §3.5 item 3)."""
    max_retries: int = 5
    """Retries of a transient upstream error (SDD §3.5 item 3)."""
    retry_backoff_s: float = 0.5
    """Base delay of the exponential retry backoff."""
    browser_executable: Path | None = None
    """Chromium binary to use instead of the one ``playwright install`` provides."""


@dataclass(frozen=True)
class ConfigFile:
    """The parsed configuration file, before it is resolved for a particular site."""

    defaults: Mapping[str, Any] = field(default_factory=dict)
    sites: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    path: Path | None = None


_FIELDS = {f.name: f for f in dataclasses.fields(Settings)}
_INT_MINIMUMS = {"concurrency_per_host": 1, "max_retries": 0}


def default_config_path(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the platform's config file path (SDD §3.8)."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        xdg = env.get("XDG_CONFIG_HOME", "")
        # The XDG spec says relative values must be ignored.
        base = Path(xdg) if xdg and os.path.isabs(xdg) else home / ".config"
    return base / APP_NAME / CONFIG_FILENAME


def _check_value(key: str, value: Any, where: str) -> Any:
    """Validate a single setting and return it converted to its Settings type."""
    if key not in _FIELDS:
        known = ", ".join(sorted(_FIELDS))
        raise ConfigError(f"{where}: unknown setting {key!r} (known: {known})")
    if key in {"output_dir", "browser_executable"}:
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{where}: {key} must be a non-empty string")
        return Path(value).expanduser()
    if key == "headless":
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: headless must be true or false")
        return value
    if key == "play_selector":
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{where}: play_selector must be a non-empty string")
        return value
    if key in _INT_MINIMUMS:
        minimum = _INT_MINIMUMS[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ConfigError(f"{where}: {key} must be an integer >= {minimum}")
        return value
    # Remaining fields are positive durations in seconds.
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigError(f"{where}: {key} must be a positive number of seconds")
    return float(value)


def _check_table(table: Any, where: str) -> dict[str, Any]:
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    return {key: _check_value(key, value, f"{where}.{key}") for key, value in table.items()}


def parse_config(data: Mapping[str, Any], path: Path | None = None) -> ConfigFile:
    """Validate an already-decoded TOML document."""
    where = str(path) if path else "config"
    unknown = set(data) - {"defaults", "sites"}
    if unknown:
        raise ConfigError(f"{where}: unknown top-level table(s): {', '.join(sorted(unknown))}")
    defaults = _check_table(data.get("defaults", {}), f"{where}: [defaults]")
    raw_sites = data.get("sites", {})
    if not isinstance(raw_sites, dict):
        raise ConfigError(f"{where}: [sites] must be a table of site tables")
    sites: dict[str, dict[str, Any]] = {}
    for domain, table in raw_sites.items():
        normalized = domain.strip().lower().rstrip(".")
        if not normalized or "/" in normalized or ":" in normalized:
            raise ConfigError(f"{where}: site key {domain!r} must be a bare domain name")
        if normalized in sites:
            raise ConfigError(f"{where}: site {domain!r} is defined more than once")
        sites[normalized] = _check_table(table, f'{where}: [sites."{domain}"]')
    return ConfigFile(defaults=defaults, sites=sites, path=path)


def load_config(path: Path | None = None) -> ConfigFile:
    """Load and validate the config file. A missing file yields an empty config."""
    path = default_config_path() if path is None else path
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ConfigFile(path=None)
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read config file: {exc.strerror}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    return parse_config(data, path)


def match_site(config: ConfigFile, host: str | None) -> str | None:
    """Return the most specific site key matching ``host``.

    A key matches the host itself and its subdomains (``example.com`` matches
    ``www.example.com``); the longest matching key wins.
    """
    if not host:
        return None
    host = host.strip().lower().rstrip(".")
    matches = [key for key in config.sites if host == key or host.endswith("." + key)]
    return max(matches, key=len) if matches else None


def resolve_settings(
    config: ConfigFile,
    host: str | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Merge built-in defaults, ``[defaults]``, the matching site and CLI ``overrides``.

    ``overrides`` come from command-line flags; ``None`` values mean "flag not given"
    and are ignored.
    """
    merged: dict[str, Any] = dict(config.defaults)
    site = match_site(config, host)
    if site is not None:
        merged.update(config.sites[site])
    for key, value in (overrides or {}).items():
        if value is not None:
            merged[key] = _check_value(key, value, f"command-line option for {key}")
    return Settings(**merged)
