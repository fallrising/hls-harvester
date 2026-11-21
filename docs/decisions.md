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
