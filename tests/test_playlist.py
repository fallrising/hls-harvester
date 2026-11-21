"""Tests for playlist parsing, URI resolution and quality selection (SDD §3.4)."""

from __future__ import annotations

import pytest

from hls_harvester.errors import ExitCode, UnsupportedContent, UsageError
from hls_harvester.playlist import (
    ByteRange,
    MasterPlaylist,
    MediaPlaylist,
    PlaylistParseError,
    parse_attributes,
    parse_playlist,
    quality_labels,
    select_variant,
)
from tests.fixtures.media import HlsSamples

BASE = "https://cdn.example.com/v/abc/index.m3u8?token=t"


def media(body: str, url: str = BASE) -> MediaPlaylist:
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n" + body
    result = parse_playlist(text, url)
    assert isinstance(result, MediaPlaylist)
    return result


def test_attributes() -> None:
    attrs = parse_attributes('BANDWIDTH=1280000,CODECS="avc1.4d401f,mp4a.40.2",RESOLUTION=640x360')
    assert attrs == {
        "BANDWIDTH": "1280000",
        "CODECS": "avc1.4d401f,mp4a.40.2",
        "RESOLUTION": "640x360",
    }


def test_relative_uris_resolve_against_final_url() -> None:
    playlist = media(
        "#EXTINF:4,\nseg1.ts\n#EXTINF:4,\n../other/seg2.ts?x=1\n"
        "#EXTINF:4,\n/root/seg3.ts\n#EXTINF:4,\nhttps://cdn2.example.com/seg4.ts\n#EXT-X-ENDLIST\n"
    )
    assert [s.uri for s in playlist.segments] == [
        "https://cdn.example.com/v/abc/seg1.ts",
        "https://cdn.example.com/v/other/seg2.ts?x=1",
        "https://cdn.example.com/root/seg3.ts",
        "https://cdn2.example.com/seg4.ts",
    ]
    assert playlist.endlist
    assert playlist.duration == 16


def test_media_sequence_numbers() -> None:
    playlist = media("#EXT-X-MEDIA-SEQUENCE:100\n#EXTINF:4,\na.ts\n#EXTINF:3.5,\nb.ts\n")
    assert [(s.sequence, s.duration) for s in playlist.segments] == [(100, 4.0), (101, 3.5)]
    assert not playlist.endlist


def test_byteranges_continue_from_previous() -> None:
    playlist = media(
        "#EXTINF:4,\n#EXT-X-BYTERANGE:100@0\nall.ts\n"
        "#EXTINF:4,\n#EXT-X-BYTERANGE:50\nall.ts\n"
        "#EXTINF:4,\n#EXT-X-BYTERANGE:10@500\nall.ts\n"
    )
    assert [s.byterange for s in playlist.segments] == [
        ByteRange(100, 0),
        ByteRange(50, 100),
        ByteRange(10, 500),
    ]
    assert playlist.segments[1].byterange is not None
    assert playlist.segments[1].byterange.header() == "bytes=100-149"


def test_byterange_without_offset_needs_previous() -> None:
    with pytest.raises(PlaylistParseError):
        media("#EXTINF:4,\n#EXT-X-BYTERANGE:50\nall.ts\n")


def test_map_and_key_apply_to_following_segments() -> None:
    playlist = media(
        '#EXT-X-MAP:URI="init.mp4",BYTERANGE="720@0"\n'
        '#EXT-X-KEY:METHOD=AES-128,URI="https://keys.example.com/k?id=1"\n'
        "#EXT-X-MEDIA-SEQUENCE:7\n#EXTINF:4,\na.m4s\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="k2",IV=0x0102\n#EXTINF:4,\nb.m4s\n'
        "#EXT-X-KEY:METHOD=NONE\n#EXTINF:4,\nc.m4s\n"
    )
    a, b, c = playlist.segments
    assert a.init is not None
    assert a.init.uri == "https://cdn.example.com/v/abc/init.mp4"
    assert a.init.byterange == ByteRange(720, 0)
    assert a.key is not None
    assert a.key.uri == "https://keys.example.com/k?id=1"
    assert a.iv() == (7).to_bytes(16, "big")
    assert b.key is not None
    assert b.key.uri == "https://cdn.example.com/v/abc/k2"
    assert b.iv() == bytes(14) + b"\x01\x02"
    assert c.key is None
    assert c.init == a.init


def test_discontinuity_periods() -> None:
    playlist = media(
        "#EXT-X-DISCONTINUITY\n#EXTINF:2,\na.ts\n#EXTINF:2,\nb.ts\n"
        "#EXT-X-DISCONTINUITY\n#EXTINF:2,\nc.ts\n#EXT-X-DISCONTINUITY\n#EXTINF:2,\nd.ts\n"
    )
    assert [s.period for s in playlist.segments] == [0, 0, 1, 2]
    assert [len(p) for p in playlist.periods()] == [2, 1, 1]
    assert not playlist.segments[0].discontinuity


