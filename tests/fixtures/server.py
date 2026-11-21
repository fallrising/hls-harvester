"""Local fixture server with fault injection and request recording (SDD §7, §8 M0).

One :class:`FixtureServer` runs two aiohttp sites on random loopback ports that stand for
two different hosts:

``origin``
    The page/site host, ``http://127.0.0.1:<port>``. Also answers ``Host: origin.test``.
``cdn``
    The media CDN, ``http://localhost:<port>``. Also answers ``Host: cdn.test``.

``127.0.0.1`` and ``localhost`` are different hosts for cookies (RFC 6265 ignores ports),
and both Chromium and Playwright's Node-side request client resolve them without any
resolver tricks. The CDN listens on ``127.0.0.1`` and, when available, ``::1``.

The host a request is attributed to is taken from the ``Host`` header when it names one
of the virtual hostnames above, otherwise from the port it arrived on.

Responses carry permissive CORS headers (the request's ``Origin`` with credentials) and
``OPTIONS`` preflights are answered, so test pages on the origin can read CDN responses.

Both hosts serve the generated samples (:mod:`tests.fixtures.media`) under ``/media/``.
Tests may register extra fixed responses with :meth:`FixtureServer.add_route`.

Faults are :class:`Rule` objects. The first rule whose host, method and path glob match
and whose activation conditions hold decides the response:

* :class:`Delay` - sleep, then serve normally (combine with a client timeout to simulate
  a read timeout);
* :class:`Respond` - a fixed status, headers and body (429 + ``Retry-After``, 503,
  401/403 ...);
* :class:`Redirect` - a 3xx to the same or the other host;
* :class:`Drop` - close the connection without a response (connection reset). Note that
  aiohttp's client transparently retries an idempotent request once after such a
  disconnect, so a ``times=1`` drop may be invisible to an aiohttp caller.

Activation: ``after_requests`` skips the first N matching requests, ``after_seconds``
waits until the rule is that old, ``times`` limits how often it fires, ``probability``
fires on a seeded random fraction. Together these simulate token expiry (401/403 after N
requests or after a time), rate limiting and flaky upstreams.

Every request is recorded with its headers in :attr:`FixtureServer.requests`, and
:attr:`FixtureServer.max_in_flight` tracks peak concurrency per host.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

ORIGIN = "origin"
CDN = "cdn"
VIRTUAL_HOSTNAMES = {ORIGIN: "origin.test", CDN: "cdn.test"}

CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".bin": "application/octet-stream",
    ".html": "text/html; charset=utf-8",
}

HOST_NAME_KEY = web.AppKey("host_name", str)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# --- fault actions ---------------------------------------------------------------------


@dataclass(frozen=True)
class Delay:
    """Sleep ``seconds`` before serving the request normally."""

    seconds: float


@dataclass(frozen=True)
class Respond:
    """Answer with a fixed response instead of the real resource."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True)
class Redirect:
    """Redirect to ``to_host`` (``origin``/``cdn``; ``None`` = same host).

    ``to_path`` defaults to the request path; the query string is kept unless
    ``keep_query`` is false.
    """

    to_host: str | None = None
    to_path: str | None = None
    status: int = 302
    keep_query: bool = True


@dataclass(frozen=True)
class Drop:
    """Abort the connection without sending a response."""


Action = Delay | Respond | Redirect | Drop


@dataclass
class Rule:
    """A fault-injection rule. See the module docstring for the semantics."""

    path: str
    action: Action
    host: str | None = None
    methods: frozenset[str] = frozenset({"GET", "HEAD"})
    query: str | None = None
    """Glob matched against the raw query string (e.g. ``"t=1*"``); None matches any."""
    after_requests: int = 0
    after_seconds: float | None = None
    times: int | None = None
    probability: float = 1.0
    matched: int = field(default=0, init=False)
    fired: int = field(default=0, init=False)
    created: float = field(default_factory=time.monotonic, init=False)

    def matches(self, host: str, method: str, path: str, query: str = "") -> bool:
        return (
            (self.host is None or self.host == host)
            and method in self.methods
            and fnmatch.fnmatchcase(path, self.path)
            and (self.query is None or fnmatch.fnmatchcase(query, self.query))
        )

    def should_fire(self, rng: random.Random) -> bool:
        """Count a matching request and decide whether the rule fires for it."""
        self.matched += 1
        if self.matched <= self.after_requests:
            return False
        if self.after_seconds is not None and time.monotonic() - self.created < self.after_seconds:
            return False
        if self.times is not None and self.fired >= self.times:
            return False
        if self.probability < 1.0 and rng.random() >= self.probability:
            return False
        self.fired += 1
        return True


# Convenience constructors for the common fault modes.


def delay(path: str, seconds: float, **kwargs: Any) -> Rule:
    return Rule(path, Delay(seconds), **kwargs)


