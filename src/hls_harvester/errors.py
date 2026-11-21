"""Process exit codes and the base error type (SDD §3.8 "結束碼").

The exit codes are defined here, and only here, so that every module can raise an
error carrying its exit code without importing the CLI.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Exit codes from SDD §3.8."""

    OK = 0
    """Success: download finished and verified, or playback ended normally."""
    INTERNAL_ERROR = 1
    """Unexpected internal error."""
    USAGE = 2
    """Usage error: bad arguments, unknown quality, download of a playlist without an end."""
    AMBIGUOUS = 3
    """Candidate selection is ambiguous and needs a human choice."""
    CAPTURE_FAILED = 4
    """Capture failed: no player request, playlist not observable, capture timed out."""
    UPSTREAM_ERROR = 5
    """Upstream error: retries exhausted, credential refresh failed, non-browser client refused."""
    UNSUPPORTED = 6
    """Unsupported content: DRM, unsupported tag or encryption method."""
    VERIFY_FAILED = 7
    """Output verification failed: FFmpeg failed, track/duration mismatch, period mismatch."""


class HarvesterError(Exception):
    """Base class for expected failures that end the process with a specific exit code."""

    exit_code: ExitCode = ExitCode.INTERNAL_ERROR

    def __init__(self, message: str, *, exit_code: ExitCode | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class UsageError(HarvesterError):
    """Invalid arguments or configuration (exit code 2)."""

    exit_code = ExitCode.USAGE


class CaptureError(HarvesterError):
    """Capture failed (exit code 4)."""

    exit_code = ExitCode.CAPTURE_FAILED


class UpstreamError(HarvesterError):
    """Upstream request failed for good (exit code 5)."""

    exit_code = ExitCode.UPSTREAM_ERROR


class CredentialsExpired(UpstreamError):
    """Upstream answered 401/403; credentials must be refreshed (SDD §3.5 item 4)."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class UnsupportedContent(HarvesterError):
    """DRM or an unsupported tag or encryption method (exit code 6)."""

    exit_code = ExitCode.UNSUPPORTED


class VerifyError(HarvesterError):
    """FFmpeg failed or the output did not verify (exit code 7)."""

    exit_code = ExitCode.VERIFY_FAILED
