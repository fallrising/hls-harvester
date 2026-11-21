"""Local HLS playback proxy (SDD §3.6).

* Binds ``127.0.0.1`` on a random port. Every resource lives under a per-session
  128-bit random path prefix; local paths never contain upstream URLs.
* Only ``Host: 127.0.0.1:<port>`` or ``localhost:<port>`` is served (403 otherwise),
  against DNS rebinding. A request with an ``Origin`` header is served only for a
  configured local player origin, which then gets that exact origin back in
  ``Access-Control-Allow-Origin``; nothing ever gets a wildcard.
* Only registered resources are served; unknown paths get 404. A playlist's URIs are
  registered when the playlist is rewritten, including new URIs that appear when a live
  playlist is refreshed.
* Playlists are fetched through the shared upstream core, parsed (DRM and unsupported
  tags are refused, SDD §1.1) and rewritten so every URI, including ``EXT-X-MAP``,
  ``EXT-X-KEY`` and ``EXT-X-MEDIA``, points at the proxy. A URI that appears several times
  (byte-range segments of one file) maps to one local path, so ``EXT-X-BYTERANGE``
  semantics are unchanged.
* A client's ``Range`` header is forwarded and 206 / ``Content-Range`` are passed back.
* A playlist without ``EXT-X-ENDLIST`` is re-fetched from upstream at most every half
  ``EXT-X-TARGETDURATION``; the media sequence is passed through unchanged.
* 401/403 from upstream: a segment's playlist is re-fetched and the segment remapped by
  its position (media sequence number); if the playlist itself is refused, the top-level
  playlist is fetched again via ``recapture`` (a fresh page capture, SDD §3.5 item 4).
  Local paths stay the same, so the player just retries.
* The proxy stops after ``idle_timeout_s`` without player requests, or on Ctrl-C, and
  forgets every mapping.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Final, Literal
from urllib.parse import urljoin, urlsplit

from aiohttp import web

from hls_harvester.errors import CredentialsExpired, HarvesterError, UpstreamError
from hls_harvester.playlist import (
    MasterPlaylist,
    MediaPlaylist,
    Playlist,
    parse_attributes,
    parse_playlist,
    select_variant,
)
from hls_harvester.upstream import Response, Upstream

log = logging.getLogger(__name__)

PREFIX_BYTES: Final = 16
"""128-bit path prefix (SDD §3.6)."""
MAX_REFRESHES: Final = 3
PLAYLIST_MIME: Final = "application/vnd.apple.mpegurl"
_URI_ATTR_RE: Final = re.compile(r'(?<=[:,])URI="([^"]*)"')
_PLAYLIST_TAGS: Final = frozenset(
    {"EXT-X-MEDIA", "EXT-X-I-FRAME-STREAM-INF", "EXT-X-IMAGE-STREAM-INF", "EXT-X-RENDITION-REPORT"}
)
_DROPPED_TAGS: Final = frozenset({"EXT-X-CONTENT-STEERING"})
"""Would send the player to a steering server directly, bypassing the proxy."""
_MEDIA_SUFFIXES: Final = frozenset(
    {
        ".ts",
        ".m4s",
        ".mp4",
        ".m4a",
        ".m4v",
        ".aac",
        ".ac3",
        ".ec3",
        ".mp3",
        ".vtt",
        ".webvtt",
        ".cmfv",
        ".cmfa",
        ".key",
        ".bin",
        ".jpg",
        ".png",
    }
)

Recapture = Callable[[], Awaitable[str]]
Position = tuple[str, int]


@dataclass
class _Resource:
    id: str
    kind: Literal["playlist", "media"]
    url: str
    """Current upstream URL; replaced in place when credentials are refreshed."""
    suffix: str
    parent: _Resource | None = None
    referrer: str | None = None
    children: dict[Position, _Resource] = field(default_factory=dict)
    # Playlist cache.
    text: str | None = None
    final_url: str | None = None
    fetched_at: float = 0.0
    ttl: float | None = None
    """None: never re-fetch (master or finished media playlist)."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def name(self) -> str:
        return self.id + self.suffix


