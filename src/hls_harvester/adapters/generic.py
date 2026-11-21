"""Generic adapter (SDD §3.7): play button discovery and title extraction.

The adapter never solves CAPTCHAs or hides automation (SDD §1.1); it only clicks what a
user would click to start the player.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_TITLE_JS = """
(selectors) => {
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    const text = el && (el.content || el.innerText || el.textContent);
    if (text && text.trim()) return text.trim();
  }
  const og = document.querySelector('meta[property="og:title"]');
  if (og && og.content && og.content.trim()) return og.content.trim();
  if (document.title && document.title.trim()) return document.title.trim();
  const h1 = document.querySelector('h1');
  return h1 && h1.innerText.trim() ? h1.innerText.trim() : null;
}
"""


@dataclass(frozen=True)
class Adapter:
    name: str
    play_selectors: tuple[str, ...]
    """Tried in order, in the main frame first and then in each iframe."""
    dismiss_selectors: tuple[str, ...] = ()
    """Overlays to close before clicking play."""
    title_selectors: tuple[str, ...] = ()
    """Tried before ``og:title``, ``<title>`` and the first ``<h1>``."""

    async def find_play_target(self, page: Any) -> Any | None:
        """The first visible element matching a play selector in any frame, or None."""
        for selector in self.play_selectors:
            for frame in page.frames:
                try:
                    locator = frame.locator(selector).first
                    if await locator.count() and await locator.is_visible():
                        return locator
                except Exception:  # frame detached or navigating; try the next one
                    continue
        return None

    async def dismiss_overlays(self, page: Any) -> None:
        for selector in self.dismiss_selectors:
            for frame in page.frames:
                try:
                    locator = frame.locator(selector).first
                    if await locator.count() and await locator.is_visible():
                        await locator.click(timeout=2000)
                except Exception:
                    continue

    async def title(self, page: Any) -> str | None:
        try:
            title = await page.main_frame.evaluate(_TITLE_JS, list(self.title_selectors))
        except Exception as exc:
            log.debug("title extraction failed: %s", exc)
            return None
        return title if isinstance(title, str) and title else None


GENERIC = Adapter(
    name="generic",
    play_selectors=(
        "button.play",
        ".play-button",
        ".vjs-big-play-button",
        ".jw-icon-playback",
        "button[aria-label*='play' i]",
        "[role='button'][aria-label*='play' i]",
        "button[title*='play' i]",
        "video",
    ),
)
