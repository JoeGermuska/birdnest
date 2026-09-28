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
