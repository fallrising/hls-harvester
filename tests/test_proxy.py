"""Playback proxy tests (SDD §3.6, §7; milestone M3).

FFmpeg plays the role of the local player: it opens the loopback URL and decodes
everything, and its decoded output must match a direct decode of the sample.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from hls_harvester.errors import ExitCode, HarvesterError
from hls_harvester.proxy import PREFIX_BYTES, PlaybackProxy
from hls_harvester.upstream import RetryPolicy, Upstream, open_playwright_transport
from tests.fixtures.cli import cli_env, start_cli
from tests.fixtures.media import HlsSamples
from tests.fixtures.server import CDN, ORIGIN, FixtureServer, expire_after, redirect
from tests.fixtures.site import Site

FAST = RetryPolicy(backoff_base_s=0.02, backoff_max_s=0.2, request_timeout_s=5)
ProxyFactory = Callable[..., Awaitable[PlaybackProxy]]


@pytest.fixture
async def upstream() -> AsyncIterator[Upstream]:
    async with open_playwright_transport() as transport:
        yield Upstream(transport, policy=FAST)


@pytest.fixture
async def make_proxy(upstream: Upstream) -> AsyncIterator[ProxyFactory]:
    proxies: list[PlaybackProxy] = []

    async def factory(**kwargs: Any) -> PlaybackProxy:
        proxy = PlaybackProxy(upstream, **kwargs)
        await proxy.start()
        proxies.append(proxy)
        return proxy

    yield factory
    for proxy in proxies:
        await proxy.close()


async def http_get(
    url: str, headers: dict[str, str] | None = None, method: str = "GET"
) -> tuple[int, dict[str, str], bytes]:
    async with (
        aiohttp.ClientSession() as session,
        session.request(method, url, headers=headers) as response,
    ):
        return response.status, dict(response.headers), await response.read()


async def decode_md5(url: str) -> str:
    """Decode every audio/video stream of ``url`` and return the MD5 of the frames."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-v", "error",
        "-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto,http,tcp",
        "-i", url, "-map", "0:v?", "-map", "0:a?", "-f", "md5", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    assert proc.returncode == 0, err.decode()
    return out.decode().strip()


def uris(text: str) -> list[str]:
    """Every URI in a playlist: URI lines and URI="..." attributes."""
    found = re.findall(r'(?<=[:,])URI="([^"]*)"', text)
    found += [line for line in text.splitlines() if line and not line.startswith("#")]
    return found


async def crawl(proxy: PlaybackProxy, url: str) -> dict[str, str]:
    """Fetch every playlist reachable from ``url`` through the proxy."""
    seen: dict[str, str] = {}
    queue = [url]
    while queue:
        current = queue.pop()
        status, _, body = await http_get(current)
        assert status == 200, current
        text = body.decode()
        seen[current] = text
        for uri in uris(text):
            assert uri.startswith(f"/{proxy.prefix}/"), uri  # only loopback resources
            absolute = f"http://127.0.0.1:{proxy.port}{uri}"
            if uri.endswith(".m3u8") and absolute not in seen:
                queue.append(absolute)
    return seen


# --- 清單重寫 (playlist rewriting) -----------------------------------------------------


@pytest.mark.parametrize("name", ["vod_fmp4", "master", "aes128", "byterange"])
async def test_player_gets_only_loopback_urls_and_same_media(
    fixture_server: FixtureServer,
    hls_samples: HlsSamples,
    make_proxy: ProxyFactory,
    upstream: Upstream,
    name: str,
) -> None:
    direct = await decode_md5(str(hls_samples.root / hls_samples.playlist(name)))
    proxy = await make_proxy()
    local = await proxy.open(fixture_server.media_url(hls_samples.playlist(name)) + "?t=SIG")
    assert local.startswith(f"http://127.0.0.1:{proxy.port}/{proxy.prefix}/")
    playlists = await crawl(proxy, local)
    assert all("SIG" not in text and "localhost" not in text for text in playlists.values())
    assert await decode_md5(local) == direct
    # The player never talked to upstream itself: every upstream request came from us.
    assert {r.header("User-Agent") for r in fixture_server.requests} == {upstream.user_agent}


