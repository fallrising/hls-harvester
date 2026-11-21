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

## M2

29. **Fixture hostnames, revised** (§7 "憑證轉送"; supersedes the cookie note in 12). Playwright's
    `APIRequestContext` resolves names in Node, which cannot resolve `*.test` or
    `*.localhost` here, and the downloader must reach the same hosts as the browser. The
    fixture origin is therefore `127.0.0.1` and the CDN `localhost` (also bound on `::1`):
    two different cookie hosts that both Chromium and Node resolve without resolver tricks.
30. **When capture stops** (§3.2 items 2–3). After loading, the capturer waits briefly for
    preloads, then clicks play (unless no play target exists). It finishes once there is a
    candidate, a click has happened (or nothing is clickable) and no playlist or segment
    response arrived for `settle_s` (1.5 s). Without a candidate it re-clicks every
    `click_retry_s` (3 s), at most 3 clicks, within the 45 s deadline.
31. **Candidate bodies** (§3.2 item 4). Bodies are read for `.m3u8`/`.m3u` URLs, HLS MIME types,
    and XHR/fetch responses that are text, `application/octet-stream` or untyped (not HTML)
    and at most 4 MiB. Only a body starting with `#EXTM3U` that parses counts.
32. **Association with the play action** (§3.2 item 5). A video counts as started by the
    player when any of its playlist, init or segment URLs was requested after the first play
    click. That covers a playlist preloaded before the click whose segments load after it.
    Without a play target, every video counts as associated.
33. **"Playlist not observable"** (§3.2 item 4) is reported when no playlist candidate exists
    but segment-like responses (by MIME type or extension) or `blob:` responses were seen.
34. **Header templates** (§3.2 item 6). Kept headers: `User-Agent`, `Origin`, `Referer`,
    `Accept`, `Accept-Language`. Auth headers are those `redact.is_sensitive_header` flags,
    except `Cookie`. Each redirect hop is its own observed request, so auth headers stay
    with the origin they were sent to. The first request to an origin defines its template.
35. **Re-capture** (§3.5 item 4). When re-requesting the media playlist fails with 401/403, the
    page is captured again in the same browser context, non-interactively, preferring a
    video with the same fingerprint (quality labels of a master, or segment count and
    duration of a media playlist), so an earlier manual choice is not asked again within the
    job. All tracks are realigned. Re-captures count toward the 3-refresh limit.
36. **`login`** (§3.1) lives in `login.py`. It finishes when the user presses Enter in the
    terminal or closes the window; the session is snapshotted every second, so closing the
    browser still saves it. The file is written atomically with mode 0600, also when it
    replaces an existing file.
37. **Login hint** (§3.7). When capture finds nothing and the page has a password field, the
    error (exit 4) says to run `hls-harvester login` and pass `--storage-state`.
38. **`browser_executable` setting.** Optional path of a Chromium binary, for machines where
    `playwright install chromium` is not possible. Tests read `HLSH_TEST_CHROMIUM` for the same
    purpose. It changes which Chromium runs, not how it presents itself.
39. **Ambiguity listing** (§3.2 items 5, 8). Candidates are listed by short ID (`v1`, `v2`),
    quality labels, segment count, duration and origin; never by full URL.

## M3

40. **Local paths** (§3.6). Resources are `/<32 hex chars>/r<n><suffix>`: a 128-bit
    `secrets.token_hex` prefix, a per-proxy counter, and the upstream extension when it is a
    known media type (`.m3u8` for playlists). Rewritten playlists use root-relative paths, so
    they work whether the player connected as `127.0.0.1` or `localhost`.
41. **Origin handling** (§3.6). A request with an `Origin` header that is not in the new
    `player_origins` setting (default: none) is refused with 403 and gets no CORS headers. An
    allowed origin gets its own value back. VLC, IINA and FFmpeg send no `Origin`, so they
    are unaffected.
42. **Byte ranges and remapping** (§3.4, §3.6). Each child resource is keyed by where it
    first appears in its playlist (variant index, media sequence number, n-th `EXT-X-MAP` or
    `EXT-X-KEY`). A URI that repeats (byte ranges of one file) keeps one local path, so
    implicit `EXT-X-BYTERANGE` offsets still work. When credentials are refreshed, the new
    upstream URL replaces the old one at the same key, so local paths never change.
43. **Live refresh** (§3.6). The proxy re-fetches a playlist without `EXT-X-ENDLIST` when a
    player asks for it and the cached copy is older than half the target duration (at least
    0.5 s). There is no background polling. Masters and finished media playlists are fetched
    once. Segments that fell far out of the live window are forgotten (404).
44. **Credential refresh in the proxy** (§3.6, §3.5 item 4). A 401/403 on a segment re-fetches
    its playlist. A 401/403 on a playlist captures the page again (`recapture`, at most 3
    times); with `--playlist` there is no page, so the player gets 502.
45. **`--quality` with `--play`** (§3.4). Only the root master is filtered: it keeps the
    chosen variant and the `EXT-X-MEDIA` renditions of its groups, and drops I-frame and
    image streams. `--output`, `--resume` and `--reencode` are download options and exit 2
    with `--play`.
46. **Dropped tag.** `EXT-X-CONTENT-STEERING` is removed from rewritten playlists, because
    it would send the player to a steering server outside the proxy.
47. **Exit status of play mode** (§3.8 "播放正常結束"). Stopping after the idle timeout and
    Ctrl-C both exit 0. Before any URL is printed, errors use the normal codes, for example
    6 for DRM. Upstream errors while a player is connected are answered with 502 and logged.
48. **New setting** `play_idle_timeout_s` (default 600, §3.6).
