"""The browse page (/shows): every show, sorted and filtered on the server, so each view is a plain link."""
import re

import pytest


@pytest.fixture(scope='module')
def history(app_module):
    import factoids
    return factoids.get_history()


def dates(html):
    return re.findall(r'class="playlist-tile" href="/playlist/(\d{4}-\d\d-\d\d)"', html)


def get(app_module, query=''):
    resp = app_module.app.test_client().get('/shows' + query)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_every_show_newest_first_in_years(app_module, history):
    html = get(app_module)
    ds = dates(html)
    assert len(ds) == len(history.show_tracks)
    assert ds == sorted(ds, reverse=True)
    newest = ds[0][:4]
    n = sum(1 for d in ds if d.startswith(newest))
    assert re.search(rf'<h2 class="year-head"[^>]*>{newest} <span class="muted">{n} shows', html)


def test_oldest_first(app_module):
    ds = dates(get(app_module, '?sort=oldest'))
    assert ds == sorted(ds)


def test_most_new_for_its_time(app_module, history):
    html = get(app_module, '?sort=new')
    ds = dates(html)
    lift = {str(s['date']): s['lift'] for s in history.browse()}
    assert [lift[d] for d in ds] == sorted((lift[d] for d in ds), reverse=True)
    assert 'class="year-head"' not in html, 'no year dividers out of date order'
    assert '· novelty ' in html, 'tiles say how new'


def test_shuffle_is_repeatable_by_seed(app_module):
    a, b = dates(get(app_module, '?sort=shuffle&seed=4')), dates(get(app_module, '?sort=shuffle&seed=4'))
    assert a == b and a != sorted(a, reverse=True)
    assert dates(get(app_module, '?sort=shuffle&seed=5')) != a


def test_filter_by_who_was_in_the_room(app_module, history):
    html = get(app_module, '?dj=heath')
    ds = set(dates(html))
    heath = history.dj_by_slug['heath']
    assert ds == {str(history.show_dates[p]) for p in history.dj_shows[heath]}
    assert f'{len(ds)} shows' in html


def test_filter_by_genre_family(app_module, history):
    ds = set(dates(get(app_module, '?family=Jazz')))
    assert ds == {str(s['date']) for s in history.browse() if 'Jazz' in s['leans']}
    assert 10 < len(ds) < len(history.show_tracks) / 2


def test_filters_combine_and_keep_each_other(app_module):
    html = get(app_module, '?dj=heath&family=Jazz&sort=oldest')
    assert set(dates(html)) <= set(dates(get(app_module, '?dj=heath')))
    # the other controls carry the current filters along
    assert re.search(r'href="/shows\?[^"]*sort=new[^"]*"', html)
    assert all('dj=heath' in h for h in re.findall(r'href="(/shows\?[^"]*sort=new[^"]*)"', html))


def test_unknown_values_are_ignored(app_module, history):
    ds = dates(get(app_module, '?sort=sideways&dj=nobody&family=Polka'))
    assert len(ds) == len(history.show_tracks)


def test_nothing_matches(app_module):
    html = get(app_module, '?dj=adrian&family=Pop')
    if not dates(html):
        assert 'No shows' in html


def test_banner_and_home_lead_here(app_module):
    client = app_module.app.test_client()
    assert 'href="/shows"' in client.get('/genres').get_data(as_text=True)
    assert 'href="/shows"' in client.get('/').get_data(as_text=True)
