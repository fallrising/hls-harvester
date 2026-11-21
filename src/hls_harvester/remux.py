"""FFmpeg remux, period handling and output verification (SDD §3.5 "重新封裝").

The downloader hands over local files only; FFmpeg never sees an upstream URL. For each
track a local playlist is written per discontinuity period. One period is remuxed
straight to the output. Several periods are compared with ffprobe (codec, resolution,
sample rate, channels): identical periods are remuxed one by one and joined with the
concat demuxer (``-c copy``); differing periods fail with exit code 7 unless
``--reencode`` is given, in which case every period is re-encoded to the parameters of
the main (longest) period and then joined. FFmpeg runs as an argument list, never
through a shell, and its stderr is redacted before it is logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from hls_harvester.errors import VerifyError
from hls_harvester.redact import redact_text

log = logging.getLogger(__name__)

HLS_INPUT_OPTIONS: Final = ("-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto")
DURATION_TOLERANCE_S: Final = 0.5
DURATION_TOLERANCE_RATIO: Final = 0.005


@dataclass(frozen=True)
class LocalSegment:
    """A downloaded segment; paths are relative to the work directory."""

    path: str
    duration: float
    init: str | None = None
    key: str | None = None
    iv: bytes | None = None


@dataclass(frozen=True)
class LocalTrack:
    name: str
    periods: tuple[tuple[LocalSegment, ...], ...]


@dataclass(frozen=True)
class StreamInfo:
    kind: str
    codec: str
    width: int | None = None
    height: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    frame_rate: str | None = None

    def signature(self) -> tuple[object, ...]:
        """The parameters periods must share (SDD §3.5)."""
        if self.kind == "video":
            return (self.kind, self.codec, self.width, self.height)
        return (self.kind, self.codec, self.sample_rate, self.channels)

    def describe(self) -> str:
        if self.kind == "video":
            return f"video {self.codec} {self.width}x{self.height}"
        return f"audio {self.codec} {self.sample_rate} Hz {self.channels} ch"


@dataclass(frozen=True)
class ProbeResult:
    streams: tuple[StreamInfo, ...]
    duration: float | None

    @property
    def kinds(self) -> set[str]:
        return {s.kind for s in self.streams}

    def signature(self) -> tuple[tuple[object, ...], ...]:
        return tuple(sorted((s.signature() for s in self.streams), key=repr))

    def first(self, kind: str) -> StreamInfo | None:
        return next((s for s in self.streams if s.kind == kind), None)


def duration_tolerance(expected: float) -> float:
    """Allowed output duration error: 0.5 s or 0.5 %, whichever is larger (SDD §3.5)."""
    return max(DURATION_TOLERANCE_S, DURATION_TOLERANCE_RATIO * expected)


# --- local playlists -------------------------------------------------------------------


def write_local_playlist(path: Path, segments: Sequence[LocalSegment]) -> None:
    """Write a VOD playlist that refers only to local files next to ``path``."""
    target = max(1, math.ceil(max((s.duration for s in segments), default=1)))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    current_init: str | None = None
    current_key: tuple[str | None, bytes | None] = (None, None)
    for segment in segments:
        if segment.init is not None and segment.init != current_init:
            lines.append(f'#EXT-X-MAP:URI="{segment.init}"')
            current_init = segment.init
        key = (segment.key, segment.iv)
        if key != current_key:
            if segment.key is None:
                lines.append("#EXT-X-KEY:METHOD=NONE")
            else:
                iv = f",IV=0x{segment.iv.hex()}" if segment.iv is not None else ""
                lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{segment.key}"{iv}')
            current_key = key
        lines.append(f"#EXTINF:{segment.duration:.6f},")
        lines.append(segment.path)
    lines.append("#EXT-X-ENDLIST")
    path.write_text("\n".join(lines) + "\n")


# --- running FFmpeg --------------------------------------------------------------------


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise VerifyError(f"{name} not found on PATH; run `hls-harvester doctor`")
    return path


async def _run(argv: Sequence[str], *, cwd: Path | None = None) -> tuple[int, bytes, str]:
    log.debug("running: %s", " ".join(argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    stderr = redact_text(err.decode("utf-8", errors="replace"))
    if stderr.strip():
        log.debug("%s stderr:\n%s", Path(argv[0]).name, stderr.strip())
    return proc.returncode or 0, out, stderr


async def run_ffmpeg(args: Sequence[str], *, cwd: Path | None = None) -> None:
    argv = [_tool("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args]
    code, _, stderr = await _run(argv, cwd=cwd)
    if code != 0:
        tail = "\n".join(stderr.strip().splitlines()[-10:])
        raise VerifyError(f"ffmpeg failed with exit code {code}: {tail}")


async def ffprobe(path: Path) -> ProbeResult:
    """Probe a local file or local playlist."""
    options = HLS_INPUT_OPTIONS if path.suffix == ".m3u8" else ()
    argv = [
        _tool("ffprobe"),
        "-v",
        "error",
        *options,
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,sample_rate,channels,avg_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    code, out, stderr = await _run(argv)
    if code != 0:
        raise VerifyError(f"ffprobe could not read {path.name}: {stderr.strip()[-500:]}")
    data: dict[str, Any] = json.loads(out or b"{}")
    streams = []
    for raw in data.get("streams", []):
        kind = raw.get("codec_type")
        if kind not in {"video", "audio"}:
            continue
        streams.append(
            StreamInfo(
                kind=kind,
                codec=str(raw.get("codec_name", "?")),
                width=raw.get("width"),
                height=raw.get("height"),
                sample_rate=int(raw["sample_rate"]) if raw.get("sample_rate") else None,
                channels=raw.get("channels"),
                frame_rate=raw.get("avg_frame_rate"),
            )
        )
    duration_text = data.get("format", {}).get("duration")
    duration = float(duration_text) if duration_text not in (None, "N/A") else None
    return ProbeResult(streams=tuple(streams), duration=duration)


# --- remux -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _PeriodInputs:
    index: int
    playlists: tuple[Path, ...]
    duration: float


def _input_args(playlists: Sequence[Path]) -> list[str]:
    args: list[str] = []
    for playlist in playlists:
        args += [*HLS_INPUT_OPTIONS, "-i", str(playlist)]
    if len(playlists) == 1:
        args += ["-map", "0:v?", "-map", "0:a?"]
    else:  # separate audio rendition: video from the variant, audio from the rendition
        args += ["-map", "0:v?", "-map", "1:a:0"]
    return args


async def _probe_period(period: _PeriodInputs) -> ProbeResult:
    probes = [await ffprobe(p) for p in period.playlists]
    if len(probes) == 1:
        return probes[0]
    video = [s for s in probes[0].streams if s.kind == "video"]
    audio = [s for s in probes[1].streams if s.kind == "audio"][:1]
    return ProbeResult(streams=(*video, *audio), duration=probes[0].duration)


def _encode_args(reference: ProbeResult) -> list[str]:
    args: list[str] = []
    video = reference.first("video")
    if video is not None:
        if video.codec != "h264":
            raise VerifyError(f"--reencode supports H.264 video only, not {video.codec}")
        width, height = video.width, video.height
        args += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
        ]
        if video.frame_rate and video.frame_rate not in {"0/0", "0"}:
            args += ["-r", video.frame_rate]
    audio = reference.first("audio")
    if audio is not None:
        if audio.codec != "aac":
            raise VerifyError(f"--reencode supports AAC audio only, not {audio.codec}")
        args += ["-c:a", "aac", "-b:a", "128k"]
        if audio.sample_rate:
            args += ["-ar", str(audio.sample_rate)]
        if audio.channels:
            args += ["-ac", str(audio.channels)]
    return args


async def remux(
    tracks: Sequence[LocalTrack],
    workdir: Path,
    output: Path,
    *,
    reencode: bool = False,
) -> set[str]:
    """Remux ``tracks`` (main track first) into ``output``; return expected stream kinds."""
    main = tracks[0]
    if any(len(t.periods) != len(main.periods) for t in tracks[1:]):
        raise VerifyError("audio and video renditions have a different number of periods")

    periods: list[_PeriodInputs] = []
    for index in range(len(main.periods)):
        playlists = []
        for track in tracks:
            path = workdir / f"{track.name}_p{index}.m3u8"
            write_local_playlist(path, track.periods[index])
            playlists.append(path)
        duration = sum(s.duration for s in main.periods[index])
        periods.append(_PeriodInputs(index, tuple(playlists), duration))

    probes = [await _probe_period(p) for p in periods]
    expected = set().union(*(p.kinds for p in probes))
    if not expected:
        raise VerifyError("the downloaded media has no audio or video stream")

    if len(periods) == 1:
        await run_ffmpeg(
            [
                *_input_args(periods[0].playlists),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        return expected

    signatures = {p.signature() for p in probes}
    encode: list[str] | None = None
    if len(signatures) > 1:
        if not reencode:
            details = "; ".join(
                f"period {i + 1}: " + ", ".join(s.describe() for s in p.streams)
                for i, p in enumerate(probes)
            )
            raise VerifyError(f"periods have different parameters ({details}); use --reencode")
        main_index = max(range(len(periods)), key=lambda i: (periods[i].duration, -i))
        encode = _encode_args(probes[main_index])
        log.info("re-encoding %d periods to match period %d", len(periods), main_index + 1)

    parts: list[str] = []
    for period in periods:
        part = f"period_{period.index}.mp4"
        codec_args = encode if encode is not None else ["-c", "copy"]
        await run_ffmpeg([*_input_args(period.playlists), *codec_args, str(workdir / part)])
        parts.append(part)
    concat_list = workdir / "concat.txt"
    concat_list.write_text("".join(f"file '{part}'\n" for part in parts))
    await run_ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-map",
            "0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        cwd=workdir,
    )
    return expected


async def verify_output(path: Path, expected_kinds: set[str], expected_duration: float) -> None:
    """ffprobe must find the expected stream kinds and a duration within tolerance."""
    probe = await ffprobe(path)
    missing = expected_kinds - probe.kinds
    if missing:
        raise VerifyError(f"output is missing {', '.join(sorted(missing))} stream(s)")
    if probe.duration is None:
        raise VerifyError("output has no duration")
    tolerance = duration_tolerance(expected_duration)
    if abs(probe.duration - expected_duration) > tolerance:
        raise VerifyError(
            f"output duration {probe.duration:.3f}s differs from the playlist's "
            f"{expected_duration:.3f}s by more than {tolerance:.3f}s"
        )
