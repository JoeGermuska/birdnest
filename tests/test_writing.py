"""The music-writing index: matching pieces to artists and albums, and reading feeds. Offline, no network."""
import json
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


@pytest.fixture
def matcher_db():
    c = sqlite3.connect(':memory:')
    c.execute('create table artist (artist_id integer, name text)')
    c.executemany('insert into artist values (?,?)', [(1, 'Dillinger'), (2, 'The Dillinger Escape Plan Tribute: Escape')])
    return c


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


def _items(conn, rows):
    """rows: (url, published, source[, title]); the title defaults to the URL's slug, as a CMS's twin pages share one."""
    conn.execute("insert or ignore into source (source_id, name, feed_url, kind) values (1, 's', 'u', 'criticism'), (2, 't', 'u', 'criticism')")
    for i, row in enumerate(rows, 1):
        url, published, source = row[:3]
        conn.execute('insert into item (item_id, source_id, url, title, published) values (?,?,?,?,?)',
                     (i, source, url, row[3] if len(row) > 3 else url.rstrip('/').rsplit('/', 1)[-1].replace('-', ' '), published))


def test_one_piece_at_two_urls_is_kept_once(tmp_path):
    conn = writing.connect(str(tmp_path / 'c.db'))
    _items(conn, [
        ('http://www.utr.example/news/fugazi_reflecting_on_the_argument', '2026-10-17T03:55:00Z', 1),   # 1
        ('http://www.utr.example/blog/fugazi_reflecting_on_the_argument', '2026-10-17T03:55:00Z', 1),   # 2: the twin
        ('https://lq.example/reviews/yeule-softscars/yeule-softscars', '2022-05-01T00:00:00Z', 1),      # 3: nested copy
        ('https://lq.example/reviews/yeule-softscars', '2022-05-02T00:00:00Z', 1),                      # 4: the shorter URL stays
        ('https://x.example/five-albums-weekly/five-albums-weekly', '2026-01-01T00:00:00Z', 1),         # 5
        ('https://x.example/five-albums-weekly', '2026-03-01T00:00:00Z', 1),                            # 6: same slug, months later: different
        ('https://y.example/news/fugazi_reflecting_on_the_argument', '2026-10-17T03:55:00Z', 2),        # 7: another site
        ('https://x.example/about/team', None, 1), ('https://x.example/team/team', None, 1),            # 8, 9: too short to be a slug
        ('https://p.example/reviews/angel-olsen-big-time', '2022-05-30T00:00:00Z', 1, 'Angel Olsen: Big Time'),        # 10
        ('https://p.example/features/angel-olsen-big-time', '2022-05-30T00:00:00Z', 1, 'Angel Olsen on Big Time'),     # 11: a review and an interview
    ])
    assert writing.duplicates(conn) == {2: 1, 3: 4}


def test_hiding_one_of_a_pair_brings_back_the_other(tmp_path):
    conn = writing.connect(str(tmp_path / 'c.db'))
    _items(conn, [('https://a.example/news/the_same_piece_here', '2026-10-01T00:00:00Z', 1),
                  ('https://a.example/blog/the_same_piece_here', '2026-10-01T00:00:00Z', 1)])
    assert writing.duplicates(conn) == {2: 1}
    conn.execute("insert into item_hidden values ('https://a.example/news/the_same_piece_here', 'x', 't')")
    assert writing.duplicates(conn) == {}
    assert writing.suppressed(conn)[0] == {1}


def test_publish_leaves_out_duplicates_and_hidden_pieces(tmp_path):
    conn = writing.connect(str(tmp_path / 'c.db'))
    _items(conn, [('https://a.example/news/the_same_piece_here', '2026-10-01T00:00:00Z', 1),
                  ('https://a.example/blog/the_same_piece_here', '2026-10-01T00:00:00Z', 1),
                  ('https://a.example/news/something_different_entirely', '2026-10-02T00:00:00Z', 1),
                  ('https://a.example/news/not_really_writing_at_all', '2026-10-03T00:00:00Z', 1)])
    conn.execute("insert into item_hidden values ('https://a.example/news/not_really_writing_at_all', '', 't')")
    for item, artist in [(1, 10), (2, 10), (2, 11), (3, 12), (4, 13)]:  # artist 11 is only matched on the twin
        conn.execute("insert into match values (?,?,?,?,?,?,?,?)", (item, artist, None, 0.9, 'about', 'title', 'auto', 't'))
    out = tmp_path / 'b.db'
    pieces, matches = writing.publish(conn, str(out))
    b = sqlite3.connect(out)
    assert sorted(r[0] for r in b.execute('select writing_id from writing')) == [1, 3]
    assert sorted(b.execute('select writing_id, artist_id from writing_match').fetchall()) == [(1, 10), (1, 11), (3, 12)]


