"""/heath: show sizes, song lengths, six-minute songs and back-to-backs (Heath's requests), and six-minute radio."""
import pytest

import factoids


@pytest.fixture(scope='module')
def history(app_module):
    return factoids.get_history()


def test_lengths_add_up(history):
    L = history.lengths()
    assert sum(L['bins']) == L['plays'] == sum(s['n'] for s in L['shows'])
    assert L['most'][0]['n'] == L['max_n'] >= L['fewest'][0]['n']
    longest, shortest = L['longest'], L['shortest']
    assert longest == sorted(longest, key=lambda r: -r['ms']) and shortest == sorted(shortest, key=lambda r: r['ms'])
    assert all(r['ms'] >= factoids.SIX_MINUTES for r in L['near_six'])


def test_back_to_back_runs_share_an_artist(history):
    B = history.back_to_back()
    assert B['n'] == len(B['runs'])
    by_date = {history.show_dates[p]: tids for p, tids in history.show_tracks.items()}
    for r in B['runs']:
        tids = by_date[r['date']][r['start'] - 1:r['start'] - 1 + len(r['tracks'])]
        assert [history.tracks[t]['name'] for t in tids] == [t['name'] for t in r['tracks']]
        assert all(set(r['artists']) <= set(history.track_artists[t]) for t in tids)


def test_page_renders(app_module):
    resp = app_module.app.test_client().get('/heath')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for heading in ('Songs per show', 'Song lengths', 'Six-minute songs', 'Back to back', 'Year by year'):
        assert heading in html
    assert '/radio?length=long' in html


def test_six_minute_radio_leans_long(app_module):
    data = app_module.app.test_client().get('/radio/set?length=long&seed=4').get_json()
    assert data['lean'] == ['length', 'long']
    long = sum(p['ms'] >= factoids.SIX_MINUTES for p in data['picks'])
    assert long >= len(data['picks']) * 0.6


def test_linked_from_heaths_dj_page_only(app_module):
    client = app_module.app.test_client()
    assert 'href="/heath"' in client.get('/dj/heath').get_data(as_text=True)
    assert 'href="/heath"' not in client.get('/dj/joe').get_data(as_text=True)
