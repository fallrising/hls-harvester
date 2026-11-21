"""Capture-layer tests against the fixture site (SDD §3.2, §7; milestone M2)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from hls_harvester.capturer import CaptureOptions, Capturer, Video, new_capture_context
from hls_harvester.errors import ExitCode, HarvesterError
from hls_harvester.playlist import MasterPlaylist
from tests.fixtures.server import CDN, ORIGIN, FixtureServer
from tests.fixtures.site import PAGE_TOKEN, Site

FAST = CaptureOptions(timeout_s=20, settle_s=0.7, click_retry_s=1.5)


@pytest.fixture
async def context(browser: Any) -> AsyncIterator[Any]:
    ctx = await new_capture_context(browser)
    yield ctx
    await ctx.close()


async def run(
    context: Any, url: str, options: CaptureOptions = FAST, **kwargs: Any
) -> tuple[Capturer, Any]:
    capturer = Capturer(context, url, options=options, **kwargs)
    result = await capturer.run()
    return capturer, result


# --- 開頁即預載 (playlist preloaded on page load) ---------------------------------------


async def test_preloaded_extensionless_playlist(
    context: Any, site: Site, fixture_server: FixtureServer
) -> None:
    capturer, result = await run(context, site.page("/watch/preload"))
    assert result.url == site.page("/hls/abc123")
    # Only the playlist became a candidate: not the MP4 or the TS segment loaded beside it.
    assert [c.url for c in capturer.candidates] == [site.page("/hls/abc123")]
    # The page requested the playlist once; the capture did not request it again.
    assert len(fixture_server.requests_to("/hls/abc123")) == 1
    assert result.video.associated  # its segments were fetched after the play click


# --- 彈窗與跳轉 (popups and redirects) --------------------------------------------------


async def test_popup_and_navigation_away(
    context: Any, site: Site, fixture_server: FixtureServer
) -> None:
    capturer, result = await run(context, site.page("/watch/popup"))
    assert result.url == site.media("vod_fmp4")
    assert capturer.popups  # the popup was seen and closed
    assert len(fixture_server.requests_to("/api/hijack")) == 2  # first click hijacked
    assert len(fixture_server.requests_to("/watch/popup")) == 2  # returned to the page
    assert fixture_server.requests_to("/ad/landing", host=CDN)
    assert context.pages == []  # every tab is closed after the capture


async def test_player_in_iframe(context: Any, site: Site) -> None:
    _, result = await run(context, site.page("/watch/iframe"))
    assert result.url == site.media("vod_fmp4")


# --- 同頁多部影片 (several videos on one page) ------------------------------------------


async def test_master_variants_are_one_video_and_ambiguity_exits_3(
    context: Any, site: Site
) -> None:
    capturer = Capturer(context, site.page("/watch/multi"), options=FAST)
    with pytest.raises(HarvesterError, match="several videos") as info:
        await capturer.run()
    assert info.value.exit_code == ExitCode.AMBIGUOUS
    videos = capturer.videos()
    assert len(videos) == 2  # master (+ its variant and audio) and the trailer
    master = next(v for v in videos if isinstance(v.root.playlist, MasterPlaylist))
    assert len(master.members) == 2  # the variant and the audio rendition it played


async def test_interactive_choice(context: Any, site: Site) -> None:
    offered: list[str] = []

    def choose(videos: Sequence[Video]) -> int:
        offered.extend(v.describe() for v in videos)
        return next(i for i, v in enumerate(videos) if "qualities" in v.describe())

    _, result = await run(context, site.page("/watch/multi"), chooser=choose)
    assert len(offered) == 2
    assert result.url == site.media("master")


async def test_video_started_by_play_wins(context: Any, site: Site) -> None:
    _, result = await run(context, site.page("/watch/multi-trailer-first"))
    assert result.url == site.media("master")


async def test_prefer_previous_choice(context: Any, site: Site) -> None:
    first = Capturer(context, site.page("/watch/multi"), options=FAST)
    with pytest.raises(HarvesterError):
        await first.run()
    trailer = next(v for v in first.videos() if not isinstance(v.root.playlist, MasterPlaylist))
    _, result = await run(context, site.page("/watch/multi"), prefer=trailer.fingerprint())
    assert result.url == site.media("vod_long")


# --- failures ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "code", "message"),
    [
        ("/watch/blob", ExitCode.CAPTURE_FAILED, "playlist not observable"),
        ("/watch/nothing", ExitCode.CAPTURE_FAILED, "no player request found"),
        ("/watch/drm", ExitCode.UNSUPPORTED, "METHOD=SAMPLE-AES"),
        ("/members/watch", ExitCode.CAPTURE_FAILED, "hls-harvester login"),
    ],
)
async def test_capture_failures(
    context: Any, site: Site, path: str, code: ExitCode, message: str
) -> None:
    with pytest.raises(HarvesterError, match=message) as info:
        await run(context, site.page(path))
    assert info.value.exit_code == code


async def test_capture_timeout(context: Any, site: Site) -> None:
    options = CaptureOptions(timeout_s=1.5, settle_s=0.5, click_retry_s=0.5, max_clicks=10)
    with pytest.raises(HarvesterError, match="timed out") as info:
        await run(context, site.page("/watch/nothing"), options)
    assert info.value.exit_code == ExitCode.CAPTURE_FAILED


# --- title and header templates ----------------------------------------------------------


async def test_title_from_og_title(context: Any, site: Site) -> None:
    _, result = await run(context, site.page("/watch/vod"))
    assert result.title == "Test Clip: Part 1/2"


@pytest.mark.parametrize("mode", ["direct", "redirect"])
async def test_header_templates(
    context: Any, site: Site, fixture_server: FixtureServer, mode: str
) -> None:
    _, result = await run(context, site.page(f"/watch/creds?mode={mode}"))
    templates = {t.origin: t for t in result.templates}
    origin = templates[fixture_server.hosts[ORIGIN].base_url]
    cdn = templates[fixture_server.hosts[CDN].base_url]
    assert origin.auth_headers == {"Authorization": f"Bearer {PAGE_TOKEN}"}
    assert cdn.auth_headers == {}
    for template in (origin, cdn):
        assert "User-Agent" in template.headers
        assert not any(k.lower() == "cookie" for k in template.headers)
    assert origin.headers["Referer"] == site.page(f"/watch/creds?mode={mode}")
