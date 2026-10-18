"""An index of music writing, matched to the artists and albums we've played.

Reads the feeds listed in writing_sources.json, keeps what it finds in writing_crawl.db (local, not committed: every
item seen, so a newly added artist can be matched to old pieces), and matches items to artists and albums with
writing_match.py. It keeps titles, tags, links and short excerpts locally, publishes only titles, dates and links, and every use of a piece links back to it.
Member-only sites are handled by their public feeds: we never log in or fetch past a paywall.

    python writing.py run [--all] [--source NAME]   check the sources that are due, match new items, print the report
    python writing.py backfill [--source NAME]      reach back through a site's sitemaps (see writing_backfill.py)
    python writing.py rematch                       match every stored item again (after new artists arrive)
    python writing.py publish                       copy the accepted matches into birdnest.db (run does this too)
    python writing.py report [--days N]             what matched lately, again
    python writing.py sources                       sync writing_sources.json into the database and list it
    python writing.py source off|on NAME            stop (or resume) checking one
    python writing.py review                        matches a person should confirm or reject

Routine use: load_playlist.py runs refresh() each week (after the new show's artists are in, before the caches are
built) and maintenance.py runs a slice of backfill_sources() each time. The commands above are for doing either by hand.

Each source is checked again after about half the typical gap between its posts (12 hours to 2 weeks), and a source
that keeps failing backs off. Requests are one a second per host, conditional where the site allows.
"""
import argparse
import html
import json
import re
import sqlite3
import statistics
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

from writing_match import Matcher

BIRDS_DB, CRAWL_DB, SOURCES_FILE = 'birdnest.db', 'writing_crawl.db', 'writing_sources.json'
USER_AGENT = 'birdsconferring-writing-index/0.1 (+https://birdsconferring.com)'
MIN_HOURS, MAX_HOURS, DEFAULT_HOURS = 12, 24 * 14, 24 * 7
MAX_BODY_CHARS, EXCERPT_CHARS, MAX_FEED_BYTES = 4000, 300, 8_000_000
ATOM, CONTENT, DC = '{http://www.w3.org/2005/Atom}', '{http://purl.org/rss/1.0/modules/content/}', '{http://purl.org/dc/elements/1.1/}'

SCHEMA = """
create table if not exists source (
    source_id integer primary key, name text unique, home_url text, feed_url text, kind text,
    members_only integer default 0, roundup integer default 0, note text, active integer default 1,
    backfill text, added_at text, last_checked text, next_check text, interval_hours real, etag text, last_modified text,
    fail_count integer default 0, last_error text, last_new_at text);
create table if not exists item (
    item_id integer primary key, source_id integer references source(source_id), url text unique,
    title text, published text, author text, tags text, excerpt text, body_text text, links text, first_seen text);
create table if not exists match (
    item_id integer references item(item_id), artist_id integer, album_id integer, score real, kind text,
    signals text, status text, created_at text, primary key (item_id, artist_id));
"""


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.isoformat().replace('+00:00', 'Z')


def connect(path=CRAWL_DB):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if 'backfill' not in {r[1] for r in conn.execute('pragma table_info(source)')}:
        conn.execute('alter table source add column backfill text')
    return conn


# ---- sources

def sync_sources(conn, path=SOURCES_FILE):
    """Add or update sources from the file. A source dropped from the file is switched off, not deleted."""
    wanted = json.load(open(path))
    names = set()
    for s in wanted:
        names.add(s['name'])
        row = (s['name'], s.get('home_url'), s['feed_url'], s.get('kind', 'criticism'), int(s.get('members_only', False)),
               int(s.get('roundup', False)), s.get('note'), s.get('backfill', 'sitemap'))
        if conn.execute('select 1 from source where name=?', (s['name'],)).fetchone():
            conn.execute('update source set home_url=?, feed_url=?, kind=?, members_only=?, roundup=?, note=?, backfill=? where name=?',
                         row[1:] + (s['name'],))
        else:
            conn.execute('insert into source (name, home_url, feed_url, kind, members_only, roundup, note, backfill, added_at) '
                         'values (?,?,?,?,?,?,?,?,?)', row + (iso(now()),))
    for (name,) in conn.execute('select name from source').fetchall():
        if name not in names:
            conn.execute('update source set active=0 where name=?', (name,))
    conn.commit()


