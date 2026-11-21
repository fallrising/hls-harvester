"""Tests for the upstream request core (SDD §3.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

import pytest

from hls_harvester.errors import CredentialsExpired, ExitCode, UpstreamError
from hls_harvester.playlist import ByteRange
from hls_harvester.upstream import (
    HeaderTemplate,
    RawResponse,
    RetryPolicy,
    TransportError,
    Upstream,
    default_referer,
    open_playwright_transport,
    origin_of,
    parse_retry_after,
)
from tests.fixtures.server import (
    CDN,
    ORIGIN,
    FixtureServer,
    delay,
    expire_after,
    rate_limited,
    redirect,
    unavailable,
)


class FakeTransport:
    """Replays scripted responses per URL and records every request."""

    def __init__(self, script: dict[str, list[RawResponse | Exception]]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def fetch(self, url: str, *, headers: Mapping[str, str], timeout_s: float) -> RawResponse:
        self.calls.append((url, dict(headers)))
        queue = self.script[url]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def ok(body: bytes = b"ok", **headers: str) -> RawResponse:
    return RawResponse(200, {k.lower().replace("_", "-"): v for k, v in headers.items()}, body)


def status(code: int, **headers: str) -> RawResponse:
    return RawResponse(code, {k.lower().replace("_", "-"): v for k, v in headers.items()}, b"")


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def upstream(transport: FakeTransport, sleeps: Sleeps | None = None, **kw: object) -> Upstream:
    return Upstream(
        transport,
        sleep=sleeps or Sleeps(),
        rng=lambda: 0.0,
        **kw,  # type: ignore[arg-type]
    )


# --- pure helpers ------------------------------------------------------------------------


def test_origin_of() -> None:
    assert origin_of("https://A.example.com/x?y") == "https://a.example.com"
    assert origin_of("https://a.example.com:443/x") == "https://a.example.com"
    assert origin_of("http://127.0.0.1:8080/x") == "http://127.0.0.1:8080"


@pytest.mark.parametrize(
    ("url", "referrer", "expected"),
    [
        (
            "https://a.com/seg.ts",
            "https://a.com/p/list.m3u8?t=1#f",
            "https://a.com/p/list.m3u8?t=1",
        ),
        ("https://cdn.com/seg.ts", "https://a.com/p/list.m3u8?t=1", "https://a.com/"),
        ("http://cdn.com/seg.ts", "https://a.com/p", None),
        ("https://cdn.com/seg.ts", None, None),
    ],
)
def test_default_referer(url: str, referrer: str | None, expected: str | None) -> None:
    assert default_referer(url, referrer) == expected


def test_parse_retry_after() -> None:
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:10 GMT", now=1445412480.0) == 10.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


def test_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(backoff_base_s=1, backoff_max_s=5)
    assert [policy.backoff(n, lambda: 0.0) for n in range(5)] == [0.5, 1.0, 2.0, 2.5, 2.5]
    assert policy.backoff(0, lambda: 1.0) == 1.0


# --- header rules ----------------------------------------------------------------------


async def test_template_and_auth_only_for_observed_origin() -> None:
    transport = FakeTransport(
        {
            "https://origin.example/list.m3u8": [
                status(302, location="https://cdn.example/l.m3u8")
            ],
            "https://cdn.example/l.m3u8": [ok()],
        }
    )
    up = upstream(
        transport,
        templates=[
            HeaderTemplate(
                "https://origin.example",
                headers={
                    "User-Agent": "Browser/1",
                    "Referer": "https://origin.example/watch",
                    "Cookie": "must-not-pass",
                },
                auth_headers={"Authorization": "Bearer secret", "X-Ticket": "t"},
            )
        ],
    )
    response = await up.get("https://origin.example/list.m3u8")
    assert response.url == "https://cdn.example/l.m3u8"
    (_, first), (_, second) = transport.calls
    assert first == {
        "User-Agent": "Browser/1",
        "Referer": "https://origin.example/watch",
        "Authorization": "Bearer secret",
        "X-Ticket": "t",
    }
    # The CDN has no template: only User-Agent (no Referer without a referrer), no auth.
    assert second == {"User-Agent": up.user_agent}


async def test_templateless_host_gets_ua_and_referer() -> None:
    transport = FakeTransport({"https://cdn.example/seg.ts": [ok()]})
    up = upstream(transport)
    await up.get("https://cdn.example/seg.ts", referrer="https://site.example/a/list.m3u8?t=1")
    assert transport.calls[0][1] == {
        "User-Agent": up.user_agent,
        "Referer": "https://site.example/",
    }


async def test_relative_redirect_and_limit() -> None:
    transport = FakeTransport(
        {
            "https://a.example/x/1": [status(301, location="../y/2")],
            "https://a.example/y/2": [ok(b"done")],
        }
    )
    response = await upstream(transport).get("https://a.example/x/1")
    assert (response.url, response.body) == ("https://a.example/y/2", b"done")

    loop = FakeTransport({"https://a.example/loop": [status(302, location="/loop")]})
    with pytest.raises(UpstreamError, match="redirects"):
        await upstream(loop).get("https://a.example/loop")
    assert len(loop.calls) == 11


# --- retries and errors ----------------------------------------------------------------


async def test_transient_errors_are_retried() -> None:
    url = "https://cdn.example/seg.ts?token=SECRET"
    transport = FakeTransport(
        {url: [TransportError("timed out"), status(503), status(429, retry_after="7"), ok(b"x")]}
    )
    sleeps = Sleeps()
    response = await upstream(transport, sleeps, policy=RetryPolicy(backoff_base_s=1)).get(url)
    assert response.body == b"x"
    assert sleeps.delays == [0.5, 1.0, 7.0]


async def test_retries_exhausted() -> None:
    url = "https://cdn.example/seg.ts?token=SECRET"
    transport = FakeTransport({url: [status(500)]})
    with pytest.raises(UpstreamError, match="gave up after 3 attempts") as info:
        await upstream(transport, policy=RetryPolicy(max_retries=2)).get(url)
    assert info.value.exit_code == ExitCode.UPSTREAM_ERROR
    assert "SECRET" not in str(info.value)
    assert len(transport.calls) == 3


async def test_retry_after_is_capped() -> None:
    url = "https://cdn.example/a"
    transport = FakeTransport({url: [status(429, retry_after="9999"), ok()]})
    sleeps = Sleeps()
    await upstream(transport, sleeps, policy=RetryPolicy(retry_after_max_s=4)).get(url)
    assert sleeps.delays == [4]


@pytest.mark.parametrize("code", [401, 403])
async def test_auth_failures_raise_credentials_expired(code: int) -> None:
    url = "https://cdn.example/a?sig=1"
    transport = FakeTransport({url: [status(code)]})
    with pytest.raises(CredentialsExpired) as info:
        await upstream(transport).get(url)
    assert info.value.status == code
    assert len(transport.calls) == 1


async def test_permanent_client_error() -> None:
    transport = FakeTransport({"https://cdn.example/a": [status(404)]})
    with pytest.raises(UpstreamError, match="HTTP 404"):
        await upstream(transport).get("https://cdn.example/a")
    assert len(transport.calls) == 1


async def test_byterange_header_and_local_slice() -> None:
    url = "https://cdn.example/all.ts"
    transport = FakeTransport({url: [ok(b"0123456789")]})
    response = await upstream(transport).get(url, byterange=ByteRange(3, 2))
    assert response.body == b"234"
    assert transport.calls[0][1]["Range"] == "bytes=2-4"

    short = FakeTransport({url: [RawResponse(206, {}, b"23")]})
    with pytest.raises(UpstreamError, match="expected 3 bytes"):
        await upstream(short).get(url, byterange=ByteRange(3, 2))


# --- real Playwright transport against the fixture server -------------------------------


@pytest.fixture
async def real_upstream() -> AsyncIterator[Upstream]:
    async with open_playwright_transport() as transport:
        yield Upstream(
            transport,
            policy=RetryPolicy(backoff_base_s=0.01, request_timeout_s=0.5),
            concurrency_per_host=2,
        )


PLAYLIST = "/media/vod_fmp4/index.m3u8"


async def test_playwright_fetch_and_headers(
    fixture_server: FixtureServer, real_upstream: Upstream
) -> None:
    referrer = fixture_server.url("/watch?v=1", ORIGIN)
    response = await real_upstream.get(fixture_server.url(PLAYLIST, CDN), referrer=referrer)
    assert response.text().startswith("#EXTM3U")
    record = fixture_server.requests_to(PLAYLIST)[0]
    assert record.header("User-Agent") == real_upstream.user_agent
    assert record.header("Referer") == fixture_server.url("/", ORIGIN)


async def test_playwright_cross_host_redirect_drops_auth(
    fixture_server: FixtureServer, real_upstream: Upstream
) -> None:
    origin = fixture_server.hosts[ORIGIN].base_url
    real_upstream.add_template(
        HeaderTemplate(origin, {"User-Agent": "Player/1"}, {"Authorization": "Bearer s3cret"})
    )
    fixture_server.add_rule(redirect("/go/list.m3u8", to_host=CDN, to_path=PLAYLIST))
    response = await real_upstream.get(fixture_server.url("/go/list.m3u8?sig=1", ORIGIN))
    assert response.url == fixture_server.url(PLAYLIST + "?sig=1", CDN)
    first, second = fixture_server.requests
    assert (first.host, first.header("Authorization"), first.header("User-Agent")) == (
        ORIGIN,
        "Bearer s3cret",
        "Player/1",
    )
    assert (second.host, second.header("Authorization")) == (CDN, None)
    assert second.header("User-Agent") == real_upstream.user_agent


async def test_playwright_retries_faults(
    fixture_server: FixtureServer, real_upstream: Upstream
) -> None:
    fixture_server.add_rule(delay(PLAYLIST, 2, times=1))
    fixture_server.add_rule(rate_limited(PLAYLIST, retry_after=0, times=1))
    fixture_server.add_rule(unavailable(PLAYLIST, times=1))
    response = await real_upstream.get(fixture_server.url(PLAYLIST, CDN))
    assert response.status == 200
    assert len(fixture_server.requests_to(PLAYLIST)) == 4


async def test_playwright_connection_refused(real_upstream: Upstream) -> None:
    real_upstream.policy = RetryPolicy(max_retries=1, backoff_base_s=0.01)
    with pytest.raises(UpstreamError, match="gave up after 2 attempts"):
        await real_upstream.get("http://127.0.0.1:9/x?token=SECRET")


async def test_playwright_expired(fixture_server: FixtureServer, real_upstream: Upstream) -> None:
    fixture_server.add_rule(expire_after(PLAYLIST, requests=1, status=403))
    await real_upstream.get(fixture_server.url(PLAYLIST, CDN))
    with pytest.raises(CredentialsExpired):
        await real_upstream.get(fixture_server.url(PLAYLIST, CDN))


async def test_playwright_range(fixture_server: FixtureServer, real_upstream: Upstream) -> None:
    response = await real_upstream.get(
        fixture_server.media_url("byterange/media.ts"), byterange=ByteRange(188, 188)
    )
    assert response.status == 206
    assert len(response.body) == 188
    assert response.body[0] == 0x47
