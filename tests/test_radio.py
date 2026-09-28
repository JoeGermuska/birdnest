"""The Birds radio sequencer (radio.py) and its JSON endpoints."""
import random

import pytest


@pytest.fixture(scope='module')
def radio(app_module):
    import radio
    return radio


def recs(picks):
    return [p['rec'] for p in picks]


def test_sequence_has_no_repeats_and_keeps_artists_apart(radio):
    cat = radio.catalog()
    for seed in range(5):
        start = radio.random_start(random.Random(seed))
        picks = radio.sequence(start, 30, 0.4, seed)
        assert len(picks) == 30
        assert len(set(recs(picks))) == 30
        for i, p in enumerate(picks):
            recent = {a for q in picks[max(0, i - radio.ARTIST_COOLDOWN):i] for a in cat.rec_artists[q['rec']]}
            assert not recent & set(cat.rec_artists[p['rec']]), 'no artist within the cooldown'
        assert all(p['reason'] for p in picks)


def test_same_seed_same_set(radio):
    start = radio.random_start(random.Random(3))
    assert recs(radio.sequence(start, 15, 0.3, 7)) == recs(radio.sequence(start, 15, 0.3, 7))


def test_continuing_leaves_out_what_was_played(radio):
    first = radio.sequence(radio.random_start(random.Random(1)), 10, 0.3, 1)
    more = radio.sequence(first[-1]['rec'], 10, 0.3, 2, played=recs(first))
    assert len(more) == 10
    assert not set(recs(more)) & set(recs(first))


def test_genre_radio_leans_toward_the_genre(radio):
    genre = 'jazz funk'
    in_genre = set(radio.genre_recs(genre))
    hits = total = 0
    for seed in range(4):
        picks = radio.sequence(radio.start_for_genre(genre, random.Random(seed)), 15, 0.3, seed, genre=genre)
        total += len(picks)
        hits += sum(p['rec'] in in_genre for p in picks)
    assert hits / total > 0.5


def test_set_and_more_endpoints(app_module):
    client = app_module.app.test_client()
    s = client.get('/radio/set?show=2021-03-04&n=8&seed=4').get_json()
    assert s['label'] == 'the show of March 4, 2021' and len(s['picks']) == 8
    ids = [p['spotify_id'] for p in s['picks']]
    assert all('data-name=' in p['html'] for p in s['picks'])
    more = client.post('/radio/more', json={'after': ids[-1], 'played': ids, 'n': 5, 'seed': 1}).get_json()
    assert len(more['picks']) == 5 and not {p['spotify_id'] for p in more['picks']} & set(ids)
    g = client.get('/radio/set?genre=shoegaze&n=5').get_json()
    assert g['genre'] == 'shoegaze' and len(g['picks']) == 5


def test_show_radio_leans_toward_that_night(radio, app_module):
    import factoids
    h = factoids.get_history()
    pid = h.pid_by_date[__import__('datetime').date(2021, 3, 4)]
    recs, reason = radio.lean_recs(('show', pid))
    starts = {radio.start_for_show(pid, random.Random(seed)) for seed in range(6)}
    assert len(starts) > 1, 'not always the first track'
    assert starts <= set(radio.catalog().show_recs[pid])
    hits = total = 0
    for seed in range(4):
        picks = radio.sequence(radio.start_for_show(pid, random.Random(seed)), 15, 0.3, seed, lean=('show', pid))
        total += len(picks)
        hits += sum(p['rec'] in recs for p in picks)
    assert hits / total > 0.6
    assert 'March 4, 2021' in reason


def test_artist_radio_leans_toward_the_artists_circle(radio, app_module):
    import factoids
    h = factoids.get_history()
    aid = next(a for a in h.artists.values() if a['name'] == 'Grateful Dead')['artist_id']
    recs, _ = radio.lean_recs(('artist', aid))
    picks = radio.sequence(radio.start_for_artist(aid), 15, 0.3, 1, lean=('artist', aid))
    assert sum(p['rec'] in recs for p in picks) / len(picks) > 0.4


def test_lean_goes_round_trip_through_the_endpoints(app_module):
    client = app_module.app.test_client()
    s = client.get('/radio/set?show=2021-03-04&n=6&seed=2').get_json()
    assert s['lean'][0] == 'show'
    ids = [p['spotify_id'] for p in s['picks']]
    more = client.post('/radio/more', json={'after': ids[-1], 'played': ids, 'n': 4, 'seed': 3, 'lean': s['lean']}).get_json()
    assert len(more['picks']) == 4
    bad = client.post('/radio/more', json={'after': ids[-1], 'played': ids, 'n': 2, 'lean': ['show', -1]}).get_json()
    assert len(bad['picks']) == 2, 'an unknown lean is ignored, not an error'
