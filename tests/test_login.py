"""Tests for ``login`` and storage-state sessions (SDD §3.1, §7 "登入工作階段")."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from hls_harvester.capturer import CaptureOptions, capture, new_capture_context
from hls_harvester.errors import ExitCode, HarvesterError
from hls_harvester.login import login, write_storage_state
from tests.fixtures.cli import browser_executable
from tests.fixtures.site import LOGIN_COOKIE, Site

FAST = CaptureOptions(timeout_s=20, settle_s=0.7, click_retry_s=1.5)


async def sign_in(page: Any) -> None:
    """Stands in for the user typing into the windowed browser."""
    await page.fill("input[name=user]", "alice")
    await page.fill("input[type=password]", "hunter2")
    async with page.expect_navigation():
        await page.click("#submit")


async def do_login(site: Site, path: Path) -> Path:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        return await login(
            site.page("/members/watch"),
            path,
            playwright=playwright,
            headless=True,
            executable=browser_executable(),
            wait_for_user=sign_in,
        )


async def test_login_writes_private_storage_state(site: Site, tmp_path: Path) -> None:
    path = await do_login(site, tmp_path / "state.json")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    state = json.loads(path.read_text())
    assert {c["name"]: c["value"] for c in state["cookies"]}["session"] == LOGIN_COOKIE
    assert "hunter2" not in path.read_text()  # the password is never stored


async def test_storage_state_unlocks_members_page(site: Site, browser: Any, tmp_path: Path) -> None:
    path = await do_login(site, tmp_path / "state.json")
    context = await new_capture_context(browser, str(path))
    try:
        result = await capture(context, site.page("/members/watch"), options=FAST)
    finally:
        await context.close()
    assert result.url == site.media("vod_fmp4")
    assert result.title == "Members Clip"


async def test_without_storage_state_login_is_suggested(site: Site, browser: Any) -> None:
    context = await new_capture_context(browser)
    try:
        with pytest.raises(HarvesterError, match="hls-harvester login") as info:
            await capture(context, site.page("/members/watch"), options=FAST)
    finally:
        await context.close()
    assert info.value.exit_code == ExitCode.CAPTURE_FAILED


def test_write_storage_state_tightens_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{}")
    path.chmod(0o644)
    write_storage_state({"cookies": [], "origins": []}, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"cookies": [], "origins": []}
