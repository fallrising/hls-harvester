"""Environment checks for ``hls-harvester doctor`` (SDD §3.1, §3.9).

Checks the Python version, FFmpeg/ffprobe >= 6.0, whether Playwright's Chromium is
installed and whether the config file is valid.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from hls_harvester.config import ConfigError, load_config

MIN_PYTHON = (3, 11)
MIN_FFMPEG = (6, 0)

_VERSION_RE = re.compile(r"\bversion\s+n?(\d+)\.(\d+)")

Which = Callable[[str], str | None]
Runner = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single doctor check."""

    name: str
    ok: bool
    detail: str
    hint: str = ""


def _run_version(argv: Sequence[str]) -> str:
    result = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return result.stdout + result.stderr


def check_python(version: tuple[int, ...] = tuple(sys.version_info[:3])) -> CheckResult:
    shown = ".".join(str(part) for part in version)
    ok = version[:2] >= MIN_PYTHON
    return CheckResult(
        "python",
        ok,
        f"Python {shown}",
        "" if ok else "hls-harvester needs Python {}.{} or newer".format(*MIN_PYTHON),
    )


def parse_ffmpeg_version(output: str) -> tuple[int, int] | None:
    """Parse ``ffmpeg -version`` / ``ffprobe -version`` output into (major, minor)."""
    first_line = output.strip().splitlines()[0] if output.strip() else ""
    match = _VERSION_RE.search(first_line)
    return (int(match[1]), int(match[2])) if match else None


def check_ffmpeg_tool(
    tool: str, *, which: Which = shutil.which, run: Runner = _run_version
) -> CheckResult:
    """Check that ``tool`` (ffmpeg or ffprobe) is on PATH and at least version 6.0."""
    hint = "install FFmpeg {}.{} or newer (it provides ffmpeg and ffprobe)".format(*MIN_FFMPEG)
    path = which(tool)
    if path is None:
        return CheckResult(tool, False, "not found on PATH", hint)
    try:
        output = run([path, "-version"])
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(tool, False, f"could not run {path}: {exc}", hint)
    version = parse_ffmpeg_version(output)
    if version is None:
        return CheckResult(tool, False, f"{path}: could not determine the version", hint)
    shown = f"{version[0]}.{version[1]}"
    if version < MIN_FFMPEG:
        return CheckResult(tool, False, f"{path}: version {shown} is too old", hint)
    return CheckResult(tool, True, f"{path}: version {shown}")


def _playwright_chromium_path() -> str:
    """Return the Chromium executable path Playwright would launch.

    Starts the Playwright driver but not the browser.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return str(playwright.chromium.executable_path)


def check_playwright_chromium(locate: Callable[[], str] = _playwright_chromium_path) -> CheckResult:
    hint = "run: playwright install chromium"
    try:
        executable = locate()
    except ImportError:
        return CheckResult(
            "playwright",
            False,
            "the playwright package is not installed",
            "reinstall hls-harvester",
        )
    except Exception as exc:  # the driver can fail in many ways; report, don't crash
        return CheckResult("playwright", False, f"could not query Playwright: {exc}", hint)
    if not Path(executable).exists():
        return CheckResult("playwright", False, "Chromium is not installed", hint)
    return CheckResult("playwright", True, f"Chromium at {executable}")


def check_config(path: Path | None = None) -> CheckResult:
    try:
        config = load_config(path)
    except ConfigError as exc:
        return CheckResult("config", False, str(exc), "fix or remove the config file")
    if config.path is None:
        return CheckResult("config", True, "no config file; using built-in defaults")
    return CheckResult("config", True, f"{config.path} is valid")


def run_checks(config_path: Path | None = None) -> list[CheckResult]:
    return [
        check_python(),
        check_ffmpeg_tool("ffmpeg"),
        check_ffmpeg_tool("ffprobe"),
        check_playwright_chromium(),
        check_config(config_path),
    ]