def rate_limited(path: str, retry_after: int = 1, **kwargs: Any) -> Rule:
    return Rule(path, Respond(429, {"Retry-After": str(retry_after)}), **kwargs)


def unavailable(path: str, **kwargs: Any) -> Rule:
    return Rule(path, Respond(503), **kwargs)


def expire_after(
    path: str,
    *,
    requests: int = 0,
    seconds: float | None = None,
    status: int = 403,
    **kwargs: Any,
) -> Rule:
    """Simulate an expired token: ``status`` once N requests passed and/or a time elapsed."""
    return Rule(path, Respond(status), after_requests=requests, after_seconds=seconds, **kwargs)


def redirect(
    path: str, to_host: str | None = None, to_path: str | None = None, **kwargs: Any
) -> Rule:
    return Rule(path, Redirect(to_host, to_path), **kwargs)


def drop(path: str, **kwargs: Any) -> Rule:
    return Rule(path, Drop(), **kwargs)


# --- recording -------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    """One request as the server saw it."""

    host: str
    method: str
    path: str
    query_string: str
    headers: list[tuple[str, str]]
    time: float
    status: int | None = None
    rule: Rule | None = None

    def header(self, name: str) -> str | None:
        """First value of header ``name`` (case-insensitive), or None."""
        lowered = name.lower()
        return next((v for k, v in self.headers if k.lower() == lowered), None)


# --- server ----------------------------------------------------------------------------


HOSTNAMES = {ORIGIN: "127.0.0.1", CDN: "localhost"}


@dataclass(frozen=True)
class HostInfo:
    name: str
    port: int

    @property
    def hostname(self) -> str:
        return HOSTNAMES[self.name]

    @property
    def base_url(self) -> str:
        return f"http://{self.hostname}:{self.port}"

    @property
    def virtual_hostname(self) -> str:
        return VIRTUAL_HOSTNAMES[self.name]

    @property
    def virtual_base_url(self) -> str:
        return f"http://{self.virtual_hostname}:{self.port}"


async def _add_cors_headers(request: web.Request, response: web.StreamResponse) -> None:
    origin = request.headers.get("Origin")
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"


