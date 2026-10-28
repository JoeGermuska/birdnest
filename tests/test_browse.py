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
    resp = app_module.app.test_client().get('/shows' + query, follow_redirects=True)
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


@pytest.mark.parametrize('sort', ['most-songs', 'fewest-songs', 'oldest-music', 'most-new', 'biggest-room'])
def test_sort_by_an_attribute(app_module, history, sort):
    import app as app_mod
    _, key, biggest_first, says = app_mod.BROWSE_BY[sort]
    html = get(app_module, f'?sort={sort}')
    by_date = {str(s['date']): key(s) for s in history.browse()}
    values = [by_date[d] for d in dates(html)]
    assert values == sorted(values, reverse=biggest_first)
    assert 'class="year-head"' not in html, 'no year dividers out of date order'
    first = next(s for s in history.browse() if str(s['date']) == dates(html)[0])
    assert says(first) in html, 'tiles say the number they are sorted by'


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


def test_rooms_add_up(app_module, history):
    """Several DJs: the shows they were all in."""
    heath, joe = history.dj_by_slug['heath'], history.dj_by_slug['joe']
    both = {str(history.show_dates[p]) for p in history.dj_shows[heath] & history.dj_shows[joe]}
    html = get(app_module, '?dj=heath&dj=joe')
    assert set(dates(html)) == both and 'with Heath and Joe in the room' in html
    # each chip toggles: Heath's takes Heath out and keeps Joe
    assert re.search(r'href="/shows\?dj=joe"[^>]*aria-current', html)


def test_filter_by_genre_family(app_module, history):
    ds = set(dates(get(app_module, '?family=Jazz')))
    assert ds == {str(s['date']) for s in history.browse() if 'Jazz' in s['heavy']}
    assert 5 < len(ds) < len(history.show_tracks) / 2


def test_filters_combine_and_keep_each_other(app_module):
    html = get(app_module, '?dj=heath&family=Jazz&sort=oldest')
    assert set(dates(html)) <= set(dates(get(app_module, '?dj=heath')))
    # the other controls carry the current filters along
    assert re.search(r'href="/shows\?[^"]*sort=shuffle[^"]*"', html)
    assert all('dj=heath' in h for h in re.findall(r'href="(/shows\?[^"]*sort=shuffle[^"]*)"', html))
    assert '<input type="hidden" name="dj" value="heath">' in html, 'the sort menu keeps the room too'


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


def test_shuffle_links_carry_no_seed_and_crawlers_are_told_off(app_module):
    """A fresh seed in every link made an endless crawl space; links are plain now and the seed is only accepted."""
    html = get(app_module, '?sort=shuffle')
    assert 'seed=' not in html
    assert re.search(r'href="/shows\?sort=shuffle" rel="nofollow"', html)
    robots = app_module.app.test_client().get('/robots.txt').get_data(as_text=True)
    for bot in ('Amazonbot', 'GPTBot'):
        assert f'User-agent: {bot}\nDisallow: /\n' in robots
    assert 'Disallow: /*seed=' in robots and 'Disallow: /*sort=shuffle' in robots


def test_one_url_per_view(app_module):
    client = app_module.app.test_client()
    junk = client.get('/shows?sort=sideways&dj=nobody&family=Polka&x=1')
    assert (junk.status_code, junk.headers['Location']) == (301, '/shows')
    assert client.get('/shows?dj=rob&dj=joe').headers['Location'] == '/shows?dj=joe&dj=rob'
    assert client.get('/shows?dj=joe&dj=rob').status_code == 200


def test_crawlers_that_ignore_robots_are_refused(app_module):
    client = app_module.app.test_client()
    bot = {'User-Agent': 'Mozilla/5.0 (compatible; meta-externalagent/1.1)'}
    assert client.get('/shows', headers=bot).status_code == 403
    assert client.get('/robots.txt', headers=bot).status_code == 200


def test_origin_lock(app_module, monkeypatch):
    client = app_module.app.test_client()
    assert client.get('/shows').status_code == 200  # no secret set: no lock
    monkeypatch.setenv('ORIGIN_SECRET', 'swordfish')
    assert client.get('/shows').status_code == 403
    assert client.get('/shows', headers={'X-Origin-Secret': 'wrong'}).status_code == 403
    assert client.get('/shows', headers={'X-Origin-Secret': 'swordfish'}).status_code == 200
    assert client.get('/robots.txt').status_code == 200