async def test_relative_uris_after_redirect(
    fixture_server: FixtureServer, hls_samples: HlsSamples, make_proxy: ProxyFactory
) -> None:
    fixture_server.add_rule(
        redirect("/go/master.m3u8", to_host=CDN, to_path="/media/master/master.m3u8")
    )
    direct = await decode_md5(str(hls_samples.root / hls_samples.playlist("master")))
    proxy = await make_proxy()
    local = await proxy.open(fixture_server.url("/go/master.m3u8", ORIGIN))
    await crawl(proxy, local)
    assert await decode_md5(local) == direct
    # Children resolved against the final (redirected) URL, not the original one.
    assert fixture_server.requests_to("/media/master/v2/index.m3u8", host=CDN)
    assert not fixture_server.requests_to("/go/v2/*")


async def test_range_requests_are_forwarded(
    fixture_server: FixtureServer, hls_samples: HlsSamples, make_proxy: ProxyFactory
) -> None:
    proxy = await make_proxy()
    local = await proxy.open(fixture_server.media_url(hls_samples.playlist("byterange")))
    _, _, body = await http_get(local)
    text = body.decode()
    assert text.count("#EXT-X-BYTERANGE:") == 2
    segment_uris = {u for u in uris(text)}
    assert len(segment_uris) == 1  # both byte ranges still refer to one resource
    data = (hls_samples.root / "byterange" / "media.ts").read_bytes()
    url = f"http://127.0.0.1:{proxy.port}{segment_uris.pop()}"
    status, headers, chunk = await http_get(url, {"Range": "bytes=188-375"})
    assert status == 206
    assert headers["Content-Range"] == f"bytes 188-375/{len(data)}"
    assert chunk == data[188:376]
    status, _, whole = await http_get(url)
    assert (status, whole) == (200, data)


async def test_quality_keeps_one_variant_and_its_audio(
    fixture_server: FixtureServer, hls_samples: HlsSamples, make_proxy: ProxyFactory
) -> None:
    proxy = await make_proxy(quality="180p")
    local = await proxy.open(fixture_server.media_url(hls_samples.playlist("master")))
    _, _, body = await http_get(local)
    text = body.decode()
    assert text.count("#EXT-X-STREAM-INF:") == 1
    assert "RESOLUTION=320x180" in text
    assert '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud"' in text


async def test_drm_is_refused_before_a_url_is_given(
    fixture_server: FixtureServer, hls_samples: HlsSamples, make_proxy: ProxyFactory
) -> None:
    proxy = await make_proxy()
    with pytest.raises(HarvesterError, match="SAMPLE-AES") as info:
        await proxy.open(fixture_server.media_url(hls_samples.playlist("sample_aes")))
    assert info.value.exit_code == ExitCode.UNSUPPORTED


# --- 代理安全 (proxy security) ----------------------------------------------------------


async def test_host_path_and_origin_checks(
    fixture_server: FixtureServer, hls_samples: HlsSamples, make_proxy: ProxyFactory
) -> None:
    player = "http://localhost:8080"
    proxy = await make_proxy(allowed_origins=[player])
    local = await proxy.open(fixture_server.media_url(hls_samples.playlist("vod_fmp4")))
    base = f"http://127.0.0.1:{proxy.port}"
    path = local.removeprefix(base)

    assert (await http_get(local))[0] == 200
    assert (await http_get(f"http://localhost:{proxy.port}{path}"))[0] == 200
    # DNS rebinding: any other Host is refused.
    assert (await http_get(local, {"Host": "attacker.example"}))[0] == 403
    assert (await http_get(local, {"Host": f"attacker.example:{proxy.port}"}))[0] == 403
    # Unregistered paths.
    assert (await http_get(f"{base}/{proxy.prefix}/r999.ts"))[0] == 404
    assert (await http_get(f"{base}/{'0' * len(proxy.prefix)}/r1.m3u8"))[0] == 404
    assert (await http_get(f"{base}/r1.m3u8"))[0] == 404
    assert (await http_get(f"{base}/"))[0] == 404
    # Another web page in the user's browser cannot read the proxy.
    status, headers, _ = await http_get(local, {"Origin": "https://evil.example"})
    assert status == 403
    assert "Access-Control-Allow-Origin" not in headers
    # The configured local player page gets its own origin back, never "*".
    status, headers, _ = await http_get(local, {"Origin": player})
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == player
    status, headers, _ = await http_get(
        local, {"Origin": player, "Access-Control-Request-Method": "GET"}, method="OPTIONS"
    )
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == player


