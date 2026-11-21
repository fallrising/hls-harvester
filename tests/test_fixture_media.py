"""Smoke tests: every generated HLS sample is well formed and playable over HTTP."""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

import pytest

from tests.fixtures.media import AES_KEY, DURATION_S, HlsSamples
from tests.fixtures.server import FixtureServer

pytestmark = pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")


async def ffprobe(url: str) -> dict[str, Any]:
    """Probe ``url`` with ffprobe without blocking the event loop the server runs on."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height,sample_rate",
        "-of",
        "json",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    assert proc.returncode == 0, err.decode()
    result: dict[str, Any] = json.loads(out)
    return result


def read(samples: HlsSamples, name: str) -> str:
    return (samples.root / samples.playlist(name)).read_text()


def test_vod_fmp4_playlist(hls_samples: HlsSamples) -> None:
    text = read(hls_samples, "vod_fmp4")
    assert text.startswith("#EXTM3U")
    assert '#EXT-X-MAP:URI="init.mp4"' in text
    assert "#EXT-X-ENDLIST" in text
    assert text.count("#EXTINF:") == DURATION_S


def test_master_playlist(hls_samples: HlsSamples) -> None:
    text = read(hls_samples, "master")
    assert text.count("#EXT-X-STREAM-INF:") == 3
    assert '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud"' in text
    for size in ("160x90", "320x180", "640x360"):
        assert f"RESOLUTION={size}" in text
    for child in ("v0", "v1", "v2", "audio"):
        assert (hls_samples.root / "master" / child / "index.m3u8").is_file()


def test_aes128_playlist(hls_samples: HlsSamples) -> None:
    text = read(hls_samples, "aes128")
    assert '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x' in text
    assert (hls_samples.root / "aes128" / "key.bin").read_bytes() == AES_KEY
    # The key-info file contains a local path and must not be served.
    assert not list(hls_samples.root.rglob("*.keyinfo"))


def test_byterange_playlist(hls_samples: HlsSamples) -> None:
    text = read(hls_samples, "byterange")
    assert text.count("#EXT-X-BYTERANGE:") == DURATION_S
    assert "media.ts" in text


@pytest.mark.parametrize("name", ["discontinuity", "discontinuity_mismatch"])
def test_discontinuity_playlist(hls_samples: HlsSamples, name: str) -> None:
    text = read(hls_samples, name)
    assert text.count("#EXT-X-DISCONTINUITY") == 2
    assert text.count("#EXTINF:") == 3 * DURATION_S


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("sample_aes", "METHOD=SAMPLE-AES,"),
        ("widevine", 'KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"'),
    ],
)
def test_drm_tag_only_playlists(hls_samples: HlsSamples, name: str, expected: str) -> None:
    text = read(hls_samples, name)
    assert expected in text
    # Tag only: there is deliberately no media behind these playlists.
    assert not list((hls_samples.root / name).glob("*.ts"))


@pytest.mark.parametrize("name", ["vod_fmp4", "aes128", "byterange"])
async def test_samples_play_over_http(
    fixture_server: FixtureServer, hls_samples: HlsSamples, name: str
) -> None:
    info = await ffprobe(fixture_server.media_url(hls_samples.playlist(name)))
    kinds = sorted(s["codec_type"] for s in info["streams"])
    assert kinds == ["audio", "video"]
    assert float(info["format"]["duration"]) == pytest.approx(DURATION_S, abs=0.5)


async def test_master_over_http(fixture_server: FixtureServer, hls_samples: HlsSamples) -> None:
    info = await ffprobe(fixture_server.media_url(hls_samples.playlist("master")))
    heights = sorted(s["height"] for s in info["streams"] if s["codec_type"] == "video")
    assert heights == [90, 180, 360]
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    # ffprobe followed the master into every variant and the audio rendition.
    paths = {r.path for r in fixture_server.requests}
    for child in ("v0", "v1", "v2", "audio"):
        assert f"/media/master/{child}/index.m3u8" in paths


async def test_aes_key_is_fetched(fixture_server: FixtureServer, hls_samples: HlsSamples) -> None:
    await ffprobe(fixture_server.media_url(hls_samples.playlist("aes128")))
    assert fixture_server.requests_to("/media/aes128/key.bin")


async def test_discontinuity_over_http(
    fixture_server: FixtureServer, hls_samples: HlsSamples
) -> None:
    info = await ffprobe(fixture_server.media_url(hls_samples.playlist("discontinuity")))
    assert sorted(s["codec_type"] for s in info["streams"]) == ["audio", "video"]
    assert len(fixture_server.requests_to("/media/discontinuity/p?/seg_*.ts")) >= 1


async def test_mismatch_periods_differ(
    fixture_server: FixtureServer, hls_samples: HlsSamples
) -> None:
    first = await ffprobe(fixture_server.media_url("discontinuity_mismatch/p0/index.m3u8"))
    middle = await ffprobe(fixture_server.media_url("discontinuity_mismatch/p1/index.m3u8"))

    def params(info: dict[str, Any]) -> tuple[int, int, str]:
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        return video["width"], video["height"], audio["sample_rate"]

    assert params(first) == (640, 360, "48000")
    assert params(middle) == (320, 180, "44100")