@pytest.mark.parametrize(
    ("tag", "fragment"),
    [
        (
            '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://x",KEYFORMAT="com.apple.streamingkeydelivery"',
            "METHOD=SAMPLE-AES",
        ),
        ('#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,URI="data:x"', "METHOD=SAMPLE-AES-CTR"),
        (
            '#EXT-X-KEY:METHOD=AES-128,URI="k",KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"',
            "KEYFORMAT",
        ),
        ('#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="skd://x"', "EXT-X-SESSION-KEY"),
        ('#EXT-X-DEFINE:NAME="a",VALUE="b"', "EXT-X-DEFINE"),
        ("#EXT-X-GAP", "EXT-X-GAP"),
        ("#EXT-X-I-FRAMES-ONLY", "EXT-X-I-FRAMES-ONLY"),
        ("#EXT-X-SKIP:SKIPPED-SEGMENTS=3", "EXT-X-SKIP"),
        ('#EXT-X-VENDOR-THING:URI="x"', "EXT-X-VENDOR-THING"),
    ],
)
def test_unsupported_tags(tag: str, fragment: str) -> None:
    with pytest.raises(UnsupportedContent, match=fragment) as info:
        media(f"{tag}\n#EXTINF:4,\na.ts\n#EXT-X-ENDLIST\n")
    assert info.value.exit_code == ExitCode.UNSUPPORTED


def test_identity_key_and_ignored_tags_are_fine() -> None:
    playlist = media(
        '#EXT-X-KEY:METHOD=AES-128,URI="k",KEYFORMAT="identity"\n'
        "#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00Z\n#EXT-X-CUE-OUT:30\n"
        '#EXT-X-PART:DURATION=1,URI="p.mp4"\n#EXTINF:4,\na.ts\n'
    )
    assert len(playlist.segments) == 1


@pytest.mark.parametrize(
    "text",
    [
        "",
        "<html>nope</html>",
        "#EXTM3U\n#EXTINF:4,\na.ts\n",  # no target duration
        "#EXTM3U\n#EXT-X-TARGETDURATION:4\na.ts\n",  # URI without EXTINF
        "#EXTM3U\n#EXT-X-TARGETDURATION:x\n",
        "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-KEY:METHOD=AES-128\n",
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n",  # STREAM-INF without URI
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\na.m3u8\n#EXTINF:4,\nb.ts\n",
    ],
)
def test_malformed(text: str) -> None:
    with pytest.raises(PlaylistParseError) as info:
        parse_playlist(text, BASE)
    assert info.value.exit_code == ExitCode.UPSTREAM_ERROR


MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="en",DEFAULT=NO,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="ja",DEFAULT=YES,URI="audio/ja.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="muxed",NAME="main",DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,AUDIO="aud"
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,AUDIO="aud"
720/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,AUDIO="muxed"
720b/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"
audio-only.m3u8
"""


def master() -> MasterPlaylist:
    result = parse_playlist(MASTER, "https://site.example/hls/master.m3u8")
    assert isinstance(result, MasterPlaylist)
    return result


def test_master_variants_and_renditions() -> None:
    playlist = master()
    assert [v.uri for v in playlist.variants][:2] == [
        "https://site.example/hls/360/index.m3u8",
        "https://site.example/hls/720/index.m3u8",
    ]
    assert playlist.variants[0].resolution == (640, 360)
    audio = playlist.audio_for(playlist.variants[0])
    assert audio is not None
    assert audio.name == "ja"
    assert audio.uri == "https://site.example/hls/audio/ja.m3u8"
    # A group whose renditions have no URI means audio is muxed into the variant.
    assert playlist.audio_for(playlist.variants[2]) is None
    assert playlist.audio_for(playlist.variants[3]) is None


def test_quality_labels_and_selection() -> None:
    playlist = master()
    assert [label for label, _ in quality_labels(playlist)] == [
        "360p",
        "720p@2500k",
        "720p@3000k",
        "64k",
    ]
    assert select_variant(playlist, None).bandwidth == 3_000_000
    assert select_variant(playlist, "360p").resolution == (640, 360)
    assert select_variant(playlist, "360").resolution == (640, 360)
    assert select_variant(playlist, "720P").bandwidth == 3_000_000
    assert select_variant(playlist, "720p@2500k").bandwidth == 2_500_000


def test_unknown_quality_lists_available() -> None:
    with pytest.raises(UsageError, match="360p, 720p@2500k") as info:
        select_variant(master(), "1080p")
    assert info.value.exit_code == ExitCode.USAGE


@pytest.mark.parametrize(
    "name", ["vod_fmp4", "aes128", "byterange", "discontinuity", "discontinuity_mismatch"]
)
def test_fixture_samples_parse(hls_samples: HlsSamples, name: str) -> None:
    path = hls_samples.root / hls_samples.playlist(name)
    playlist = parse_playlist(
        path.read_text(), f"http://127.0.0.1/media/{hls_samples.playlist(name)}"
    )
    assert isinstance(playlist, MediaPlaylist)
    assert playlist.endlist
    assert playlist.segments


def test_fixture_master_parses(hls_samples: HlsSamples) -> None:
    path = hls_samples.root / hls_samples.playlist("master")
    playlist = parse_playlist(path.read_text(), "http://h/media/master/master.m3u8")
    assert isinstance(playlist, MasterPlaylist)
    assert [label for label, _ in quality_labels(playlist)] == ["90p", "180p", "360p"]
    audio = playlist.audio_for(select_variant(playlist, None))
    assert audio is not None
    assert audio.uri == "http://h/media/master/audio/index.m3u8"


@pytest.mark.parametrize(("name", "tag"), [("sample_aes", "SAMPLE-AES"), ("widevine", "KEYFORMAT")])
def test_fixture_drm_samples_rejected(hls_samples: HlsSamples, name: str, tag: str) -> None:
    path = hls_samples.root / hls_samples.playlist(name)
    with pytest.raises(UnsupportedContent, match=tag):
        parse_playlist(path.read_text(), "http://h/x.m3u8")
