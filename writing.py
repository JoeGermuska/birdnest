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
    python writing.py review [--artist NAME] [--list]   confirm or reject the borderline matches, one at a time
    python writing.py veto ARTIST | hide URL | unhide URL | duplicates   keep wrong matches and duplicates off the site

Routine use: refresh_writing.py, weekly (refresh() then one slice of backfill_sources()). The commands above are for
doing the pieces by hand.

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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

from writing_match import Matcher, norm

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
create table if not exists artist_veto (artist_id integer primary key, created_at text);
create table if not exists item_hidden (url text primary key, reason text, created_at text);
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
    vetoed = {r[0] for r in conn.execute('select artist_id from artist_veto')}
    n = 0
    for m in matcher.match(title=title, tags=tags, url=url, body=body, links=links):
        if m['artist_id'] in keep or (m['artist_id'] in vetoed and not CORROBORATED & set(m['signals'].split(','))):
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


# ---- the same piece under two URLs

def duplicates(conn):
    """-> {item_id kept out: item_id kept in}. A site's CMS sometimes serves one piece at two URLs ('/blog/x' and
    '/news/x', or '/reviews/x' and '/reviews/x/x'), each calling itself canonical. Same source, same last path segment
    (a real slug: 8+ letters with a - or _), the same title and dates within a week is one piece; the shortest URL stays.
    (A review and an interview can share a slug, so the title has to agree too.) Items a person has hidden are out of
    the running first, so hiding one of a pair brings the other back."""
    hidden = {r[0] for r in conn.execute('select i.item_id from item i join item_hidden h using (url)')}
    groups = defaultdict(list)
    for r in conn.execute('select item_id, source_id, url, title, published from item'):
        tail = urlparse(r['url']).path.rstrip('/').rsplit('/', 1)[-1].lower()
        if r['item_id'] not in hidden and len(tail) >= 8 and re.search(r'[-_]', tail):
            groups[(r['source_id'], tail, norm(r['title']))].append(r)
    out = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: r['published'] or '')
        clusters = []  # [first date, [rows]]: a piece that shares a slug but not a season is a different piece
        for r in group:
            d = datetime.fromisoformat(r['published'].replace('Z', '+00:00')) if r['published'] else None
            if clusters and (d is None or clusters[-1][0] is None or abs((d - clusters[-1][0]).days) <= 7):
                clusters[-1][1].append(r)
                clusters[-1][0] = clusters[-1][0] or d
            else:
                clusters.append([d, [r]])
        for _, rows in clusters:
            if len(rows) > 1:
                rows.sort(key=lambda r: (urlparse(r['url']).path.count('/'), len(r['url']), r['item_id']))
                for r in rows[1:]:
                    out[r['item_id']] = rows[0]['item_id']
    return out


def suppressed(conn):
    """(item ids we don't show: hidden by hand or a duplicate, {duplicate: the one that stands for it})"""
    dups = duplicates(conn)
    hidden = {r[0] for r in conn.execute('select i.item_id from item i join item_hidden h using (url)')}
    return hidden | set(dups), dups


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
    out_ids, stands_for = suppressed(conn)
    rows = conn.execute(
        f"select i.item_id, i.url, i.title, i.published, s.name source, s.kind source_kind, s.members_only, s.home_url, s.note, "
        f"m.artist_id, m.album_id, m.kind, m.score from match m join item i using (item_id) join source s using (source_id) "
        f"where m.status in ('auto','confirmed') and s.active=1 and s.kind in ({marks})", PUBLISH_KINDS).fetchall()
    rows = [dict(r) for r in rows]
    for r in rows:
        if r['item_id'] in stands_for:  # a duplicate's matches go to the copy that's kept (hidden ones just drop)
            r['item_id'] = stands_for[r['item_id']]
    rows = [r for r in rows if r['item_id'] not in out_ids]
    best = {}
    for r in rows:  # the same artist on both copies counts once, at the better match
        k = (r['item_id'], r['artist_id'])
        if k not in best or (r['kind'] == 'about', r['score']) > (best[k]['kind'] == 'about', best[k]['score']):
            best[k] = r
    rows = list(best.values())
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


# ---- review: a person's verdicts on the borderline matches

