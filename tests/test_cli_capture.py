"""End-to-end ``hls-harvester get <page-url>`` runs (SDD §7; milestone M2).

The CLI runs in a subprocess with a real headless Chromium and real FFmpeg against the
fixture site.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hls_harvester.downloader import temp_output_for, work_dir_for
from hls_harvester.errors import ExitCode
from tests.fixtures.cli import cli_env, run_cli
from tests.fixtures.server import CDN, ORIGIN, FixtureServer, expire_after, unavailable
from tests.fixtures.site import LOGIN_COOKIE, ORIGIN_COOKIE, PAGE_TOKEN, Site

SECRETS = (
    PAGE_TOKEN,
    ORIGIN_COOKIE,
    "CDN-COOKIE-SECRET",
    "QUERY-SECRET",
    "CDN-QUERY-SECRET",
    LOGIN_COOKIE,
)


def config(out: Path, extra: str = "") -> str:
    return f'[defaults]\noutput_dir = "{out}"\n{extra}'


@pytest.fixture
def out(tmp_path: Path) -> Path:
    path = tmp_path / "videos"
    path.mkdir()
    return path


async def probe_duration(path: Path) -> float:
    from hls_harvester.remux import ffprobe

    probe = await ffprobe(path)
    assert probe.kinds == {"video", "audio"}
    assert probe.duration is not None
    return probe.duration


# --- 完整下載 (complete download) ------------------------------------------------------


async def test_page_to_verified_file(site: Site, home: Path, out: Path) -> None:
    env = cli_env(home, config(out))
    code, stdout, err = await run_cli(env, "get", site.page("/watch/vod"), "--non-interactive")
    assert code == ExitCode.OK, err
    final = out / "Test Clip Part 1 2.mp4"  # og:title, sanitised
    assert stdout.strip() == str(final)
    assert await probe_duration(final) == pytest.approx(2, abs=0.5)
    assert sorted(p.name for p in out.iterdir()) == [final.name]


async def test_failed_download_leaves_no_partial_file(
    site: Site, fixture_server: FixtureServer, home: Path, out: Path
) -> None:
    # The player fetches the first two segments; the downloader then cannot get one.
    fixture_server.add_rule(unavailable("/media/vod_fmp4/seg_001.m4s", host=CDN, after_requests=1))
    env = cli_env(home, config(out, "max_retries = 1\nretry_backoff_s = 0.05\n"))
    code, _, err = await run_cli(env, "get", site.page("/watch/vod"), "--non-interactive")
    assert code == ExitCode.UPSTREAM_ERROR, err
    final = out / "Test Clip Part 1 2.mp4"
    assert not final.exists()
    assert not temp_output_for(final).exists()
    assert work_dir_for(final).is_dir()


# --- 同頁多部影片 (several videos) ------------------------------------------------------


async def test_ambiguous_page_non_interactive(site: Site, home: Path, out: Path) -> None:
    code, _, err = await run_cli(
        cli_env(home, config(out)), "get", site.page("/watch/multi"), "--non-interactive"
    )
    assert code == ExitCode.AMBIGUOUS
    assert "several videos" in err
    assert list(out.iterdir()) == []


# --- 長片與憑證過期 (long video, credentials expire half-way) --------------------------


async def test_long_video_recaptures_after_expiry(
    site: Site, fixture_server: FixtureServer, home: Path, out: Path
) -> None:
    # Token 1 (first page load): the playlist works for the player and the downloader's
    # first request, then expires; its segment signatures expire after 300 requests.
    fixture_server.add_rule(expire_after("/signed/long.m3u8", requests=2, status=403, query="t=1"))
    fixture_server.add_rule(
        expire_after("/media/vod_600/seg_*", requests=300, status=403, query="t=1")
    )
    env = cli_env(home, config(out))
    code, stdout, err = await run_cli(env, "get", site.page("/watch/long"), "--non-interactive")
    assert code == ExitCode.OK, err
    assert site.long_tokens == [1, 2]  # exactly one re-capture (limit: 3)
    final = Path(stdout.strip())
    assert await probe_duration(final) == pytest.approx(300, abs=1.5)
    fetched = {
        r.path for r in fixture_server.requests_to("/media/vod_600/seg_*") if r.status == 200
    }
    assert len(fetched) == 600
    # After the re-capture only the new token is used.
    stale = [
        r
        for r in fixture_server.requests_to("/media/vod_600/seg_*")
        if r.query_string == "t=1" and r.status == 200
    ]
    assert len(stale) == 300


# --- 憑證轉送 (credential forwarding) --------------------------------------------------


def write_state(path: Path) -> Path:
    """A session with a cookie for the CDN only, as if the user had signed in there."""
    state = {
        "cookies": [
            {
                "name": "cdn_session",
                "value": "CDN-COOKIE-SECRET",
                "domain": "localhost",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    path.write_text(json.dumps(state))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("mode", ["direct", "redirect"])
async def test_credentials_go_only_where_they_belong(
    site: Site, fixture_server: FixtureServer, home: Path, out: Path, tmp_path: Path, mode: str
) -> None:
    state = write_state(tmp_path / "state.json")
    env = cli_env(home, config(out))
    page = site.page(f"/watch/creds?mode={mode}")
    code, _, err = await run_cli(
        env, "get", page, "--storage-state", str(state), "--non-interactive", "-vv"
    )
    assert code == ExitCode.OK, err

    # The downloader's own request for the master is the second one (the player made the
    # first). It carries the origin's cookie, Referer, User-Agent and Authorization.
    player, downloader = fixture_server.requests_to("/secure/master.m3u8", host=ORIGIN)[:2]
    assert downloader.status == 200
    assert f"sid={ORIGIN_COOKIE}" in (downloader.header("Cookie") or "")
    assert "cdn_session" not in (downloader.header("Cookie") or "")
    assert downloader.header("Authorization") == f"Bearer {PAGE_TOKEN}"
    assert downloader.header("Referer") == page
    assert downloader.header("User-Agent") == player.header("User-Agent")

    # The CDN never sees the origin's cookie or Authorization, but gets its own cookie.
    cdn_requests = [r for r in fixture_server.requests if r.host == CDN]
    assert cdn_requests
    for record in cdn_requests:
        assert record.header("Authorization") is None
        assert ORIGIN_COOKIE not in (record.header("Cookie") or "")
    assert any("cdn_session=CDN-COOKIE-SECRET" in (r.header("Cookie") or "") for r in cdn_requests)
    if mode == "redirect":
        hops = fixture_server.requests_to("/secure/variant.m3u8", host=ORIGIN)
        assert any(r.header("Authorization") == f"Bearer {PAGE_TOKEN}" for r in hops)

    # No credential or query string in the logs (which include the FFmpeg command lines).
    assert "running: " in err
    for secret in SECRETS:
        assert secret not in err


async def test_job_json_has_no_credentials(
    site: Site, fixture_server: FixtureServer, home: Path, out: Path, tmp_path: Path
) -> None:
    state = write_state(tmp_path / "state.json")
    fixture_server.add_rule(unavailable("/media/vod_fmp4/seg_001.m4s", host=CDN, after_requests=1))
    env = cli_env(home, config(out, "max_retries = 0\n"))
    code, _, err = await run_cli(
        env,
        "get",
        site.page("/watch/creds?mode=redirect"),
        "--storage-state",
        str(state),
        "--non-interactive",
    )
    assert code == ExitCode.UPSTREAM_ERROR, err
    jobs = list(out.glob(".*.hlsh/job.json"))
    assert len(jobs) == 1
    text = jobs[0].read_text()
    for secret in SECRETS:
        assert secret not in text
    assert "?<redacted>" in text


# --- 登入工作階段 (login session) ------------------------------------------------------


async def test_members_page_needs_login(site: Site, home: Path, out: Path) -> None:
    code, _, err = await run_cli(
        cli_env(home, config(out)), "get", site.page("/members/watch"), "--non-interactive"
    )
    assert code == ExitCode.CAPTURE_FAILED
    assert "hls-harvester login" in err


async def test_members_page_with_storage_state(
    site: Site, home: Path, out: Path, tmp_path: Path
) -> None:
    from tests.test_login import do_login

    state = await do_login(site, tmp_path / "state.json")
    code, stdout, err = await run_cli(
        cli_env(home, config(out)),
        "get",
        site.page("/members/watch"),
        "--storage-state",
        str(state),
        "--non-interactive",
    )
    assert code == ExitCode.OK, err
    assert Path(stdout.strip()).name == "Members Clip.mp4"
