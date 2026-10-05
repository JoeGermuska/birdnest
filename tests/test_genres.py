"""Loading artists must reuse genre rows, not make a new one per artist."""
import models


def _artist(session, spotify_id, genres):
    return models.Artist.get_or_create(session, spotify_id, {
        'name': spotify_id, 'external_urls': {}, 'genres': genres})


def test_genres_shared_across_artists():
    session = models.get_session(':memory:')
    session.add(_artist(session, 'a', ['jazz', 'funk', 'jazz']))
    session.add(_artist(session, 'b', ['jazz']))
    session.add(_artist(session, 'c', ['funk', 'soul']))
    session.commit()
    names = sorted(g.name for g in session.query(models.Genre))
    assert names == ['funk', 'jazz', 'soul']
    assert sorted(session.query(models.Artist).filter_by(spotify_id='a').one().genres) == ['funk', 'jazz']
