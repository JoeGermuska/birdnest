"""The music-writing index: matching pieces to artists and albums, and reading feeds. Offline, no network."""
import sqlite3

import pytest

import writing
import writing_match
from writing_match import Matcher, norm

WORDS = {'real', 'estate', 'low', 'status', 'quo', 'power'}


@pytest.fixture
def matcher():
    c = sqlite3.connect(':memory:')
    c.executescript("""
        create table artist (artist_id integer, name text, spotify_id text);
        create table album (album_id integer, name text);
        create table album_artist (album_id integer, artist_id integer);
        create table track (track_id integer, name text);
        create table track_artist (track_id integer, artist_id integer);
        create table mb_artist (artist_id integer, mbid text);
        create table artist_link (artist_id integer, source text, url text);
        insert into artist values (1, 'Takaat', null), (2, 'Real Estate', null), (3, 'Dolly Parton', 'SPOTIFYID000000000000a'),
                                  (4, 'Sleater-Kinney', null), (5, 'Powers', null), (6, 'Björk', null);
        insert into album values (10, 'Is Noise, Vol. 3 (Deluxe Edition)'), (11, 'Dolly'), (12, 'Days'), (13, 'Medulla');
        insert into album_artist values (10, 1), (11, 3), (12, 2), (13, 6);
        insert into artist_link values (4, 'bandcamp', 'https://sleaterkinney.bandcamp.com'), (1, 'bandcamp', 'https://daily.bandcamp.com/');
    """)
    return Matcher(c, words=WORDS)


def best(ms, aid):
    return next((m for m in ms if m['artist_id'] == aid), None)


def test_artist_and_album_in_the_slug_is_a_confident_match(matcher):
    m = best(matcher.match(title='TAKAAT ~ Is Noise, Vol. 3', url='https://acloserlisten.com/2026/09/27/takaat-is-noise-vol-3/'), 1)
    assert m['status'] == 'auto' and m['album_id'] == 10 and m['kind'] == 'about'
    assert 'pair_title' in m['signals'] and 'pair_slug' in m['signals']


def test_a_name_of_common_words_needs_more_than_the_title(matcher):
    assert best(matcher.match(title='A Real Estate agent on the rise'), 2)['status'] == 'review'
    assert best(matcher.match(title='A real estate agent on the rise'), 2) is None  # not written as the band is
    assert best(matcher.match(title='Real Estate: Days'), 2)['status'] == 'auto'  # with their album
    assert best(matcher.match(title='Reviews', tags=['Real Estate']), 2)['status'] == 'review'  # tag alone, ambiguous name


def test_plurals_of_common_words_count_as_common(matcher):
    assert best(matcher.match(title='Lee Perry at the height of his Powers'), 5)['status'] == 'review'
    assert best(matcher.match(title='Lee Perry at the height of his powers'), 5) is None


def test_a_tag_that_is_the_artist_is_confident(matcher):
    assert best(matcher.match(title='A new record', tags=['Björk', 'Soundtrack']), 6)['status'] == 'auto'
    assert best(matcher.match(title='Bjork details a live album'), 6)['status'] == 'auto'  # accents don't matter


def test_an_album_that_is_part_of_the_artists_name_is_not_a_pair(matcher):
    ms = matcher.match(title='Dolly Parton at 80')
    m = best(ms, 3)
    assert 'pair' not in m['signals']


def test_links_to_the_artists_pages_match(matcher):
    ms = matcher.match(title='A roundup', links=['https://sleaterkinney.bandcamp.com/album/path-of-wellness',
                                                  'https://open.spotify.com/artist/SPOTIFYID000000000000a'])
    assert best(ms, 4)['signals'] == 'link' and best(ms, 4)['kind'] == 'mentions'
    assert best(ms, 3)['status'] == 'auto'


def test_a_bad_link_to_bandcamps_own_site_matches_nothing(matcher):
    assert best(matcher.match(title='Daily', links=['https://daily.bandcamp.com/features/x']), 1) is None


