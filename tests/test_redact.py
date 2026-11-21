"""Tests for log redaction (SDD §3.8)."""

from __future__ import annotations

import io
import json
import logging

import pytest

from hls_harvester.redact import (
    REDACTED,
    JsonRedactingFormatter,
    RedactingFormatter,
    configure_logging,
    is_sensitive_header,
    redact_headers,
    redact_text,
    redact_url,
)

SIGNED = "https://cdn.example.com:8443/v/1/seg_001.ts?token=abc123&expires=999#frag"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SIGNED, f"https://cdn.example.com:8443/v/1/seg_001.ts?{REDACTED}#{REDACTED}"),
        ("https://example.com/a/b.m3u8", "https://example.com/a/b.m3u8"),
        ("https://user:pw@example.com/x?y=1", f"https://example.com/x?{REDACTED}"),
        ("http://[::1]:8080/p?q=1", f"http://[::1]:8080/p?{REDACTED}"),
        ("seg_001.ts?sig=abc", f"seg_001.ts?{REDACTED}"),
        ("https://example.com", "https://example.com"),
        ("http://[bad/x?y", REDACTED),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    assert redact_url(url) == expected


def test_redact_url_never_leaks_query_values() -> None:
    out = redact_url(SIGNED)
    assert "abc123" not in out
    assert "999" not in out


@pytest.mark.parametrize(
    "name",
    [
        "Cookie",
        "set-cookie",
        "AUTHORIZATION",
        "Proxy-Authorization",
        "X-Auth-Token",
        "X-Api-Key",
        "X-Session-Id",
        "X-Amz-Security-Token",
        "x-csrf-token",
    ],
)
def test_sensitive_headers(name: str) -> None:
    assert is_sensitive_header(name)


@pytest.mark.parametrize("name", ["User-Agent", "Accept", "Range", "Content-Type", "Referer"])
def test_non_sensitive_headers(name: str) -> None:
    assert not is_sensitive_header(name)


def test_custom_auth_header_from_template() -> None:
    assert not is_sensitive_header("X-Player-Ticket")
    assert is_sensitive_header("x-player-ticket", extra=["X-Player-Ticket"])


def test_redact_headers() -> None:
    headers = {
        "User-Agent": "UA/1.0",
        "Cookie": "sid=secret",
        "Authorization": "Bearer t0ken",
        "Referer": "https://site.example/watch?v=42&sig=zz",
        "X-Player-Ticket": "tkt",
    }
    out = dict(redact_headers(headers, sensitive=["X-Player-Ticket"]))
    assert out == {
        "User-Agent": "UA/1.0",
        "Cookie": REDACTED,
        "Authorization": REDACTED,
        "Referer": f"https://site.example/watch?{REDACTED}",
        "X-Player-Ticket": REDACTED,
    }


def test_redact_headers_keeps_repeated_pairs() -> None:
    pairs = [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("Accept", "*/*")]
    assert redact_headers(pairs) == [
        ("Set-Cookie", REDACTED),
        ("Set-Cookie", REDACTED),
        ("Accept", "*/*"),
    ]


def test_redact_text_ffmpeg_stderr() -> None:
    stderr = (
        "[hls @ 0x1] Opening 'https://cdn.example.com/a/seg_1.ts?token=SECRET1' for reading\n"
        "[https @ 0x2] HTTP error 403 Forbidden\n"
        "Cookie: sid=SECRET2\n"
        "  Authorization: Bearer SECRET3\n"
    )
    out = redact_text(stderr)
    for secret in ("SECRET1", "SECRET2", "SECRET3"):
        assert secret not in out
    assert "https://cdn.example.com/a/seg_1.ts?<redacted>" in out
    assert "HTTP error 403 Forbidden" in out
    assert "Cookie: <redacted>" in out


def test_redact_text_dict_repr_and_json() -> None:
    text = (
        "headers={'Cookie': 'sid=S1', 'User-Agent': 'UA'} "
        '{"authorization": "Bearer S2", "x-auth-token": "S3"} token Bearer S4'
    )
    out = redact_text(text)
    for secret in ("S1", "S2", "S3", "S4"):
        assert secret not in out
    assert "'User-Agent': 'UA'" in out


def test_redact_text_known_secrets_and_idempotence() -> None:
    out = redact_text("cookie value is s3cr3t-value here", secrets=["s3cr3t-value"])
    assert out == f"cookie value is {REDACTED} here"
    once = redact_text(f"get {SIGNED} done")
    assert redact_text(once) == once


def test_formatter_redacts_args_and_exceptions() -> None:
    record = logging.LogRecord(
        "hls_harvester.x", logging.ERROR, __file__, 1, "fetch %s failed", (SIGNED,), None
    )
    try:
        raise RuntimeError(f"boom {SIGNED}")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()
    text = RedactingFormatter("%(message)s").format(record)
    assert "abc123" not in text
    assert "seg_001.ts?<redacted>" in text

    payload = json.loads(JsonRedactingFormatter().format(record))
    assert payload["level"] == "ERROR"
    assert "abc123" not in payload["message"]
    assert "abc123" not in payload["exception"]


def test_configure_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    configure_logging(2)
    logger = logging.getLogger("hls_harvester.test")
    logger.debug("GET %s", SIGNED)
    configure_logging(0, json_format=True)
    logger.info("hidden at default level")
    logger.warning("GET %s", SIGNED)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert "abc123" not in stream.getvalue()
    assert json.loads(lines[1])["level"] == "WARNING"
