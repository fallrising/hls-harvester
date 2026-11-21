"""Shared upstream request core (SDD §3.3), used by the downloader and the proxy.

* A :class:`Transport` sends one request and never follows redirects. The default is
  Playwright's ``APIRequestContext`` (:class:`PlaywrightTransport`), which keeps an
  RFC 6265 cookie jar and picks the right cookies for every request by itself. In M2
  the capture browser's ``BrowserContext.request`` is plugged in here, so page and
  downloader share cookies.
* :class:`Upstream` follows redirects itself (at most 10 hops) and, on every hop,
  rebuilds the headers for the destination: a host with a recorded
  :class:`HeaderTemplate` gets that template; ``Authorization`` and custom auth headers
  only go to the exact origin where they were observed; any other host only gets
  ``User-Agent`` and a ``Referer`` derived from the referring URL. ``Cookie`` is never set
  here.
* Transient failures (timeout, connection error, 408, 429, 5xx) are retried with
  exponential backoff plus jitter, honouring ``Retry-After``. 401/403 raise
  :class:`~hls_harvester.errors.CredentialsExpired` so the caller can refresh them.
* Concurrency is bounded per host.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from hls_harvester import __version__
from hls_harvester.errors import CredentialsExpired, UpstreamError
from hls_harvester.redact import redact_text, redact_url

if TYPE_CHECKING:
    from hls_harvester.playlist import ByteRange

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = f"hls-harvester/{__version__}"
MAX_REDIRECTS = 10
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class TransportError(Exception):
    """A request failed without an HTTP response (timeout, reset, refused ...)."""


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    """Header names are lower case."""
    body: bytes


class Transport(Protocol):
    async def fetch(self, url: str, *, headers: Mapping[str, str], timeout_s: float) -> RawResponse:
        """Send one GET without following redirects; raise TransportError on failure."""
        ...


@dataclass(frozen=True)
class Response:
    url: str
    """Final URL after redirects; relative URIs in the body resolve against it."""
    status: int
    headers: Mapping[str, str]
    body: bytes

    def text(self) -> str:
        try:
            return self.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UpstreamError(f"{redact_url(self.url)}: response is not UTF-8 text") from exc


@dataclass(frozen=True)
class HeaderTemplate:
    """Headers the player sent to one origin (SDD §3.2 item 6).

    ``headers`` are the non-credential headers (``User-Agent``, ``Origin``, ``Referer``,
    ``Accept`` ...); ``auth_headers`` (``Authorization`` and site-specific auth headers)
    are only ever sent back to this exact origin. ``Cookie`` is not part of a template.
    """

    origin: str
    headers: Mapping[str, str] = field(default_factory=dict)
    auth_headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    """Retries after the first attempt (SDD §3.5 item 3)."""
    backoff_base_s: float = 0.5
    backoff_max_s: float = 30.0
    retry_after_max_s: float = 120.0
    request_timeout_s: float = 30.0
    """Per-attempt timeout. Playwright has a single request timeout, see decisions.md."""

    def backoff(self, attempt: int, rng: Callable[[], float]) -> float:
        """Exponential backoff with jitter: half fixed, half random."""
        cap = min(self.backoff_max_s, self.backoff_base_s * float(2**attempt))
        return cap * (0.5 + 0.5 * rng())


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` with the default port omitted."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is None or (scheme, port) in {("http", 80), ("https", 443)}:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def default_referer(url: str, referrer: str | None) -> str | None:
    """Browser-style ``strict-origin-when-cross-origin`` Referer for ``url``."""
    if not referrer:
        return None
    ref = urlsplit(referrer)
    if ref.scheme not in {"http", "https"}:
        return None
    if urlsplit(url).scheme == "http" and ref.scheme == "https":
        return None  # no downgrade
    if origin_of(url) == origin_of(referrer):
        return urlunsplit((ref.scheme, ref.netloc, ref.path, ref.query, ""))
    return origin_of(referrer) + "/"


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    return max(0.0, when.timestamp() - now)


class Upstream:
    """Upstream GETs with header rules, redirects, retries and per-host limits."""

    def __init__(
        self,
        transport: Transport,
        *,
        policy: RetryPolicy | None = None,
        concurrency_per_host: int = 4,
        templates: Iterable[HeaderTemplate] = (),
        user_agent: str = DEFAULT_USER_AGENT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.transport = transport
        self.policy = policy or RetryPolicy()
        self.concurrency_per_host = concurrency_per_host
        self.templates = {t.origin: t for t in templates}
        self.user_agent = user_agent
        self._sleep = sleep
        self._rng = rng
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self.in_flight: dict[str, int] = {}
        self.max_in_flight: dict[str, int] = {}

    def add_template(self, template: HeaderTemplate) -> None:
        self.templates[template.origin] = template

    def headers_for(self, url: str, referrer: str | None) -> dict[str, str]:
        """Headers for a request to ``url`` (SDD §3.3). Never includes ``Cookie``."""
        template = self.templates.get(origin_of(url))
        if template is not None:
            headers = {k: v for k, v in template.headers.items() if k.lower() != "cookie"}
            headers.update(template.auth_headers)
            if not any(k.lower() == "user-agent" for k in headers):
                headers["User-Agent"] = self.user_agent
            return headers
        headers = {"User-Agent": self.user_agent}
        referer = default_referer(url, referrer)
        if referer:
            headers["Referer"] = referer
        return headers

    async def get(
        self,
        url: str,
        *,
        referrer: str | None = None,
        byterange: ByteRange | None = None,
    ) -> Response:
        """GET ``url``, following redirects, retrying transient errors.

        With ``byterange`` a ``Range`` header is sent; a 200 reply is sliced locally.
        Raises :class:`CredentialsExpired` on 401/403 and
        :class:`~hls_harvester.errors.UpstreamError` on other failures.
        """
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            headers = self.headers_for(current, referrer)
            if byterange is not None:
                headers["Range"] = byterange.header()
            raw = await self._fetch_with_retries(current, headers)
            if raw.status in _REDIRECT_STATUSES:
                location = raw.headers.get("location")
                if not location:
                    raise UpstreamError(f"{redact_url(current)}: redirect without Location")
                current = urljoin(current, location)
                continue
            if raw.status in {401, 403}:
                raise CredentialsExpired(
                    f"{redact_url(current)}: HTTP {raw.status}", status=raw.status
                )
            if not 200 <= raw.status < 300:
                raise UpstreamError(f"{redact_url(current)}: HTTP {raw.status}")
            body = raw.body
            if byterange is not None:
                body = self._apply_range(current, raw, byterange)
            return Response(url=current, status=raw.status, headers=raw.headers, body=body)
        raise UpstreamError(f"{redact_url(url)}: more than {MAX_REDIRECTS} redirects")

    @staticmethod
    def _apply_range(url: str, raw: RawResponse, byterange: ByteRange) -> bytes:
        if raw.status == 206:
            body = raw.body
        else:  # the server ignored Range and sent the whole resource
            body = raw.body[byterange.offset : byterange.offset + byterange.length]
        if len(body) != byterange.length:
            raise UpstreamError(
                f"{redact_url(url)}: expected {byterange.length} bytes for "
                f"{byterange.header()}, got {len(body)}"
            )
        return body

    def _semaphore(self, url: str) -> asyncio.Semaphore:
        host = origin_of(url)
        if host not in self._semaphores:
            self._semaphores[host] = asyncio.Semaphore(self.concurrency_per_host)
        return self._semaphores[host]

    async def _fetch_once(self, url: str, headers: Mapping[str, str]) -> RawResponse:
        host = origin_of(url)
        async with self._semaphore(url):
            self.in_flight[host] = self.in_flight.get(host, 0) + 1
            self.max_in_flight[host] = max(self.max_in_flight.get(host, 0), self.in_flight[host])
            try:
                return await self.transport.fetch(
                    url, headers=headers, timeout_s=self.policy.request_timeout_s
                )
            finally:
                self.in_flight[host] -= 1

    async def _fetch_with_retries(self, url: str, headers: Mapping[str, str]) -> RawResponse:
        policy = self.policy
        attempt = 0
        while True:
            reason: str
            retry_after: float | None = None
            try:
                raw = await self._fetch_once(url, headers)
            except TransportError as exc:
                reason = str(exc)
            else:
                if raw.status not in {408, 429} and raw.status < 500:
                    log.debug("GET %s -> %d", url, raw.status)
                    return raw
                reason = f"HTTP {raw.status}"
                retry_after = parse_retry_after(raw.headers.get("retry-after"))
            if attempt >= policy.max_retries:
                raise UpstreamError(
                    f"{redact_url(url)}: {redact_text(reason)} "
                    f"(gave up after {attempt + 1} attempts)"
                )
            delay = policy.backoff(attempt, self._rng)
            if retry_after is not None:
                delay = max(delay, min(retry_after, policy.retry_after_max_s))
            log.info("GET %s failed (%s); retry %d in %.2fs", url, reason, attempt + 1, delay)
            attempt += 1
            await self._sleep(delay)


# --- Playwright transport --------------------------------------------------------------


class PlaywrightTransport:
    """Transport over a Playwright ``APIRequestContext`` (no browser needed)."""

    def __init__(self, request_context: Any) -> None:
        self._context = request_context

    async def fetch(self, url: str, *, headers: Mapping[str, str], timeout_s: float) -> RawResponse:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        try:
            response = await self._context.fetch(
                url,
                method="GET",
                headers=dict(headers),
                max_redirects=0,
                max_retries=0,
                timeout=timeout_s * 1000,
                fail_on_status_code=False,
            )
            body = await response.body()
        except PlaywrightTimeoutError:
            raise TransportError(f"timed out after {timeout_s:g}s") from None
        except PlaywrightError as exc:
            # The first line names the cause (e.g. "socket hang up (ECONNRESET)"); the
            # call log below it repeats the full URL, so it is dropped.
            first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            message = redact_text(first.removeprefix("APIRequestContext.fetch: "))
            raise TransportError(message) from None
        headers_out = {k.lower(): v for k, v in response.headers.items()}
        await response.dispose()
        return RawResponse(status=int(response.status), headers=headers_out, body=body)


@asynccontextmanager
async def open_playwright_transport(
    storage_state: Path | None = None,
) -> AsyncIterator[PlaywrightTransport]:
    """A standalone request context, optionally loaded from a ``login`` storage state."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        kwargs: dict[str, Any] = {}
        if storage_state is not None:
            kwargs["storage_state"] = str(storage_state)
        context = await playwright.request.new_context(**kwargs)
        try:
            yield PlaywrightTransport(context)
        finally:
            await context.dispose()
