"""Tests for the doctor checks (SDD §3.1, §3.9)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from hls_harvester import doctor


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023", (6, 1)),
        ("ffmpeg version 7.1 Copyright (c) 2000-2024 the FFmpeg developers", (7, 1)),
        ("ffprobe version n6.0 Copyright", (6, 0)),
        ("ffmpeg version 5.1.4-0+deb12u1 Copyright", (5, 1)),
        ("ffmpeg version N-112345-gabcdef Copyright", None),
        ("", None),
    ],
)
def test_parse_ffmpeg_version(output: str, expected: tuple[int, int] | None) -> None:
    assert doctor.parse_ffmpeg_version(output) == expected


def fake_run(output: str) -> doctor.Runner:
    def run(argv: Sequence[str]) -> str:
        return output

    return run


def test_ffmpeg_ok() -> None:
    result = doctor.check_ffmpeg_tool(
        "ffmpeg", which=lambda _: "/usr/bin/ffmpeg", run=fake_run("ffmpeg version 6.0 x")
    )
    assert result.ok
    assert "6.0" in result.detail


def test_ffmpeg_too_old() -> None:
    result = doctor.check_ffmpeg_tool(
        "ffmpeg", which=lambda _: "/usr/bin/ffmpeg", run=fake_run("ffmpeg version 5.1.2 x")
    )
    assert not result.ok
    assert "too old" in result.detail
    assert result.hint


def test_ffmpeg_missing() -> None:
    result = doctor.check_ffmpeg_tool("ffprobe", which=lambda _: None)
    assert not result.ok
    assert result.detail == "not found on PATH"


def test_ffmpeg_unparseable_version() -> None:
    result = doctor.check_ffmpeg_tool(
        "ffmpeg", which=lambda _: "/x/ffmpeg", run=fake_run("something else")
    )
    assert not result.ok
    assert "could not determine" in result.detail


def test_ffmpeg_cannot_run() -> None:
    def broken(argv: Sequence[str]) -> str:
        raise subprocess.TimeoutExpired(list(argv), 30)

    result = doctor.check_ffmpeg_tool("ffmpeg", which=lambda _: "/x/ffmpeg", run=broken)
    assert not result.ok


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.parametrize("tool", ["ffmpeg", "ffprobe"])
def test_real_ffmpeg_tools(tool: str) -> None:
    # CI installs FFmpeg >= 6, so the real check must pass there.
    result = doctor.check_ffmpeg_tool(tool)
    assert result.ok, result.detail


def test_python_version() -> None:
    assert doctor.check_python((3, 11, 0)).ok
    assert doctor.check_python((3, 12, 4)).ok
    old = doctor.check_python((3, 10, 14))
    assert not old.ok
    assert "3.11" in old.hint


def test_playwright_chromium_present(tmp_path: Path) -> None:
    exe = tmp_path / "chrome"
    exe.write_text("")
    assert doctor.check_playwright_chromium(lambda: str(exe)).ok


def test_playwright_chromium_missing(tmp_path: Path) -> None:
    result = doctor.check_playwright_chromium(lambda: str(tmp_path / "missing"))
    assert not result.ok
    assert "playwright install chromium" in result.hint


def test_playwright_package_missing() -> None:
    def locate() -> str:
        raise ImportError("no playwright")

    result = doctor.check_playwright_chromium(locate)
    assert not result.ok
    assert "not installed" in result.detail


def test_playwright_driver_error() -> None:
    def locate() -> str:
        raise RuntimeError("driver crashed")

    assert not doctor.check_playwright_chromium(locate).ok


def test_config_check(tmp_path: Path) -> None:
    assert doctor.check_config(tmp_path / "none.toml").ok
    good = tmp_path / "good.toml"
    good.write_text("[defaults]\nheadless = true\n")
    assert doctor.check_config(good).ok
    bad = tmp_path / "bad.toml"
    bad.write_text("[defaults]\nheadless = 1\n")
    result = doctor.check_config(bad)
    assert not result.ok
    assert "headless" in result.detail


def test_run_checks_covers_everything(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        doctor,
        "check_playwright_chromium",
        lambda: doctor.CheckResult("playwright", False, "stub"),
    )
    names = [r.name for r in doctor.run_checks(tmp_path / "none.toml")]
    assert names == ["python", "ffmpeg", "ffprobe", "playwright", "config"]
