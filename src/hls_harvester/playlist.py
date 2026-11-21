"""Playlist handling (SDD §3.4): parsing, URI resolution, quality selection and periods.

A small strict parser for the subset of RFC 8216 the downloader and proxy need. Every
relative URI is resolved against the playlist's *final* URL (after redirects), which the
caller passes in. Content we cannot deliver completely is rejected with
:class:`~hls_harvester.errors.UnsupportedContent` naming the tag, instead of silently
producing a partial playlist:

* DRM (SDD §1.1): ``METHOD=SAMPLE-AES`` / ``SAMPLE-AES-CTR`` and any ``KEYFORMAT`` other
  than ``identity``, in ``EXT-X-KEY`` or ``EXT-X-SESSION-KEY``;
* tags that change which resources exist: ``EXT-X-DEFINE`` (variable substitution),
  ``EXT-X-SKIP`` (delta updates), ``EXT-X-GAP``, ``EXT-X-I-FRAMES-ONLY``;
* unknown tags that carry a ``URI`` attribute.

Unknown tags without a URI are ignored, as RFC 8216 §4 requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urljoin

from hls_harvester.errors import UnsupportedContent, UpstreamError, UsageError

_ATTR_RE: Final = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')

IDENTITY_KEYFORMAT: Final = "identity"

# Tags understood or deliberately ignored (they never add resources we would miss).
_IGNORED_TAGS: Final = frozenset(
    {
        "EXT-X-INDEPENDENT-SEGMENTS",
        "EXT-X-START",
        "EXT-X-PROGRAM-DATE-TIME",
        "EXT-X-DATERANGE",
        "EXT-X-BITRATE",
        "EXT-X-ALLOW-CACHE",
        "EXT-X-SERVER-CONTROL",
        "EXT-X-PART-INF",
        # Low-latency parts duplicate the full segments that a finished playlist lists.
        "EXT-X-PART",
        "EXT-X-PRELOAD-HINT",
        "EXT-X-RENDITION-REPORT",
        # Trick-play and metadata playlists are not needed to play or download the video.
        "EXT-X-I-FRAME-STREAM-INF",
        "EXT-X-SESSION-DATA",
        "EXT-X-CONTENT-STEERING",
        "EXT-X-IMAGE-STREAM-INF",
        "EXT-X-TILES",
    }
)
_UNSUPPORTED_TAGS: Final = {
    "EXT-X-DEFINE": "variable substitution is not supported",
    "EXT-X-SKIP": "delta playlist updates are not supported",
    "EXT-X-GAP": "gap segments have no media to download",
    "EXT-X-I-FRAMES-ONLY": "I-frame-only playlists are not a playable video",
}


class PlaylistParseError(UpstreamError):
    """The response is not a valid HLS playlist."""


# --- model -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ByteRange:
    length: int
    offset: int

    @property
    def last(self) -> int:
        """Inclusive last byte, as used in a ``Range`` header."""
        return self.offset + self.length - 1

    def header(self) -> str:
        return f"bytes={self.offset}-{self.last}"


@dataclass(frozen=True)
class Key:
    """An ``EXT-X-KEY``. ``method`` is ``AES-128``; DRM never reaches this type."""

    method: str
    uri: str
    iv: bytes | None


@dataclass(frozen=True)
class InitSection:
    uri: str
    byterange: ByteRange | None = None


@dataclass(frozen=True)
class Segment:
    uri: str
    duration: float
    sequence: int
    """Media sequence number (``EXT-X-MEDIA-SEQUENCE`` + index)."""
    period: int
    """Index of the discontinuity period, starting at 0."""
    discontinuity: bool = False
    byterange: ByteRange | None = None
    key: Key | None = None
    init: InitSection | None = None

    def iv(self) -> bytes | None:
        """The effective AES-128 IV: explicit, else the sequence number (RFC 8216 §5.2)."""
        if self.key is None:
            return None
        return self.key.iv if self.key.iv is not None else self.sequence.to_bytes(16, "big")


@dataclass(frozen=True)
class MediaPlaylist:
    url: str
    target_duration: float
    media_sequence: int
    segments: tuple[Segment, ...]
    endlist: bool
    playlist_type: str | None = None
    version: int | None = None

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)

    def periods(self) -> list[tuple[Segment, ...]]:
        """Segments split at ``EXT-X-DISCONTINUITY`` (SDD §3.4)."""
        result: list[list[Segment]] = []
        for segment in self.segments:
            if not result or segment.discontinuity:
                result.append([])
            result[-1].append(segment)
        return [tuple(p) for p in result]


@dataclass(frozen=True)
class Rendition:
    type: str
    group_id: str
    name: str
    uri: str | None
    language: str | None = None
    default: bool = False
    autoselect: bool = False
    channels: str | None = None


@dataclass(frozen=True)
class Variant:
    uri: str
    bandwidth: int
    average_bandwidth: int | None = None
    resolution: tuple[int, int] | None = None
    codecs: str | None = None
    frame_rate: float | None = None
    audio: str | None = None
    video: str | None = None
    subtitles: str | None = None


@dataclass(frozen=True)
class MasterPlaylist:
    url: str
    variants: tuple[Variant, ...]
    renditions: tuple[Rendition, ...] = field(default=())

    def audio_for(self, variant: Variant) -> Rendition | None:
        """The audio rendition to download with ``variant``, or None if audio is muxed.

        Prefers the group's ``DEFAULT=YES`` rendition that has a URI, then the first one
        with a URI. A group whose renditions have no URI means audio is in the variant.
        """
        if variant.audio is None:
            return None
        group = [
            r
            for r in self.renditions
            if r.type == "AUDIO" and r.group_id == variant.audio and r.uri is not None
        ]
        if not group:
            return None
        return next((r for r in group if r.default), group[0])


Playlist = MasterPlaylist | MediaPlaylist


# --- parsing ---------------------------------------------------------------------------


def parse_attributes(text: str) -> dict[str, str]:
    """Parse an RFC 8216 §4.2 attribute list; quoted values are unquoted."""
    attrs: dict[str, str] = {}
    for name, value in _ATTR_RE.findall(text):
        attrs[name] = value[1:-1] if value.startswith('"') and value.endswith('"') else value
    return attrs


def _int(value: str, tag: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise PlaylistParseError(f"invalid integer in {tag}: {value!r}") from None


def _float(value: str, tag: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise PlaylistParseError(f"invalid number in {tag}: {value!r}") from None


def _byterange(spec: str, tag: str, previous_end: int | None) -> ByteRange:
    length_text, _, offset_text = spec.partition("@")
    length = _int(length_text, tag)
    if offset_text:
        offset = _int(offset_text, tag)
    elif previous_end is not None:
        offset = previous_end
    else:
        raise PlaylistParseError(f"{tag} without an offset does not follow a previous range")
    if length <= 0 or offset < 0:
        raise PlaylistParseError(f"invalid range in {tag}: {spec!r}")
    return ByteRange(length, offset)


def check_key_attributes(tag: str, attrs: dict[str, str]) -> str:
    """Reject DRM key tags (SDD §1.1) and return the method."""
    method = attrs.get("METHOD", "")
    keyformat = attrs.get("KEYFORMAT", IDENTITY_KEYFORMAT)
    if method == "NONE":
        return method
    if method != "AES-128" or keyformat != IDENTITY_KEYFORMAT:
        described = f"#{tag}:METHOD={method}"
        if "KEYFORMAT" in attrs:
            described += f',KEYFORMAT="{keyformat}"'
        reason = (
            "DRM-protected content is not supported"
            if method.startswith("SAMPLE-AES") or keyformat != IDENTITY_KEYFORMAT
            else "unsupported encryption method"
        )
        raise UnsupportedContent(f"{described}: {reason}")
    return method


def _check_tag(name: str, value: str) -> None:
    if name in _UNSUPPORTED_TAGS:
        raise UnsupportedContent(f"#{name}: {_UNSUPPORTED_TAGS[name]}")
    if name == "EXT-X-SESSION-KEY":
        check_key_attributes(name, parse_attributes(value))
    elif name not in _IGNORED_TAGS and "URI=" in value:
        raise UnsupportedContent(f"#{name}: unknown tag referring to another resource")


def _lines(text: str) -> list[str]:
    lines = [line.strip() for line in text.lstrip("﻿").splitlines()]
    lines = [line for line in lines if line]
    if not lines or lines[0] != "#EXTM3U":
        raise PlaylistParseError("response is not an HLS playlist (no #EXTM3U header)")
    return lines


def _split_tag(line: str) -> tuple[str, str]:
    name, _, value = line[1:].partition(":")
    return name, value


def parse_playlist(text: str, url: str) -> Playlist:
    """Parse ``text`` fetched from ``url`` (the final URL after redirects)."""
    lines = _lines(text)
    tags = {_split_tag(line)[0] for line in lines if line.startswith("#EXT")}
    if "EXT-X-STREAM-INF" in tags:
        if tags & {"EXTINF", "EXT-X-TARGETDURATION"}:
            raise PlaylistParseError("playlist mixes master and media playlist tags")
        return _parse_master(lines, url)
    return _parse_media(lines, url)


def _parse_master(lines: list[str], url: str) -> MasterPlaylist:
    variants: list[Variant] = []
    renditions: list[Rendition] = []
    pending: dict[str, str] | None = None
    for line in lines[1:]:
        if not line.startswith("#"):
            if pending is None:
                raise PlaylistParseError("URI line without #EXT-X-STREAM-INF in master playlist")
            variants.append(_variant(pending, urljoin(url, line)))
            pending = None
            continue
        if not line.startswith("#EXT"):
            continue  # comment
        name, value = _split_tag(line)
        if name == "EXT-X-STREAM-INF":
            pending = parse_attributes(value)
        elif name == "EXT-X-MEDIA":
            attrs = parse_attributes(value)
            uri = attrs.get("URI")
            renditions.append(
                Rendition(
                    type=attrs.get("TYPE", ""),
                    group_id=attrs.get("GROUP-ID", ""),
                    name=attrs.get("NAME", ""),
                    uri=urljoin(url, uri) if uri else None,
                    language=attrs.get("LANGUAGE"),
                    default=attrs.get("DEFAULT") == "YES",
                    autoselect=attrs.get("AUTOSELECT") == "YES",
                    channels=attrs.get("CHANNELS"),
                )
            )
        elif name not in {"EXTM3U", "EXT-X-VERSION"}:
            _check_tag(name, value)
    if pending is not None:
        raise PlaylistParseError("#EXT-X-STREAM-INF without a URI line")
    if not variants:
        raise PlaylistParseError("master playlist has no variants")
    return MasterPlaylist(url=url, variants=tuple(variants), renditions=tuple(renditions))


def _variant(attrs: dict[str, str], uri: str) -> Variant:
    tag = "EXT-X-STREAM-INF"
    resolution = None
    if "RESOLUTION" in attrs:
        width, _, height = attrs["RESOLUTION"].lower().partition("x")
        resolution = (_int(width, tag), _int(height, tag))
    return Variant(
        uri=uri,
        bandwidth=_int(attrs.get("BANDWIDTH", "0"), tag),
        average_bandwidth=(
            _int(attrs["AVERAGE-BANDWIDTH"], tag) if "AVERAGE-BANDWIDTH" in attrs else None
        ),
        resolution=resolution,
        codecs=attrs.get("CODECS"),
        frame_rate=_float(attrs["FRAME-RATE"], tag) if "FRAME-RATE" in attrs else None,
        audio=attrs.get("AUDIO"),
        video=attrs.get("VIDEO"),
        subtitles=attrs.get("SUBTITLES"),
    )


def _parse_media(lines: list[str], url: str) -> MediaPlaylist:
    target_duration: float | None = None
    media_sequence = 0
    endlist = False
    playlist_type: str | None = None
    version: int | None = None

    segments: list[Segment] = []
    duration: float | None = None
    byterange_spec: str | None = None
    discontinuity = False
    key: Key | None = None
    init: InitSection | None = None
    range_end: dict[str, int] = {}  # resource URI -> end of its previous byte range
    period = 0

    for line in lines[1:]:
        if not line.startswith("#"):
            if duration is None:
                raise PlaylistParseError(f"segment URI without #EXTINF: {line[:64]!r}")
            uri = urljoin(url, line)
            byterange = None
            if byterange_spec is not None:
                byterange = _byterange(byterange_spec, "EXT-X-BYTERANGE", range_end.get(uri))
                range_end[uri] = byterange.offset + byterange.length
            if discontinuity and segments:
                period += 1
            segments.append(
                Segment(
                    uri=uri,
                    duration=duration,
                    sequence=media_sequence + len(segments),
                    period=period,
                    discontinuity=discontinuity and len(segments) > 0,
                    byterange=byterange,
                    key=key,
                    init=init,
                )
            )
            duration, byterange_spec, discontinuity = None, None, False
            continue
        if not line.startswith("#EXT"):
            continue  # comment
        name, value = _split_tag(line)
        if name == "EXTINF":
            duration = _float(value.split(",", 1)[0], name)
            if duration < 0:
                raise PlaylistParseError(f"negative #EXTINF duration: {value!r}")
        elif name == "EXT-X-BYTERANGE":
            byterange_spec = value
        elif name == "EXT-X-TARGETDURATION":
            target_duration = _float(value, name)
        elif name == "EXT-X-MEDIA-SEQUENCE":
            media_sequence = _int(value, name)
        elif name == "EXT-X-DISCONTINUITY-SEQUENCE":
            _int(value, name)  # validated only; periods are numbered per playlist
        elif name == "EXT-X-DISCONTINUITY":
            discontinuity = True
        elif name == "EXT-X-ENDLIST":
            endlist = True
        elif name == "EXT-X-PLAYLIST-TYPE":
            playlist_type = value.upper()
        elif name == "EXT-X-VERSION":
            version = _int(value, name)
        elif name == "EXT-X-KEY":
            key = _key(parse_attributes(value), url)
        elif name == "EXT-X-MAP":
            init = _map(parse_attributes(value), url, range_end)
        elif name in {"EXT-X-STREAM-INF", "EXT-X-MEDIA"}:
            raise PlaylistParseError(f"#{name} in a media playlist")
        elif name != "EXTM3U":
            _check_tag(name, value)

    if target_duration is None:
        raise PlaylistParseError("media playlist has no #EXT-X-TARGETDURATION")
    return MediaPlaylist(
        url=url,
        target_duration=target_duration,
        media_sequence=media_sequence,
        segments=tuple(segments),
        endlist=endlist,
        playlist_type=playlist_type,
        version=version,
    )


def _key(attrs: dict[str, str], url: str) -> Key | None:
    method = check_key_attributes("EXT-X-KEY", attrs)
    if method == "NONE":
        return None
    uri = attrs.get("URI")
    if not uri:
        raise PlaylistParseError("#EXT-X-KEY:METHOD=AES-128 without a URI")
    iv = None
    if "IV" in attrs:
        text = attrs["IV"]
        if not text.lower().startswith("0x"):
            raise PlaylistParseError(f"invalid IV in #EXT-X-KEY: {text!r}")
        try:
            iv = bytes.fromhex(text[2:].rjust(32, "0"))
        except ValueError:
            raise PlaylistParseError(f"invalid IV in #EXT-X-KEY: {text!r}") from None
        if len(iv) != 16:
            raise PlaylistParseError(f"invalid IV length in #EXT-X-KEY: {text!r}")
    return Key(method=method, uri=urljoin(url, uri), iv=iv)


def _map(attrs: dict[str, str], url: str, range_end: dict[str, int]) -> InitSection:
    uri = attrs.get("URI")
    if not uri:
        raise PlaylistParseError("#EXT-X-MAP without a URI")
    absolute = urljoin(url, uri)
    byterange = None
    if "BYTERANGE" in attrs:
        spec = attrs["BYTERANGE"]
        if "@" not in spec:
            spec += "@0"  # RFC 8216 §4.3.2.5: offset defaults to 0 for EXT-X-MAP
        byterange = _byterange(spec, "EXT-X-MAP", None)
    return InitSection(uri=absolute, byterange=byterange)


# --- quality selection -----------------------------------------------------------------


def quality_labels(master: MasterPlaylist) -> list[tuple[str, Variant]]:
    """Human-readable quality labels, e.g. ``720p`` (``720p@2500k`` if heights repeat)."""

    def base(v: Variant) -> str:
        return f"{v.resolution[1]}p" if v.resolution else f"{v.bandwidth // 1000}k"

    counts: dict[str, int] = {}
    for variant in master.variants:
        counts[base(variant)] = counts.get(base(variant), 0) + 1
    labelled = []
    for variant in master.variants:
        label = base(variant)
        if counts[label] > 1 and variant.resolution:
            label = f"{label}@{variant.bandwidth // 1000}k"
        labelled.append((label, variant))
    return labelled


def select_variant(master: MasterPlaylist, quality: str | None) -> Variant:
    """Pick the highest-bandwidth variant, or the one matching ``quality`` (SDD §3.1).

    ``quality`` matches a label from :func:`quality_labels` (case-insensitive); a bare
    height such as ``720`` also matches ``720p``. Unknown qualities raise a usage error
    (exit code 2) listing the available values.
    """
    if quality is None:
        return max(master.variants, key=lambda v: (v.bandwidth, v.resolution or (0, 0)))
    wanted = quality.strip().lower()
    labels = quality_labels(master)
    matches = [v for label, v in labels if label.lower() in {wanted, f"{wanted}p"}]
    if not matches:
        # "720p" also selects the best of several "720p@...k" variants.
        matches = [
            v for label, v in labels if label.lower().split("@")[0] in {wanted, f"{wanted}p"}
        ]
    if not matches:
        available = ", ".join(label for label, _ in labels)
        raise UsageError(f"quality {quality!r} is not available; choose one of: {available}")
    return max(matches, key=lambda v: v.bandwidth)
