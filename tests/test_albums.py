"""Album pages, the albums ranking, album grouping, and MusicBrainz album matching (enrich_mb_albums.py)."""
import pytest

import enrich_mb_albums
import factoids


@pytest.fixture(scope='module')
def history(app_module):
    return factoids.get_history()


def test_groups_cover_every_album_once(history):
    seen = [a for ids in history.album_members.values() for a in ids]
    assert sorted(seen) == sorted(history.albums)
    for canon, ids in history.album_members.items():
        assert ids[0] == canon and all(history.album_group[a] == canon for a in ids)


def test_duplicates_redirect_to_one_page(app_module, history):
    canon, ids = next((c, ids) for c, ids in history.album_members.items() if len(ids) > 1 and history.album_shows.get(c))
    client = app_module.app.test_client()
    other = history.albums[ids[1]]['spotify_id']
    resp = client.get(f'/album/{other}')
    assert resp.status_code == 302 and resp.headers['Location'].endswith(f"/album/{history.albums[canon]['spotify_id']}")
    page = client.get(f"/album/{history.albums[canon]['spotify_id']}")
    assert page.status_code == 200
    assert f"/radio?album={history.albums[canon]['spotify_id']}" in page.get_data(as_text=True)


def test_unknown_album_404s(app_module):
    assert app_module.app.test_client().get('/album/nope').status_code == 404


def test_album_plays_match_the_shows(history):
    canon = min(history.album_rank, key=history.album_rank.get)
    profile = history.album_profile(canon)
    members = set(history.album_members[canon])
    expected = sum(1 for tids in history.show_tracks.values() for t in tids if history.tracks[t]['album_id'] in members)
    assert profile['n_plays'] == expected == sum(len(r['dates']) for r in profile['tracks'])


def test_ranking_leaves_out_singles(history):
    table = history.ranking('albums')
    assert table['rows']
    for r in table['rows']:
        canon = history.album_by_spotify[r['anchor']]
        assert history.albums[canon].get('album_type') != 'single' and r['value'] >= 2


def test_show_rows_link_their_album(history):
    pid = max(history.show_tracks, key=history.show_dates.get)
    rows = history.show_rows(pid)
    assert all(r['album_page'] in history.album_by_spotify for r in rows if r['album_id'])


def test_album_radio_leans_toward_the_album(app_module, history):
    canon = min(history.album_rank, key=history.album_rank.get)
    sid = history.albums[canon]['spotify_id']
    data = app_module.app.test_client().get(f'/radio/set?album={sid}&seed=3').get_json()
    assert data['lean'] == ['album', canon]


def test_links_from_prefer_the_release_group():
    group = [{'type': 'wikidata', 'url': {'resource': 'https://www.wikidata.org/wiki/Q1'}},
             {'type': 'discogs', 'url': {'resource': 'https://www.discogs.com/master/1'}},
             {'type': 'discogs', 'url': {'resource': 'https://www.discogs.com/master/2'}}]
    release = [{'type': 'discogs', 'url': {'resource': 'https://www.discogs.com/release/9'}},
               {'type': 'streaming', 'url': {'resource': 'https://music.apple.com/us/album/1'}},
               {'type': 'purchase for download', 'url': {'resource': 'https://x.bandcamp.com/album/y'}}]
    assert enrich_mb_albums.links_from(release, group) == {
        'wikidata': 'https://www.wikidata.org/wiki/Q1', 'discogs': 'https://www.discogs.com/master/1',
        'apple': 'https://music.apple.com/us/album/1', 'bandcamp': 'https://x.bandcamp.com/album/y'}


def test_barcode_match_needs_one_release_group(monkeypatch):
    def fake(results):
        monkeypatch.setattr(enrich_mb_albums, 'get', lambda *a, **k: {'releases': results})
    rel = lambda bc, rg: {'id': f'r-{rg}', 'barcode': bc, 'release-group': {'id': rg}}
    fake([rel('0825764107235', 'a'), rel('825764107235', 'a'), rel('999', 'b')])
    assert enrich_mb_albums.barcode_match('825764107235') == ('r-a', 'a')
    fake([rel('825764107235', 'a'), rel('825764107235', 'b')])
    assert enrich_mb_albums.barcode_match('825764107235') is None
    fake([])
    assert enrich_mb_albums.barcode_match('825764107235') is None