CORROBORATED = {'pair_title', 'pair_slug', 'body_pair', 'link'}  # signals that survive a veto: more than the name


def set_status(conn, item_id, artist_id, status):
    conn.execute('update match set status=? where item_id=? and artist_id=?', (status, item_id, artist_id))
    conn.commit()


def veto_artist(conn, artist_id):
    """Never match this artist on their name alone (a 'Charlie' or a 'Journey'); a link or one of their albums still counts.
    Their matches already stored without such support are dropped."""
    conn.execute('insert or ignore into artist_veto values (?,?)', (artist_id, iso(now())))
    for item_id, signals in conn.execute("select item_id, signals from match where artist_id=? and status in ('auto','review')",
                                         (artist_id,)).fetchall():
        if not CORROBORATED & set(signals.split(',')):
            conn.execute('delete from match where item_id=? and artist_id=?', (item_id, artist_id))
    conn.commit()


def review_loop(conn, rows, names, ask=input, out=print, open_url=None):
    """Walk through borderline matches. y = it's about them (published), n = it isn't, x = never match this artist on
    the name alone, s = skip, o = open the piece, q = quit. -> (confirmed, rejected, vetoed) counts."""
    done = {'y': 0, 'n': 0, 'x': 0}
    vetoed = set()
    pending = {}
    for r in rows:
        pending[r['artist_id']] = pending.get(r['artist_id'], 0) + 1
    for i, r in enumerate(rows, 1):
        if r['artist_id'] in vetoed:
            continue
        out(f"\n[{i}/{len(rows)}] {names.get(r['artist_id'], '?')}  ({pending[r['artist_id']]} pending for this artist)\n"
            f"  {r['title']}\n  {r['source']} · {r['signals']} · {r['score']:.2f}\n  {r['url']}")
        while True:
            try:
                key = ask('  y it is about them / n no / x never on the name alone / s skip / o open / q quit > ').strip().lower()[:1]
            except EOFError:
                key = 'q'
            if key == 'o' and open_url:
                open_url(r['url'])
                continue
            if key in ('y', 'n', 'x', 's', 'q'):
                break
        if key == 'q':
            break
        if key == 'y':
            set_status(conn, r['item_id'], r['artist_id'], 'confirmed')
        elif key == 'n':
            set_status(conn, r['item_id'], r['artist_id'], 'rejected')
        elif key == 'x':
            veto_artist(conn, r['artist_id'])
            vetoed.add(r['artist_id'])
        if key in done:
            done[key] += 1
        pending[r['artist_id']] -= 1
    return done['y'], done['n'], done['x']


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
    out_ids, _ = suppressed(conn)
    rows = [r for r in rows if r['item_id'] not in out_ids]

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


def backfill_sources(source=None, sitemaps=15, pages=50, feed_requests=5, news=False, minutes=5, birds_path=BIRDS_DB):
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
    n = writing_backfill.backfill(conn, Matcher(birds), sources, sitemaps, pages, feed_requests, seconds=minutes * 60)
    print(f'{n} new items')
    report(conn, birds, started)
    pieces, matches = publish(conn, birds_path)
    print(f'\npublished {pieces} pieces / {matches} matches to {birds_path}')


def cmd_backfill(args):
    backfill_sources(args.source, args.sitemaps, args.pages, args.feed_requests, args.news, args.minutes)


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


def pick_artist(birds, name):
    """The artists named NAME: an exact match (any case) if there is one, otherwise the ones whose name contains it."""
    exact = birds.execute('select artist_id, name from artist where lower(name)=lower(?)', (name,)).fetchall()
    return exact or birds.execute('select artist_id, name from artist where name like ? order by name', (f'%{name}%',)).fetchall()