def test_a_distinctive_name_in_the_body_is_only_a_mention(matcher):
    m = best(matcher.match(title='Notes on a week', body='We also heard Sleater-Kinney play a long set.'), 4)
    assert m['kind'] == 'mentions' and m['status'] == 'auto'
    assert best(matcher.match(title='Notes on a week', body='Takaat came up once'), 1) is None  # short name: noise


def test_names_that_normalize_to_nothing_do_not_match_everything():
    c = sqlite3.connect(':memory:')
    c.executescript("""create table artist (artist_id integer, name text, spotify_id text);
        create table album (album_id integer, name text); create table album_artist (album_id integer, artist_id integer);
        create table track (track_id integer, name text); create table track_artist (track_id integer, artist_id integer);
        create table mb_artist (artist_id integer, mbid text); create table artist_link (artist_id integer, source text, url text);
        insert into artist values (1, '&', null), (2, '原信夫', null);""")
    m = Matcher(c, words=set())
    assert m.match(title='Rock and roll') == []
    assert best(m.match(title='原信夫 の音楽'), 2)


RSS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>x</title>
 <item><title>Takaat :: Is Noise</title><link>https://ex.com/2026/10/takaat/?utm_source=rss&amp;id=3</link>
  <pubDate>Thu, 15 Oct 2026 12:00:00 +0000</pubDate><dc:creator>Ann</dc:creator>
  <category>Takaat</category><category>Noise</category>
  <description>&lt;p&gt;Short &lt;b&gt;take&lt;/b&gt;&lt;/p&gt;</description>
  <content:encoded><![CDATA[<p>Long text <a href="https://takaat.bandcamp.com/album/x">here</a></p>]]></content:encoded></item>
 <item><link>https://ex.com/no-title</link></item></channel></rss>"""

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>An entry</title>
 <link rel="alternate" href="/p/an-entry/"/><published>2026-10-01T08:00:00Z</published>
 <category term="Jazz"/><summary>Summary here</summary></entry></feed>"""


def test_parse_rss():
    [it] = writing.parse_feed(RSS)
    assert it['url'] == 'https://ex.com/2026/10/takaat?id=3'  # tracking dropped, trailing slash gone
    assert it['title'] == 'Takaat :: Is Noise' and it['tags'] == ['Takaat', 'Noise'] and it['author'] == 'Ann'
    assert it['published'] == '2026-10-15T12:00:00Z' and it['excerpt'] == 'Short take'
    assert it['links'] == ['https://takaat.bandcamp.com/album/x'] and it['body_text'].startswith('Long text here')


def test_parse_atom():
    [it] = writing.parse_feed(ATOM, 'https://blog.example/feed')
    assert it['url'] == 'https://blog.example/p/an-entry' and it['tags'] == ['Jazz'] and it['published'] == '2026-10-01T08:00:00Z'


def test_check_interval_follows_how_often_a_source_posts():
    from datetime import datetime, timedelta, timezone
    t = datetime(2026, 10, 15, tzinfo=timezone.utc)
    daily = [(t - timedelta(days=i)).isoformat().replace('+00:00', 'Z') for i in range(10)]
    assert writing.next_interval_hours(daily, None, t) == 12                          # floor
    monthly = [(t - timedelta(days=30 * i)).isoformat().replace('+00:00', 'Z') for i in range(5)]
    assert writing.next_interval_hours(monthly, None, t) == 24 * 14                   # ceiling
    assert writing.next_interval_hours(daily[:2], None, t) == writing.DEFAULT_HOURS   # too little to go on
    old = [(t - timedelta(days=100 + i)).isoformat().replace('+00:00', 'Z') for i in range(10)]
    assert writing.next_interval_hours(old, None, t) >= writing.DEFAULT_HOURS         # an archive now


def test_a_person_s_verdict_survives_a_rematch(matcher, tmp_path):
    conn = writing.connect(str(tmp_path / 'c.db'))
    conn.execute("insert into source (source_id, name, feed_url) values (1, 's', 'u')")
    conn.execute("insert into item (item_id, source_id, url, title) values (1, 1, 'https://ex.com/a', 'Takaat :: Is Noise')")
    writing.match_item(conn, matcher, 1, 'Takaat :: Is Noise', [], 'https://ex.com/a', '', [], 't')
    conn.execute("update match set status='rejected' where item_id=1")
    writing.match_item(conn, matcher, 1, 'Takaat :: Is Noise', [], 'https://ex.com/a', '', [], 't2')
    assert [r['status'] for r in conn.execute('select status from match')] == ['rejected']


def test_artist_pages_list_the_writing(app_module):
    import factoids
    history = factoids.get_history()
    ids = [a for a, ps in history.reading_by_artist.items() if any(p['kind'] == 'about' for p in ps) and history.artists[a]['spotify_id']]
    if not ids:
        pytest.skip('no published writing in this database')
    aid = ids[0]
    piece = next(p for p in history.reading_by_artist[aid] if p['kind'] == 'about')
    html = app_module.app.test_client().get(f"/artist/{history.artists[aid]['spotify_id']}").get_data(as_text=True)
    assert 'Further reading' in html and piece['url'] in html and 'reading-excerpt' not in html


def test_reading_pages(app_module):
    import factoids
    history = factoids.get_history()
    if not history.reading_sources:
        pytest.skip('no published writing in this database')
    client = app_module.app.test_client()
    index = client.get('/reading').get_data(as_text=True)
    slug, src = next(iter(history.reading_sources.items()))
    assert 'Reading' in index and f'/reading/{slug}' in index
    page = client.get(f'/reading/{slug}')
    assert page.status_code == 200 and src['name'] in page.get_data(as_text=True)
    assert client.get('/reading/no-such-publication').status_code == 404
    # a piece's source links back to that publication's page
    aid = next(a for a, ps in history.reading_by_artist.items() if ps and history.artists[a]['spotify_id'])
    p = history.reading_by_artist[aid][0]
    html = client.get(f"/artist/{history.artists[aid]['spotify_id']}").get_data(as_text=True)
    assert f"/reading/{p['source_slug']}" in html
    assert 'href="/reading"' in client.get('/').get_data(as_text=True)


def test_slugs():
    assert writing.slugify('Nate Chinen: The Gig') == 'nate-chinen-the-gig'
    assert writing.slugify('Something Else! Reviews') == 'something-else-reviews'


def test_a_site_that_says_slow_down_is_left_alone(monkeypatch):
    import writing_backfill as wb

    class Resp:
        status_code, headers = 429, {'Retry-After': '120'}

    class Session:
        def get(self, *a, **k):
            return Resp()
    monkeypatch.setattr(wb.time, 'sleep', lambda s: None)
    with pytest.raises(wb.Throttled, match='429'):
        wb.polite('https://slow.example/x', Session())
    assert wb.HOST_DELAY >= 2 and wb.MAX_ERRORS <= 3  # gentle defaults: don't loosen these casually


def _db(matcher, tmp_path, title, tags=(), url='https://ex.com/a'):
    conn = writing.connect(str(tmp_path / 'c.db'))
    conn.execute("insert or ignore into source (source_id, name, feed_url, kind) values (1, 's', 'u', 'criticism')")
    conn.execute("insert into item (item_id, source_id, url, title) values (1, 1, ?, ?)", (url, title))
    writing.match_item(conn, matcher, 1, title, list(tags), url, '', [], 't')
    return conn


def test_review_loop_confirms_rejects_and_vetoes(matcher, tmp_path):
    conn = _db(matcher, tmp_path, 'A Real Estate agent on the rise')
    conn.execute("insert into item (item_id, source_id, url, title) values (2, 1, 'https://ex.com/b', 'Real Estate prices Real Estate')")
    writing.match_item(conn, matcher, 2, 'Real Estate prices Real Estate', [], 'https://ex.com/b', '', [], 't')
    rows = conn.execute("select m.*, i.title, i.url, 's' source from match m join item i using (item_id) where status='review' order by item_id").fetchall()
    assert len(rows) == 2
    answers = iter(['o', 'y', 'x'])
    opened = []
    y, n, x = writing.review_loop(conn, rows, {2: 'Real Estate'}, ask=lambda prompt: next(answers), out=lambda *a: None,
                                  open_url=opened.append)
    assert (y, n, x) == (1, 0, 1) and opened == ['https://ex.com/a']
    assert [r['status'] for r in conn.execute('select status from match order by item_id')] == ['confirmed']  # the vetoed row is gone
    assert conn.execute('select artist_id from artist_veto').fetchall()[0][0] == 2


def test_a_veto_keeps_the_name_alone_out_but_not_a_pair_or_link(matcher, tmp_path):
    conn = _db(matcher, tmp_path, 'A Real Estate agent on the rise')
    writing.veto_artist(conn, 2)
    writing.match_item(conn, matcher, 1, 'A Real Estate agent on the rise', [], 'https://ex.com/a', '', [], 't2')
    assert conn.execute('select count(*) from match').fetchone()[0] == 0
    writing.match_item(conn, matcher, 1, 'Real Estate: Days', [], 'https://ex.com/a', '', [], 't3')  # with their album
    assert conn.execute("select count(*) from match where artist_id=2 and status='auto'").fetchone()[0] == 1


def test_capitalized_multiword_names_with_an_unusual_word_are_not_ambiguous():
    c = sqlite3.connect(':memory:')
    c.executescript("""create table artist (artist_id integer, name text, spotify_id text);
        create table album (album_id integer, name text); create table album_artist (album_id integer, artist_id integer);
        create table track (track_id integer, name text); create table track_artist (track_id integer, artist_id integer);
        create table mb_artist (artist_id integer, mbid text); create table artist_link (artist_id integer, source text, url text);
        insert into artist values (1, 'Uncle Tupelo', null), (2, 'Real Estate', null), (3, 'Journey', null);""")
    m = Matcher(c, words={'uncle', 'tupelo', 'real', 'estate', 'journey'})
    assert best(m.match(title='Vinyl Update: Uncle Tupelo and more'), 1)['status'] == 'auto'
    assert best(m.match(title='Real Estate Is Booming'), 2)['status'] == 'review'  # every word is an everyday one
    assert best(m.match(title='A Musical Journey'), 3)['status'] == 'review'      # a single word stays ambiguous
    assert best(m.match(title='Notes', body='We heard Uncle Tupelo again.'), 1) is None  # the body still needs more


def test_a_source_gets_only_its_time_budget(monkeypatch):
    import writing_backfill as wb
    fetched = []
    monkeypatch.setattr(wb, 'fetch_page', lambda url, session, name: fetched.append(url) or {})
    assert wb.fetch_candidates({'name': 's'}, ['https://ex.com/a', 'https://ex.com/b'], lambda *a: None, seconds=0) == {}
    assert fetched == []
    assert len(wb.fetch_candidates({'name': 's'}, ['https://ex.com/a', 'https://ex.com/b'], lambda *a: None, seconds=60)) == 2


def test_publications_are_a_ranking(app_module):
    import factoids
    history = factoids.get_history()
    if not history.reading_sources:
        pytest.skip('no published writing in this database')
    assert 'publications' in factoids.RANKINGS
    table = history.ranking('publications', limit=100)
    top = table['rows'][0]
    assert top['rank'] == 1 and top['value'] == len(top['pieces']) + sum(1 for p in history.reading_pieces.values()
                                                                        if p['source_slug'] == top['anchor'] and not p['published'])
    html = app_module.app.test_client().get('/rankings/publications').get_data(as_text=True)
    assert f"/reading/{top['anchor']}" in html and top['pieces'][0]['url'] in html
    assert 'href="/rankings/publications"' in html  # the tab


def test_writing_help_lists_every_command(capsys):
    writing.main(['help'])
    out = capsys.readouterr().out
    for cmd in ('run', 'rematch', 'backfill', 'publish', 'sources', 'report', 'review', 'source off|on', 'refresh_writing.py'):
        assert cmd in out
    writing.main([])  # no command is the same help
    assert 'Weekly' in capsys.readouterr().out
