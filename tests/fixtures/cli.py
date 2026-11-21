"""Helpers to run the ``hls-harvester`` CLI in a subprocess against the fixture server.

The fixture server runs in the test's event loop, so the CLI (which runs its own loop)
must be a separate process. Each run gets an isolated config file.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BROWSER_ENV = "HLSH_TEST_CHROMIUM"
"""Optional path of a Chromium binary for tests, when Playwright's own build is missing."""


def browser_executable() -> str | None:
    return os.environ.get(BROWSER_ENV) or None


def _default_browsers_path() -> Path:
    """Playwright's default browser cache for the real user (before HOME is replaced)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "ms-playwright"


def cli_env(home: Path, config: str = "") -> dict[str, str]:
    """Environment with an isolated config file (XDG on Linux, ~/Library on macOS)."""
    executable = browser_executable()
    if executable and "browser_executable" not in config:
        if "[defaults]" not in config:
            config = "[defaults]\n" + config
        config = config.replace(
            "[defaults]\n", f'[defaults]\nbrowser_executable = "{executable}"\n', 1
        )
    env = dict(os.environ)
    # Playwright finds its browsers under $HOME unless told otherwise; keep the real ones.
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_default_browsers_path()))
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    for base in (home / ".config", home / "Library" / "Application Support"):
        (base / "hls-harvester").mkdir(parents=True, exist_ok=True)
        (base / "hls-harvester" / "config.toml").write_text(config)
    return env


async def start_cli(env: dict[str, str], *args: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "hls_harvester",
        *args,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


CLI_TIMEOUT_S = 240


async def run_cli(env: dict[str, str], *args: str) -> tuple[int, str, str]:
    proc = await start_cli(env, *args)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT_S)
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()