def veto(conn, birds, name, drop_all=False, dry_run=False, force=False, interactive=False, ask=input, out=print):
    """veto NAME: this artist's name alone never matches (a 'Dillinger' who isn't The Dillinger Escape Plan). Their
    matches resting on the name alone are dropped now and never come back (drop_all: every match of theirs).
    dry_run only lists what would go. interactive goes piece by piece (y drop this one, n keep it for good, a drop this and all
    the rest and veto the artist, q stop). Otherwise it lists them and asks, unless force. -> (matches dropped, changed?)"""
    found = pick_artist(birds, name)
    if len(found) != 1:
        out('no artist called that' if not found else 'which one?\n  ' + '\n  '.join(f'{n} (id {a})' for a, n in found[:15]))
        return 0, False
    aid, artist = found[0]
    pieces = conn.execute("select m.item_id, i.title, i.url, s.name source, m.signals, m.status from match m "
                          "join item i using (item_id) join source s using (source_id) "
                          "where m.artist_id=? and m.status in ('auto','review','confirmed') order by i.published desc", (aid,)).fetchall()
    held = lambda p: bool(CORROBORATED & set(p['signals'].split(',')))  # an album or a link behind it: a plain veto keeps it
    would_go = [p for p in pieces if drop_all or not held(p)]
    out(f'{artist}: {len(pieces)} matches; {len(would_go)} would be dropped' +
        ('' if drop_all or len(would_go) == len(pieces) else f", {len(pieces) - len(would_go)} kept (an album or link behind them; --all drops those too)"))
    if not pieces:
        return 0, False
    if dry_run:
        for p in pieces:
            out(f"  {'drop' if p in would_go else 'keep'}  {p['title'][:76]}  [{p['source']}; {p['signals']}]")
        out('dry run: nothing changed')
        return 0, False
    if interactive:
        dropped = 0
        for i, p in enumerate(pieces, 1):
            out(f"\n[{i}/{len(pieces)}] {p['title']}\n  {p['source']} · {p['signals']}\n  {p['url']}")
            while True:
                try:
                    key = ask('  y drop this one / n keep it / a drop this and all the rest, and veto / q stop > ').strip().lower()[:1]
                except EOFError:
                    key = 'q'
                if key in ('y', 'n', 'a', 'q'):
                    break
            if key == 'q':
                break
            if key == 'y':
                set_status(conn, p['item_id'], aid, 'rejected')
                dropped += 1
            elif key == 'n':
                set_status(conn, p['item_id'], aid, 'confirmed')  # said keep: a later 'a' or veto won't take it
            elif key == 'a':
                before = len(pieces) - i + 1
                veto_artist(conn, aid)
                if drop_all:
                    conn.execute("update match set status='rejected' where artist_id=? and status!='rejected'", (aid,))
                    conn.commit()
                return dropped + before, True
        return dropped, bool(dropped)
    if not force:
        if not sys.stdin.isatty():
            out('not changing anything without a terminal to ask on: add --force, or --dry-run to look')
            return 0, False
        for p in would_go[:15]:
            out(f"  {p['title'][:76]}  [{p['source']}]")
        if len(would_go) > 15:
            out(f'  ... and {len(would_go) - 15} more')
        if ask(f'Drop these {len(would_go)} and never match {artist} on the name alone? [y/N] ').strip().lower()[:1] != 'y':
            out('left alone')
            return 0, False
    veto_artist(conn, aid)
    if drop_all:
        conn.execute("update match set status='rejected' where artist_id=?", (aid,))
        conn.commit()
    return len(would_go), True


def cmd_veto(args):
    conn, birds = connect(), sqlite3.connect(BIRDS_DB)
    if args.undo:
        found = pick_artist(birds, args.name)
        if len(found) != 1:
            print('no artist called that' if not found else 'which one?\n  ' + '\n  '.join(f'{n} (id {a})' for a, n in found[:15]))
            return
        conn.execute('delete from artist_veto where artist_id=?', (found[0][0],))
        conn.commit()
        print(f'{found[0][1]}: matches on the name alone are allowed again (they come back at the next rematch)')
        return
    dropped, changed = veto(conn, birds, args.name, args.all, args.dry_run, args.force, args.interactive)
    if changed:
        pieces_n, matches_n = publish(conn)
        print(f'dropped {dropped}; published {pieces_n} pieces / {matches_n} matches to {BIRDS_DB}')


