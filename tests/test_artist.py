"""Artist pages: Wikidata's description, home and years from MusicBrainz, social links."""
import pytest

import factoids


@pytest.fixture(scope='module')
def history(app_module):
    return factoids.get_history()


def test_artist_page_says_who_they_are(app_module, history):
    described = [a for a, about in history.artist_about.items() if about.get('description') and history.artist_shows.get(a)]
    if not described:
        pytest.skip('no artist descriptions in this database')
    aid = max(described, key=lambda a: len(history.artist_shows[a]))
    html = app_module.app.test_client().get(f"/artist/{history.artists[aid]['spotify_id']}").get_data(as_text=True)
    header = history.artist_header(aid)
    assert header['description'] in html
    for name, url in header['socials']:
        assert url in html


def test_header_years_and_socials():
    assert factoids.social_service('https://www.instagram.com/milesdavis/') == 'Instagram'
    assert factoids.social_service('https://m.facebook.com/x') == 'Facebook'
    assert factoids.social_service('https://x.com/someone') == 'X'
    assert factoids.social_service('https://myspace.com/someone') is None
    assert factoids.flag('US') == '🇺🇸' and factoids.flag(None) == ''


def test_cache_carries_the_running_code(tmp_path):
    """A cache is stamped with the code that built it (as loaded), not whatever is on disk when it's saved."""
    assert factoids._signature.__code__.co_names.count('CODE_HASH') == 1
    assert len(factoids.CODE_HASH) == 40
