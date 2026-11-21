"""Playwright capture (SDD §3.2): find the HLS playlist a page's player uses.

1. The caller supplies a ``BrowserContext`` created with ``service_workers="block"`` so
   every request is visible on the network layer. ``request``/``response`` listeners are
   registered on the *context* before navigation, so requests from iframes and new tabs
   are observed too.
2. New tabs that are not the target page are recorded and closed. When the main frame is
   navigated to another origin, the capturer goes back to the target page and tries
   again (at most ``max_returns`` times). If no candidate appears after clicking play, it
   waits ``click_retry_s`` and clicks again (at most ``max_clicks`` times).
3. Candidates: responses whose URL ends in ``.m3u8``, whose MIME type is an HLS type, or
   that are small text responses to the player's XHR/fetch requests (covering playlists
   without an extension). Only bodies that start with ``#EXTM3U`` and parse as HLS count;
   an MP4 trailer, a segment or other text never becomes a candidate on its MIME type
   alone. If the player fetched segments but no playlist was readable (for example a
   playlist handed over as ``blob:``), capture fails with "playlist not observable".
4. Selection: a master playlist, its variants and its renditions form one video; a media
   playlist no master refers to is a video of its own. A video whose requests (playlist
   or segments) happened after the play click is associated with the player; if exactly
   one video is, it is chosen. Otherwise the user chooses, or ``--non-interactive`` exits
   with code 3.
5. Header templates: for every origin the chosen video used, the non-credential headers
   the player sent (``User-Agent``, ``Origin``, ``Referer``, ``Accept``,
   ``Accept-Language``) and, only for that origin, ``Authorization`` and custom auth
   headers. ``Cookie`` is never recorded: the context's cookie jar supplies cookies.
6. The title comes from the adapter (``og:title``, ``<title>``, ``<h1>``).

Logs name candidates by their short ID (``c1``, ``v1``) and failure reasons only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Final
from urllib.parse import urlsplit

from hls_harvester.adapters import GENERIC, Adapter
from hls_harvester.errors import AmbiguousSelection, CaptureError, HarvesterError
from hls_harvester.playlist import (
    MasterPlaylist,
    MediaPlaylist,
    Playlist,
    PlaylistParseError,
    parse_playlist,
    quality_labels,
)
from hls_harvester.redact import is_sensitive_header, redact_url
from hls_harvester.upstream import HeaderTemplate, origin_of

log = logging.getLogger(__name__)

HLS_MIME_TYPES: Final = frozenset(
    {
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "application/mpegurl",
        "audio/mpegurl",
        "audio/x-mpegurl",
    }
)
_TEXTUAL_MIME_TYPES: Final = frozenset(
    {"", "application/octet-stream", "binary/octet-stream", "application/text"}
)
_SEGMENT_MIME_TYPES: Final = frozenset(
    {"video/mp2t", "video/iso.segment", "audio/aac", "audio/mp4", "video/mp4", "audio/mpeg"}
)
_SEGMENT_SUFFIXES: Final = frozenset(
    {".ts", ".m4s", ".aac", ".m4a", ".m4v", ".mp4", ".cmfv", ".cmfa", ".mp3"}
)
MAX_PLAYLIST_BYTES: Final = 4 * 1024 * 1024
TEMPLATE_HEADERS: Final = ("user-agent", "origin", "referer", "accept", "accept-language")

Chooser = Callable[[Sequence["Video"]], int | None]


@dataclass(frozen=True)
class CaptureOptions:
    timeout_s: float = 45.0
    """Overall capture deadline (SDD §3.2 item 2)."""
    settle_s: float = 1.5
    """Quiet time after the last playlist/segment event before capture ends."""
    click_retry_s: float = 3.0
    """Wait after a play click before clicking again (SDD §3.2 item 3)."""
    max_clicks: int = 3
    max_returns: int = 3


@dataclass
class _Observed:
    url: str
    time: float
    resource_type: str
    headers: dict[str, str]
    chain: tuple[str, ...]
    """Request URLs of the redirect chain that ended here, oldest first."""


@dataclass
class Candidate:
    id: str
    url: str
    """Final URL (after redirects); relative URIs resolve against it."""
    aliases: frozenset[str]
    time: float
    playlist: Playlist | None = None
    error: HarvesterError | None = None

    @property
    def is_master(self) -> bool:
        return isinstance(self.playlist, MasterPlaylist)


@dataclass
class Video:
    id: str
    root: Candidate
    members: list[Candidate] = field(default_factory=list)
    urls: set[str] = field(default_factory=set)
    associated: bool = False

    @property
    def url(self) -> str:
        return self.root.url

    def media(self) -> list[MediaPlaylist]:
        return [
            c.playlist for c in (self.root, *self.members) if isinstance(c.playlist, MediaPlaylist)
        ]

    def fingerprint(self) -> tuple[object, ...]:
        """Identifies the same video across captures (whose URLs carry new tokens)."""
        playlist = self.root.playlist
        if isinstance(playlist, MasterPlaylist):
            return ("master", tuple(label for label, _ in quality_labels(playlist)))
        if isinstance(playlist, MediaPlaylist):
            return ("media", len(playlist.segments), round(playlist.duration, 1))
        return ("unknown", self.root.id)

    def describe(self) -> str:
        playlist = self.root.playlist
        parts = [self.id]
        if isinstance(playlist, MasterPlaylist):
            labels = ", ".join(label for label, _ in quality_labels(playlist))
            parts.append(f"{len(playlist.variants)} qualities ({labels})")
        elif isinstance(playlist, MediaPlaylist):
            parts.append(f"{len(playlist.segments)} segments")
        durations = [m.duration for m in self.media()]
        if durations:
            parts.append(f"{max(durations):.1f} s")
        parts.append(f"from {origin_of(self.url)}")
        if self.associated:
            parts.append("started by the play button")
        return ", ".join(parts)


@dataclass(frozen=True)
class CaptureResult:
    page_url: str
    title: str | None
    video: Video
    templates: tuple[HeaderTemplate, ...]

    @property
    def url(self) -> str:
        return self.video.url


def _mime(headers: dict[str, str]) -> str:
    return (headers.get("content-type") or "").split(";", 1)[0].strip().lower()


def _suffix(url: str) -> str:
    return PurePosixPath(urlsplit(url).path).suffix.lower()


def _title_case(name: str) -> str:
    return "-".join(part.capitalize() for part in name.split("-"))


class Capturer:
    """One capture run over a browser context."""

    def __init__(
        self,
        context: Any,
        page_url: str,
        *,
        options: CaptureOptions | None = None,
        adapter: Adapter = GENERIC,
        chooser: Chooser | None = None,
        prefer: tuple[object, ...] | None = None,
    ) -> None:
        self.context = context
        self.page_url = page_url
        self.options = options or CaptureOptions()
        self.adapter = adapter
        self.chooser = chooser
        self.prefer = prefer
        self.page: Any = None
        self.observed: list[_Observed] = []
        self.candidates: list[Candidate] = []
        self.segment_times: list[float] = []
        self.blob_playlists = 0
        self.popups: list[str] = []
        self.play_time: float | None = None
        self.last_event: float | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._loop = asyncio.get_running_loop()

    def _now(self) -> float:
        return self._loop.time()

    # --- listeners ----------------------------------------------------------------------

    def _on_response(self, response: Any) -> None:
        task = asyncio.ensure_future(self._handle_response(response, self._now()))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_page(self, page: Any) -> None:
        if self.page is None or page == self.page:
            return
        self.popups.append(page.url)
        log.info("closing a new tab opened by the page (%s)", redact_url(page.url or ""))
        task = asyncio.ensure_future(self._close_quietly(page))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _close_quietly(page: Any) -> None:
        with contextlib.suppress(Exception):
            await page.close()

    async def _handle_response(self, response: Any, when: float) -> None:
        try:
            await self._inspect(response, when)
        except Exception as exc:  # a closed page or aborted body must not stop the capture
            log.debug("ignoring a response that could not be inspected: %s", exc)

    async def _inspect(self, response: Any, when: float) -> None:
        request = response.request
        url: str = response.url
        scheme = urlsplit(url).scheme
        if scheme == "blob":
            self.blob_playlists += 1
            return
        if scheme not in {"http", "https"}:
            return
        chain = [request.url]
        previous = request.redirected_from
        while previous is not None:
            chain.insert(0, previous.url)
            previous = previous.redirected_from
        try:
            headers = await request.all_headers()
        except Exception:
            headers = {}
        self.observed.append(_Observed(url, when, request.resource_type, headers, tuple(chain)))
        if not 200 <= response.status < 300:
            return
        response_headers: dict[str, str] = {
            k.lower(): v for k, v in (await response.all_headers()).items()
        }
        mime = _mime(response_headers)
        if self._maybe_playlist(url, mime, request.resource_type, response_headers):
            body: bytes = await response.body()
            if len(body) <= MAX_PLAYLIST_BYTES and body.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(
                b"#EXTM3U"
            ):
                self._add_candidate(url, chain, body, when)
                return
        if mime in _SEGMENT_MIME_TYPES or _suffix(url) in _SEGMENT_SUFFIXES:
            self.segment_times.append(when)
            self.last_event = when

    @staticmethod
    def _maybe_playlist(url: str, mime: str, resource_type: str, headers: dict[str, str]) -> bool:
        if _suffix(url) in {".m3u8", ".m3u"} or mime in HLS_MIME_TYPES:
            return True
        if resource_type not in {"xhr", "fetch", "other"}:
            return False
        if not (mime.startswith("text/") or mime in _TEXTUAL_MIME_TYPES) or mime == "text/html":
            return False
        length = headers.get("content-length")
        return length is None or (length.isdigit() and int(length) <= MAX_PLAYLIST_BYTES)

    def _add_candidate(self, url: str, chain: list[str], body: bytes, when: float) -> None:
        if any(url == c.url for c in self.candidates):
            return  # the player re-requested the same playlist
        candidate = Candidate(
            id=f"c{len(self.candidates) + 1}",
            url=url,
            aliases=frozenset({url, *chain}),
            time=when,
        )
        try:
            candidate.playlist = parse_playlist(body.decode("utf-8"), url)
        except (UnicodeDecodeError, PlaylistParseError) as exc:
            log.debug("response looked like HLS but did not parse: %s", exc)
            return
        except HarvesterError as exc:  # DRM or unsupported tags: remember why
            candidate.error = exc
        self.candidates.append(candidate)
        self.last_event = when
        kind = "master" if candidate.is_master else "media"
        log.info("candidate %s: %s playlist", candidate.id, kind)

    # --- driving the page ---------------------------------------------------------------

    def _away(self) -> bool:
        url = self.page.url if self.page is not None else ""
        if not url or url == "about:blank" or self.page.is_closed():
            return False
        return origin_of(url) != origin_of(self.page_url)

    async def _open(self) -> None:
        if self.page is None or self.page.is_closed():
            self.page = await self.context.new_page()
        try:
            await self.page.goto(
                self.page_url, wait_until="domcontentloaded", timeout=self.options.timeout_s * 1000
            )
        except Exception as exc:
            raise CaptureError(f"could not open the page: {str(exc).splitlines()[0]}") from None

    async def _click_play(self) -> bool:
        await self.adapter.dismiss_overlays(self.page)
        target = await self.adapter.find_play_target(self.page)
        if target is None:
            return False
        if self.play_time is None:
            self.play_time = self._now()
        try:
            await target.click(timeout=3000)
        except Exception as exc:  # e.g. the click navigated the page away
            log.debug("play click did not complete: %s", exc)
        return True

    async def _drive(self) -> None:
        opts = self.options
        deadline = self._now() + opts.timeout_s
        clicks = 0
        returns = 0
        last_click: float | None = None
        no_target = False
        candidates_at_click = 0
        await asyncio.sleep(min(opts.settle_s, 1.0))  # let preloads arrive

        while self._now() < deadline:
            now = self._now()
            if self._away():
                if returns >= opts.max_returns:
                    raise CaptureError(
                        "the page kept navigating to another site; no player request found"
                    )
                returns += 1
                log.info(
                    "the page navigated away; returning to it (%d/%d)", returns, opts.max_returns
                )
                await self._open()
                last_click = None
                await asyncio.sleep(min(opts.settle_s, 1.0))
                continue
            quiet = self.last_event is not None and now - self.last_event >= opts.settle_s
            if self.candidates and quiet and (clicks > 0 or no_target):
                return
            waiting = last_click is not None and now - last_click < opts.click_retry_s
            new_since_click = len(self.candidates) > candidates_at_click
            if not waiting and not (clicks > 0 and new_since_click):
                if clicks >= opts.max_clicks:
                    return
                if await self._click_play():
                    clicks += 1
                    last_click = self._now()
                    candidates_at_click = len(self.candidates)
                    log.info("clicked play (%d/%d)", clicks, opts.max_clicks)
                else:
                    no_target = True
                    if self.candidates and quiet:
                        return
                    last_click = self._now()  # look for a play button again later
                    clicks += 1
            await asyncio.sleep(0.1)
        if not self.candidates:
            raise CaptureError(f"capture timed out after {opts.timeout_s:g}s")

    # --- selection ----------------------------------------------------------------------

    def _times_by_url(self) -> dict[str, list[float]]:
        times: dict[str, list[float]] = {}
        for obs in self.observed:
            for url in {obs.url, *obs.chain}:
                times.setdefault(url, []).append(obs.time)
        return times

    def videos(self) -> list[Video]:
        """Group candidates into videos (SDD §3.2 item 5)."""
        owner: dict[str, Candidate] = {}
        for candidate in self.candidates:
            if isinstance(candidate.playlist, MasterPlaylist):
                for variant in candidate.playlist.variants:
                    owner.setdefault(variant.uri, candidate)
                for rendition in candidate.playlist.renditions:
                    if rendition.uri:
                        owner.setdefault(rendition.uri, candidate)
        groups: dict[str, Video] = {}
        for candidate in self.candidates:
            master = next((owner[a] for a in candidate.aliases if a in owner), None)
            if master is not None and master is not candidate:
                groups.setdefault(master.id, Video("", master)).members.append(candidate)
            else:
                groups.setdefault(candidate.id, Video("", candidate))
        videos = sorted(groups.values(), key=lambda v: v.root.time)
        times = self._times_by_url()
        for index, video in enumerate(videos, start=1):
            video.id = f"v{index}"
            for candidate in (video.root, *video.members):
                video.urls |= candidate.aliases
                if isinstance(candidate.playlist, MediaPlaylist):
                    for segment in candidate.playlist.segments:
                        video.urls.add(segment.uri)
                        if segment.init is not None:
                            video.urls.add(segment.init.uri)
            if self.play_time is None:
                video.associated = True
            else:
                video.associated = any(
                    t >= self.play_time for url in video.urls for t in times.get(url, ())
                )
        return videos

    def select(self, videos: list[Video]) -> Video:
        playable = [v for v in videos if v.root.error is None]
        if not playable:
            errors = [v.root.error for v in videos if v.root.error is not None]
            if errors:
                raise errors[0]
            if self.segment_times or self.blob_playlists:
                raise CaptureError(
                    "playlist not observable: the player requested media, but no HLS playlist "
                    "could be read from the network (it may be decoded in the page and handed "
                    "to the player as a blob: URL)"
                )
            raise CaptureError("no player request found")
        if len(playable) == 1:
            return playable[0]
        if self.prefer is not None:
            same = [v for v in playable if v.fingerprint() == self.prefer]
            if len(same) == 1:
                return same[0]
        associated = [v for v in playable if v.associated]
        if len(associated) == 1:
            return associated[0]
        choices = associated or playable
        listing = "; ".join(v.describe() for v in choices)
        if self.chooser is not None:
            index = self.chooser(choices)
            if index is not None and 0 <= index < len(choices):
                return choices[index]
        raise AmbiguousSelection(f"several videos found, choose one: {listing}")

    def templates(self, video: Video) -> tuple[HeaderTemplate, ...]:
        """Per-origin request header templates for ``video`` (SDD §3.2 item 6)."""
        result: dict[str, HeaderTemplate] = {}
        for obs in sorted(self.observed, key=lambda o: o.time):
            if not ({obs.url, *obs.chain} & video.urls):
                continue
            # Each hop of a redirect is observed as its own response, with the headers
            # actually sent to that URL, so auth headers stay with the origin that got them.
            origin = origin_of(obs.url)
            if origin in result:
                continue
            headers = {_title_case(k): v for k, v in obs.headers.items() if k in TEMPLATE_HEADERS}
            auth = {
                _title_case(k): v
                for k, v in obs.headers.items()
                if k not in {"cookie", *TEMPLATE_HEADERS} and is_sensitive_header(k)
            }
            result[origin] = HeaderTemplate(origin, headers, auth)
        return tuple(result.values())

    # --- entry point --------------------------------------------------------------------

    async def run(self) -> CaptureResult:
        self.context.on("response", self._on_response)
        self.context.on("page", self._on_page)
        try:
            await self._open()
            try:
                await self._drive()
            finally:
                if self._tasks:
                    await asyncio.wait(set(self._tasks), timeout=5)
            videos = self.videos()
            try:
                video = self.select(videos)
            except CaptureError as exc:
                if await self._login_form_present():
                    raise CaptureError(
                        f"{exc}. The page shows a login form: sign in with "
                        "`hls-harvester login <site-url> --storage-state <file>` and pass "
                        "--storage-state to get"
                    ) from None
                raise
            log.info("selected %s", video.describe())
            title = await self.adapter.title(self.page)
            return CaptureResult(self.page_url, title, video, self.templates(video))
        finally:
            self.context.remove_listener("response", self._on_response)
            self.context.remove_listener("page", self._on_page)
            for page in list(self.context.pages):
                await self._close_quietly(page)

    async def _login_form_present(self) -> bool:
        if self.page is None or self.page.is_closed():
            return False
        for frame in self.page.frames:
            with contextlib.suppress(Exception):
                if await frame.locator("input[type='password']").count():
                    return True
        return False


async def capture(
    context: Any,
    page_url: str,
    *,
    options: CaptureOptions | None = None,
    adapter: Adapter = GENERIC,
    chooser: Chooser | None = None,
    prefer: tuple[object, ...] | None = None,
) -> CaptureResult:
    """Capture ``page_url`` in ``context`` (see the module docstring)."""
    capturer = Capturer(
        context, page_url, options=options, adapter=adapter, chooser=chooser, prefer=prefer
    )
    return await capturer.run()


async def launch_browser(playwright: Any, *, headless: bool, executable: str | None) -> Any:
    """Launch Chromium; ``executable`` overrides Playwright's own build."""
    kwargs: dict[str, Any] = {"headless": headless}
    if executable:
        kwargs["executable_path"] = executable
    try:
        return await playwright.chromium.launch(**kwargs)
    except Exception as exc:
        raise CaptureError(
            f"could not start Chromium ({str(exc).splitlines()[0]}); "
            "run `playwright install chromium` or `hls-harvester doctor`"
        ) from None


async def new_capture_context(browser: Any, storage_state: str | None = None) -> Any:
    """A fresh, isolated context with service workers blocked (SDD §3.2 item 2)."""
    kwargs: dict[str, Any] = {"service_workers": "block"}
    if storage_state is not None:
        kwargs["storage_state"] = storage_state
    return await browser.new_context(**kwargs)