# ---- feeds

TAG_RE = re.compile(r'<[^>]+>')


def plain(s):
    return re.sub(r'\s+', ' ', html.unescape(TAG_RE.sub(' ', s or ''))).strip()


def canonical(url, base=''):
    u = urlparse(urljoin(base, (url or '').strip()))
    q = [(k, v) for k, v in parse_qsl(u.query) if not k.lower().startswith(('utm_', 'fbclid', 'mc_', 'ref'))]
    path = u.path.rstrip('/') or '/'
    return urlunparse((u.scheme, u.netloc.lower(), path, '', urlencode(q), ''))


def parse_date(s):
    s = (s or '').strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace('Z', '+00:00'))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return iso(d.astimezone(timezone.utc))


def parse_feed(data, base=''):
    """RSS 2.0 or Atom bytes -> [{url, title, published, author, tags, excerpt, body_text, links}]."""
    root = ET.fromstring(data)
    items = []
    for e in root.iter():
        tag = e.tag.split('}')[-1]
        if tag not in ('item', 'entry'):
            continue
        get = lambda *names: next((c for n in names if (c := e.find(n)) is not None), None)
        t_el = get('title', ATOM + 'title')  # not `or`: an Element with no children is falsy
        title = plain(t_el.text) if t_el is not None else ''
        link = get('link', ATOM + 'link')
        url = None
        if link is not None:
            url = link.text if link.text and link.text.strip() else None
            if url is None:  # Atom: <link rel="alternate" href=...>
                alts = [l for l in e.findall(ATOM + 'link') if l.get('rel', 'alternate') == 'alternate']
                url = (alts[0] if alts else link).get('href')
        guid = e.find('guid')
        if not url and guid is not None and (guid.text or '').startswith('http'):
            url = guid.text
        if not url or not title:
            continue
        raw = ''
        for n in (CONTENT + 'encoded', ATOM + 'content', 'description', ATOM + 'summary'):
            c = e.find(n)
            if c is not None and (c.text or len(c)):
                raw = c.text or ''
                break
        desc = e.find('description')
        short = plain((desc.text if desc is not None else '') or raw)
        body = plain(raw)
        tags = [plain(c.text) if c.text else c.get('term', '') for c in e.findall('category') + e.findall(ATOM + 'category')]
        author = get(DC + 'creator', 'author', ATOM + 'author')
        author = plain(''.join(author.itertext())) if author is not None else None
        d = get('pubDate', ATOM + 'published', ATOM + 'updated', DC + 'date')
        links = sorted({urljoin(base, h) for h in re.findall(r'href=["\']([^"\']+)["\']', html.unescape(raw))
                        if h.startswith(('http', '/'))})
        items.append({'url': canonical(url, base), 'title': title, 'published': parse_date(d.text if d is not None else None),
                      'author': author, 'tags': [t for t in tags if t], 'excerpt': short[:EXCERPT_CHARS],
                      'body_text': body[:MAX_BODY_CHARS], 'links': links[:60]})
    return items


def next_interval_hours(dates, last_new_at, at=None):
    """About half the typical gap between recent posts, within MIN..MAX hours; a quiet source is checked weekly or less."""
    at = at or now()
    stamps = sorted((datetime.fromisoformat(d.replace('Z', '+00:00')) for d in dates if d), reverse=True)[:20]
    if len(stamps) < 3:
        return DEFAULT_HOURS
    gaps = [(a - b).total_seconds() / 3600 for a, b in zip(stamps, stamps[1:]) if a != b]
    if not gaps:
        return DEFAULT_HOURS
    hours = statistics.median(gaps) / 2
    if (at - stamps[0]).days > 60:  # nothing new in two months: this one is an archive now
        hours = max(hours, DEFAULT_HOURS)
    return min(MAX_HOURS, max(MIN_HOURS, hours))


_last_hit = {}


