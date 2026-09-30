"""Shared fixtures: the app running on a local port with Spotify configured, and a logged-in browser page
whose Spotify is tests/fake_spotify.js.

Needs pytest and playwright (pip install -r requirements-dev.txt) and a Chromium: set PLAYWRIGHT_CHROMIUM to its
path, or let Playwright find its own (`playwright install chromium`). Browser tests are skipped without them.
"""
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # the app opens birdnest.db relative to the working directory
os.environ.setdefault('FLASK_SECRET_KEY', 'test-secret')
os.environ.setdefault('SPOTIPY_CLIENT_ID', 'test-client')
os.environ.setdefault('SPOTIPY_CLIENT_SECRET', 'test-secret')

FAKE_SPOTIFY = (Path(__file__).parent / 'fake_spotify.js').read_text()
FAKE_EMBED = (Path(__file__).parent / 'fake_spotify_embed.js').read_text()


@pytest.fixture(scope='session')
def app_module():
    import app
    return app


@pytest.fixture(scope='session')
def server(app_module):
    from werkzeug.serving import make_server
    srv = make_server('127.0.0.1', 0, app_module.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope='session')
def browser():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        path = os.environ.get('PLAYWRIGHT_CHROMIUM') or ('/opt/pw-browsers/chromium' if os.path.exists('/opt/pw-browsers/chromium') else None)
        try:
            b = p.chromium.launch(executable_path=path) if path else p.chromium.launch()
        except Exception as e:
            pytest.skip(f"no Chromium for Playwright: {e}")
        yield b
        b.close()


def session_cookie(app_module):
    """A signed Flask session cookie holding a Spotify token with all the radio's scopes."""
    serializer = app_module.app.session_interface.get_signing_serializer(app_module.app)
    return serializer.dumps({'token_info': {'access_token': 'test-token', 'refresh_token': 'test-refresh',
                                            'expires_at': int(time.time()) + 3600, 'expires_in': 3600,
                                            'token_type': 'Bearer', 'scope': app_module.SPOTIFY_SCOPES}})


def _page(browser, server, cookie=None):
    """A page with the fake Spotify (SDK and Web API) and fake Spotify embed standing in for the real ones; other
    outside requests are refused, and noted in page.outside."""
    ctx = browser.new_context(viewport={'width': 1200, 'height': 900})
    if cookie:
        ctx.add_cookies([{'name': 'session', 'value': cookie, 'domain': '127.0.0.1', 'path': '/'}])
    pg = ctx.new_page()
    pg.errors, pg.outside = [], []
    pg.on('pageerror', lambda e: pg.errors.append(str(e)))

    def route(r, _req=None):
        url = r.request.url
        if url.startswith('https://sdk.scdn.co/spotify-player.js'):
            return r.fulfill(body=FAKE_SPOTIFY, content_type='application/javascript')
        if url.startswith('https://open.spotify.com/embed/iframe-api/v1'):
            return r.fulfill(body=FAKE_EMBED, content_type='application/javascript')
        if url.startswith(server):
            return r.continue_()
        pg.outside.append(url)
        return r.abort()
    pg.route('**/*', route)
    return ctx, pg


@pytest.fixture
def page(browser, server, app_module):
    """A logged-in page: the full player, on the Web Playback SDK."""
    ctx, pg = _page(browser, server, session_cookie(app_module))
    yield pg
    ctx.close()


@pytest.fixture
def guest_page(browser, server):
    """A page for someone not logged in: the player in Spotify's embed."""
    ctx, pg = _page(browser, server)
    yield pg
    ctx.close()
