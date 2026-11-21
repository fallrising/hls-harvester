"""Synthetic HLS samples generated with FFmpeg at test time (SDD §7, §8 M0).

All media comes from lavfi ``testsrc`` video and ``sine`` audio, so nothing copyrighted
or external is needed. :func:`generate_samples` writes every sample below one root
directory, which the fixture server exposes under ``/media/``.

Samples (name -> playlist path relative to the root):

``vod_fmp4``
    2-second VOD, fMP4 (``EXT-X-MAP`` init segment), muxed H.264 + AAC, 1 s segments.
``master``
    Master playlist with three video-only variants (160x90, 320x180, 640x360) and one
    separate audio rendition (``EXT-X-MEDIA TYPE=AUDIO``), all fMP4.
``aes128``
    2-second MPEG-TS VOD encrypted with AES-128; key URI ``key.bin`` next to the playlist.
``byterange``
    2-second MPEG-TS VOD stored as a single file addressed with ``EXT-X-BYTERANGE``.
``discontinuity``
    Three 2-second MPEG-TS periods with identical encoding parameters separated by
    ``EXT-X-DISCONTINUITY`` (simulated ad insertion).
``discontinuity_mismatch``
    Same as ``discontinuity`` but the middle period has a different resolution and
    sample rate (for the "periods differ" case, SDD §3.5).
``sample_aes``
    Playlist-only ``METHOD=SAMPLE-AES`` sample (FairPlay key format); no media exists.
``widevine``
    Playlist-only sample whose ``EXT-X-KEY`` uses the Widevine ``KEYFORMAT``; no media.

The two playlist-only samples exist so later milestones can test that DRM is detected
and refused (SDD §1.1); nothing here decrypts or handles DRM.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DURATION_S = 2
FRAME_RATE = 25
AES_KEY = bytes(range(16))
AES_IV = bytes(range(16, 32))

_X264 = [
    "-c:v",
    "libx264",
    "-preset",
    "ultrafast",
    "-profile:v",
    "baseline",
    "-pix_fmt",
    "yuv420p",
    "-g",
    str(FRAME_RATE),
    "-keyint_min",
    str(FRAME_RATE),
    "-sc_threshold",
    "0",
]
_AAC = ["-c:a", "aac", "-b:a", "64k", "-ac", "2"]
# Nominal codec strings for the master playlist (H.264 Constrained Baseline, AAC-LC).
_CODECS_AV = "avc1.42c01e,mp4a.40.2"


@dataclass(frozen=True)
class HlsSamples:
    """Location of the generated samples."""

    root: Path
    playlists: dict[str, str] = field(default_factory=dict)
    aes_key: bytes = AES_KEY

    def playlist(self, name: str) -> str:
        """Playlist path of sample ``name``, relative to :attr:`root`."""
        return self.playlists[name]


def _lavfi_video(source: str, size: str, duration: float) -> list[str]:
    return ["-f", "lavfi", "-i", f"{source}=size={size}:rate={FRAME_RATE}:duration={duration}"]


def _lavfi_audio(frequency: int, sample_rate: int, duration: float) -> list[str]:
    spec = f"sine=frequency={frequency}:sample_rate={sample_rate}:duration={duration}"
    return ["-f", "lavfi", "-i", spec]


def _hls(out_dir: Path, *, fmp4: bool, segment_prefix: str = "seg", extra: list[str] | None = None):
    """Common HLS muxer arguments writing ``index.m3u8`` into ``out_dir``."""
    ext = "m4s" if fmp4 else "ts"
    args = ["-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod"]
    if fmp4:
        args += ["-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4"]
    else:
        args += ["-hls_segment_type", "mpegts"]
    args += extra or []
    args += ["-hls_segment_filename", str(out_dir / f"{segment_prefix}_%03d.{ext}")]
    return [*args, str(out_dir / "index.m3u8")]


def _ffmpeg(args: list[str]) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to generate the HLS test fixtures")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr}")


def _make_vod_fmp4(root: Path) -> str:
    out = root / "vod_fmp4"
    out.mkdir()
    _ffmpeg(
        [
            *_lavfi_video("testsrc", "320x180", DURATION_S),
            *_lavfi_audio(440, 48000, DURATION_S),
            *_X264,
            *_AAC,
            *_hls(out, fmp4=True),
        ]
    )
    return "vod_fmp4/index.m3u8"


def _dir_bits(path: Path) -> int:
    return sum(p.stat().st_size for p in path.iterdir() if p.suffix in {".m4s", ".mp4"}) * 8


def _make_master(root: Path) -> str:
    out = root / "master"
    out.mkdir()
    variants = [("v0", "160x90"), ("v1", "320x180"), ("v2", "640x360")]
    for name, size in variants:
        (out / name).mkdir()
        _ffmpeg(
            [
                *_lavfi_video("testsrc", size, DURATION_S),
                "-an",
                *_X264,
                *_hls(out / name, fmp4=True),
            ]
        )
    (out / "audio").mkdir()
    _ffmpeg([*_lavfi_audio(440, 48000, DURATION_S), "-vn", *_AAC, *_hls(out / "audio", fmp4=True)])

    audio_bps = _dir_bits(out / "audio") // DURATION_S
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Sine",LANGUAGE="en",'
        'DEFAULT=YES,AUTOSELECT=YES,CHANNELS="2",URI="audio/index.m3u8"',
    ]
    for name, size in variants:
        bandwidth = _dir_bits(out / name) // DURATION_S + audio_bps
        lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={size},"
            f'CODECS="{_CODECS_AV}",FRAME-RATE={FRAME_RATE}.000,AUDIO="aud"'
        )
        lines.append(f"{name}/index.m3u8")
    (out / "master.m3u8").write_text("\n".join(lines) + "\n")
    return "master/master.m3u8"


def _make_aes128(root: Path, work: Path) -> str:
    out = root / "aes128"
    out.mkdir()
    key_path = out / "key.bin"
    key_path.write_bytes(AES_KEY)
    # The key-info file holds a local path, so it lives outside the served tree.
    key_info = work / "aes128.keyinfo"
    key_info.write_text(f"key.bin\n{key_path}\n{AES_IV.hex()}\n")
    _ffmpeg(
        [
            *_lavfi_video("testsrc", "320x180", DURATION_S),
            *_lavfi_audio(440, 48000, DURATION_S),
            *_X264,
            *_AAC,
            *_hls(out, fmp4=False, extra=["-hls_key_info_file", str(key_info)]),
        ]
    )
    return "aes128/index.m3u8"


def _make_byterange(root: Path) -> str:
    out = root / "byterange"
    out.mkdir()
    _ffmpeg(
        [
            *_lavfi_video("testsrc", "320x180", DURATION_S),
            *_lavfi_audio(440, 48000, DURATION_S),
            *_X264,
            *_AAC,
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_playlist_type",
            "vod",
            "-hls_segment_type",
            "mpegts",
            "-hls_flags",
            "single_file",
            "-hls_segment_filename",
            str(out / "media.ts"),
            str(out / "index.m3u8"),
        ]
    )
    return "byterange/index.m3u8"


def _media_lines(playlist: Path) -> tuple[int, list[str]]:
    """Return (target duration, segment lines) of a generated VOD media playlist."""
    target = 1
    body: list[str] = []
    for line in playlist.read_text().splitlines():
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target = int(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:") or (line and not line.startswith("#")):
            body.append(line)
    return target, body


def _make_discontinuity(root: Path, name: str, periods: list[tuple[str, str, int, int]]) -> str:
    """Generate one MPEG-TS period per entry (source, size, sine Hz, sample rate)."""
    out = root / name
    out.mkdir()
    target = 1
    lines: list[str] = []
    for index, (source, size, frequency, rate) in enumerate(periods):
        period_dir = out / f"p{index}"
        period_dir.mkdir()
        _ffmpeg(
            [
                *_lavfi_video(source, size, DURATION_S),
                *_lavfi_audio(frequency, rate, DURATION_S),
                *_X264,
                *_AAC,
                *_hls(period_dir, fmp4=False),
            ]
        )
        period_target, body = _media_lines(period_dir / "index.m3u8")
        target = max(target, period_target)
        if index:
            lines.append("#EXT-X-DISCONTINUITY")
        lines += [line if line.startswith("#") else f"p{index}/{line}" for line in body]
    header = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    (out / "index.m3u8").write_text("\n".join([*header, *lines, "#EXT-X-ENDLIST"]) + "\n")
    return f"{name}/index.m3u8"


def _write_key_only_playlist(root: Path, name: str, key_tag: str) -> str:
    out = root / name
    out.mkdir()
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:5",
        "#EXT-X-TARGETDURATION:1",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        key_tag,
        "#EXTINF:1.000000,",
        "seg_000.ts",
        "#EXTINF:1.000000,",
        "seg_001.ts",
        "#EXT-X-ENDLIST",
    ]
    (out / "index.m3u8").write_text("\n".join(lines) + "\n")
    return f"{name}/index.m3u8"


def generate_samples(root: Path, work: Path) -> HlsSamples:
    """Generate every sample under ``root``; ``work`` holds files that must not be served."""
    root.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    same = ("testsrc", "640x360", 440, 48000)
    playlists = {
        "vod_fmp4": _make_vod_fmp4(root),
        "master": _make_master(root),
        "aes128": _make_aes128(root, work),
        "byterange": _make_byterange(root),
        "discontinuity": _make_discontinuity(
            root,
            "discontinuity",
            [same, ("testsrc2", "640x360", 660, 48000), ("rgbtestsrc", "640x360", 880, 48000)],
        ),
        "discontinuity_mismatch": _make_discontinuity(
            root,
            "discontinuity_mismatch",
            [same, ("testsrc2", "320x180", 660, 44100), same],
        ),
        "sample_aes": _write_key_only_playlist(
            root,
            "sample_aes",
            '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://fixture-key",'
            'KEYFORMAT="com.apple.streamingkeydelivery",KEYFORMATVERSIONS="1"',
        ),
        "widevine": _write_key_only_playlist(
            root,
            "widevine",
            '#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,URI="data:text/plain;base64,AAAA",'
            'KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",KEYFORMATVERSIONS="1"',
        ),
    }
    return HlsSamples(root=root, playlists=playlists)
