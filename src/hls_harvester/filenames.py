"""Output file names (SDD §3.2 item 7, §3.9).

Titles become file names by removing path separators, control characters and Windows
reserved characters and names, limiting the length, and avoiding existing files.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import unquote, urlsplit

FALLBACK_NAME: Final = "video"
MAX_NAME_BYTES: Final = 150
"""Leaves room for suffixes and the work-directory prefix within a 255-byte name."""

_FORBIDDEN_RE: Final = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_WINDOWS_RESERVED: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
# Path segments that say nothing about the video; the parent directory is used instead.
_GENERIC_STEMS: Final = frozenset(
    {
        "index",
        "master",
        "playlist",
        "prog_index",
        "chunklist",
        "manifest",
        "main",
        "media",
        "stream",
        "video",
        "mono",
        "hls",
        "variant",
        "output",
    }
)


def sanitize_filename(title: str, *, max_bytes: int = MAX_NAME_BYTES) -> str:
    """Turn ``title`` into a safe file name stem (no extension)."""
    text = unicodedata.normalize("NFC", title)
    text = "".join(" " if unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"} else c for c in text)
    text = _FORBIDDEN_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip().strip(".").strip()
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")
    if not text or text.split(".")[0].upper() in _WINDOWS_RESERVED:
        text = f"_{text}" if text else FALLBACK_NAME
    if text.startswith("."):
        text = "_" + text
    return text


def name_from_url(url: str) -> str:
    """A file name stem for a playlist URL, used when no page title exists (M1)."""
    path = PurePosixPath(unquote(urlsplit(url).path))
    parts = [p for p in path.parts if p not in {"/", ""}]
    while parts:
        stem = PurePosixPath(parts[-1]).stem
        if stem and stem.lower() not in _GENERIC_STEMS:
            return sanitize_filename(stem)
        parts.pop()
    return FALLBACK_NAME


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """``directory/stem+suffix``, or ``stem (n)+suffix`` if that file already exists."""
    candidate = directory / f"{stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate
