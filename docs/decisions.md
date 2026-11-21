# Implementation decisions

Short records of where the SDD (docs/SDD.md) was ambiguous or silent and which reading the
implementation took. Newest last. Each entry names the SDD section it interprets.

## M0

1. **Exit codes live in `errors.py`, not `cli.py`** (§3.8, §6). §6 puts exit codes in `cli.py`,
   but later modules (downloader, capturer) must raise errors carrying an exit code without
   importing the CLI. `errors.ExitCode` is the single definition; `cli.py` only maps
   `HarvesterError.exit_code` to the process status.
2. **`doctor` checks live in `doctor.py`** (§3.1, §6). Keeps `cli.py` to argument handling.
   Like 1, this adds a module §6 does not list.
3. **`doctor` also validates the config file** (§3.1). The task brief lists Python, FFmpeg and
   Playwright; §3.1 adds config validity, so all four are checked. Exit 0 only if all pass.
4. **Unparseable FFmpeg versions fail `doctor`** (§3.9). Git snapshot builds print
   `version N-12345-g...`; without a version number ">= 6.0" cannot be confirmed, so the check
   fails and says so rather than guessing.
5. **`get --reencode` is accepted** (§3.1, §3.5). The §3.1 synopsis omits it, but §3.5 says the
   user "explicitly specifies `--reencode`", so it is a `get` flag.
6. **Logging flags `-v`/`-vv`/`--log-json` are global** (§3.8) and accepted before or after the
   subcommand.
7. **Unimplemented commands exit 1** (task brief). `get` and `login` still parse arguments and
   load the config first, so usage and config errors already return 2.
8. **Config precedence**: built-in defaults < `[defaults]` < matching `[sites."<domain>"]` < CLI
   flags (§3.8). A site key matches the host and its subdomains (`example.com` matches
   `www.example.com`); the longest matching key wins.
9. **Config validation is strict** (§3.1 "設定檔是否有效"). Unknown keys, unknown top-level
   tables and wrong types are errors (exit 2), so typos are reported by `doctor` instead of being
   ignored. Keys are those in the §3.8 example. Built-in defaults: `capture_timeout_s = 45`
   (§3.2), `concurrency_per_host = 4` (§3.5), `headless = true` (§3.2), `cache_ttl_s = 600`
   (§3.3, v2 only), `output_dir = "."` (the §3.8 `~/Videos` is read as an example, not a
   default).
10. **Config path**: macOS always uses `~/Library/Application Support/...` and ignores
    `XDG_CONFIG_HOME`; elsewhere `$XDG_CONFIG_HOME` (absolute values only, per the XDG spec) or
    `~/.config` (§3.8).
11. **Redaction goes further than §3.8's minimum.** Besides query strings, URL userinfo and
    fragments are removed (both can carry tokens); the port is kept as part of the host. Header
    names containing `auth`, `token`, `secret`, `key`, `session`, `signature`, `csrf`/`xsrf`,
    `password` or `credential` are treated as custom auth headers even when no template declared
    them; URL-valued headers (`Referer`, `Origin`, `Location`, `Content-Location`) are redacted
    as URLs. Known limitation: §3.8 keeps the URL path, so a CDN that signs URLs in the *path*
    will still have that token logged; revisit when a real site needs it.
12. **Fixture hostnames** (§7 "憑證轉送"). The two fixture hosts (`origin`, `cdn`) listen on
    different `127.0.0.1` ports and also answer to the virtual names `origin.test` / `cdn.test`
    via the `Host` header. Cookies ignore ports (RFC 6265), so M2's cookie-isolation tests must
    use the virtual names with a resolver override (e.g. Chromium `--host-resolver-rules`).
13. **Extra fixture samples.** Besides the M0 list, the fixtures include a Widevine-`KEYFORMAT`
    playlist and a discontinuity sample whose middle period has different parameters, because
    the §7 "不支援內容" and "不連續區段" cases need them. Both DRM samples are playlist text
    only; nothing decrypts or handles DRM.
14. **Runtime dependencies** are `playwright` and `aiohttp` (§5). The HLS parser is chosen in
    M1.

