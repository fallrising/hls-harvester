"""Smoke tests: the fixture server's hosts, recording and every fault mode work."""

from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest

from tests.fixtures.media import AES_KEY, HlsSamples
from tests.fixtures.server import (
    CDN,
    ORIGIN,
    FixtureServer,
    Rule,
    delay,
    drop,
    expire_after,
    rate_limited,
    redirect,
    unavailable,
)

PLAYLIST = "/media/vod_fmp4/index.m3u8"


async def get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    allow_redirects: bool = True,
    total: float = 10,
) -> tuple[int, dict[str, str], bytes]:
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=total)) as session,
        session.get(url, headers=headers, allow_redirects=allow_redirects) as resp,
    ):
        return resp.status, dict(resp.headers), await resp.read()


async def test_both_hosts_serve_media(fixture_server: FixtureServer) -> None:
    assert fixture_server.hosts[ORIGIN].port != fixture_server.hosts[CDN].port
    for host in (ORIGIN, CDN):
        status, headers, body = await get(fixture_server.url(PLAYLIST, host))
        assert status == 200
        assert headers["Content-Type"] == "application/vnd.apple.mpegurl"
        assert body.startswith(b"#EXTM3U")
    assert [r.host for r in fixture_server.requests] == [ORIGIN, CDN]


async def test_virtual_hostnames_by_host_header(fixture_server: FixtureServer) -> None:
    # Sent to the origin port, but the Host header names the CDN.
    port = fixture_server.hosts[ORIGIN].port
    status, _, _ = await get(fixture_server.url(PLAYLIST), headers={"Host": f"cdn.test:{port}"})
    assert status == 200
    assert fixture_server.requests[-1].host == CDN


async def test_records_headers_and_query(fixture_server: FixtureServer) -> None:
    headers = {"Cookie": "sid=abc", "Referer": "http://origin.test/page", "X-Custom": "1"}
    await get(fixture_server.url(PLAYLIST + "?token=xyz", CDN), headers=headers)
    record = fixture_server.requests_to(PLAYLIST, host=CDN)[0]
    assert record.query_string == "token=xyz"
    assert record.header("cookie") == "sid=abc"
    assert record.header("Referer") == "http://origin.test/page"
    assert record.header("x-custom") == "1"
    assert record.status == 200


async def test_unknown_path_and_traversal_404(fixture_server: FixtureServer) -> None:
    assert (await get(fixture_server.url("/nope")))[0] == 404
    assert (await get(fixture_server.url("/media/does/not/exist.ts")))[0] == 404
    assert (await get(fixture_server.url("/media/%2e%2e/%2e%2e/etc/passwd")))[0] == 404


async def test_aes_key_and_content_types(
    fixture_server: FixtureServer, hls_samples: HlsSamples
) -> None:
    status, headers, body = await get(fixture_server.media_url("aes128/key.bin"))
    assert (status, body) == (200, AES_KEY)
    _, headers, _ = await get(fixture_server.media_url("vod_fmp4/seg_000.m4s"))
    assert headers["Content-Type"] == "video/iso.segment"


async def test_range_request(fixture_server: FixtureServer) -> None:
    url = fixture_server.media_url("byterange/media.ts")
    status, headers, body = await get(url, headers={"Range": "bytes=0-187"})
    assert status == 206
    assert headers["Content-Range"].startswith("bytes 0-187/")
    assert len(body) == 188
    assert body[0] == 0x47  # MPEG-TS sync byte
    status, headers, _ = await get(url, headers={"Range": "bytes=99999999-"})
    assert status == 416


async def test_add_route_per_host(fixture_server: FixtureServer) -> None:
    fixture_server.add_route("/watch", "<html>page</html>", host=ORIGIN, content_type="text/html")
    status, headers, body = await get(fixture_server.url("/watch", ORIGIN))
    assert (status, body) == (200, b"<html>page</html>")
    assert headers["Content-Type"].startswith("text/html")
    assert (await get(fixture_server.url("/watch", CDN)))[0] == 404


async def test_delay_causes_client_timeout(fixture_server: FixtureServer) -> None:
    fixture_server.add_rule(delay(PLAYLIST, 0.5, times=1))
    with pytest.raises(asyncio.TimeoutError):
        await get(fixture_server.url(PLAYLIST), total=0.1)
    # The rule fired once; the next request is served without delay.
    start = time.monotonic()
    assert (await get(fixture_server.url(PLAYLIST)))[0] == 200
    assert time.monotonic() - start < 0.4


