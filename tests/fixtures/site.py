"""Test web site for the capture layer (SDD §7, milestone M2).

:func:`install_site` registers pages on a :class:`~tests.fixtures.server.FixtureServer`.
Pages live on the origin host (``127.0.0.1``); media comes from the CDN host
(``localhost``). A small script (``/static/player.js``) imitates an HLS player: it fetches
the playlist, follows a master to its first variant and audio rendition, and fetches the
init section and the first two segments. Nothing is loaded from the internet.

Pages (all on the origin):

``/watch/vod``             play button -> 2 s fMP4 VOD; ``og:title`` "Test Clip: Part 1/2"
``/watch/preload``         playlist preloaded on page load from an extension-less URL served
                           as ``text/plain``; play reuses it. Also loads an MP4 and a TS
                           segment directly (must not become candidates).
``/watch/popup``           first click opens a popup and navigates the page to the CDN;
                           later clicks play
``/watch/iframe``          the player and its button live in an ``<iframe>``
``/watch/multi``           play loads a 3-quality master *and* an unrelated trailer
``/watch/multi-trailer-first``  the trailer plays on load, the master on click
``/watch/blob``            the playlist is decoded in the page and handed over as ``blob:``
``/watch/nothing``         a button that never plays anything
``/watch/drm``             play loads a SAMPLE-AES playlist
``/members/watch``         needs the ``session`` cookie from ``/login``; otherwise a login form
``/watch/creds?mode=...``  sets an origin cookie and fetches an origin master with
                           ``Authorization``; ``mode=direct`` refers to the CDN media playlist
                           directly, ``mode=redirect`` through an origin URL that 302s to it
``/watch/long``            600-segment VOD on the CDN signed with a per-page-load token
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

from aiohttp import web

from tests.fixtures.media import HlsSamples
from tests.fixtures.server import CDN, ORIGIN, FixtureServer

PAGE_TOKEN = "PAGE-AUTH-SECRET"
ORIGIN_COOKIE = "ORIGIN-COOKIE-SECRET"
LOGIN_COOKIE = "LOGIN-SESSION-SECRET"

PLAYER_JS = r"""
async function getText(url, opts) {
  const r = await fetch(url, Object.assign({credentials: 'include'}, opts || {}));
  return {text: await r.text(), url: r.url};
}
function lines(t) { return t.split(/\r?\n/).map(s => s.trim()).filter(Boolean); }
async function loadMedia(url, text) {
  let n = 0;
  for (const l of lines(text)) {
    const map = l.match(/^#EXT-X-MAP:URI="([^"]+)"/);
    if (map) await fetch(new URL(map[1], url), {credentials: 'include'});
    const key = l.match(/^#EXT-X-KEY:.*URI="([^"]+)"/);
    if (key && key[1].startsWith('http')) {
      await fetch(new URL(key[1], url), {credentials: 'include'});
    }
    if (!l.startsWith('#')) {
      await fetch(new URL(l, url), {credentials: 'include'}).then(r => r.arrayBuffer());
      if (++n >= 2) break;
    }
  }
}
async function playHls(src, opts) {
  opts = opts || {};
  const init = opts.auth ? {headers: {Authorization: opts.auth}} : {};
  const pl = opts.preloaded || await getText(src, init);
  if (pl.text.includes('#EXT-X-STREAM-INF')) {
    const ls = lines(pl.text);
    let variant = null, audio = null;
    for (let i = 0; i < ls.length; i++) {
      const a = ls[i].match(/^#EXT-X-MEDIA:TYPE=AUDIO.*URI="([^"]+)"/);
      if (a && !audio) audio = a[1];
      if (ls[i].startsWith('#EXT-X-STREAM-INF') && !variant) variant = ls[i + 1];
    }
    const v = await getText(new URL(variant, pl.url).href);
    await loadMedia(v.url, v.text);
    if (audio) {
      const au = await getText(new URL(audio, pl.url).href);
      await loadMedia(au.url, au.text);
    }
  } else {
    await loadMedia(pl.url, pl.text);
  }
  document.body.dataset.playing = (document.body.dataset.playing || '') + '1';
}
"""


def _page(title: str, body: str, script: str = "", og_title: str | None = None) -> str:
    og = f'<meta property="og:title" content="{og_title}">' if og_title else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>{og}
<script src="/static/player.js"></script></head>
<body>{body}
<script>{script}</script>
</body></html>"""


PLAY_BUTTON = '<h1>{h1}</h1><button class="play" id="play">Play</button>'


@dataclass
class Site:
    """Handles and counters of the installed test site."""

    server: FixtureServer
    samples: HlsSamples
    hijacks_left: int = 1
    long_tokens: list[int] = field(default_factory=list)

    def page(self, path: str) -> str:
        return self.server.url(path, ORIGIN)

    def cdn(self, path: str) -> str:
        return self.server.url(path, CDN)

    def media(self, name: str) -> str:
        return self.server.media_url(self.samples.playlist(name), CDN)


def _html(text: str, **headers: str) -> web.Response:
    return web.Response(text=text, content_type="text/html", headers=headers)


def install_site(server: FixtureServer, samples: HlsSamples) -> Site:
    site = Site(server, samples)
    add = server.add_route

    add("/static/player.js", PLAYER_JS, host=ORIGIN, content_type="text/javascript")

    def on_click(js: str) -> str:
        return f"document.getElementById('play').addEventListener('click', async () => {{{js}}});"

    vod = site.media("vod_fmp4")

    # --- /watch/vod
    add(
        "/watch/vod",
        _page(
            "Video page",
            PLAY_BUTTON.format(h1="Heading"),
            on_click(f"await playHls({json.dumps(vod)});"),
            og_title="Test Clip: Part 1/2",
        ),
        host=ORIGIN,
        content_type="text/html",
    )

    # --- /watch/preload: extension-less playlist as text/plain, preloaded on load
    vod_text = (samples.root / samples.playlist("vod_fmp4")).read_text()
    absolute = "\n".join(
        line.replace('URI="init.mp4"', f'URI="{site.cdn("/media/vod_fmp4/init.mp4")}"')
        if line.startswith("#EXT-X-MAP")
        else (site.cdn(f"/media/vod_fmp4/{line}") if line and not line.startswith("#") else line)
        for line in vod_text.splitlines()
    )
    add("/hls/abc123", absolute + "\n", host=ORIGIN, content_type="text/plain")
    preload_js = f"""
      window.preloaded = getText('/hls/abc123');
      fetch({json.dumps(site.cdn("/media/vod_fmp4/init.mp4"))}).then(r => r.arrayBuffer());
      fetch({json.dumps(site.cdn("/media/vod_long/seg_000.ts"))}).then(r => r.arrayBuffer());
      {on_click("await playHls(null, {preloaded: await window.preloaded});")}
    """
    add(
        "/watch/preload",
        _page("Preload", PLAY_BUTTON.format(h1="Preloaded"), preload_js),
        host=ORIGIN,
        content_type="text/html",
    )

    # --- /watch/popup: the first click hijacks the page
    async def hijack(_: web.Request) -> web.Response:
        hijack_now = site.hijacks_left > 0
        site.hijacks_left -= 1
        return web.json_response({"hijack": hijack_now})

    add("/api/hijack", hijack, host=ORIGIN)
    for path in ("/ad/popup", "/ad/landing"):
        add(
            path,
            "<html><body><h1>Advertisement</h1></body></html>",
            host=CDN,
            content_type="text/html",
        )
    popup_js = on_click(
        f"""
        const r = await fetch('/api/hijack').then(r => r.json());
        if (r.hijack) {{
          window.open({json.dumps(site.cdn("/ad/popup"))});
          location.href = {json.dumps(site.cdn("/ad/landing"))};
          return;
        }}
        await playHls({json.dumps(vod)});
        """
    )
    add(
        "/watch/popup",
        _page("Popup", PLAY_BUTTON.format(h1="Popup"), popup_js),
        host=ORIGIN,
        content_type="text/html",
    )

    # --- /watch/iframe
    add(
        "/frame/player",
        _page(
            "Frame",
            PLAY_BUTTON.format(h1="In frame"),
            on_click(f"await playHls({json.dumps(vod)});"),
        ),
        host=ORIGIN,
        content_type="text/html",
    )
    add(
        "/watch/iframe",
        _page(
            "Iframe page",
            '<h1>Outer</h1><iframe src="/frame/player" width="400" height="300"></iframe>',
        ),
        host=ORIGIN,
        content_type="text/html",
    )

    # --- /watch/multi and /watch/multi-trailer-first
    master = site.media("master")
    trailer = site.media("vod_long")
    add(
        "/watch/multi",
        _page(
            "Multi",
            PLAY_BUTTON.format(h1="Two players"),
            on_click(
                f"await Promise.all([playHls({json.dumps(master)}), "
                f"playHls({json.dumps(trailer)})]);"
            ),
        ),
        host=ORIGIN,
        content_type="text/html",
    )
    add(
        "/watch/multi-trailer-first",
        _page(
            "Multi trailer first",
            PLAY_BUTTON.format(h1="Trailer autoplays"),
            f"playHls({json.dumps(trailer)});" + on_click(f"await playHls({json.dumps(master)});"),
        ),
        host=ORIGIN,
        content_type="text/html",
    )

    # --- /watch/blob: playlist decoded in the page, never fetched as HLS
    long_text = (samples.root / samples.playlist("vod_long")).read_text()
    obfuscated = base64.b64encode(
        "\n".join(
            site.cdn(f"/media/vod_long/{line}") if line and not line.startswith("#") else line
            for line in long_text.splitlines()
        ).encode()
    ).decode()
    add("/api/obfuscated", obfuscated, host=ORIGIN, content_type="text/plain")
    blob_js = on_click(
        """
        const text = atob(await fetch('/api/obfuscated').then(r => r.text()));
        const type = 'application/vnd.apple.mpegurl';
        const blobUrl = URL.createObjectURL(new Blob([text], {type}));
        await loadMedia(blobUrl, text);
        """
    )
    add(
        "/watch/blob",
        _page("Blob", PLAY_BUTTON.format(h1="Blob"), blob_js),
        host=ORIGIN,
        content_type="text/html",
    )

    # --- /watch/nothing and /watch/drm
    add(
        "/watch/nothing",
        _page("Nothing", PLAY_BUTTON.format(h1="Broken player"), on_click("")),
        host=ORIGIN,
        content_type="text/html",
    )
    add(
        "/watch/drm",
        _page(
            "DRM",
            PLAY_BUTTON.format(h1="DRM"),
            on_click(f"await getText({json.dumps(site.media('sample_aes'))});"),
        ),
        host=ORIGIN,
        content_type="text/html",
    )

    # --- login-protected page
    login_form = _page(
        "Sign in",
        '<h1>Please sign in</h1><form method="post" action="/login">'
        '<input name="user"><input type="password" name="password">'
        '<button type="submit" id="submit">Sign in</button></form>',
    )

    async def members(request: web.Request) -> web.Response:
        if request.cookies.get("session") != LOGIN_COOKIE:
            return _html(login_form)
        return _html(
            _page(
                "Members",
                PLAY_BUTTON.format(h1="Members only"),
                on_click(f"await playHls({json.dumps(vod)});"),
                og_title="Members Clip",
            )
        )

    async def login(request: web.Request) -> web.StreamResponse:
        if request.method == "POST":
            form = await request.post()
            if form.get("password") == "hunter2":
                response = web.HTTPFound("/members/watch")
                response.set_cookie("session", LOGIN_COOKIE, path="/", httponly=True)
                raise response
        return _html(login_form)

    add("/members/watch", members, host=ORIGIN)
    add("/login", login, host=ORIGIN)

    # --- credential forwarding
    async def creds_page(request: web.Request) -> web.Response:
        mode = request.query.get("mode", "direct")
        js = on_click(
            f"await playHls('/secure/master.m3u8?mode={mode}&sig=QUERY-SECRET', "
            f"{{auth: 'Bearer {PAGE_TOKEN}'}});"
        )
        response = _html(_page("Credentials", PLAY_BUTTON.format(h1="Creds"), js))
        response.set_cookie("sid", ORIGIN_COOKIE, path="/")
        return response

    cdn_variant = site.cdn("/media/vod_fmp4/index.m3u8?cdn_sig=CDN-QUERY-SECRET")

    async def secure_master(request: web.Request) -> web.Response:
        if (
            request.headers.get("Authorization") != f"Bearer {PAGE_TOKEN}"
            or request.cookies.get("sid") != ORIGIN_COOKIE
        ):
            return web.Response(status=403, text="forbidden")
        if request.query.get("mode") == "redirect":
            variant = "/secure/variant.m3u8?sig=QUERY-SECRET-2"
        else:
            variant = cdn_variant
        text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=320x180\n" + variant + "\n"
        return web.Response(text=text, content_type="application/vnd.apple.mpegurl")

    async def secure_variant(_: web.Request) -> web.StreamResponse:
        raise web.HTTPFound(cdn_variant)

    add("/watch/creds", creds_page, host=ORIGIN)
    add("/secure/master.m3u8", secure_master, host=ORIGIN)
    add("/secure/variant.m3u8", secure_variant, host=ORIGIN)

    # --- /watch/long: 600 segments signed per page load
    long600 = (samples.root / samples.playlist("vod_600")).read_text()

    async def long_page(_: web.Request) -> web.Response:
        site.long_tokens.append(len(site.long_tokens) + 1)
        token = site.long_tokens[-1]
        src = site.cdn(f"/signed/long.m3u8?t={token}")
        return _html(
            _page(
                "Long film",
                PLAY_BUTTON.format(h1="Long"),
                on_click(f"await playHls({json.dumps(src)});"),
            )
        )

    async def long_playlist(request: web.Request) -> web.Response:
        token = request.query.get("t", "")
        text = "\n".join(
            site.cdn(f"/media/vod_600/{line}?t={token}")
            if line and not line.startswith("#")
            else line
            for line in long600.splitlines()
        )
        return web.Response(text=text + "\n", content_type="application/vnd.apple.mpegurl")

    add("/watch/long", long_page, host=ORIGIN)
    add("/signed/long.m3u8", long_playlist, host=CDN)
    return site
