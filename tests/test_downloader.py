"""Download tests against the fixture server (SDD §3.5, §7; milestone M1).

Uses the real Playwright request transport and real FFmpeg/ffprobe.
"""

from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import web

from hls_harvester.downloader import download, temp_output_for, work_dir_for
from hls_harvester.errors import ExitCode, HarvesterError
from hls_harvester.remux import ffprobe
from hls_harvester.upstream import RetryPolicy, Upstream, open_playwright_transport
from tests.fixtures.media import LONG_DURATION_S, HlsSamples
from tests.fixtures.server import (
    CDN,
    Delay,
    FixtureServer,
    Respond,
    Rule,
    expire_after,
    unavailable,
)

FAST = RetryPolicy(backoff_base_s=0.02, backoff_max_s=0.2, request_timeout_s=5)


@pytest.fixture
async def upstream() -> AsyncIterator[Upstream]:
    async with open_playwright_transport() as transport:
        yield Upstream(transport, policy=FAST, concurrency_per_host=4)


@pytest.fixture
def out(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path


async def fetch(
    server: FixtureServer,
    samples: HlsSamples,
    name: str,
    upstream: Upstream,
    out: Path,
    **kwargs: object,
) -> Path:
    url = server.media_url(samples.playlist(name)) + "?token=SECRET123"
    return await download(url, upstream=upstream, output_dir=out, name=name, **kwargs)  # type: ignore[arg-type]


async def decode_errors(path: Path) -> str:
    """Decode the whole file; FFmpeg prints timestamp/decoding problems at -v error."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(path),
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    assert proc.returncode == 0
    return err.decode()


def assert_clean(out: Path, final: Path) -> None:
    assert not work_dir_for(final).exists()
    assert not temp_output_for(final).exists()
    assert sorted(p.name for p in out.iterdir()) == [final.name]


# --- complete downloads ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["vod_fmp4", "aes128", "byterange"])
async def test_download_samples(
    fixture_server: FixtureServer,
    hls_samples: HlsSamples,
    upstream: Upstream,
    out: Path,
    name: str,
) -> None:
    final = await fetch(fixture_server, hls_samples, name, upstream, out)
    assert final == out / f"{name}.mp4"
    probe = await ffprobe(final)
    assert probe.kinds == {"video", "audio"}
    assert probe.duration == pytest.approx(2, abs=0.5)
    assert_clean(out, final)


async def test_aes128_output_is_decrypted(
    fixture_server: FixtureServer, hls_samples: HlsSamples, upstream: Upstream, out: Path
) -> None:
    final = await fetch(fixture_server, hls_samples, "aes128", upstream, out)
    assert await decode_errors(final) == ""
    assert fixture_server.requests_to("/media/aes128/key.bin")


async def test_master_quality_and_separate_audio(
    fixture_server: FixtureServer, hls_samples: HlsSamples, upstream: Upstream, out: Path
) -> None:
    final = await fetch(fixture_server, hls_samples, "master", upstream, out, quality="180p")
    probe = await ffprobe(final)
    video = probe.first("video")
    assert video is not None
    assert (video.width, video.height) == (320, 180)
    assert probe.first("audio") is not None
    paths = {r.path for r in fixture_server.requests}
    assert "/media/master/v1/index.m3u8" in paths
    assert "/media/master/audio/index.m3u8" in paths
    assert not any(p.startswith(("/media/master/v0/", "/media/master/v2/")) for p in paths)


async def test_unknown_quality_is_usage_error(
    fixture_server: FixtureServer, hls_samples: HlsSamples, upstream: Upstream, out: Path
) -> None:
    with pytest.raises(HarvesterError, match="90p, 180p, 360p") as info:
        await fetch(fixture_server, hls_samples, "master", upstream, out, quality="4k")
    assert info.value.exit_code == ExitCode.USAGE


# --- 不連續區段 (discontinuities) -------------------------------------------------------


async def test_discontinuity_same_parameters(
    fixture_server: FixtureServer, hls_samples: HlsSamples, upstream: Upstream, out: Path
) -> None:
    final = await fetch(fixture_server, hls_samples, "discontinuity", upstream, out)
    probe = await ffprobe(final)
    assert probe.duration == pytest.approx(6, abs=0.5)
    assert await decode_errors(final) == ""
    assert_clean(out, final)


async def test_discontinuity_mismatch_fails_with_7(
    fixture_server: FixtureServer, hls_samples: HlsSamples, upstream: Upstream, out: Path
) -> None:
    with pytest.raises(HarvesterError, match="different parameters") as info:
        await fetch(fixture_server, hls_samples, "discontinuity_mismatch", upstream, out)
    assert info.value.exit_code == ExitCode.VERIFY_FAILED
    final = out / "discontinuity_mismatch.mp4"
    assert not final.exists()
    assert not temp_output_for(final).exists()
    assert work_dir_for(final).is_dir()  # kept for --resume


async def test_discontinuity_mismatch_reencode(
    fixture_server: FixtureServer, hls_samples: HlsSamples, upstream: Upstream, out: Path
) -> None:
    final = await fetch(
        fixture_server, hls_samples, "discontinuity_mismatch", upstream, out, reencode=True
    )
    probe = await ffprobe(final)
    video, audio = probe.first("video"), probe.first("audio")
    assert video is not None
    assert audio is not None
    assert (video.width, video.height) == (640, 360)
    assert audio.sample_rate == 48000
    assert probe.duration == pytest.approx(6, abs=0.5)


# --- 不支援內容 (unsupported content) -------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tag"),
    [("sample_aes", "METHOD=SAMPLE-AES"), ("widevine", "urn:uuid:edef8ba9")],
)
async def test_drm_is_refused_with_6(
    fixture_server: FixtureServer,
    hls_samples: HlsSamples,
    upstream: Upstream,
    out: Path,
    name: str,
    tag: str,
) -> None:
    with pytest.raises(HarvesterError, match=tag) as info:
        await fetch(fixture_server, hls_samples, name, upstream, out)
    assert info.value.exit_code == ExitCode.UNSUPPORTED
    assert list(out.iterdir()) == []


async def test_live_playlist_needs_play_mode(
    fixture_server: FixtureServer, upstream: Upstream, out: Path
) -> None:
    fixture_server.add_route(
        "/live/index.m3u8",
        "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n/media/vod_long/seg_000.ts\n",
    )
    with pytest.raises(HarvesterError, match="--play") as info:
        await download(
            fixture_server.url("/live/index.m3u8", CDN), upstream=upstream, output_dir=out
        )
    assert info.value.exit_code == ExitCode.USAGE


# --- 暫時性錯誤 (transient errors) -----------------------------------------------------


async def test_transient_errors_and_concurrency_limit(
    fixture_server: FixtureServer, hls_samples: HlsSamples, out: Path
) -> None:
    segments = "/media/vod_long/seg_*.ts"
    # About 10 % of segment requests fail: a timeout, a 429 with Retry-After or a 503.
    fixture_server.add_rule(Rule(segments, Delay(3), probability=0.035))
    fixture_server.add_rule(Rule(segments, Respond(429, {"Retry-After": "1"}), probability=0.035))
    fixture_server.add_rule(Rule(segments, Respond(503), probability=0.035))
    policy = RetryPolicy(backoff_base_s=0.02, backoff_max_s=0.2, request_timeout_s=0.5)
    async with open_playwright_transport() as transport:
        upstream = Upstream(transport, policy=policy, concurrency_per_host=2)
        final = await fetch(fixture_server, hls_samples, "vod_long", upstream, out)
    probe = await ffprobe(final)
    assert probe.duration == pytest.approx(LONG_DURATION_S, abs=0.5)
    assert sum(rule.fired for rule in fixture_server.rules) > 0
    assert max(upstream.max_in_flight.values()) <= 2
    assert fixture_server.max_in_flight[CDN] <= 2


async def test_permanent_failure_keeps_work_dir_without_secrets(
    fixture_server: FixtureServer, hls_samples: HlsSamples, upstream: Upstream, out: Path
) -> None:
    fixture_server.add_rule(unavailable("/media/vod_long/seg_007.ts"))
    with pytest.raises(HarvesterError, match="gave up") as info:
        await fetch(fixture_server, hls_samples, "vod_long", upstream, out)
    assert info.value.exit_code == ExitCode.UPSTREAM_ERROR
    assert "SECRET123" not in str(info.value)
    final = out / "vod_long.mp4"
    workdir = work_dir_for(final)
    assert not final.exists()
    assert stat.S_IMODE(workdir.stat().st_mode) == 0o700
    job_path = workdir / "job.json"
    assert stat.S_IMODE(job_path.stat().st_mode) == 0o600
    text = job_path.read_text()
    assert "SECRET123" not in text
    assert "token" not in text
    job = json.loads(text)
    assert job["source"].endswith("/media/vod_long/index.m3u8?<redacted>")
    done = [s for s in job["tracks"]["main"]["segments"] if s["done"]]
    assert done
    assert all(stat.S_IMODE((workdir / s["file"]).stat().st_mode) == 0o600 for s in done)


async def test_existing_work_dir_requires_a_choice(
    fixture_server: FixtureServer, hls_samples: HlsSamples, upstream: Upstream, out: Path
) -> None:
    work_dir_for(out / "vod_fmp4.mp4").mkdir()
    with pytest.raises(HarvesterError, match="--resume") as info:
        await fetch(fixture_server, hls_samples, "vod_fmp4", upstream, out)
    assert info.value.exit_code == ExitCode.USAGE
    seen: list[Path] = []

    def restart(workdir: Path) -> str:
        seen.append(workdir)
        return "restart"

    final = await fetch(
        fixture_server, hls_samples, "vod_fmp4", upstream, out, confirm_restart=restart
    )
    assert seen == [work_dir_for(final)]
    assert final.exists()


async def test_never_overwrites(
    fixture_server: FixtureServer, hls_samples: HlsSamples, upstream: Upstream, out: Path
) -> None:
    (out / "vod_fmp4.mp4").write_bytes(b"keep")
    final = await fetch(fixture_server, hls_samples, "vod_fmp4", upstream, out)
    assert final.name == "vod_fmp4 (1).mp4"
    assert (out / "vod_fmp4.mp4").read_bytes() == b"keep"
    with pytest.raises(HarvesterError, match="already exists"):
        await fetch(
            fixture_server, hls_samples, "vod_fmp4", upstream, out, output=out / "vod_fmp4.mp4"
        )


# --- credential expiry: re-request the media playlist ----------------------------------


def tokenized_playlist(server: FixtureServer, samples: HlsSamples) -> list[int]:
    """Serve vod_long with a token per playlist request; segments 403 on stale tokens."""
    issued: list[int] = []
    body = (samples.root / samples.playlist("vod_long")).read_text()

    async def handler(request: web.Request) -> web.StreamResponse:
        issued.append(len(issued) + 1)
        token = issued[-1]
        lines = [
            f"/media/vod_long/{line}?t={token}" if line and not line.startswith("#") else line
            for line in body.splitlines()
        ]
        return web.Response(
            text="\n".join(lines) + "\n", content_type="application/vnd.apple.mpegurl"
        )

    server.add_route("/signed/index.m3u8", handler, host=CDN)
    return issued


async def test_credentials_refresh_mid_download(
    fixture_server: FixtureServer, hls_samples: HlsSamples, upstream: Upstream, out: Path
) -> None:
    issued = tokenized_playlist(fixture_server, hls_samples)
    # Token 1 expires after 12 segment requests; later tokens stay valid.
    fixture_server.add_rule(
        expire_after("/media/vod_long/seg_*", requests=12, status=403, query="t=1")
    )
    final = await download(
        fixture_server.url("/signed/index.m3u8", CDN), upstream=upstream, output_dir=out
    )
    assert issued == [1, 2]
    probe = await ffprobe(final)
    assert probe.duration == pytest.approx(LONG_DURATION_S, abs=0.5)
    ok_with_new_token = [
        r
        for r in fixture_server.requests_to("/media/vod_long/seg_*")
        if r.status == 200 and r.query_string == "t=2"
    ]
    assert len(ok_with_new_token) == 30 - 12


async def test_credentials_refresh_gives_up_after_3(
    fixture_server: FixtureServer, hls_samples: HlsSamples, upstream: Upstream, out: Path
) -> None:
    issued = tokenized_playlist(fixture_server, hls_samples)
    fixture_server.add_rule(expire_after("/media/vod_long/seg_*", requests=2, status=403))
    with pytest.raises(HarvesterError, match="after 3 refreshes") as info:
        await download(
            fixture_server.url("/signed/index.m3u8", CDN), upstream=upstream, output_dir=out
        )
    assert info.value.exit_code == ExitCode.UPSTREAM_ERROR
    assert issued == [1, 2, 3, 4]


async def test_expired_playlist_cannot_be_refreshed(
    fixture_server: FixtureServer, hls_samples: HlsSamples, upstream: Upstream, out: Path
) -> None:
    tokenized_playlist(fixture_server, hls_samples)
    fixture_server.add_rule(expire_after("/media/vod_long/seg_*", requests=2, status=401))
    fixture_server.add_rule(expire_after("/signed/index.m3u8", requests=1, status=401))
    with pytest.raises(HarvesterError, match="re-requesting the playlist failed") as info:
        await download(
            fixture_server.url("/signed/index.m3u8", CDN), upstream=upstream, output_dir=out
        )
    assert info.value.exit_code == ExitCode.UPSTREAM_ERROR
