"""The linking to-do lists (/todo/...) and the 🤓 menu that leads to them."""
import re

import pytest

import app as app_mod


@pytest.fixture(scope='module')
def client(app_module):
    return app_module.app.test_client()


@pytest.mark.parametrize('page', app_mod.TODO_PAGES, ids=lambda p: p['endpoint'])
def test_each_list_renders_with_its_check(client, page):
    with app_mod.app.test_request_context():
        url = app_mod.url_for(page['endpoint'])
    resp = client.get(url)
    if resp.status_code == 404:
        pytest.skip(f"no candidate table for {url} in this database")
    html = resp.get_data(as_text=True)
    assert f'data-check="{page["check"]}"' in html
    clips = re.findall(r'data-copy="([^"]+)"', html)
    assert clips and all(re.fullmatch(r'(https://open\.spotify\.com/album/)?[0-9A-Za-z]{22}', c) for c in clips)


def test_menu_in_the_banner_links_every_list(client):
    html = client.get('/heath').get_data(as_text=True)
    menu = html[html.index('class="nerd-menu"'):html.index('id="theme-toggle"')]
    with app_mod.app.test_request_context():
        for page in app_mod.TODO_PAGES:
            assert f'href="{app_mod.url_for(page["endpoint"])}"' in menu


def test_musicbrainz_list_leaves_out_albums_already_linked(client, app_module):
    import factoids
    history = factoids.get_history()
    con = app_module.app.session.connection().connection
    linked = {history.albums[history.album_group[a]]['spotify_id']
              for a, in con.execute("select album_id from mb_album where method = 'spotify-url'")}
    html = client.get('/todo/musicbrainz/albums').get_data(as_text=True)
    rows = set(re.findall(r'<a href="/album/([0-9A-Za-z]{22})"', html))
    assert rows and not rows & linked