def polite_get(url, headers, session):
    host = urlparse(url).netloc
    wait = 1.0 - (time.monotonic() - _last_hit.get(host, -10))
    if wait > 0:
        time.sleep(wait)
    try:
        return session.get(url, headers=headers, timeout=25, stream=False)
    finally:
        _last_hit[host] = time.monotonic()


def check_source(conn, src, session, matcher, log=print):
    """Fetch one source's feed, store new items, match them. -> number of new items."""
    t = now()
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5'}
    if src['etag']:
        headers['If-None-Match'] = src['etag']
    if src['last_modified']:
        headers['If-Modified-Since'] = src['last_modified']
    try:
        resp = polite_get(src['feed_url'], headers, session)
        if resp.status_code == 304:
            conn.execute('update source set last_checked=?, next_check=?, fail_count=0, last_error=null where source_id=?',
                         (iso(t), iso(t + timedelta(hours=src['interval_hours'] or DEFAULT_HOURS)), src['source_id']))
            conn.commit()
            return 0
        resp.raise_for_status()
        if len(resp.content) > MAX_FEED_BYTES:
            raise ValueError('feed too large')
        parsed = parse_feed(resp.content, src['feed_url'])
        if not parsed:
            raise ValueError('no items in feed')
    except Exception as e:  # a bad source shouldn't stop the others
        fails = (src['fail_count'] or 0) + 1
        backoff = min(MAX_HOURS, MIN_HOURS * 2 ** fails)
        conn.execute('update source set last_checked=?, next_check=?, fail_count=?, last_error=? where source_id=?',
                     (iso(t), iso(t + timedelta(hours=backoff)), fails, f'{type(e).__name__}: {e}'[:200], src['source_id']))
        conn.commit()
        log(f"  ! {src['name']}: {type(e).__name__}: {str(e)[:100]}" + ('  (3+ failures in a row)' if fails >= 3 else ''))
        return 0
    new = 0
    for it in parsed:
        if conn.execute('select 1 from item where url=?', (it['url'],)).fetchone():
            continue
        cur = conn.execute(
            'insert into item (source_id, url, title, published, author, tags, excerpt, body_text, links, first_seen) '
            'values (?,?,?,?,?,?,?,?,?,?)',
            (src['source_id'], it['url'], it['title'], it['published'], it['author'], json.dumps(it['tags']),
             it['excerpt'], it['body_text'], json.dumps(it['links']), iso(t)))
        match_item(conn, matcher, cur.lastrowid, it['title'], it['tags'], it['url'], it['body_text'] or it['excerpt'], it['links'], iso(t))
        new += 1
    dates = [r[0] for r in conn.execute('select published from item where source_id=?', (src['source_id'],))]
    hours = next_interval_hours(dates, None, t)
    conn.execute('update source set last_checked=?, next_check=?, interval_hours=?, etag=?, last_modified=?, fail_count=0, '
                 'last_error=null, last_new_at=case when ? > 0 then ? else last_new_at end where source_id=?',
                 (iso(t), iso(t + timedelta(hours=hours)), hours, resp.headers.get('ETag'), resp.headers.get('Last-Modified'),
                  new, iso(t), src['source_id']))
    conn.commit()
    return new


# ---- matching

def match_item(conn, matcher, item_id, title, tags, url, body, links, stamp):
    """Store the matches for one item. A match a person has confirmed or rejected is left as they set it."""
    keep = {r[0] for r in conn.execute("select artist_id from match where item_id=? and status in ('confirmed','rejected')", (item_id,))}
    conn.execute("delete from match where item_id=? and status in ('auto','review')", (item_id,))
    n = 0
    for m in matcher.match(title=title, tags=tags, url=url, body=body, links=links):
        if m['artist_id'] in keep:
            continue
        conn.execute('insert into match values (?,?,?,?,?,?,?,?)',
                     (item_id, m['artist_id'], m['album_id'], m['score'], m['kind'], m['signals'], m['status'], stamp))
        n += 1
    return n


