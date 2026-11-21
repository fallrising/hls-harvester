"""Segment downloader (SDD §3.5): job state, resume, credential refresh, remux, verify.

Flow for one ``get`` in download mode:

1. Resolve the source: fetch the playlist; for a master, pick the variant (``--quality``)
   and its audio rendition. Every media playlist must have ``EXT-X-ENDLIST``.
2. Create ``.<name>.hlsh/`` (0700) next to the output with ``job.json`` (0600). The job
   records each resource's file, size and completion, but never a credential or an
   upstream URL: the source is stored redacted (scheme, host, path).
3. Fetch init sections, keys and segments through :class:`~hls_harvester.upstream.Upstream`
   (which bounds concurrency per host and retries transient errors). Each file is written
   atomically and ``job.json`` is updated after every completed resource, so a killed
   process loses at most the resources in flight.
4. On 401/403 the track's new requests pause and its media playlist is re-requested; the
   new playlist is aligned with the old one by media sequence number and duration, and
   only unfinished resources use the new URLs. If re-requesting fails too, the page is
   captured again in the same browser session (``recapture``) and every track is realigned
   to the new URLs; without a page (``--playlist``) the download stops with exit code 5.
   At most 3 refreshes per job.
5. ``--resume`` re-requests the playlist (fresh credentials), aligns it with ``job.json``
   and downloads only resources that are unfinished or whose file size does not match.
6. Remux and verify (:mod:`hls_harvester.remux`) into a hidden temporary file in the
   output directory; only a verified file is renamed to the final path, after which the
   work directory is deleted. On failure the work directory stays for ``--resume`` and
   no partial file is left at the final path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from hls_harvester.errors import CredentialsExpired, HarvesterError, UpstreamError, UsageError
from hls_harvester.filenames import name_from_url, sanitize_filename, unique_path
from hls_harvester.playlist import (
    ByteRange,
    MasterPlaylist,
    MediaPlaylist,
    parse_playlist,
    quality_labels,
    select_variant,
)
from hls_harvester.redact import redact_url
from hls_harvester.remux import LocalSegment, LocalTrack, remux, verify_output
from hls_harvester.upstream import Upstream

log = logging.getLogger(__name__)

JOB_FORMAT: Final = 1
MAX_REFRESHES: Final = 3
DURATION_MATCH_S: Final = 0.01
CONTAINERS: Final = frozenset({".mp4", ".m4v", ".mov", ".mkv"})
_SEGMENT_SUFFIXES: Final = frozenset(
    {".ts", ".m4s", ".mp4", ".m4a", ".m4v", ".aac", ".ac3", ".ec3", ".mp3", ".cmfv", ".cmfa"}
)

RestartChoice = Literal["resume", "restart", "abort"]
ConfirmRestart = Callable[[Path], RestartChoice]
Recapture = Callable[[], Awaitable[str]]
"""Captures the page again and returns the new top-level playlist URL."""
Progress = Callable[[int, int], None]


# --- source resolution -----------------------------------------------------------------


@dataclass(frozen=True)
class SourceTrack:
    name: str
    """``main`` (the variant or the only media playlist) or ``audio``."""
    url: str
    """URL the media playlist was requested from; re-requested on credential refresh."""
    referrer: str | None
    playlist: MediaPlaylist


@dataclass(frozen=True)
class Source:
    url: str
    tracks: tuple[SourceTrack, ...]
    quality: str | None = None
    audio: str | None = None


async def fetch_media_playlist(upstream: Upstream, url: str, referrer: str | None) -> MediaPlaylist:
    response = await upstream.get(url, referrer=referrer)
    playlist = parse_playlist(response.text(), response.url)
    if not isinstance(playlist, MediaPlaylist):
        raise UpstreamError(f"{redact_url(url)}: expected a media playlist, got a master")
    return playlist


async def resolve_source(upstream: Upstream, url: str, quality: str | None) -> Source:
    """Fetch ``url`` and select the media playlists to download (SDD §3.4, §3.5 item 1)."""
    response = await upstream.get(url)
    playlist = parse_playlist(response.text(), response.url)
    if isinstance(playlist, MediaPlaylist):
        if quality is not None:
            log.warning("--quality %s ignored: the playlist offers a single quality", quality)
        source = Source(url, (SourceTrack("main", url, None, playlist),))
    else:
        source = await _resolve_master(upstream, url, playlist, quality)
    for track in source.tracks:
        if not track.playlist.endlist:
            raise UsageError(
                "the playlist has no #EXT-X-ENDLIST (it is still being updated); "
                "it cannot be downloaded completely, use --play instead"
            )
        if not track.playlist.segments:
            raise UpstreamError(f"{redact_url(track.url)}: playlist has no segments")
    return source


async def _resolve_master(
    upstream: Upstream, url: str, master: MasterPlaylist, quality: str | None
) -> Source:
    variant = select_variant(master, quality)
    label = next(label for label, v in quality_labels(master) if v is variant)
    tracks = [
        SourceTrack(
            "main",
            variant.uri,
            master.url,
            await fetch_media_playlist(upstream, variant.uri, master.url),
        )
    ]
    audio = master.audio_for(variant)
    if audio is not None and audio.uri is not None:
        tracks.append(
            SourceTrack(
                "audio",
                audio.uri,
                master.url,
                await fetch_media_playlist(upstream, audio.uri, master.url),
            )
        )
    if variant.subtitles:
        log.warning("subtitle renditions are not downloaded yet")
    log.info("selected quality %s%s", label, f" with audio {audio.name!r}" if audio else "")
    return Source(url, tuple(tracks), quality=label, audio=audio.name if audio else None)


# --- job state -------------------------------------------------------------------------


@dataclass
class Resource:
    kind: Literal["init", "key", "segment"]
    file: str
    """Path relative to the work directory."""
    url: str = field(repr=False)
    byterange: ByteRange | None = None
    sequence: int | None = None
    duration: float | None = None
    size: int | None = None
    done: bool = False

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"file": self.file, "size": self.size, "done": self.done}
        if self.sequence is not None:
            data["seq"] = self.sequence
            data["duration"] = self.duration
        if self.byterange is not None:
            data["length"] = self.byterange.length
        return data


@dataclass
class TrackJob:
    name: str
    source: SourceTrack
    inits: list[Resource]
    keys: list[Resource]
    segments: list[Resource]
    init_of: list[int | None]
    """Index into ``inits`` for each segment."""
    key_of: list[int | None]
    iv_of: list[bytes | None]
    generation: int = 0
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def resources(self) -> list[Resource]:
        return [*self.inits, *self.keys, *self.segments]

    def local_track(self) -> LocalTrack:
        periods: list[list[LocalSegment]] = []
        for index, (res, seg) in enumerate(
            zip(self.segments, self.source.playlist.segments, strict=True)
        ):
            if not periods or seg.discontinuity:
                periods.append([])
            init = self.init_of[index]
            key = self.key_of[index]
            periods[-1].append(
                LocalSegment(
                    path=res.file,
                    duration=seg.duration,
                    init=self.inits[init].file if init is not None else None,
                    key=self.keys[key].file if key is not None else None,
                    iv=self.iv_of[index],
                )
            )
        return LocalTrack(self.name, tuple(tuple(p) for p in periods))


def _suffix(url: str, default: str) -> str:
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    return suffix if suffix in _SEGMENT_SUFFIXES else default


def build_track(source: SourceTrack) -> TrackJob:
    """Turn a media playlist into the resources to fetch."""
    name = source.name
    inits: list[Resource] = []
    keys: list[Resource] = []
    init_index: dict[tuple[str, ByteRange | None], int] = {}
    key_index: dict[str, int] = {}
    segments: list[Resource] = []
    init_of: list[int | None] = []
    key_of: list[int | None] = []
    iv_of: list[bytes | None] = []
    for seg in source.playlist.segments:
        init_slot = None
        if seg.init is not None:
            ident = (seg.init.uri, seg.init.byterange)
            if ident not in init_index:
                init_index[ident] = len(inits)
                inits.append(
                    Resource(
                        "init",
                        f"{name}/init_{len(inits):03d}{_suffix(seg.init.uri, '.mp4')}",
                        seg.init.uri,
                        seg.init.byterange,
                    )
                )
            init_slot = init_index[ident]
        key_slot = None
        if seg.key is not None:
            if seg.key.uri not in key_index:
                key_index[seg.key.uri] = len(keys)
                keys.append(Resource("key", f"{name}/key_{len(keys):03d}.key", seg.key.uri))
            key_slot = key_index[seg.key.uri]
        default_suffix = ".m4s" if seg.init is not None else ".ts"
        segments.append(
            Resource(
                "segment",
                f"{name}/seg_{seg.sequence:06d}{_suffix(seg.uri, default_suffix)}",
                seg.uri,
                seg.byterange,
                sequence=seg.sequence,
                duration=seg.duration,
            )
        )
        init_of.append(init_slot)
        key_of.append(key_slot)
        iv_of.append(seg.iv())
    return TrackJob(name, source, inits, keys, segments, init_of, key_of, iv_of)


def align(old: TrackJob, new: TrackJob) -> None:
    """Copy upstream URLs from ``new`` into ``old`` after checking they describe the same
    media (same segments by sequence number and duration, same init sections and keys).
    """
    problems = []
    if len(old.segments) != len(new.segments):
        problems.append(f"{len(old.segments)} vs {len(new.segments)} segments")
    else:
        for a, b in zip(old.segments, new.segments, strict=True):
            if a.sequence != b.sequence or abs((a.duration or 0) - (b.duration or 0)) > (
                DURATION_MATCH_S
            ):
                problems.append(f"segment {a.sequence} does not match")
                break
    if len(old.inits) != len(new.inits) or len(old.keys) != len(new.keys):
        problems.append("init sections or keys differ")
    if problems:
        raise UpstreamError(
            f"cannot align the new {old.name} playlist with the old one: {'; '.join(problems)}"
        )
    for a, b in zip(old.resources(), new.resources(), strict=True):
        if a.byterange != b.byterange:
            raise UpstreamError(f"cannot align {old.name} playlist: byte ranges differ")
        a.url = b.url
    old.source = new.source


class Job:
    """``job.json`` plus the runtime track state."""

    def __init__(self, workdir: Path, source: Source, tracks: list[TrackJob]) -> None:
        self.workdir = workdir
        self.source = source
        self.tracks = tracks

    @property
    def path(self) -> Path:
        return self.workdir / "job.json"

    def to_json(self) -> dict[str, Any]:
        return {
            "format": JOB_FORMAT,
            "source": redact_url(self.source.url),
            "quality": self.source.quality,
            "audio": self.source.audio,
            "tracks": {
                t.name: {
                    "inits": [r.to_json() for r in t.inits],
                    "keys": [r.to_json() for r in t.keys],
                    "segments": [r.to_json() for r in t.segments],
                }
                for t in self.tracks
            },
        }

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        _write_private(tmp, json.dumps(self.to_json(), indent=1).encode())
        os.replace(tmp, self.path)

    def restore(self) -> None:
        """Mark resources done that ``job.json`` and the files on disk agree on."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise UsageError(f"cannot read {self.path}: {exc}") from exc
        if data.get("format") != JOB_FORMAT:
            raise UsageError(f"{self.path}: unknown job format")
        if data.get("source") != redact_url(self.source.url):
            raise UsageError(
                f"{self.workdir} belongs to another source ({data.get('source')}); "
                "remove it or choose another --output"
            )
        if (data.get("quality"), data.get("audio")) != (self.source.quality, self.source.audio):
            raise UsageError(
                f"{self.workdir} was started with quality {data.get('quality')}; "
                "resume with the same --quality"
            )
        saved_tracks = data.get("tracks", {})
        if set(saved_tracks) != {t.name for t in self.tracks}:
            raise UpstreamError("cannot align the playlist with the saved job: tracks differ")
        for track in self.tracks:
            saved = saved_tracks[track.name]
            for kind, resources in (
                ("inits", track.inits),
                ("keys", track.keys),
                ("segments", track.segments),
            ):
                entries = saved.get(kind, [])
                if len(entries) != len(resources):
                    raise UpstreamError(
                        f"cannot align the {track.name} playlist with the saved job: "
                        f"{len(entries)} vs {len(resources)} {kind}"
                    )
                for entry, res in zip(entries, resources, strict=True):
                    if entry.get("file") != res.file or (
                        res.sequence is not None
                        and (
                            entry.get("seq") != res.sequence
                            or abs(float(entry.get("duration") or 0) - (res.duration or 0))
                            > DURATION_MATCH_S
                        )
                    ):
                        raise UpstreamError(
                            f"cannot align the {track.name} playlist with the saved job "
                            f"(at {res.file})"
                        )
                    size = entry.get("size")
                    path = self.workdir / res.file
                    if entry.get("done") and isinstance(size, int) and _size(path) == size:
                        res.done, res.size = True, size


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


