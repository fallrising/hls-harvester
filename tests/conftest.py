"""Shared pytest fixtures: generated HLS samples and the fixture server (SDD §8 M0)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.cli import browser_executable
from tests.fixtures.media import HlsSamples, generate_samples
from tests.fixtures.server import FixtureServer
from tests.fixtures.site import Site, install_site


@pytest.fixture(scope="session")
def hls_samples(tmp_path_factory: pytest.TempPathFactory) -> HlsSamples:
    """Synthetic HLS samples, generated once per test session with FFmpeg."""
    base = tmp_path_factory.mktemp("hls")
    return generate_samples(base / "media", base / "work")


@pytest.fixture
async def fixture_server(hls_samples: HlsSamples) -> AsyncIterator[FixtureServer]:
    """A fresh two-host fixture server (origin + CDN) on random loopback ports."""
    async with FixtureServer(hls_samples.root) as server:
        yield server


@pytest.fixture
def site(fixture_server: FixtureServer, hls_samples: HlsSamples) -> Site:
    """The capture test site (pages on the origin, media on the CDN)."""
    return install_site(fixture_server, hls_samples)


@pytest.fixture
async def browser() -> AsyncIterator[Any]:
    """A headless Chromium; ``HLSH_TEST_CHROMIUM`` may point at a local build."""
    from playwright.async_api import async_playwright

    from hls_harvester.capturer import launch_browser

    async with async_playwright() as playwright:
        instance = await launch_browser(playwright, headless=True, executable=browser_executable())
        yield instance
        await instance.close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An isolated HOME for CLI subprocess runs."""
    path = tmp_path / "home"
    path.mkdir()
    return path