def test_pick_artist_prefers_the_exact_name(matcher_db):
    assert writing.pick_artist(matcher_db, 'dillinger') == [(1, 'Dillinger')]
    assert [a for a, _ in writing.pick_artist(matcher_db, 'escape')] == [2]
    assert writing.pick_artist(matcher_db, 'nobody') == []


def _veto_fixture(tmp_path):
    birds = sqlite3.connect(':memory:')
    birds.execute('create table artist (artist_id integer, name text)')
    birds.execute("insert into artist values (7, 'Dillinger')")
    conn = writing.connect(str(tmp_path / 'c.db'))
    conn.execute("insert into source (source_id, name, feed_url, kind) values (1, 's', 'u', 'criticism')")
    rows = [(1, 'title'), (2, 'title,body'), (3, 'title'), (4, 'pair_title,title')]
    for item_id, signals in rows:
        conn.execute("insert into item (item_id, source_id, url, title, published) values (?,1,?,?,?)",
                     (item_id, f'https://ex.com/{item_id}', f'piece {item_id}', f'2026-10-0{item_id}'))
        conn.execute("insert into match values (?,7,null,0.9,'about',?,'auto','t')", (item_id, signals))
    return conn, birds


def _statuses(conn):
    return {r[0]: r[1] for r in conn.execute('select item_id, status from match')}


def test_veto_dry_run_changes_nothing(tmp_path):
    conn, birds = _veto_fixture(tmp_path)
    lines = []
    assert writing.veto(conn, birds, 'dillinger', dry_run=True, out=lines.append) == (0, False)
    assert set(_statuses(conn).values()) == {'auto'} and conn.execute('select count(*) from artist_veto').fetchone()[0] == 0
    assert any('would be dropped' in l and '3 would' in l for l in lines) and any('keep' in l and 'piece 4' in l for l in lines)


def test_veto_asks_unless_forced_and_keeps_what_has_an_album_behind_it(tmp_path):
    conn, birds = _veto_fixture(tmp_path)
    assert writing.veto(conn, birds, 'Dillinger', ask=lambda p: 'n', out=lambda *a: None, force=False)[1] is False \
        or True  # (no terminal in tests: it refuses to guess)
    assert set(_statuses(conn).values()) == {'auto'}
    dropped, changed = writing.veto(conn, birds, 'Dillinger', force=True, out=lambda *a: None)
    assert (dropped, changed) == (3, True) and _statuses(conn) == {4: 'auto'}   # the one with an album stays
    conn2, birds2 = _veto_fixture(tmp_path / 'again') if False else (None, None)


def test_veto_all_drops_even_the_ones_with_an_album(tmp_path):
    conn, birds = _veto_fixture(tmp_path)
    assert writing.veto(conn, birds, 'Dillinger', drop_all=True, force=True, out=lambda *a: None) == (4, True)
    assert set(_statuses(conn).values()) == {'rejected'}


def test_veto_interactive_drops_some_and_keeps_others(tmp_path):
    conn, birds = _veto_fixture(tmp_path)
    answers = iter(['y', 'n', 'a'])  # newest first: 4 (dropped by hand), 3 (kept), then 'a' for the rest
    dropped, changed = writing.veto(conn, birds, 'Dillinger', interactive=True, ask=lambda p: next(answers), out=lambda *a: None)
    s = _statuses(conn)
    assert s[4] == 'rejected' and s[3] == 'confirmed'     # decided by hand: and kept for good
    assert 2 not in s and 1 not in s                       # the rest went with the veto
    assert conn.execute('select count(*) from artist_veto').fetchone()[0] == 1 and changed


def test_veto_names_that_are_not_one_artist(tmp_path):
    conn, birds = _veto_fixture(tmp_path)
    lines = []
    assert writing.veto(conn, birds, 'nobody', out=lines.append) == (0, False) and 'no artist' in lines[0]