async def test_rate_limited_with_retry_after(fixture_server: FixtureServer) -> None:
    fixture_server.add_rule(rate_limited("/media/vod_fmp4/*.m4s", retry_after=2, times=1))
    status, headers, _ = await get(fixture_server.media_url("vod_fmp4/seg_000.m4s"))
    assert status == 429
    assert headers["Retry-After"] == "2"
    assert (await get(fixture_server.media_url("vod_fmp4/seg_000.m4s")))[0] == 200


async def test_unavailable_then_recovers(fixture_server: FixtureServer) -> None:
    rule = fixture_server.add_rule(unavailable(PLAYLIST, times=2))
    statuses = [(await get(fixture_server.url(PLAYLIST)))[0] for _ in range(3)]
    assert statuses == [503, 503, 200]
    assert rule.fired == 2


async def test_expire_after_requests(fixture_server: FixtureServer) -> None:
    fixture_server.add_rule(expire_after("/media/vod_fmp4/seg_*", requests=2, status=403))
    url = fixture_server.media_url("vod_fmp4/seg_000.m4s")
    statuses = [(await get(url))[0] for _ in range(4)]
    assert statuses == [200, 200, 403, 403]


async def test_expire_after_time(fixture_server: FixtureServer) -> None:
    fixture_server.add_rule(expire_after(PLAYLIST, seconds=0.3, status=401))
    assert (await get(fixture_server.url(PLAYLIST)))[0] == 200
    await asyncio.sleep(0.35)
    assert (await get(fixture_server.url(PLAYLIST)))[0] == 401


async def test_rules_scoped_to_host(fixture_server: FixtureServer) -> None:
    fixture_server.add_rule(unavailable(PLAYLIST, host=CDN))
    assert (await get(fixture_server.url(PLAYLIST, ORIGIN)))[0] == 200
    assert (await get(fixture_server.url(PLAYLIST, CDN)))[0] == 503


async def test_same_host_redirect(fixture_server: FixtureServer) -> None:
    fixture_server.add_rule(redirect("/old/index.m3u8", to_path=PLAYLIST))
    status, headers, _ = await get(
        fixture_server.url("/old/index.m3u8?sig=1"), allow_redirects=False
    )
    assert status == 302
    assert headers["Location"] == fixture_server.url(PLAYLIST + "?sig=1", ORIGIN)
    status, _, body = await get(fixture_server.url("/old/index.m3u8"))
    assert status == 200
    assert body.startswith(b"#EXTM3U")


async def test_cross_host_redirect(fixture_server: FixtureServer) -> None:
    fixture_server.add_rule(redirect(PLAYLIST, to_host=CDN, host=ORIGIN))
    status, _, _ = await get(
        fixture_server.url(PLAYLIST, ORIGIN), headers={"Authorization": "Bearer t"}
    )
    assert status == 200
    hops = fixture_server.requests_to(PLAYLIST)
    assert [(r.host, r.status) for r in hops] == [(ORIGIN, 302), (CDN, 200)]


async def test_cross_host_redirect_keeps_virtual_hostname(fixture_server: FixtureServer) -> None:
    fixture_server.add_rule(redirect(PLAYLIST, to_host=CDN, host=ORIGIN))
    port = fixture_server.hosts[ORIGIN].port
    _, headers, _ = await get(
        fixture_server.url(PLAYLIST), headers={"Host": f"origin.test:{port}"}, allow_redirects=False
    )
    assert headers["Location"] == fixture_server.url(PLAYLIST, CDN, virtual=True)


async def test_drop_connection(fixture_server: FixtureServer) -> None:
    # No ``times`` limit: aiohttp's client retries once after a disconnect.
    rule = fixture_server.add_rule(drop(PLAYLIST))
    with pytest.raises(aiohttp.ServerDisconnectedError):
        await get(fixture_server.url(PLAYLIST))
    assert rule.fired >= 1
    fixture_server.clear_rules()
    assert (await get(fixture_server.url(PLAYLIST)))[0] == 200


async def test_probability_is_seeded(fixture_server: FixtureServer) -> None:
    fixture_server.add_rule(Rule("/media/vod_fmp4/*", unavailable("").action, probability=0.3))
    url = fixture_server.media_url("vod_fmp4/init.mp4")
    statuses = [(await get(url))[0] for _ in range(40)]
    failures = statuses.count(503)
    assert 0 < failures < 40
    assert set(statuses) == {200, 503}


async def test_max_in_flight(fixture_server: FixtureServer) -> None:
    fixture_server.add_rule(delay("/media/*", 0.2))
    urls = [fixture_server.media_url("vod_fmp4/seg_000.m4s") for _ in range(5)]
    results = await asyncio.gather(*(get(u) for u in urls))
    assert all(status == 200 for status, _, _ in results)
    assert fixture_server.max_in_flight[CDN] == 5
    assert fixture_server.in_flight[CDN] == 0
