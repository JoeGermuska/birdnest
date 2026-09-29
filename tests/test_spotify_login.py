"""Spotify login (OAuth): the callback only accepts the `state` this browser was sent off with."""
from urllib.parse import parse_qs, urlparse

import pytest


@pytest.fixture
def exchanged(monkeypatch):
    """Codes the app tried to exchange for a token (instead of calling Spotify)."""
    from spotipy.oauth2 import SpotifyOAuth
    codes = []
    monkeypatch.setattr(SpotifyOAuth, 'get_access_token', lambda self, code, **kw: codes.append(code))
    return codes


def login(client):
    """Start a login; the state sent to Spotify."""
    resp = client.get('/spotify/login?next=/genres')
    assert resp.status_code == 302
    url = urlparse(resp.headers['Location'])
    assert url.netloc == 'accounts.spotify.com'
    return parse_qs(url.query)['state'][0]


def test_login_sends_a_fresh_state_each_time(app_module):
    client = app_module.app.test_client()
    first, second = login(client), login(client)
    assert len(first) >= 20 and first != second


def test_callback_with_the_right_state_logs_in(app_module, exchanged):
    client = app_module.app.test_client()
    state = login(client)
    resp = client.get(f'/spotify/callback?code=abc&state={state}')
    assert exchanged == ['abc']
    assert resp.headers['Location'].endswith('/genres'), 'back to where the login started'


@pytest.mark.parametrize('query', ['code=abc&state=wrong', 'code=abc', 'code=abc&state=%C3%A9t%C3%A9'])
def test_callback_with_a_wrong_or_missing_state_is_refused(app_module, exchanged, query):
    client = app_module.app.test_client()
    login(client)
    resp = client.get(f'/spotify/callback?{query}')
    assert exchanged == [], 'the code is not exchanged'
    assert resp.status_code == 302
    with client.session_transaction() as s:
        assert 'token_info' not in s
        assert 'log in again' in s['save_error']


def test_a_state_only_works_once(app_module, exchanged):
    client = app_module.app.test_client()
    state = login(client)
    client.get(f'/spotify/callback?code=abc&state={state}')
    client.get(f'/spotify/callback?code=def&state={state}')
    assert exchanged == ['abc']


def test_callback_without_a_login_started_here_is_refused(app_module, exchanged):
    """Someone else's link to our callback, with their code and state: no login started in this browser."""
    client = app_module.app.test_client()
    client.get('/spotify/callback?code=theirs&state=theirs')
    assert exchanged == []
