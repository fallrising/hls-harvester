"""Tests for output file naming (SDD §3.2 item 7, §3.9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hls_harvester.filenames import name_from_url, sanitize_filename, unique_path


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("My Video: Part 1/2", "My Video Part 1 2"),
        ("  spaced\tout\n title  ", "spaced out title"),
        ('a<b>c"d|e?f*g\\h', "a b c d e f g h"),
        ("..hidden..", "hidden"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("", "video"),
        ("///", "video"),
        ("bidi‮override", "bidi override"),
    ],
)
def test_sanitize(title: str, expected: str) -> None:
    assert sanitize_filename(title) == expected


def test_sanitize_limits_bytes_without_breaking_characters() -> None:
    name = sanitize_filename("影" * 200, max_bytes=100)
    assert len(name.encode()) <= 100
    assert set(name) == {"影"}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://cdn.example/shows/episode-12/index.m3u8?token=x", "episode-12"),
        ("https://cdn.example/v/My%20Clip.m3u8", "My Clip"),
        ("https://cdn.example/hls/master.m3u8", "video"),
        ("https://cdn.example/", "video"),
    ],
)
def test_name_from_url(url: str, expected: str) -> None:
    assert name_from_url(url) == expected


def test_unique_path(tmp_path: Path) -> None:
    assert unique_path(tmp_path, "a", ".mp4") == tmp_path / "a.mp4"
    (tmp_path / "a.mp4").touch()
    (tmp_path / "a (1).mp4").touch()
    assert unique_path(tmp_path, "a", ".mp4") == tmp_path / "a (2).mp4"