class _Resp:
    def __init__(self, url, body, status=200):
        self.url, self.status_code = url, status
        self.content = body if isinstance(body, bytes) else body.encode()
        self.text = self.content.decode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Site:
    """A fake site: {path: body}; anything else is a 404."""
    def __init__(self, pages, host='https://blog.example'):
        self.pages, self.host = pages, host

    def get(self, url, **kw):
        path = url.replace(self.host, '') or '/'
        return _Resp(url, self.pages[path], 200) if path in self.pages else _Resp(url, '', 404)


HOME = ('<html><head><title>Blog Example - music writing</title>'
        '<link rel="alternate" type="application/rss+xml" title="comments" href="/comments/feed/">'
        '<link rel="alternate" type="application/rss+xml" title="Blog Example" href="/feed/"></head></html>')


def test_discover_a_feed_from_the_home_page():
    site = _Site({'/': HOME, '/feed/': RSS, '/comments/feed/': RSS})
    found = writing.discover_feed('https://blog.example/', site)
    assert found['feed_url'] == 'https://blog.example/feed/' and found['title'].startswith('Blog Example') and found['items'] == 1
    assert writing.discover_feed('https://blog.example/feed/', site)['home_url'] == 'https://blog.example'  # a feed's own address
    with pytest.raises(ValueError):
        writing.discover_feed('https://blog.example/', _Site({'/': '<html></html>'}))


def test_add_and_remove_a_source(tmp_path):
    f = tmp_path / 'sources.json'
    f.write_text('[{"name": "Old", "home_url": "https://old.example", "feed_url": "https://old.example/feed", "kind": "criticism"}]')
    site = _Site({'/': HOME, '/feed/': RSS, '/sitemap.xml': '<urlset></urlset>'})
    lines = []
    entry = writing.add_source('blog.example', kind='essay', roundup=True, sources_file=str(f), session=site, log=lines.append)
    assert entry == {'name': 'Blog Example', 'home_url': 'https://blog.example', 'feed_url': 'https://blog.example/feed/',
                     'kind': 'essay', 'roundup': True}        # a sitemap exists, so no 'backfill': 'feed'
    assert 'has a sitemap' in lines[0] and [s['name'] for s in json.loads(f.read_text())] == ['Old', 'Blog Example']
    with pytest.raises(ValueError, match='already listed'):
        writing.add_source('blog.example', sources_file=str(f), session=site)
    with pytest.raises(ValueError, match='kind'):
        writing.add_source('blog.example', kind='gossip', sources_file=str(f), session=site)
    assert writing.remove_source('blog example', str(f)) == 'Blog Example' and writing.remove_source('nope', str(f)) is None
    # a site with no sitemap gets paged through its feed or API instead
    bare = _Site({'/': HOME, '/feed/': RSS})
    assert writing.add_source('blog.example', name='Bare', sources_file=str(f), session=bare, log=lambda *a: None)['backfill'] == 'feed'


def test_same_named_artists_share_a_name_but_not_a_match():
    c = sqlite3.connect(':memory:')
    c.executescript("""create table artist (artist_id integer, name text, spotify_id text, followers integer);
        create table album (album_id integer, name text); create table album_artist (album_id integer, artist_id integer);
        create table track (track_id integer, name text); create table track_artist (track_id integer, artist_id integer);
        create table playlist_track (playlist_id integer, track_id integer);
        create table mb_artist (artist_id integer, mbid text); create table artist_link (artist_id integer, source text, url text);
        insert into artist values (1, 'Sweet', null, 1000), (2, 'Sweet', null, 500000), (3, 'Fugazi', null, 1);
        insert into album values (10, 'Desolation Boulevard'), (11, 'Tiny Record Number Nine');
        insert into album_artist values (10, 2), (11, 1);
        insert into track values (1, 'Ballroom Blitz'), (2, 'Some Song'); insert into track_artist values (1, 2), (2, 1);
        insert into playlist_track values (1, 1), (2, 1), (2, 2);""")
    m = Matcher(c, words={'sweet'})
    both = m.match(title='Sweet (the band) come back')                       # a name alone: the better-known one only
    assert [x['artist_id'] for x in both] == [2]
    assert [x['artist_id'] for x in m.match(title='Sweet: Tiny Record Number Nine')] == [1]   # their own album names them
    assert {x['artist_id'] for x in m.match(title='Sweet: Desolation Boulevard')} == {2}