def cmd_hide(args):
    """hide URL: keep a piece off the site (a duplicate, junk, not writing). unhide URL undoes it."""
    conn = connect()
    url = canonical(args.url)
    item = conn.execute('select item_id, title from item where url=?', (url,)).fetchone()
    if args.undo:
        cur = conn.execute('delete from item_hidden where url=?', (url,))
        conn.commit()
        print('shown again; run: python writing.py publish' if cur.rowcount else 'that one was not hidden')
        return
    if not item:
        tail = urlparse(url).path.rstrip('/').rsplit('/', 1)[-1]
        near = conn.execute('select url from item where url like ? limit 5', (f'%{tail}%',)).fetchall() if tail else []
        print(f'no stored piece at {url}' + ('; did you mean:\n  ' + '\n  '.join(r[0] for r in near) if near else ''))
        return
    conn.execute('insert or replace into item_hidden values (?,?,?)', (url, args.reason, iso(now())))
    conn.commit()
    print(f"hidden: {item['title']}\nrun: python writing.py publish")


def cmd_duplicates(args):
    conn = connect()
    dups = duplicates(conn)
    keep = {}
    for loser, winner in dups.items():
        keep.setdefault(winner, []).append(loser)
    url = {r[0]: r[1] for r in conn.execute('select item_id, url from item where item_id in (%s)' % ','.join(
        str(i) for i in list(dups) + list(dups.values()) or [0]))}
    for winner, losers in sorted(keep.items(), key=lambda kv: url[kv[0]]):
        print(f'kept  {url[winner]}')
        for l in losers:
            print(f'  dup {url[l]}')
    print(f'\n{len(dups)} duplicates of {len(keep)} pieces left out of what is published. To bring one back or drop the other: '
          'python writing.py hide URL')


def cmd_review(args):
    conn, birds = connect(), sqlite3.connect(BIRDS_DB)
    names = {a: n for a, n in birds.execute('select artist_id, name from artist')}
    rows = conn.execute("select m.*, i.title, i.url, s.name source from match m join item i using(item_id) "
                        "join source s using(source_id) where m.status='review'").fetchall()
    out_ids, _ = suppressed(conn)
    rows = [r for r in rows if r['item_id'] not in out_ids]
    if args.artist:
        rows = [r for r in rows if args.artist.lower() in names.get(r['artist_id'], '').lower()]
    # by artist (the biggest piles first, where a veto helps most), best-scored first within each
    by_artist = {}
    for r in rows:
        by_artist[r['artist_id']] = by_artist.get(r['artist_id'], 0) + 1
    rows.sort(key=lambda r: (-by_artist[r['artist_id']], names.get(r['artist_id'], ''), -r['score']))
    if args.limit:
        rows = rows[:args.limit]
    if args.list or not sys.stdin.isatty():
        for r in rows:
            print(f"{r['score']:.2f} {names.get(r['artist_id'], '?')}: {r['title'][:80]}  [{r['source']}; {r['signals']}]\n     {r['url']}")
        return
    import webbrowser
    y, n, x = review_loop(conn, rows, names, open_url=webbrowser.open)
    print(f'\nconfirmed {y}, rejected {n}, never-on-the-name {x}')
    if y:
        pieces, matches = publish(conn)
        print(f'published {pieces} pieces / {matches} matches to {BIRDS_DB}')


HELP = """writing.py: the index of music writing

Weekly (after load_playlist.py): python refresh_writing.py
  does the whole routine in one go: re-match, check the feeds that are due, a gentle slice of the backfill, publish.

The pieces, for doing by hand:
  run [--all] [--source NAME]       check the feeds that are due (every one with --all, or just NAME), match what's new, publish
  rematch                           match every stored item again; needed after new artists arrive (refresh_writing.py does it)
  backfill [--source NAME]          reach back through a site's sitemaps (or its WordPress API / paged feed): a gentle slice,
            [--minutes M]           polite and resumable. M is the time each source gets in each phase (default 5)
  publish                           copy the accepted matches into birdnest.db (run, backfill and review already do this)

Looking:
  sources                           every source: pieces stored, matched, about artists we've played, latest post, any trouble
  report [--days N]                 what matched lately

Deciding:
  review [--artist NAME] [--list]   go through the borderline matches: y it's about them, n it isn't, x never match this artist
                                    on the name alone, s skip, o open the piece, q quit (--list just prints them)
  veto ARTIST [-n|-f|-i] [--all]    an artist's name alone never matches (a 'Dillinger' who isn't The Dillinger Escape Plan): drops
                                    their matches that rest on the name, for good, after asking. -n (--dry-run) only lists them,
                                    -f (--force) doesn't ask, -i (--interactive) goes piece by piece for when only some are wrong;
                                    --all also drops matches with an album or link behind them. unveto ARTIST undoes it
  hide URL [--reason WHY]           keep one piece off the site (junk, not writing, a duplicate you'd rather not show)
  unhide URL                        bring it back
  duplicates                        pieces a site serves at two URLs ('/blog/x' and '/news/x'): one is kept automatically
  source off|on NAME                stop or resume checking one source. To add or remove one, edit writing_sources.json

Whatever changes birdnest.db (publish does), commit it and deploy. See the top of writing.py for how it all works."""


