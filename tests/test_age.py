"""Song age: release years per play (MusicBrainz, else Spotify), the show page's Era row, and /rankings/age."""
from datetime import date

import pytest

import factoids


@pytest.fixture(scope='module')
def history(app_module):
    return factoids.get_history()


def test_release_year_rejects_placeholders_and_the_future(history):
    h = history
    saved = dict(h.track_released)
    t = next(iter(h.tracks))
    try:
        for released, on, expected in [('1900-01-01', None, None), ('0000', None, None), ('1928-04', None, 1928),
                                       ('2026-10-02', date(2026, 10, 1), None), ('2026-10-01', date(2026, 10, 1), 2026)]:
            h.track_released[t] = (released, 'spotify')
            assert h.release_year(t, on) == expected, released
    finally:
        h.track_released.clear()
        h.track_released.update(saved)


def test_show_age_adds_up(history):
    pid = max(history.show_tracks, key=history.show_dates.get)
    age = history.show_age(pid)
    assert age and age['first'] <= age['median'] <= age['last'] <= history.show_dates[pid].year
    assert sum(n for _, n in age['decades']) == age['known'] <= age['tracks']


def test_age_page(app_module, history):
    A = history.age_page()
    assert A['known'] <= A['plays'] and 0 < A['mb_share'] < 1
    for row in A['spread']:  # each year's bars cover all its songs, and the median falls among them
        assert abs(sum(share for _, share, _ in row['bins']) - 1) < 1e-9 and sum(n for *_, n in row['bins']) == row['total']
        assert A['bins'][0] <= row['median'] <= row['year']
    assert A['peak'] == max(share for row in A['spread'] for _, share, _ in row['bins'])
    assert [o['released'] for o in A['oldest']] == sorted(o['released'] for o in A['oldest'])
    assert all(0 <= f['days'] <= factoids.FRESH_DAYS for f in A['fresh'])
    resp = app_module.app.test_client().get('/rankings/age')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for anchor in ('id="djs"', 'id="oldest"', 'id="fresh"'):
        assert anchor in html


def test_show_page_has_the_era_row(app_module, history):
    d = max(history.show_dates[p] for p in history.show_tracks)
    html = app_module.app.test_client().get(f'/playlist/{d}').get_data(as_text=True)
    assert 'class="era"' in html and f'/rankings/age?h={d}' in html