def _suffix_for(url: str, kind: str) -> str:
    if kind == "playlist":
        return ".m3u8"
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    return suffix if suffix in _MEDIA_SUFFIXES else ""


class PlaybackProxy:
    """Serve one captured video to local players. Use ``start`` / ``open`` / ``close``."""

    def __init__(
        self,
        upstream: Upstream,
        *,
        quality: str | None = None,
        allowed_origins: Iterable[str] = (),
        idle_timeout_s: float = 600.0,
        recapture: Recapture | None = None,
    ) -> None:
        self.upstream = upstream
        self.quality = quality
        self.allowed_origins = frozenset(o.rstrip("/").lower() for o in allowed_origins)
        self.idle_timeout_s = idle_timeout_s
        self.recapture = recapture
        self.prefix = secrets.token_hex(PREFIX_BYTES)
        self.port = 0
        self.refreshes = 0
        self.root: _Resource | None = None
        self._by_name: dict[str, _Resource] = {}
        self._counter = 0
        self._runner: web.AppRunner | None = None
        self._loop = asyncio.get_running_loop()
        self._last_request = self._loop.time()
        self._stopped = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._refresh_generation = 0

    # --- lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None, handle_signals=False)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = int(self._runner.addresses[-1][1])

    async def open(self, url: str) -> str:
        """Register the top-level playlist, fetch it once (so DRM is refused before a URL
        is handed out) and return its loopback URL."""
        self.root = self._new_resource("playlist", url, parent=None, referrer=None)
        await self._playlist_text(self.root)
        self._last_request = self._loop.time()
        return self.url_for(self.root)

    def url_for(self, resource: _Resource) -> str:
        return f"http://127.0.0.1:{self.port}{self._path(resource)}"

    def _path(self, resource: _Resource) -> str:
        return f"/{self.prefix}/{resource.name}"

    async def serve_until_idle(self) -> None:
        """Return once no player request arrived for ``idle_timeout_s`` (or ``stop``)."""
        while not self._stopped.is_set():
            idle = self._loop.time() - self._last_request
            remaining = self.idle_timeout_s - idle
            if remaining <= 0:
                log.info("no player requests for %.0fs; stopping", self.idle_timeout_s)
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=min(remaining, 1.0))

    def stop(self) -> None:
        self._stopped.set()

    async def close(self) -> None:
        self._stopped.set()
        self._by_name.clear()  # revoke every mapping
        self.root = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # --- registry -----------------------------------------------------------------------

    def _new_resource(
        self,
        kind: Literal["playlist", "media"],
        url: str,
        *,
        parent: _Resource | None,
        referrer: str | None,
    ) -> _Resource:
        self._counter += 1
        resource = _Resource(
            id=f"r{self._counter}",
            kind=kind,
            url=url,
            suffix=_suffix_for(url, kind),
            parent=parent,
            referrer=referrer,
        )
        self._by_name[resource.name] = resource
        return resource

    def _child(
        self,
        parent: _Resource,
        position: Position,
        url: str,
        kind: Literal["playlist", "media"],
        referrer: str,
    ) -> _Resource:
        """The child at ``position``; a refreshed URL replaces the old one in place."""
        child = parent.children.get(position)
        if child is None:
            child = self._new_resource(kind, url, parent=parent, referrer=referrer)
            parent.children[position] = child
        elif child.url != url:
            child.url = url
            child.referrer = referrer
            child.text = None  # a refreshed playlist must be fetched from its new URL
        return child

    def _prune(self, parent: _Resource, media_sequence: int, window: int) -> None:
        """Forget segments that dropped out of a live playlist long ago."""
        floor = media_sequence - 2 * max(window, 1)
        for position in [p for p in parent.children if p[0] == "seg" and p[1] < floor]:
            child = parent.children.pop(position)
            self._by_name.pop(child.name, None)

    # --- upstream with credential refresh -----------------------------------------------

    async def _refresh_from_root(self, generation: int, *, refetch_root: bool) -> None:
        """Get fresh credentials for the top-level playlist (SDD §3.5 item 4).

        With ``refetch_root`` the new top-level playlist is fetched and rewritten, which
        remaps its child playlists in place; the root's own fetch passes False because it
        retries with the new URL itself (and holds the root's lock).
        """
        async with self._refresh_lock:
            if generation == self._refresh_generation:
                if self.root is None:
                    raise UpstreamError("the proxy is closed")
                if self.refreshes >= MAX_REFRESHES:
                    raise UpstreamError(
                        f"credentials expired again after {MAX_REFRESHES} refreshes"
                    )
                self.refreshes += 1
                if self.recapture is None:
                    raise UpstreamError(
                        "upstream refused the playlist (401/403) and there is no page to "
                        "capture again; pass the page URL instead of --playlist"
                    )
                log.info(
                    "re-capturing the page for fresh credentials (%d/%d)",
                    self.refreshes,
                    MAX_REFRESHES,
                )
                self.root.url = await self.recapture()
                self.root.text = None
                self._refresh_generation += 1
        if refetch_root and self.root is not None:
            await self._playlist_text(self.root, force=True)

    async def _fetch_playlist(self, resource: _Resource) -> Response:
        generation = self._refresh_generation
        try:
            return await self.upstream.get(resource.url, referrer=resource.referrer)
        except CredentialsExpired:
            await self._refresh_from_root(generation, refetch_root=resource is not self.root)
            return await self.upstream.get(resource.url, referrer=resource.referrer)

    async def _fetch_media(self, resource: _Resource, range_header: str | None) -> Response:
        parent = resource.parent
        referrer = parent.final_url if parent is not None else None
        try:
            return await self.upstream.get(
                resource.url, referrer=referrer, range_header=range_header
            )
        except CredentialsExpired:
            if parent is None:
                raise
            log.info("%s was refused; re-fetching its playlist", resource.id)
            await self._playlist_text(parent, force=True)
            return await self.upstream.get(
                resource.url, referrer=parent.final_url, range_header=range_header
            )

    # --- playlists ----------------------------------------------------------------------

    async def _playlist_text(self, resource: _Resource, *, force: bool = False) -> str:
        async with resource.lock:
            now = self._loop.time()
            fresh = resource.text is not None and (
                resource.ttl is None or now - resource.fetched_at < resource.ttl
            )
            if fresh and not force and resource.text is not None:
                return resource.text
            response = await self._fetch_playlist(resource)
            body = response.text()
            playlist = parse_playlist(body, response.url)
            resource.final_url = response.url
            resource.text = self._rewrite(resource, body, playlist)
            resource.fetched_at = self._loop.time()
            if isinstance(playlist, MediaPlaylist) and not playlist.endlist:
                resource.ttl = max(0.5, playlist.target_duration / 2)
                self._prune(resource, playlist.media_sequence, len(playlist.segments))
            else:
                resource.ttl = None
            return resource.text

    def _filter_master(self, lines: list[str], base: str, master: MasterPlaylist) -> list[str]:
        """Keep only the ``--quality`` variant and the renditions it refers to (SDD §3.4)."""
        assert self.quality is not None
        chosen = select_variant(master, self.quality)
        groups = {g for g in (chosen.audio, chosen.video, chosen.subtitles) if g}
        kept: list[str] = []
        skip_uri = False
        for line in lines:
            if not line:
                continue
            if not line.startswith("#"):
                if not skip_uri:
                    kept.append(line)
                skip_uri = False
                continue
            name, _, value = line[1:].partition(":")
            if name == "EXT-X-STREAM-INF":
                uri = self._next_uri(lines, line)
                skip_uri = urljoin(base, uri) != chosen.uri
                if skip_uri:
                    continue
            trick_play = name in {"EXT-X-I-FRAME-STREAM-INF", "EXT-X-IMAGE-STREAM-INF"}
            other_group = (
                name == "EXT-X-MEDIA" and parse_attributes(value).get("GROUP-ID") not in groups
            )
            if trick_play or other_group:
                continue
            kept.append(line)
        return kept

    @staticmethod
    def _next_uri(lines: list[str], tag_line: str) -> str:
        start = lines.index(tag_line) + 1
        return next((ln for ln in lines[start:] if ln and not ln.startswith("#")), "")

    def _rewrite(self, resource: _Resource, body: str, playlist: Playlist) -> str:
        """Point every URI of ``body`` at the proxy (see the module docstring)."""
        base = resource.final_url or resource.url
        lines = [line.strip() for line in body.lstrip("\ufeff").splitlines()]
        is_master = isinstance(playlist, MasterPlaylist)
        if isinstance(playlist, MasterPlaylist) and resource is self.root and self.quality:
            lines = self._filter_master(lines, base, playlist)
        media_sequence = playlist.media_sequence if isinstance(playlist, MediaPlaylist) else 0
        counters: dict[str, int] = {}
        first_position: dict[str, Position] = {}

        def local(url: str, kind: Literal["playlist", "media"], position: Position) -> str:
            if url.startswith("data:"):
                return url
            # A URI used several times (byte ranges of one file) keeps one local path.
            key = first_position.setdefault(url, position)
            return self._path(self._child(resource, key, url, kind, base))

        def next_index(tag: str) -> int:
            counters[tag] = counters.get(tag, 0) + 1
            return counters[tag] - 1

        out: list[str] = []
        for line in lines:
            if not line:
                continue
            if not line.startswith("#"):
                url = urljoin(base, line)
                if is_master:
                    out.append(local(url, "playlist", ("var", next_index("var"))))
                else:
                    sequence = media_sequence + next_index("seg")
                    out.append(local(url, "media", ("seg", sequence)))
                continue
            name = line[1:].partition(":")[0]
            if name in _DROPPED_TAGS:
                continue
            if 'URI="' in line:
                kind: Literal["playlist", "media"] = (
                    "playlist" if name in _PLAYLIST_TAGS else "media"
                )
                position = (name, next_index(name))

                def replace(
                    match: re.Match[str],
                    kind: Literal["playlist", "media"] = kind,
                    position: Position = position,
                ) -> str:
                    return f'URI="{local(urljoin(base, match.group(1)), kind, position)}"'

                line = _URI_ATTR_RE.sub(replace, line)
            out.append(line)
        return "\n".join(out) + "\n"

    # --- HTTP ---------------------------------------------------------------------------

    def _host_allowed(self, request: web.Request) -> bool:
        host = request.headers.get("Host", "").lower()
        return host in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def _resource_for(self, path: str) -> _Resource | None:
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != "":
            return None
        if not hmac.compare_digest(parts[1].encode(), self.prefix.encode()):
            return None
        return self._by_name.get(parts[2])

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self._last_request = self._loop.time()
        if not self._host_allowed(request):
            return web.Response(status=403, text="forbidden")
        cors: dict[str, str] = {}
        origin = request.headers.get("Origin")
        if origin is not None:
            if origin.rstrip("/").lower() not in self.allowed_origins:
                return web.Response(status=403, text="forbidden")
            cors = {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Expose-Headers": "Content-Length, Content-Range",
                "Vary": "Origin",
            }
        resource = self._resource_for(request.path)
        if resource is None:
            return web.Response(status=404, text="not found", headers=cors)
        if request.method == "OPTIONS":
            return web.Response(
                status=204,
                headers={
                    **cors,
                    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                    "Access-Control-Allow-Headers": "Range",
                },
            )
        if request.method not in {"GET", "HEAD"}:
            return web.Response(status=405, headers=cors)
        try:
            if resource.kind == "playlist":
                text = await self._playlist_text(resource)
                return web.Response(
                    text=text,
                    headers={**cors, "Content-Type": PLAYLIST_MIME, "Cache-Control": "no-store"},
                )
            upstream = await self._fetch_media(resource, request.headers.get("Range"))
        except HarvesterError as exc:
            log.warning("%s: %s", resource.id, exc)
            return web.Response(status=502, text="upstream error", headers=cors)
        headers = {**cors, "Accept-Ranges": "bytes"}
        for name in ("content-type", "content-range"):
            if name in upstream.headers:
                headers[name.title()] = upstream.headers[name]
        return web.Response(status=upstream.status, body=upstream.body, headers=headers)
