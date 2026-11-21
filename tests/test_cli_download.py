"""End-to-end ``hls-harvester get --playlist`` runs in a subprocess (SDD §7; M1).

The fixture server runs in the test's event loop, so the CLI (which runs its own event
loop) is started as a separate process.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from hls_harvester.downloader import work_dir_for
from hls_harvester.errors import ExitCode
from tests.fixtures.media import HlsSamples
from tests.fixtures.server import FixtureServer, delay

SECRET = "SECRET-TOKEN-42"


def cli_env(home: Path, config: str = "") -> dict[str, str]:
    """Environment with an isolated config file (XDG on Linux, ~/Library on macOS)."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    for base in (home / ".config", home / "Library" / "Application Support"):
        (base / "hls-harvester").mkdir(parents=True, exist_ok=True)
        (base / "hls-harvester" / "config.toml").write_text(config)
    return env


async def start_cli(env: dict[str, str], *args: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "hls_harvester",
        *args,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def run_cli(env: dict[str, str], *args: str) -> tuple[int, str, str]:
    proc = await start_cli(env, *args)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


async def test_cli_download_prints_path(
    fixture_server: FixtureServer, hls_samples: HlsSamples, home: Path, tmp_path: Path
) -> None:
    env = cli_env(home)
    target = tmp_path / "clip.mp4"
    url = fixture_server.media_url(hls_samples.playlist("vod_fmp4")) + f"?token={SECRET}"
    code, out, err = await run_cli(env, "get", url, "--playlist", "--output", str(target), "-vv")
    assert code == ExitCode.OK, err
    assert out.strip() == str(target)
    assert target.stat().st_size > 0
    # Logs (at debug level) never contain the query string.
    assert SECRET not in err
    assert "<redacted>" in err


@pytest.mark.parametrize(
    ("name", "extra", "code", "message"),
    [
        ("sample_aes", (), ExitCode.UNSUPPORTED, "METHOD=SAMPLE-AES"),
        ("widevine", (), ExitCode.UNSUPPORTED, "urn:uuid:edef8ba9"),
        ("discontinuity_mismatch", (), ExitCode.VERIFY_FAILED, "--reencode"),
        ("discontinuity_mismatch", ("--reencode",), ExitCode.OK, ""),
        ("master", ("--quality", "1080p"), ExitCode.USAGE, "90p, 180p, 360p"),
    ],
)
async def test_cli_exit_codes(
    fixture_server: FixtureServer,
    hls_samples: HlsSamples,
    home: Path,
    tmp_path: Path,
    name: str,
    extra: tuple[str, ...],
    code: int,
    message: str,
) -> None:
    env = cli_env(home, f'[defaults]\noutput_dir = "{tmp_path}"\n')
    url = fixture_server.media_url(hls_samples.playlist(name))
    actual, _, err = await run_cli(env, "get", url, "--playlist", "--non-interactive", *extra)
    assert actual == code, err
    assert message in err


async def test_cli_get_without_playlist_flag_is_not_implemented(home: Path) -> None:
    code, _, err = await run_cli(cli_env(home), "get", "https://example.com/watch")
    assert code == ExitCode.INTERNAL_ERROR
    assert "not implemented" in err


# --- 續傳 (resume) ---------------------------------------------------------------------


async def test_resume_after_kill_is_byte_identical(
    fixture_server: FixtureServer, hls_samples: HlsSamples, home: Path, tmp_path: Path
) -> None:
    env = cli_env(home, "[defaults]\nconcurrency_per_host = 1\n")
    url = fixture_server.media_url(hls_samples.playlist("vod_long")) + f"?token={SECRET}"
    segments = "/media/vod_long/seg_*.ts"

    # Reference: one uninterrupted download.
    reference = tmp_path / "reference.mp4"
    code, _, err = await run_cli(env, "get", url, "--playlist", "--output", str(reference))
    assert code == ExitCode.OK, err

    # Interrupted download: slow the segments down and kill the process part-way.
    target = tmp_path / "resumed.mp4"
    slow = fixture_server.add_rule(delay(segments, 0.15))
    fixture_server.requests.clear()
    proc = await start_cli(env, "get", url, "--playlist", "--output", str(target))

    def finished() -> int:
        return sum(r.status == 200 for r in fixture_server.requests_to(segments))

    async def wait_for_progress() -> None:
        while finished() < 10:  # noqa: ASYNC110 - polling recorded server state
            await asyncio.sleep(0.05)

    await asyncio.wait_for(wait_for_progress(), timeout=60)
    proc.kill()
    await proc.wait()
    assert not target.exists()

    job_path = work_dir_for(target) / "job.json"
    job_text = job_path.read_text()
    assert SECRET not in job_text
    job = json.loads(job_text)
    done = {s["seq"] for s in job["tracks"]["main"]["segments"] if s["done"]}
    assert 10 <= len(done) < 30

    # Resume: only the unfinished segments are requested again.
    fixture_server.rules.remove(slow)
    fixture_server.requests.clear()
    code, _, err = await run_cli(
        env, "get", url, "--playlist", "--output", str(target), "--resume", "-v"
    )
    assert code == ExitCode.OK, err
    fetched = {r.path.rsplit("/", 1)[1] for r in fixture_server.requests_to(segments)}
    # The fixture's media sequence starts at 0, so sequence N is seg_00N.ts.
    assert fetched == {f"seg_{i:03d}.ts" for i in range(30) if i not in done}
    assert target.read_bytes() == reference.read_bytes()
    assert not work_dir_for(target).exists()
    assert SECRET not in err


async def test_existing_work_dir_non_interactive(
    fixture_server: FixtureServer, hls_samples: HlsSamples, home: Path, tmp_path: Path
) -> None:
    target = tmp_path / "x.mp4"
    work_dir_for(target).mkdir()
    url = fixture_server.media_url(hls_samples.playlist("vod_fmp4"))
    code, _, err = await run_cli(
        cli_env(home), "get", url, "--playlist", "--output", str(target), "--non-interactive"
    )
    assert code == ExitCode.USAGE
    assert "--resume" in err