async def test_prefix_is_random_and_128_bits(make_proxy: ProxyFactory) -> None:
    first, second = await make_proxy(), await make_proxy()
    assert PREFIX_BYTES * 8 >= 128
    assert len(first.prefix) == PREFIX_BYTES * 2
    assert re.fullmatch(r"[0-9a-f]+", first.prefix)
    assert first.prefix != second.prefix


async def test_close_revokes_everything(
    fixture_server: FixtureServer, hls_samples: HlsSamples, make_proxy: ProxyFactory
) -> None:
    proxy = await make_proxy()
    local = await proxy.open(fixture_server.media_url(hls_samples.playlist("vod_fmp4")))
    await proxy.close()
    with pytest.raises(aiohttp.ClientError):
        await http_get(local)


# --- 即時播放 (live playback) ----------------------------------------------------------


def install_live(server: FixtureServer, step_s: float = 0.4) -> list[float]:
    """A live playlist over vod_long: a 3-segment window that advances every ``step_s``."""
    started: list[float] = []

    async def live(_: web.Request) -> web.Response:
        if not started:
            started.append(time.monotonic())
        head = min(29, 2 + int((time.monotonic() - started[0]) / step_s))
        first = head - 2
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1",
                 f"#EXT-X-MEDIA-SEQUENCE:{first}"]  # fmt: skip
        for seq in range(first, head + 1):
            lines += ["#EXTINF:1.000000,", f"/media/vod_long/seg_{seq:03d}.ts"]
        return web.Response(
            text="\n".join(lines) + "\n", content_type="application/vnd.apple.mpegurl"
        )

    server.add_route("/live/index.m3u8", live, host=CDN)
    return started


async def test_live_playlist_gets_new_segments(
    fixture_server: FixtureServer, hls_samples: HlsSamples, make_proxy: ProxyFactory
) -> None:
    install_live(fixture_server)
    proxy = await make_proxy()
    local = await proxy.open(fixture_server.url("/live/index.m3u8", CDN))
    _, _, first = await http_get(local)
    first_seq = int(re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", first.decode())[1])  # type: ignore[index]
    await asyncio.sleep(1.3)
    _, _, second = await http_get(local)
    text = second.decode()
    second_seq = int(re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)[1])  # type: ignore[index]
    assert second_seq > first_seq  # the media sequence is passed through
    assert "#EXT-X-ENDLIST" not in text
    new_segment = uris(text)[-1]
    assert new_segment not in uris(first.decode())  # a newly registered URI
    status, _, data = await http_get(f"http://127.0.0.1:{proxy.port}{new_segment}")
    assert status == 200
    expected = (hls_samples.root / "vod_long" / f"seg_{second_seq + 2:03d}.ts").read_bytes()
    assert data == expected
    # Within half a target duration the proxy answers from its cache.
    before = len(fixture_server.requests_to("/live/index.m3u8"))
    await http_get(local)
    assert len(fixture_server.requests_to("/live/index.m3u8")) == before


async def test_idle_timeout_stops_the_proxy(
    fixture_server: FixtureServer, hls_samples: HlsSamples, make_proxy: ProxyFactory
) -> None:
    proxy = await make_proxy(idle_timeout_s=0.6)
    local = await proxy.open(fixture_server.media_url(hls_samples.playlist("vod_fmp4")))
    await http_get(local)
    started = time.monotonic()
    await asyncio.wait_for(proxy.serve_until_idle(), timeout=5)
    assert 0.4 <= time.monotonic() - started <= 2.5


# --- credential refresh ----------------------------------------------------------------


def install_signed(server: FixtureServer, samples: HlsSamples) -> list[int]:
    """vod_long with a new token per playlist request (``?t=N`` on every segment)."""
    issued: list[int] = []
    body = (samples.root / samples.playlist("vod_long")).read_text()

    async def handler(_: web.Request) -> web.Response:
        issued.append(len(issued) + 1)
        text = "\n".join(
            f"/media/vod_long/{line}?t={issued[-1]}" if line and not line.startswith("#") else line
            for line in body.splitlines()
        )
        return web.Response(text=text + "\n", content_type="application/vnd.apple.mpegurl")

    server.add_route("/signed/index.m3u8", handler, host=CDN)
    return issued