## M1

15. **Upstream transport in direct mode** (§3.3). With no capture there is no
    `BrowserContext`, so `get --playlist` uses a standalone Playwright `APIRequestContext`
    (same API and RFC 6265 cookie jar, no browser binary needed). M2 swaps in
    `BrowserContext.request`. Playwright has one timeout per request, so the §3.5 10 s
    connect / 30 s read pair is a single `request_timeout_s = 30` per attempt.
16. **Own HLS parser instead of the `m3u8` package** (§3.4, §5 "或同等 HLS parser"). Rejecting
    unsupported URI tags needs to see every tag, which `m3u8` silently drops, and a typed
    parser keeps `mypy --strict` meaningful.
17. **`get --playlist`** (§8 M1 "直接給清單 URL"). Marks `PAGE_URL` as a playlist URL and skips
    capture. Without a page there is no title, so the file name comes from the URL path
    (generic names such as `index`/`master` fall back to the parent directory, then `video`).
18. **Exit code for non-HLS responses** (§3.8). A response that is not a valid playlist
    (no `#EXTM3U`, broken tags) is an upstream error, exit 5.
19. **Unsupported tags** (§3.4). Besides DRM keys (`SAMPLE-AES*`, non-identity `KEYFORMAT`,
    in `EXT-X-KEY` and `EXT-X-SESSION-KEY`), `EXT-X-DEFINE`, `EXT-X-SKIP`, `EXT-X-GAP`,
    `EXT-X-I-FRAMES-ONLY` and unknown tags with a `URI=` attribute exit 6. Low-latency parts,
    I-frame/image streams, session data and content steering are ignored: a finished
    playlist lists full segments, so nothing is missing.
20. **Quality labels** (§3.1). `<height>p`, or `<height>p@<kbps>k` when heights repeat, or
    `<kbps>k` without `RESOLUTION`; `720` also matches `720p`. No `--quality` picks the
    highest bandwidth. A media playlist has one quality, so `--quality` is ignored there with
    a warning.
21. **Audio rendition** (§3.4). The variant's `AUDIO` group's `DEFAULT=YES` rendition with a
    URI, else its first with a URI; a group without URIs means audio is muxed. Subtitle
    renditions are not downloaded yet (warning only); WebVTT needs a conversion step.
22. **Credential refresh without capture** (§3.5 item 4). Re-requesting the media playlist is
    implemented; when that also gets 401/403 the job stops with exit 5, because re-capture
    needs the M2 capture layer. The 3-refresh limit counts every refresh in the job.
23. **Resume edge cases** (§3.5 item 5). `--resume` without a `job.json` warns and starts
    fresh. A work directory from another source (redacted URL differs) or quality exits 2;
    a playlist that no longer aligns (segment count, sequence or duration) exits 5. An
    existing work directory without `--resume` prompts `[r]esume/[s]tart over/[a]bort` only
    on a TTY without `--non-interactive`; otherwise it exits 2.
24. **Output files** (§3.2 item 7, §3.5). An existing file is never overwritten: derived
    names get ` (n)`, an explicit existing `--output` exits 2. Containers: `.mp4` (default),
    `.m4v`, `.mov`, `.mkv`. The hidden temporary output is created 0600 and gets the normal
    umask mode only after verification, just before the rename.
25. **`--reencode`** (§3.5 "第一個主要區段"). Read as the longest period (earliest on ties).
    Every period is re-encoded, not just the differing ones, so the concat step can still
    use `-c copy` with identical codec parameters. Only H.264/AAC references are supported;
    others exit 7.
26. **Ctrl-C exits 130** (`ExitCode.INTERRUPTED`). §3.8 has no code for it; 130 is the shell
    convention and the work directory is kept for `--resume`.
27. **New modules** (§6): `remux.py` (FFmpeg, periods, verification) and `filenames.py` keep
    `downloader.py` to fetching and job state.
28. **Headers for hosts without a template** (§3.3). `User-Agent` is the honest
    `hls-harvester/<version>` (no browser impersonation); `Referer` follows the browser
    default `strict-origin-when-cross-origin` from the referring playlist.