def cmd_help(args):
    print(HELP)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd')
    sub.add_parser('help', help='what each command does').set_defaults(fn=cmd_help)
    r = sub.add_parser('run', help='check the feeds that are due')
    r.add_argument('--all', action='store_true', help='check every source, due or not')
    r.add_argument('--source', help='check just this one')
    r.set_defaults(fn=cmd_run)
    bf = sub.add_parser('backfill', help='reach back through archives, gently')
    bf.add_argument('--source', help='just this one')
    bf.add_argument('--sitemaps', type=int, default=15, help='most sitemap files to read per source per run')
    bf.add_argument('--pages', type=int, default=50, help='most candidate pages to fetch per source per run')
    bf.add_argument('--feed-requests', type=int, default=5, help='most requests per source per run for sources paged through their feed or API')
    bf.add_argument('--minutes', type=float, default=5, help='most time each source gets in each phase (a slow Crawl-delay means fewer pages)')
    bf.add_argument('--news', action='store_true', help='include news sources too')
    bf.set_defaults(fn=cmd_backfill)
    sub.add_parser('rematch', help='match everything stored again').set_defaults(fn=cmd_rematch)
    rp = sub.add_parser('report', help='what matched lately')
    rp.add_argument('--days', type=int, default=7)
    rp.set_defaults(fn=cmd_report)
    sub.add_parser('publish', help='copy accepted matches into birdnest.db').set_defaults(fn=cmd_publish)
    sub.add_parser('sources', help='every source and what it yields').set_defaults(fn=cmd_sources)
    so = sub.add_parser('source', help='turn a source on or off')
    so.add_argument('state', choices=['on', 'off'])
    so.add_argument('name')
    so.set_defaults(fn=cmd_toggle)
    hd = sub.add_parser('hide', help='keep one piece off the site')
    hd.add_argument('url')
    hd.add_argument('--reason', default='')
    hd.set_defaults(fn=cmd_hide, undo=False)
    uh = sub.add_parser('unhide', help='bring a hidden piece back')
    uh.add_argument('url')
    uh.set_defaults(fn=cmd_hide, undo=True, reason='')
    vt = sub.add_parser('veto', help="never match an artist on their name alone")
    vt.add_argument('name')
    vt.add_argument('--all', action='store_true', help='drop every match of theirs, not only the ones resting on the name')
    vt.add_argument('-n', '--dry-run', action='store_true', help='list what would be dropped; change nothing')
    vt.add_argument('-f', '--force', action='store_true', help="don't ask first")
    vt.add_argument('-i', '--interactive', action='store_true', help='go piece by piece: drop some, keep others')
    vt.set_defaults(fn=cmd_veto, undo=False)
    uv = sub.add_parser('unveto', help='allow an artist\'s name to match again')
    uv.add_argument('name')
    uv.set_defaults(fn=cmd_veto, undo=True, all=False)
    sub.add_parser('duplicates', help='pieces served at two URLs of one site').set_defaults(fn=cmd_duplicates)
    rv = sub.add_parser('review', help='confirm or reject borderline matches')
    rv.add_argument('--limit', type=int, default=0, help='only this many (default: all)')
    rv.add_argument('--artist', help='only matches for artists whose name contains this')
    rv.add_argument('--list', action='store_true', help='just list them, no questions')
    rv.set_defaults(fn=cmd_review)
    args = p.parse_args(argv)
    (getattr(args, 'fn', None) or cmd_help)(args)  # no command: the help


if __name__ == '__main__':
    main()