class FixtureServer:
    """Two-host HLS fixture server. Use as ``async with FixtureServer(root) as server``."""

    def __init__(self, media_root: Path, *, seed: int = 0) -> None:
        self.media_root = media_root.resolve()
        self.rules: list[Rule] = []
        self.requests: list[RecordedRequest] = []
        self.in_flight: dict[str, int] = {ORIGIN: 0, CDN: 0}
        self.max_in_flight: dict[str, int] = {ORIGIN: 0, CDN: 0}
        self.hosts: dict[str, HostInfo] = {}
        self._routes: dict[tuple[str | None, str], Handler] = {}
        self._rng = random.Random(seed)
        self._runners: list[web.AppRunner] = []

    # lifecycle

    async def start(self) -> None:
        for name in (ORIGIN, CDN):
            app = web.Application()
            app[HOST_NAME_KEY] = name
            app.router.add_route("*", "/{tail:.*}", self._handle)
            app.on_response_prepare.append(_add_cors_headers)
            # Cancel handlers whose client disconnected (e.g. timed out during a Delay), so
            # in-flight accounting reflects open connections only.
            runner = web.AppRunner(
                app, access_log=None, handle_signals=False, handler_cancellation=True
            )
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = int(runner.addresses[-1][1])
            if name == CDN:
                # "localhost" may resolve to ::1 first; listen there too when possible.
                with contextlib.suppress(OSError):
                    await web.TCPSite(runner, "::1", port).start()
            self.hosts[name] = HostInfo(name, port)
            self._runners.append(runner)

    async def close(self) -> None:
        for runner in self._runners:
            await runner.cleanup()
        self._runners.clear()

    async def __aenter__(self) -> FixtureServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # configuration

    def url(self, path: str, host: str = ORIGIN, *, virtual: bool = False) -> str:
        """Absolute URL of ``path`` on ``host``."""
        info = self.hosts[host]
        base = info.virtual_base_url if virtual else info.base_url
        return base + (path if path.startswith("/") else "/" + path)

    def media_url(self, relative: str, host: str = CDN, *, virtual: bool = False) -> str:
        """URL of a generated sample file, e.g. ``media_url(samples.playlist("vod_fmp4"))``."""
        return self.url("/media/" + relative.lstrip("/"), host, virtual=virtual)

    def add_rule(self, rule: Rule) -> Rule:
        self.rules.append(rule)
        return rule

    def clear_rules(self) -> None:
        self.rules.clear()

    def add_route(
        self,
        path: str,
        body: bytes | str | Handler,
        *,
        host: str | None = None,
        content_type: str | None = None,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Serve a fixed body (or call ``body`` as a handler) at ``path``."""
        if callable(body):
            self._routes[(host, path)] = body
            return
        data = body.encode() if isinstance(body, str) else body
        ctype = content_type or CONTENT_TYPES.get(Path(path).suffix, "application/octet-stream")

        async def fixed(_: web.Request) -> web.StreamResponse:
            return web.Response(
                status=status, body=data, headers={"Content-Type": ctype, **(headers or {})}
            )

        self._routes[(host, path)] = fixed

    def requests_to(self, path: str = "*", host: str | None = None) -> list[RecordedRequest]:
        """Recorded requests whose path matches the glob ``path`` (and ``host``)."""
        return [
            r
            for r in self.requests
            if (host is None or r.host == host) and fnmatch.fnmatchcase(r.path, path)
        ]

    # request handling

    def _host_of(self, request: web.Request) -> str:
        hostname = (request.host or "").rsplit(":", 1)[0].lower()
        for name, virtual in VIRTUAL_HOSTNAMES.items():
            if hostname == virtual:
                return name
        return request.app[HOST_NAME_KEY]

    def _location(self, request: web.Request, source_host: str, action: Redirect) -> str:
        target = action.to_host or source_host
        virtual = (request.host or "").rsplit(":", 1)[0].lower() in VIRTUAL_HOSTNAMES.values()
        location = self.url(action.to_path or request.path, target, virtual=virtual)
        if action.keep_query and request.query_string:
            location += "?" + request.query_string
        return location

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        host = self._host_of(request)
        record = RecordedRequest(
            host=host,
            method=request.method,
            path=request.path,
            query_string=request.query_string,
            headers=list(request.headers.items()),
            time=time.monotonic(),
        )
        self.requests.append(record)
        self.in_flight[host] += 1
        self.max_in_flight[host] = max(self.max_in_flight[host], self.in_flight[host])
        try:
            response = await self._respond(request, host, record)
            record.status = response.status
            return response
        except ConnectionResetError:
            # The client went away (e.g. it timed out during a Delay); status stays None.
            raise asyncio.CancelledError from None
        finally:
            self.in_flight[host] -= 1

    async def _respond(
        self, request: web.Request, host: str, record: RecordedRequest
    ) -> web.StreamResponse:
        if request.method == "OPTIONS" and "Access-Control-Request-Method" in request.headers:
            return web.Response(
                status=204,
                headers={
                    "Access-Control-Allow-Methods": "GET, HEAD",
                    "Access-Control-Allow-Headers": request.headers.get(
                        "Access-Control-Request-Headers", "*"
                    ),
                    "Access-Control-Max-Age": "600",
                },
            )
        for rule in self.rules:
            if not rule.matches(host, request.method, request.path, request.query_string):
                continue
            if not rule.should_fire(self._rng):
                continue
            record.rule = rule
            action = rule.action
            if isinstance(action, Delay):
                await asyncio.sleep(action.seconds)
                break
            if isinstance(action, Respond):
                return web.Response(status=action.status, headers=action.headers, body=action.body)
            if isinstance(action, Redirect):
                location = self._location(request, host, action)
                return web.Response(status=action.status, headers={"Location": location})
            if isinstance(action, Drop):
                if request.transport is not None:
                    request.transport.abort()
                # Abandon the request; aiohttp then closes it quietly without a response.
                raise asyncio.CancelledError
            break

        handler = self._routes.get((host, request.path)) or self._routes.get((None, request.path))
        if handler is not None:
            return await handler(request)
        if request.path.startswith("/media/"):
            return await self._serve_file(request, request.path.removeprefix("/media/"))
        return web.Response(status=404, text="not found")

    async def _serve_file(self, request: web.Request, relative: str) -> web.StreamResponse:
        path = (self.media_root / relative).resolve()
        if not path.is_relative_to(self.media_root) or not path.is_file():
            return web.Response(status=404, text="not found")
        data = path.read_bytes()
        headers = {
            "Content-Type": CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
            "Accept-Ranges": "bytes",
        }
        status = 200
        if "Range" in request.headers:
            try:
                start, stop, _ = request.http_range.indices(len(data))
            except ValueError:
                start, stop = 0, 0
            if start >= stop:
                headers["Content-Range"] = f"bytes */{len(data)}"
                return web.Response(status=416, headers=headers)
            headers["Content-Range"] = f"bytes {start}-{stop - 1}/{len(data)}"
            data = data[start:stop]
            status = 206
        response = web.StreamResponse(status=status, headers=headers)
        response.content_length = len(data)
        # Send the body inside the handler so in-flight accounting covers the transfer.
        await response.prepare(request)
        if request.method != "HEAD":
            await response.write(data)
        await response.write_eof()
        return response
