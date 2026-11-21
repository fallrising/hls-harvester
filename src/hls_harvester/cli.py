"""Command line interface (SDD §3.1) and exit-code handling (SDD §3.8).

Subcommands: ``get``, ``login`` and ``doctor``. Only ``doctor`` is implemented in M0;
``get`` and ``login`` validate their arguments and configuration, then exit with
code 1 and "not implemented".
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

from hls_harvester import __version__, doctor
from hls_harvester.config import load_config, resolve_settings
from hls_harvester.errors import ExitCode, HarvesterError
from hls_harvester.redact import configure_logging, redact_text

log = logging.getLogger(__name__)


def _http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise argparse.ArgumentTypeError("expected an http:// or https:// URL")
    return value


def _logging_options(*, suppress_defaults: bool) -> argparse.ArgumentParser:
    """Logging flags (SDD §3.8), accepted before or after the subcommand."""
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_argument_group("logging")
    # Subcommand copies must not overwrite values given before the subcommand.
    verbose_default: object = argparse.SUPPRESS if suppress_defaults else 0
    json_default: object = argparse.SUPPRESS if suppress_defaults else False
    group.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=verbose_default,
        help="more log output (-v info, -vv debug)",
    )
    group.add_argument(
        "--log-json",
        action="store_true",
        default=json_default,
        help="write structured JSON logs to stderr",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hls-harvester",
        description="Capture an HLS stream from a page you may access, then play or download it.",
        parents=[_logging_options(suppress_defaults=False)],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    common = [_logging_options(suppress_defaults=True)]

    get = sub.add_parser(
        "get", parents=common, help="capture a page's stream and download (default) or play it"
    )
    get.add_argument("page_url", type=_http_url, metavar="PAGE_URL", help="URL of the video page")
    mode = get.add_mutually_exclusive_group()
    mode.add_argument(
        "--download",
        dest="mode",
        action="store_const",
        const="download",
        help="download to a file (default)",
    )
    mode.add_argument(
        "--play", dest="mode", action="store_const", const="play", help="serve a local playlist"
    )
    get.set_defaults(mode="download")
    get.add_argument("--quality", metavar="Q", help="quality to select, e.g. 720p")
    get.add_argument("--output", type=Path, metavar="PATH", help="output file path")
    get.add_argument(
        "--storage-state", type=Path, metavar="FILE", help="Playwright storage state from `login`"
    )
    get.add_argument("--headed", action="store_true", help="show the capture browser window")
    get.add_argument("--resume", action="store_true", help="resume an interrupted download")
    get.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt; exit with code 3 when the choice is ambiguous",
    )
    get.add_argument(
        "--reencode",
        action="store_true",
        help="re-encode periods whose parameters differ instead of failing (SDD §3.5)",
    )

    login = sub.add_parser(
        "login", parents=common, help="log in by hand and save the browser session"
    )
    login.add_argument("site_url", type=_http_url, metavar="SITE_URL", help="site to log in to")
    login.add_argument(
        "--storage-state",
        type=Path,
        metavar="FILE",
        required=True,
        help="where to write the session (mode 0600)",
    )

    sub.add_parser("doctor", parents=common, help="check Python, FFmpeg, Playwright and config")
    return parser


def _not_implemented(command: str, stderr: TextIO) -> int:
    print(f"hls-harvester {command}: not implemented", file=stderr)
    return ExitCode.INTERNAL_ERROR


def cmd_get(args: argparse.Namespace, stderr: TextIO) -> int:
    config = load_config()
    resolve_settings(
        config,
        host=urlsplit(args.page_url).hostname,
        overrides={"headless": False if args.headed else None},
    )
    return _not_implemented("get", stderr)


def cmd_login(args: argparse.Namespace, stderr: TextIO) -> int:
    load_config()
    return _not_implemented("login", stderr)


def cmd_doctor(args: argparse.Namespace, stdout: TextIO) -> int:
    results = doctor.run_checks()
    for result in results:
        mark = "ok" if result.ok else "MISSING"
        print(f"[{mark:>7}] {result.name}: {result.detail}", file=stdout)
        if not result.ok and result.hint:
            print(f"          -> {result.hint}", file=stdout)
    failed = [r.name for r in results if not r.ok]
    if failed:
        print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}", file=stdout)
        return ExitCode.INTERNAL_ERROR
    print("\nall checks passed", file=stdout)
    return ExitCode.OK


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose, json_format=args.log_json)
    try:
        if args.command == "get":
            return cmd_get(args, sys.stderr)
        if args.command == "login":
            return cmd_login(args, sys.stderr)
        return cmd_doctor(args, sys.stdout)
    except HarvesterError as exc:
        print(f"hls-harvester: {redact_text(str(exc))}", file=sys.stderr)
        return int(exc.exit_code)
    except Exception:
        log.exception("unexpected internal error")
        return ExitCode.INTERNAL_ERROR


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())