def rematch(conn, matcher):
    stamp = iso(now())
    old = {(r[0], r[1]) for r in conn.execute("select item_id, artist_id from match where status in ('auto','review')")}
    for r in conn.execute('select * from item').fetchall():
        match_item(conn, matcher, r['item_id'], r['title'], json.loads(r['tags'] or '[]'), r['url'],
                   r['body_text'] or r['excerpt'] or '', json.loads(r['links'] or '[]'), stamp)
    # matches that were already there keep their original date, so the report only shows what's new
    for item_id, artist_id in old:
        conn.execute('update match set created_at=(select min(created_at) from match m2 where m2.item_id=match.item_id) '
                     'where item_id=? and artist_id=? and created_at=?', (item_id, artist_id, stamp))
    conn.commit()


# ---- publishing

PUBLISH_KINDS = ('criticism', 'essay', 'interview')  # news is kept and matched, but isn't what we're listing

PUBLISH_SCHEMA = """
drop table if exists writing_match;
drop table if exists writing;
drop table if exists writing_source;
create table writing_source (
    slug text primary key, name text, kind text, home_url text, members_only integer, note text);
create table writing (
    writing_id integer primary key, url text, title text, published text,
    source text, source_slug text references writing_source(slug), source_kind text, members_only integer);
create table writing_match (
    writing_id integer references writing(writing_id), artist_id integer, album_id integer, kind text, score real);
create index writing_match_artist on writing_match(artist_id);
"""