# --- fetching --------------------------------------------------------------------------


class Fetcher:
    def __init__(
        self,
        upstream: Upstream,
        job: Job,
        progress: Progress | None = None,
        recapture: Recapture | None = None,
    ) -> None:
        self.upstream = upstream
        self.job = job
        self.progress = progress
        self.recapture = recapture
        self.refreshes = 0
        self.total = sum(len(t.resources()) for t in job.tracks)
        self.completed = sum(r.done for t in job.tracks for r in t.resources())

    async def run(self) -> None:
        for track in self.job.tracks:
            track.ready.set()
        try:
            async with asyncio.TaskGroup() as group:
                for track in self.job.tracks:
                    for resource in track.resources():
                        if not resource.done:
                            group.create_task(self._fetch(track, resource))
        except BaseExceptionGroup as group_error:
            raise _first_error(group_error) from None

    async def _fetch(self, track: TrackJob, resource: Resource) -> None:
        while True:
            await track.ready.wait()
            generation = track.generation
            try:
                response = await self.upstream.get(
                    resource.url, referrer=track.source.playlist.url, byterange=resource.byterange
                )
            except CredentialsExpired as exc:
                log.info("%s: %s; refreshing the %s playlist", resource.file, exc, track.name)
                await self._refresh(track, generation)
                continue
            break
        path = self.job.workdir / resource.file
        tmp = path.with_name(path.name + ".part")
        _write_private(tmp, response.body)
        os.replace(tmp, path)
        resource.size = len(response.body)
        resource.done = True
        self.job.save()
        self.completed += 1
        if self.progress is not None:
            self.progress(self.completed, self.total)

    async def _refresh(self, track: TrackJob, generation: int) -> None:
        """Re-request the track's media playlist and align it (SDD §3.5 item 4)."""
        async with track.lock:
            if track.generation != generation:
                return  # another request already refreshed this track
            track.ready.clear()
            try:
                if self.refreshes >= MAX_REFRESHES:
                    raise UpstreamError(
                        f"credentials expired again after {MAX_REFRESHES} refreshes"
                    )
                self.refreshes += 1
                try:
                    playlist = await fetch_media_playlist(
                        self.upstream, track.source.url, track.source.referrer
                    )
                except CredentialsExpired as exc:
                    await self._recapture(exc)
                else:
                    fresh = SourceTrack(
                        track.name, track.source.url, track.source.referrer, playlist
                    )
                    align(track, build_track(fresh))
                    track.generation += 1
                log.info(
                    "refreshed the %s playlist (%d/%d)", track.name, self.refreshes, MAX_REFRESHES
                )
            finally:
                track.ready.set()

    async def _recapture(self, cause: CredentialsExpired) -> None:
        """Capture the page again in the same browser session and realign every track."""
        if self.recapture is None:
            raise UpstreamError(
                f"credentials expired and re-requesting the playlist failed ({cause}); "
                "pass the page URL instead of --playlist so the page can be captured again"
            )
        log.info("re-capturing the page for fresh credentials")
        try:
            url = await self.recapture()
            source = await resolve_source(self.upstream, url, self.job.source.quality)
        except HarvesterError as exc:
            raise UpstreamError(
                f"credentials expired and capturing the page again failed: {exc}"
            ) from None
        fresh = {t.name: t for t in source.tracks}
        for track in self.job.tracks:
            if track.name not in fresh:
                raise UpstreamError(f"the page no longer offers the {track.name} rendition")
            align(track, build_track(fresh[track.name]))
            track.generation += 1