async def test_expired_segment_is_remapped_in_place(
    fixture_server: FixtureServer, hls_samples: HlsSamples, make_proxy: ProxyFactory
) -> None:
    issued = install_signed(fixture_server, hls_samples)
    fixture_server.add_rule(expire_after("/media/vod_long/seg_*", requests=1, query="t=1"))
    proxy = await make_proxy()
    local = await proxy.open(fixture_server.url("/signed/index.m3u8", CDN))
    _, _, body = await http_get(local)
    segments = uris(body.decode())
    base = f"http://127.0.0.1:{proxy.port}"
    assert (await http_get(base + segments[0]))[0] == 200  # token 1 still valid
    status, _, data = await http_get(base + segments[1])  # token 1 expired: refresh
    assert status == 200
    assert data == (hls_samples.root / "vod_long" / "seg_001.ts").read_bytes()
    assert issued == [1, 2]
    _, _, again = await http_get(local)
    assert uris(again.decode()) == segments  # local paths did not change


async def test_refused_playlist_triggers_recapture(
    fixture_server: FixtureServer, hls_samples: HlsSamples, make_proxy: ProxyFactory
) -> None:
    install_signed(fixture_server, hls_samples)
    fixture_server.add_rule(
        expire_after("/signed/index.m3u8", requests=1, status=401, query="old=1")
    )
    fixture_server.add_rule(expire_after("/media/vod_long/seg_*", query="t=1"))
    recaptures: list[str] = []

    async def recapture() -> str:
        recaptures.append("again")
        return fixture_server.url("/signed/index.m3u8?new=1", CDN)

    proxy = await make_proxy(recapture=recapture)
    local = await proxy.open(fixture_server.url("/signed/index.m3u8?old=1", CDN))
    _, _, body = await http_get(local)
    first = uris(body.decode())[0]
    status, _, _ = await http_get(f"http://127.0.0.1:{proxy.port}{first}")
    assert status == 200
    assert recaptures == ["again"]


async def test_no_recapture_without_a_page(
    fixture_server: FixtureServer, hls_samples: HlsSamples, make_proxy: ProxyFactory
) -> None:
    install_signed(fixture_server, hls_samples)
    fixture_server.add_rule(expire_after("/signed/index.m3u8", requests=1, status=403))
    fixture_server.add_rule(expire_after("/media/vod_long/seg_*", query="t=1"))
    proxy = await make_proxy()
    local = await proxy.open(fixture_server.url("/signed/index.m3u8", CDN))
    _, _, body = await http_get(local)
    status, _, _ = await http_get(f"http://127.0.0.1:{proxy.port}{uris(body.decode())[0]}")
    assert status == 502


# --- CLI -------------------------------------------------------------------------------


async def play_via_cli(env: dict[str, str], *args: str) -> tuple[str, int, str]:
    """Start ``get --play``, fetch the printed URL like a player, wait for the idle stop."""
    proc = await start_cli(env, "get", *args, "--play", "--non-interactive")
    assert proc.stdout is not None
    assert proc.stderr is not None
    line = (await asyncio.wait_for(proc.stdout.readline(), timeout=120)).decode().strip()
    assert line, (await proc.stderr.read()).decode()
    status, _, body = await http_get(line)
    assert status == 200
    assert body.startswith(b"#EXTM3U")
    for uri in uris(body.decode())[:2]:
        port = line.split(":")[2].split("/")[0]
        assert (await http_get(f"http://127.0.0.1:{port}{uri}"))[0] == 200
    _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    assert proc.returncode is not None
    return line, proc.returncode, err.decode()


async def test_cli_play_playlist(
    fixture_server: FixtureServer, hls_samples: HlsSamples, home: Path
) -> None:
    env = cli_env(home, "[defaults]\nplay_idle_timeout_s = 1.5\n")
    url = fixture_server.media_url(hls_samples.playlist("vod_long")) + "?token=SECRET-PLAY"
    line, code, err = await play_via_cli(env, url, "--playlist", "-vv")
    assert line.startswith("http://127.0.0.1:")
    assert code == ExitCode.OK, err
    assert "SECRET-PLAY" not in err
    assert "SECRET-PLAY" not in line


async def test_cli_play_page(site: Site, home: Path) -> None:
    env = cli_env(home, "[defaults]\nplay_idle_timeout_s = 1.5\n")
    _, code, err = await play_via_cli(env, site.page("/watch/vod"))
    assert code == ExitCode.OK, err
