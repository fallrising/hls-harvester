"""Log redaction (SDD §3.8 "日誌").

Every log line goes through this module. URLs keep only scheme, host and path; query
strings, ``Cookie``, ``Authorization`` and custom authentication headers are replaced with
``<redacted>``. FFmpeg stderr passes through :func:`redact_text` before it is logged.

The functions here are deliberately conservative: when in doubt, a value is redacted.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Final
from urllib.parse import urlsplit, urlunsplit

REDACTED: Final = "<redacted>"

# Headers that always carry credentials.
_CREDENTIAL_HEADERS: Final = frozenset(
    {"cookie", "set-cookie", "authorization", "proxy-authorization"}
)

# Header names containing one of these fragments are treated as custom auth headers
# (e.g. "X-Auth-Token", "X-Api-Key", "X-Session-Id") even when nobody declared them.
_SENSITIVE_NAME_FRAGMENTS: Final = (
    "auth",
    "token",
    "secret",
    "key",
    "session",
    "signature",
    "csrf",
    "xsrf",
    "password",
    "credential",
)

# Headers whose value is a URL; the value is kept but redacted like any other URL.
_URL_HEADERS: Final = frozenset({"referer", "origin", "location", "content-location"})

_URL_CHARS: Final = r"[^\s'\"<>()\[\]{}`]"
# The "<redacted>" alternative keeps redact_text idempotent on already-redacted URLs.
_URL_RE: Final = re.compile(
    rf"\b[A-Za-z][A-Za-z0-9+.\-]*://{_URL_CHARS}+(?:<redacted>{_URL_CHARS}*)*"
)

_HEADER_NAMES_RE: Final = (
    r"(?:cookie|set-cookie|authorization|proxy-authorization"
    r"|[\w-]*(?:auth|token|secret|api-?key|session|signature|csrf|xsrf|password)[\w-]*)"
)
# "Cookie: a=b" on its own line (HTTP dumps, FFmpeg -headers echo).
_HEADER_LINE_RE: Final = re.compile(
    rf"(?im)^(?P<prefix>[ \t>]*)(?P<name>{_HEADER_NAMES_RE})[ \t]*:[ \t]*(?P<value>[^\r\n]*)$"
)
# "'Cookie': 'a=b'" or "\"Authorization\": \"x\"" inside a repr()/JSON dump.
_QUOTED_HEADER_RE: Final = re.compile(
    rf"(?i)(?P<q>['\"])(?P<name>{_HEADER_NAMES_RE})(?P=q)(?P<sep>\s*[:=]\s*)"
    r"(?P<vq>['\"])(?P<value>(?:\\.|(?!(?P=vq)).)*)(?P=vq)"
)
_BEARER_RE: Final = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]+")


def redact_url(url: str) -> str:
    """Return ``url`` with only scheme, host (and port) and path kept.

    Query strings and fragments become ``<redacted>``; userinfo (``user:pass@``) is
    dropped. Relative references keep their path. Unparseable input is fully redacted.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return REDACTED
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    query = REDACTED if parts.query else ""
    fragment = REDACTED if parts.fragment else ""
    redacted = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    # urlunsplit would percent-encode nothing here, but it drops a lone "?"; add markers
    # by hand so they are never URL-encoded.
    if query:
        redacted += "?" + query
    if fragment:
        redacted += "#" + fragment
    return redacted


def is_sensitive_header(name: str, extra: Iterable[str] = ()) -> bool:
    """Return True when a header with ``name`` may carry credentials.

    ``extra`` lists site-specific auth header names recorded in a header template
    (SDD §3.2 item 6); they are compared case-insensitively.
    """
    lowered = name.strip().lower()
    if lowered in _CREDENTIAL_HEADERS:
        return True
    if lowered in {e.strip().lower() for e in extra}:
        return True
    return any(fragment in lowered for fragment in _SENSITIVE_NAME_FRAGMENTS)


def redact_headers(
    headers: Mapping[str, str] | Iterable[tuple[str, str]],
    sensitive: Iterable[str] = (),
) -> list[tuple[str, str]]:
    """Return a copy of ``headers`` that is safe to log.

    Credential headers become ``<redacted>``, URL-valued headers (``Referer``,
    ``Location`` ...) go through :func:`redact_url`, everything else is kept. Pairs are
    returned as a list so repeated headers (``Set-Cookie``) survive.
    """
    extra = tuple(sensitive)
    pairs = headers.items() if isinstance(headers, Mapping) else headers
    out: list[tuple[str, str]] = []
    for name, value in pairs:
        if is_sensitive_header(name, extra):
            out.append((name, REDACTED))
        elif name.strip().lower() in _URL_HEADERS:
            out.append((name, redact_url(value)))
        else:
            out.append((name, value))
    return out


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact URLs, credential headers and bearer tokens inside free text.

    Used as the last line of defence for log messages and FFmpeg stderr. ``secrets``
    are literal values (cookie values, tokens) to scrub wherever they appear.
    """
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = _HEADER_LINE_RE.sub(lambda m: f"{m['prefix']}{m['name']}: {REDACTED}", text)
    text = _QUOTED_HEADER_RE.sub(
        lambda m: f"{m['q']}{m['name']}{m['q']}{m['sep']}{m['vq']}{REDACTED}{m['vq']}", text
    )
    text = _BEARER_RE.sub(lambda m: f"{m[1]} {REDACTED}", text)
    return _URL_RE.sub(lambda m: redact_url(m.group(0)), text)


class RedactingFormatter(logging.Formatter):
    """A formatter whose complete output (message, args, traceback) is redacted."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class JsonRedactingFormatter(logging.Formatter):
    """Structured (one JSON object per line) formatter for ``--log-json``; redacted."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(verbosity: int = 0, *, json_format: bool = False) -> logging.Handler:
    """Install a single redacting stderr handler on the ``hls_harvester`` logger.

    ``verbosity`` 0 logs warnings, 1 (``-v``) info, 2+ (``-vv``) debug.
    """
    level = logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonRedactingFormatter()
        if json_format
        else RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger = logging.getLogger("hls_harvester")
    for old in list(logger.handlers):
        logger.removeHandler(old)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return handler
