"""Shared pytest fixtures: generated HLS samples and the fixture server (SDD §8 M0)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.fixtures.media import HlsSamples, generate_samples
from tests.fixtures.server import FixtureServer


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
