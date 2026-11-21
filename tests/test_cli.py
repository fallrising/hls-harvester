"""Tests for the CLI surface (SDD §3.1) and exit codes (SDD §3.8)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hls_harvester import cli, doctor
from hls_harvester.errors import ExitCode


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the config lookup at an empty temporary directory."""
    monkeypatch.setattr(
        "hls_harvester.config.default_config_path", lambda **_: tmp_path / "config.toml"
    )
    return tmp_path / "config.toml"


def test_exit_codes_match_sdd() -> None:
    assert {code.name: int(code) for code in ExitCode} == {
        "OK": 0,
        "INTERNAL_ERROR": 1,
        "USAGE": 2,
        "AMBIGUOUS": 3,
        "CAPTURE_FAILED": 4,
        "UPSTREAM_ERROR": 5,
        "UNSUPPORTED": 6,
        "VERIFY_FAILED": 7,
        "INTERRUPTED": 130,
    }


def test_get_parses_all_flags() -> None:
    args = cli.build_parser().parse_args(
        [
            "get",
            "https://example.com/watch?v=1",
            "--play",
            "--quality",
            "720p",
            "--output",
            "out.mp4",
            "--storage-state",
            "state.json",
            "--headed",
            "--resume",
            "--non-interactive",
            "--reencode",
            "-vv",
            "--log-json",
        ]
    )
    assert args.command == "get"
    assert args.mode == "play"
    assert args.quality == "720p"
    assert args.output == Path("out.mp4")
    assert args.storage_state == Path("state.json")
    assert args.headed
    assert args.resume
    assert args.non_interactive
    assert args.reencode
    assert args.verbose == 2
    assert args.log_json is True


def test_get_defaults_to_download() -> None:
    args = cli.build_parser().parse_args(["get", "https://example.com/v"])
    assert args.mode == "download"
    assert args.verbose == 0
    assert args.log_json is False


def test_logging_flags_before_subcommand() -> None:
    args = cli.build_parser().parse_args(["-v", "--log-json", "doctor"])
    assert args.verbose == 1
    assert args.log_json is True


@pytest.mark.parametrize(
    "argv",
    [
        ["get"],
        ["get", "https://example.com", "--play", "--download"],
        ["get", "ftp://example.com/x"],
        ["get", "not a url"],
        ["login", "https://example.com"],
        ["frobnicate"],
        [],
    ],
)
def test_usage_errors_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == ExitCode.USAGE


@pytest.mark.parametrize(
    "argv",
    [
        ["get", "https://example.com/v"],
        ["get", "https://example.com/v", "--play"],
        ["login", "https://example.com", "--storage-state", "s.json"],
    ],
)
def test_unimplemented_commands(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(argv) == ExitCode.INTERNAL_ERROR
    assert "not implemented" in capsys.readouterr().err


def test_get_with_invalid_config_is_usage_error(
    isolated_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    isolated_config.write_text("[defaults]\nheadless = 'nope'\n")
    assert cli.main(["get", "https://example.com/v?token=SECRET"]) == ExitCode.USAGE
    err = capsys.readouterr().err
    assert "headless" in err
    assert "SECRET" not in err


def test_doctor_all_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        doctor,
        "run_checks",
        lambda: [doctor.CheckResult("python", True, "Python 3.11.9")],
    )
    assert cli.main(["doctor"]) == ExitCode.OK
    assert "all checks passed" in capsys.readouterr().out


def test_doctor_lists_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        doctor,
        "run_checks",
        lambda: [
            doctor.CheckResult("python", True, "Python 3.11.9"),
            doctor.CheckResult("ffmpeg", False, "not found on PATH", "install FFmpeg"),
            doctor.CheckResult("playwright", False, "Chromium is not installed", "run: x"),
        ],
    )
    assert cli.main(["doctor"]) != ExitCode.OK
    out = capsys.readouterr().out
    assert "MISSING] ffmpeg: not found on PATH" in out
    assert "install FFmpeg" in out
    assert "2 check(s) failed: ffmpeg, playwright" in out


def test_unexpected_exception_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[doctor.CheckResult]:
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "run_checks", boom)
    assert cli.main(["doctor"]) == ExitCode.INTERNAL_ERROR


def test_console_entry_point_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "hls_harvester", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("hls-harvester ")