def slugify(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def publish(conn, birds_path=BIRDS_DB):
    """Copy the accepted matches into birdnest.db (tables writing, writing_match), replacing what was there. Only
    pieces from sources we list (not news) and matches not rejected; just titles, dates and links, never text."""
    marks = ','.join('?' * len(PUBLISH_KINDS))
    rows = conn.execute(
        f"select i.item_id, i.url, i.title, i.published, s.name source, s.kind source_kind, s.members_only, s.home_url, s.note, "
        f"m.artist_id, m.album_id, m.kind, m.score from match m join item i using (item_id) join source s using (source_id) "
        f"where m.status in ('auto','confirmed') and s.active=1 and s.kind in ({marks})", PUBLISH_KINDS).fetchall()
    birds = sqlite3.connect(birds_path)
    birds.executescript(PUBLISH_SCHEMA)
    seen, sources = set(), set()
    for r in rows:
        slug = slugify(r['source'])
        if slug not in sources:
            sources.add(slug)
            birds.execute('insert into writing_source values (?,?,?,?,?,?)',
                          (slug, r['source'], r['source_kind'], r['home_url'], r['members_only'], r['note']))
        if r['item_id'] not in seen:
            seen.add(r['item_id'])
            birds.execute('insert into writing values (?,?,?,?,?,?,?,?)',
                          (r['item_id'], r['url'], r['title'], (r['published'] or '')[:10] or None,
                           r['source'], slug, r['source_kind'], r['members_only']))
        birds.execute('insert into writing_match values (?,?,?,?,?)',
                      (r['item_id'], r['artist_id'], r['album_id'], r['kind'], r['score']))
    birds.commit()
    birds.close()
    return len(seen), len(rows)


# ---- the report

def recent_artists(birds, shows=8):
    ids = set()
    for (pid,) in birds.execute('select playlist_id from playlist where date is not null order by date desc limit ?', (shows,)):
        ids |= {a for (a,) in birds.execute('select ta.artist_id from playlist_track pt join track_artist ta using (track_id) '
                                            'where pt.playlist_id=?', (pid,))}
    return ids


def report(conn, birds, since, shows=8, limit=40, out=print):
    names = {a: n for a, n in birds.execute('select artist_id, name from artist')}
    albums = {a: n for a, n in birds.execute('select album_id, name from album')}
    recent = recent_artists(birds, shows)
    rows = conn.execute(
        "select m.*, i.title, i.url, i.published, s.name as source, s.members_only, s.kind from match m "
        "join item i using (item_id) join source s using (source_id) where m.created_at >= ? and m.status in ('auto','review') "
        "order by m.score desc, i.published desc", (since,)).fetchall()

    def show(r):
        lock = ' [members]' if r['members_only'] else ''
        what = f" / {albums[r['album_id']]}" if r['album_id'] in albums else ''
        flag = '' if r['status'] == 'auto' else '  (to review)'
        out(f"  {names.get(r['artist_id'], '?')}{what}: {r['title'][:80]}\n    {r['source']}{lock}, {r['kind']}, {r['score']:.2f} "
            f"{r['signals']}{flag}\n    {r['url']}")

    mine = [r for r in rows if r['artist_id'] in recent and r['status'] == 'auto']
    out(f"\n== New writing about artists from the last {shows} shows ({len(mine)})")
    for r in mine[:limit]:
        show(r)
    rest = [r for r in rows if r['status'] == 'auto' and r['artist_id'] not in recent]
    out(f"\n== Other new matches ({len(rest)})")
    for r in rest[:limit]:
        show(r)
    out(f"\n== {sum(r['status'] == 'review' for r in rows)} more matches to review (python writing.py review)")


def refresh(source=None, force=False, rematch_first=False, birds_path=BIRDS_DB):
    """Check the sources that are due (or all, or one), match what's new, print the report, publish into birdnest.db.
    rematch_first: match everything stored against the artists as they are now, for after a new show's artists arrive."""
    conn, birds = connect(), sqlite3.connect(birds_path)
    sync_sources(conn)
    started = iso(now())
    matcher = Matcher(birds)
    if rematch_first:
        rematch(conn, matcher)
    q = 'select * from source where active=1'
    params = []
    if source:
        q += ' and name=?'
        params.append(source)
    elif not force:
        q += ' and (next_check is null or next_check <= ?)'
        params.append(started)
    sources = conn.execute(q + ' order by name', params).fetchall()
    print(f'{len(sources)} sources due')
    session = requests.Session()
    total = 0
    for src in sources:
        n = check_source(conn, src, session, matcher)
        total += n
        print(f"  {src['name']}: {n} new")
    print(f'{total} new items')
    report(conn, birds, started)
    pieces, matches = publish(conn, birds_path)
    print(f'\npublished {pieces} pieces / {matches} matches to {birds_path}')


def cmd_run(args):
    refresh(args.source, args.all)


def backfill_sources(source=None, sitemaps=15, pages=50, feed_requests=5, news=False, birds_path=BIRDS_DB):
    """One gentle slice of the backfill (see writing_backfill.py), then the report and publish."""
    import writing_backfill
    conn, birds = connect(), sqlite3.connect(birds_path)
    sync_sources(conn)
    q, params = 'select * from source where active=1', []
    if source:
        q += ' and name=?'
        params.append(source)
    sources = conn.execute(q + ' order by name', params).fetchall()
    if not news:  # news sources are matched from their feeds but never listed, so don't spend requests reaching back
        sources = [s for s in sources if s['kind'] in PUBLISH_KINDS]
    started = iso(now())
    n = writing_backfill.backfill(conn, Matcher(birds), sources, sitemaps, pages, feed_requests)
    print(f'{n} new items')
    report(conn, birds, started)
    pieces, matches = publish(conn, birds_path)
    print(f'\npublished {pieces} pieces / {matches} matches to {birds_path}')


def cmd_backfill(args):
    backfill_sources(args.source, args.sitemaps, args.pages, args.feed_requests, args.news)


def cmd_rematch(args):
    conn, birds = connect(), sqlite3.connect(BIRDS_DB)
    rematch(conn, Matcher(birds))
    print('rematched', conn.execute('select count(*) from item').fetchone()[0], 'items;',
          conn.execute("select count(*) from match where status='auto'").fetchone()[0], 'auto,',
          conn.execute("select count(*) from match where status='review'").fetchone()[0], 'to review')


def cmd_publish(args):
    pieces, matches = publish(connect())
    print(f'published {pieces} pieces / {matches} matches to {BIRDS_DB}')


def cmd_report(args):
    since = iso(now() - timedelta(days=args.days))
    report(connect(), sqlite3.connect(BIRDS_DB), since)


def cmd_sources(args):
    """Every source with what it has yielded, to see the whole set at once and decide which earn their place."""
    conn = connect()
    sync_sources(conn)
    rows = conn.execute("""
        select s.*, count(i.item_id) items, max(i.published) latest,
               count(distinct case when m.status in ('auto','confirmed') then i.item_id end) matched_items,
               count(distinct case when m.status in ('auto','confirmed') and m.kind='about' then i.item_id end) about_items,
               count(distinct case when m.status in ('auto','confirmed') then m.artist_id end) artists
        from source s left join item i using (source_id) left join match m on m.item_id = i.item_id
        group by s.source_id order by about_items desc, s.name""").fetchall()
    print(f"{'source':32} {'kind':10} {'check':8} {'items':>5} {'matched':>8} {'about':>6} {'artists':>7}  {'latest post':11}  notes")
    for r in rows:
        check = 'OFF' if not r['active'] else f"{r['interval_hours']:.0f}h" if r['interval_hours'] else 'new'
        pct = f"{100 * r['matched_items'] / r['items']:.0f}%" if r['items'] else '-'
        notes = [n for n in (
            'not listed (news)' if r['kind'] not in PUBLISH_KINDS else '', 'members' if r['members_only'] else '',
            'roundups' if r['roundup'] else '', f"! {r['last_error']}" if r['last_error'] else '',
            'no matches yet' if r['items'] and not r['matched_items'] else '',
            'quiet: nothing new in 60+ days' if r['latest'] and r['latest'] < iso(now() - timedelta(days=60)) else '') if n]
        print(f"{r['name']:32} {r['kind']:10} {check:8} {r['items']:5} {pct:>8} {r['about_items']:6} {r['artists']:7}  {(r['latest'] or '')[:10]:11}  {'; '.join(notes)}")
    print(f"\n{len(rows)} sources, {sum(r['items'] for r in rows)} items. matched = items with an accepted match; about = items about an artist we've played.")


def cmd_toggle(args):
    conn = connect()
    cur = conn.execute('update source set active=? where name=?', (1 if args.state == 'on' else 0, args.name))
    conn.commit()
    print('ok' if cur.rowcount else f'no source named {args.name!r}')


def cmd_review(args):
    conn, birds = connect(), sqlite3.connect(BIRDS_DB)
    names = {a: n for a, n in birds.execute('select artist_id, name from artist')}
    for r in conn.execute("select m.*, i.title, i.url, s.name source from match m join item i using (item_id) "
                          "join source s using (source_id) where m.status='review' order by m.score desc limit ?", (args.limit,)):
        print(f"{r['score']:.2f} {names.get(r['artist_id'], '?')}: {r['title'][:80]}  [{r['source']}; {r['signals']}]\n     {r['url']}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--all', action='store_true', help='check every source, due or not')
    r.add_argument('--source', help='check just this one')
    r.set_defaults(fn=cmd_run)
    bf = sub.add_parser('backfill')
    bf.add_argument('--source', help='just this one')
    bf.add_argument('--sitemaps', type=int, default=15, help='most sitemap files to read per source per run')
    bf.add_argument('--pages', type=int, default=50, help='most candidate pages to fetch per source per run')
    bf.add_argument('--feed-requests', type=int, default=5, help='most requests per source per run for sources paged through their feed or API')
    bf.add_argument('--news', action='store_true', help='include news sources too')
    bf.set_defaults(fn=cmd_backfill)
    sub.add_parser('rematch').set_defaults(fn=cmd_rematch)
    rp = sub.add_parser('report')
    rp.add_argument('--days', type=int, default=7)
    rp.set_defaults(fn=cmd_report)
    sub.add_parser('publish').set_defaults(fn=cmd_publish)
    sub.add_parser('sources').set_defaults(fn=cmd_sources)
    so = sub.add_parser('source')
    so.add_argument('state', choices=['on', 'off'])
    so.add_argument('name')
    so.set_defaults(fn=cmd_toggle)
    rv = sub.add_parser('review')
    rv.add_argument('--limit', type=int, default=60)
    rv.set_defaults(fn=cmd_review)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == '__main__':
    main()