def _first_error(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """The most meaningful error of a TaskGroup failure."""
    flat: list[BaseException] = []

    def walk(error: BaseException) -> None:
        if isinstance(error, BaseExceptionGroup):
            for inner in error.exceptions:
                walk(inner)
        else:
            flat.append(error)

    walk(group)
    harvester = [e for e in flat if isinstance(e, HarvesterError)]
    return harvester[0] if harvester else flat[0]


# --- orchestration ---------------------------------------------------------------------


def choose_output(output: Path | None, output_dir: Path, name: str) -> Path:
    """The final output path (SDD §3.2 item 7): never overwrites an existing file."""
    if output is None:
        directory = output_dir.expanduser()
        if not directory.is_dir():
            raise UsageError(f"output directory {directory} does not exist")
        return unique_path(directory.resolve(), name, ".mp4")
    output = output.expanduser()
    if output.is_dir():
        return unique_path(output.resolve(), name, ".mp4")
    if output.suffix == "":
        output = output.with_name(output.name + ".mp4")
    if output.suffix.lower() not in CONTAINERS:
        raise UsageError(
            f"unsupported output container {output.suffix!r}; use one of "
            + ", ".join(sorted(CONTAINERS))
        )
    if output.exists():
        raise UsageError(f"{output} already exists")
    if not output.parent.is_dir():
        raise UsageError(f"directory {output.parent} does not exist")
    return output.resolve()


def work_dir_for(final: Path) -> Path:
    return final.parent / f".{final.stem}.hlsh"


def temp_output_for(final: Path) -> Path:
    return final.parent / f".{final.stem}.part{final.suffix}"


def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


async def download(
    url: str,
    *,
    upstream: Upstream,
    output_dir: Path,
    output: Path | None = None,
    name: str | None = None,
    quality: str | None = None,
    resume: bool = False,
    reencode: bool = False,
    confirm_restart: ConfirmRestart | None = None,
    progress: Progress | None = None,
    recapture: Recapture | None = None,
) -> Path:
    """Download ``url`` (a master or media playlist URL) and return the verified file."""
    source = await resolve_source(upstream, url, quality)
    stem = sanitize_filename(name) if name else name_from_url(url)
    final = choose_output(output, output_dir, stem)
    workdir = work_dir_for(final)

    if workdir.exists() and not resume:
        choice = confirm_restart(workdir) if confirm_restart is not None else "abort"
        if choice == "resume":
            resume = True
        elif choice == "restart":
            shutil.rmtree(workdir)
        else:
            raise UsageError(
                f"an unfinished download exists in {workdir}; "
                "use --resume to continue it, or remove the directory to start over"
            )
    if resume and not (workdir / "job.json").exists():
        log.warning("nothing to resume in %s; starting a new download", workdir)
        resume = False

    workdir.mkdir(mode=0o700, exist_ok=True)
    tracks = [build_track(t) for t in source.tracks]
    for track in tracks:
        (workdir / track.name).mkdir(mode=0o700, exist_ok=True)
    job = Job(workdir, source, tracks)
    if resume:
        job.restore()
        pending = sum(not r.done for t in tracks for r in t.resources())
        log.info(
            "resuming: %d of %d resources left", pending, sum(len(t.resources()) for t in tracks)
        )
    job.save()

    await Fetcher(upstream, job, progress, recapture).run()

    tmp = temp_output_for(final)
    tmp.unlink(missing_ok=True)
    _write_private(tmp, b"")  # FFmpeg keeps the 0600 mode of an existing file
    try:
        expected = await remux([t.local_track() for t in tracks], workdir, tmp, reencode=reencode)
        await verify_output(tmp, expected, tracks[0].source.playlist.duration)
        os.chmod(tmp, 0o666 & ~_current_umask())
        os.replace(tmp, final)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    shutil.rmtree(workdir, ignore_errors=True)
    return final
