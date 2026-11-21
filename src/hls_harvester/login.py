"""``hls-harvester login`` (SDD §3.1): save a session the user created by hand.

A windowed, isolated browser opens the site; the user signs in themselves. When they
press Enter in the terminal or close the browser window, the context's Playwright
``storage_state`` (cookies and local storage) is written to the given file with mode
0600. The tool never asks for, sees or stores a password.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from hls_harvester.capturer import launch_browser, new_capture_context
from hls_harvester.errors import UsageError

log = logging.getLogger(__name__)

WaitForUser = Callable[[Any], Awaitable[None]]
SNAPSHOT_INTERVAL_S = 1.0


def write_storage_state(state: dict[str, Any], path: Path) -> None:
    """Write ``state`` to ``path`` atomically with mode 0600."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


async def wait_for_enter_or_close(page: Any) -> None:
    """Default wait: Enter on stdin, or the user closing the page or the browser."""
    print(
        "Sign in in the browser window, then press Enter here (or close the window).",
        file=sys.stderr,
    )
    closed = asyncio.get_running_loop().create_future()

    def on_close(*_: object) -> None:
        if not closed.done():
            closed.set_result(None)

    page.on("close", on_close)
    page.context.browser.on("disconnected", on_close)
    enter = asyncio.ensure_future(asyncio.to_thread(sys.stdin.readline))
    await asyncio.wait({closed, enter}, return_when=asyncio.FIRST_COMPLETED)


async def login(
    site_url: str,
    storage_state: Path,
    *,
    playwright: Any,
    headless: bool = False,
    executable: str | None = None,
    wait_for_user: WaitForUser = wait_for_enter_or_close,
) -> Path:
    """Open ``site_url`` for a manual sign-in and save the session to ``storage_state``."""
    if not storage_state.parent.is_dir():
        raise UsageError(f"directory {storage_state.parent} does not exist")
    browser = await launch_browser(playwright, headless=headless, executable=executable)
    snapshot: dict[str, Any] | None = None
    try:
        context = await new_capture_context(browser)
        page = await context.new_page()
        await page.goto(site_url, wait_until="domcontentloaded")

        async def keep_snapshots() -> None:
            nonlocal snapshot
            while True:
                with contextlib.suppress(Exception):
                    snapshot = await context.storage_state()
                await asyncio.sleep(SNAPSHOT_INTERVAL_S)

        snapshots = asyncio.ensure_future(keep_snapshots())
        try:
            await wait_for_user(page)
        finally:
            snapshots.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await snapshots
        with contextlib.suppress(Exception):  # the user may have closed the browser
            snapshot = await context.storage_state()
    finally:
        with contextlib.suppress(Exception):
            await browser.close()
    if snapshot is None:
        raise UsageError("the browser closed before the session could be saved")
    write_storage_state(snapshot, storage_state)
    log.info("saved the session (%d cookies)", len(snapshot.get("cookies", [])))
    return storage_state
