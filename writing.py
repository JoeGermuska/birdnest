"""An index of music writing, matched to the artists and albums we've played.

Reads the feeds listed in writing_sources.json, keeps what it finds in writing_crawl.db (local, not committed: every
item seen, so a newly added artist can be matched to old pieces), and matches items to artists and albums with
writing_match.py. It keeps titles, tags, links and short excerpts locally, publishes only titles, dates and links, and every use of a piece links back to it.
Member-only sites are handled by their public feeds: we never log in or fetch past a paywall.

    python writing.py run [--all] [--source NAME]   check the sources that are due, match new items, print the report
    python writing.py backfill [--source NAME]      reach back through a site's sitemaps (see writing_backfill.py)
    python writing.py episodes [--pages N]          find podcast episodes' own pages, where the feed links only to the show
    python writing.py deepen [--pages N]            read the full text of pieces the feed gave only a teaser for
    python writing.py why URL [ARTIST]              why a piece did (or didn't) match
    python writing.py rematch                       match every stored item again (after new artists arrive)
    python writing.py publish                       copy the accepted matches into birdnest.db (run does this too)
    python writing.py report [--days N]             what matched lately, again
    python writing.py sources                       sync writing_sources.json into the database and list it
    python writing.py add URL                       offer one page for matching (and its feed, if it has one)
    python writing.py source add URL | remove NAME | off|on NAME   change the list of sources
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
import shutil
import statistics
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import requests

from writing_match import Matcher, norm

BIRDS_DB, CRAWL_DB, SOURCES_FILE = 'birdnest.db', 'writing_crawl.db', 'writing_sources.json'
USER_AGENT = 'birdsconferring-writing-index/0.1 (+https://birdsconferring.com)'
MIN_HOURS, MAX_HOURS, DEFAULT_HOURS = 12, 24 * 14, 24 * 7
MAX_BODY_CHARS, EXCERPT_CHARS, MAX_FEED_BYTES = 4000, 300, 8_000_000
THIN_CHARS = 1000  # an item with less text than this has only a teaser from its feed
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
    if 'episode_pages' not in {r[1] for r in conn.execute('pragma table_info(source)')}:
        conn.execute('alter table source add column episode_pages text')  # a page (or a template with {year}) that lists a podcast's episodes
    for col in ('page_url text', 'page_tried integer'):  # an episode's own page, found from the show's site (null: not found / not tried; 1 found, 2 not)
        if col.split()[0] not in {r[1] for r in conn.execute('pragma table_info(item)')}:
            conn.execute(f'alter table item add column {col}')
    if 'deepened' not in {r[1] for r in conn.execute('pragma table_info(item)')}:
        conn.execute('alter table item add column deepened integer')  # null: not tried; 1: full text read; 2: page wouldn't give it
    return conn


# ---- sources

def sync_sources(conn, path=SOURCES_FILE):
    """Add or update sources from the file. A source dropped from the file is switched off, not deleted."""
    wanted = json.load(open(path))
    names = set()
    for s in wanted:
        names.add(s['name'])
        row = (s['name'], s.get('home_url'), s.get('feed_url'), s.get('kind', 'criticism'), int(s.get('members_only', False)),
               int(s.get('roundup', False)), s.get('note'), s.get('backfill', 'sitemap'), s.get('episode_pages'))
        if conn.execute('select 1 from source where name=?', (s['name'],)).fetchone():
            conn.execute('update source set home_url=?, feed_url=?, kind=?, members_only=?, roundup=?, note=?, backfill=?, episode_pages=? '
                         'where name=?', row[1:] + (s['name'],))
        else:
            conn.execute('insert into source (name, home_url, feed_url, kind, members_only, roundup, note, backfill, episode_pages, added_at) '
                         'values (?,?,?,?,?,?,?,?,?,?)', row + (iso(now()),))
    for (name,) in conn.execute('select name from source').fetchall():
        if name not in names:
            conn.execute('update source set active=0 where name=?', (name,))
    conn.commit()


# ---- adding and removing sources

KINDS = ('criticism', 'essay', 'interview', 'news', 'podcast')
FEED_GUESSES = ('/feed/', '/feed', '/rss', '/atom.xml', '/rss.xml', '/index.xml')


def feed_links(text, base):
    """The feeds a page announces in its <head> (not the comment feeds), as absolute addresses."""
    found = []
    for tag in re.findall(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', text):
        href, label = re.search(r'href=["\']([^"\']+)', tag), re.search(r'title=["\']([^"\']*)', tag)
        if href and 'comment' not in (label.group(1).lower() if label else '') and '/comments/' not in href.group(1):
            found.append(urljoin(base, html.unescape(href.group(1))))
    return list(dict.fromkeys(found))


def discover_feed(url, session):
    """Find a site's feed from its address (or from the feed's own). -> {feed_url, home_url, title, items}; ValueError if none."""
    headers = {'User-Agent': USER_AGENT}
    r = session.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    root = '{0.scheme}://{0.netloc}'.format(urlparse(r.url))
    try:  # the address might be a feed already
        items = parse_feed(r.content, r.url)
        if items:
            return {'feed_url': r.url, 'home_url': root, 'title': None, 'items': len(items)}
    except ET.ParseError:
        pass
    text = r.text
    title = (re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)', text)
             or re.search(r'<title[^>]*>([^<]+)', text))
    title = plain(title.group(1)) if title else None
    found = feed_links(text, r.url)
    for cand in dict.fromkeys(found + [root + g for g in FEED_GUESSES]):
        try:
            fr = session.get(cand, headers=headers, timeout=25)
            items = parse_feed(fr.content, cand) if fr.status_code < 400 else []
        except Exception:
            continue
        if items:
            return {'feed_url': cand, 'home_url': root, 'title': title, 'items': len(items)}
    raise ValueError(f'no feed found at {url}')


def has_sitemap(home_url, session):
    """Does the site announce a sitemap (so the backfill can read it), or must it be paged through its feed or API?"""
    try:
        r = session.get(home_url.rstrip('/') + '/robots.txt', headers={'User-Agent': USER_AGENT}, timeout=25)
        if r.status_code < 400 and re.search(r'(?im)^sitemap:', r.text):
            return True
        r = session.get(home_url.rstrip('/') + '/sitemap.xml', headers={'User-Agent': USER_AGENT}, timeout=25)
        return r.status_code < 400 and (b'<urlset' in r.content[:2000] or b'<sitemapindex' in r.content[:2000])
    except Exception:
        return False


def add_source(url, name=None, kind='criticism', members_only=False, roundup=False, sources_file=SOURCES_FILE, session=None, log=print):
    """Add a site to writing_sources.json (and say what it found); the next sync puts it in the database."""
    session = session or requests.Session()
    if kind not in KINDS:
        raise ValueError(f'kind must be one of {", ".join(KINDS)}')
    if '://' not in url:
        url = 'https://' + url
    found = discover_feed(url, session)
    existing = json.load(open(sources_file))
    name = name or re.split(r'\s+[|–—-]\s+', found['title'] or '')[0].strip() or urlparse(found['home_url']).netloc.removeprefix('www.')
    for s in existing:
        if s['feed_url'] == found['feed_url'] or s['name'].lower() == name.lower():
            raise ValueError(f"already listed as {s['name']!r} ({s['feed_url']})")
    entry = {'name': name, 'home_url': found['home_url'], 'feed_url': found['feed_url'], 'kind': kind}
    if members_only:
        entry['members_only'] = True
    if roundup:
        entry['roundup'] = True
    mapped = has_sitemap(found['home_url'], session)
    if not mapped:
        entry['backfill'] = 'feed'
    existing.append(entry)
    json.dump(existing, open(sources_file, 'w'), indent=1, ensure_ascii=False)
    log(f"added {name!r} ({kind}): {found['feed_url']}, {found['items']} items in the feed; "
        + ('has a sitemap, so the backfill can read its archive' if mapped else
           'no sitemap found: the backfill will try its WordPress API or a paged feed'))
    return entry


def site(url):
    return urlparse(url).netloc.lower().removeprefix('www.')


def elsewhere_name(kind):
    return 'Elsewhere' if kind == 'criticism' else f'Elsewhere ({kind}s)'


def ensure_elsewhere(conn, kind, sources_file=SOURCES_FILE):
    """The catch-all source for single pages added by hand (it has no feed and no archive to read). -> its row"""
    name = elsewhere_name(kind)
    existing = json.load(open(sources_file))
    if not any(s['name'] == name for s in existing):
        existing.append({'name': name, 'kind': kind, 'feed_url': None, 'backfill': 'none',
                         'note': 'single pages added by hand with writing.py add; no feed'})
        json.dump(existing, open(sources_file, 'w'), indent=1, ensure_ascii=False)
    sync_sources(conn, sources_file)
    return conn.execute('select * from source where name=?', (name,)).fetchone()


def blank_page():
    """A page we couldn't read: nothing but what the person gives (title, date, text)."""
    return {'title': '', 'published': None, 'tags': [], 'excerpt': '', 'body_text': '', 'links': [], 'feeds': []}


def resolve_artists(birds, names):
    """Artist ids from names (as we spell them, ignoring case and accents) or ids. ValueError if one is unknown or ambiguous."""
    rows = birds.execute('select artist_id, name from artist').fetchall()
    ids = []
    for n in names:
        if n.strip().isdigit():
            hits = [r[0] for r in rows if r[0] == int(n)]
        else:
            hits = [r[0] for r in rows if norm(r[1]) == norm(n)]
        if not hits:
            raise ValueError(f'no artist {n!r} in our data (only artists we have played are there)')
        if len(hits) > 1:
            raise ValueError(f'{n!r} is more than one artist ({", ".join(str(h) for h in hits)}): give the id instead')
        ids.append(hits[0])
    return ids


def plan_page(conn, matcher, url, page, source=None, kind='criticism', title=None, date=None, artists=()):
    """What adding this page would do: {title, published, source (name), source_row (or None for the catch-all), matches,
    feeds (announced by the page, not already listed, and only when the site isn't one we follow)}. ValueError if it can't be."""
    title = (title or page['title'] or '').strip()
    if not title:
        raise ValueError('no title found on the page; give one with --title')
    published = page['published']
    if date:
        published = parse_date(date)
        if not published:
            raise ValueError(f'--date {date!r} is not a date (use YYYY-MM-DD)')
    sources = conn.execute('select * from source where active=1').fetchall()
    if source:
        row = next((s for s in sources if s['name'].lower() == source.lower()), None)
        if not row:
            raise ValueError(f'no source named {source!r}; python writing.py sources lists them')
    else:  # a site we already follow files it there
        row = next((s for s in sources if s['home_url'] and site(s['home_url']) == site(url)), None)
    followed = row is not None and row['feed_url'] is not None
    feeds = [] if followed else [f for f in page.get('feeds', []) if not any(f == s['feed_url'] for s in sources)]
    matches = matcher.match(title=title, tags=page['tags'], url=url, body=page['body_text'] or page['excerpt'], links=page['links'])
    return {'title': title, 'published': published, 'source': row['name'] if row else elsewhere_name(kind),
            'source_row': row, 'kind': row['kind'] if row else kind, 'matches': matches, 'feeds': feeds, 'forced': list(artists)}


def store_page(conn, matcher, url, page, plan, sources_file=SOURCES_FILE):
    """Keep the page as an item of its source (the catch-all, made when it is first needed) and match it. -> item_id, or None
    when we already hold it."""
    import writing_backfill
    src = plan['source_row'] or ensure_elsewhere(conn, plan['kind'], sources_file)
    it = {'url': url, 'title': plan['title'], 'published': plan['published'], 'tags': page['tags'], 'excerpt': page['excerpt'],
          'body_text': page['body_text'], 'links': page['links']}
    if not writing_backfill.store_item(conn, matcher, src['source_id'], it, iso(now())):
        return None
    conn.execute('update item set deepened=1 where url=?', (url,))  # nothing more to read from it
    item_id = conn.execute('select item_id from item where url=?', (url,)).fetchone()[0]
    for artist_id in plan.get('forced', ()):  # the person said it is about them: confirmed, so a rematch keeps it
        conn.execute("insert or replace into match values (?,?,?,?,?,?,?,?)",
                     (item_id, artist_id, None, 1.0, 'about', 'manual', 'confirmed', iso(now())))
    conn.commit()
    return item_id


def remove_source(name, sources_file=SOURCES_FILE):
    """Take a source out of writing_sources.json (the next sync switches it off; what it gave stays stored). -> name or None"""
    existing = json.load(open(sources_file))
    keep = [s for s in existing if s['name'].lower() != name.lower()]
    if len(keep) == len(existing):
        return None
    json.dump(keep, open(sources_file, 'w'), indent=1, ensure_ascii=False)
    return next(s['name'] for s in existing if s['name'].lower() == name.lower())


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
        if not url and guid is not None and guid.text:  # no episode page: the show's page, told apart by the guid below
            ch = root.find('channel/link')
            url = ch.text.strip() if ch is not None and ch.text and ch.text.strip() else None
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
                      'body_text': body[:MAX_BODY_CHARS], 'links': links[:60], 'guid': (guid.text or '').strip() if guid is not None else ''})
    # podcast hosts often give every episode the show's page as its link: tell them apart by guid, so none overwrites another
    shared = {u for u, n in Counter(i['url'] for i in items).items() if n > 1}
    for i in items:
        if i['url'] in shared:
            i['url'] += '#' + quote(i['guid'] or i['title'], safe='')
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

PUBLISH_KINDS = ('criticism', 'essay', 'interview', 'podcast')  # news is kept and matched, but isn't what we're listing

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
        f"select i.item_id, coalesce(i.page_url, i.url) url, i.title, i.published, s.name source, s.kind source_kind, s.members_only, s.home_url, s.note, "
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


def review_loop(conn, rows, names, ask=input, out=print, open_url=None, blurbs=None):
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
        who = (blurbs or {}).get(r['artist_id'])
        out(f"\n[{i}/{len(rows)}] {names.get(r['artist_id'], '?')}  ({pending[r['artist_id']]} pending for this artist)"
            + (f"\n  {who}" if who else '')
            + f"\n  {r['title']}\n  {r['source']} · {r['signals']} · {r['score']:.2f}\n  {r['url']}")
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


def refresh(source=None, force=False, rematch_first=False, birds_path=BIRDS_DB, deepen_pages=40, episode_pages=40):
    """Check the sources that are due (or all, or one), match what's new, print the report, publish into birdnest.db.
    rematch_first: match everything stored against the artists as they are now, for after a new show's artists arrive."""
    conn, birds = connect(), sqlite3.connect(birds_path)
    sync_sources(conn)
    started = iso(now())
    matcher = Matcher(birds)
    if rematch_first:
        rematch(conn, matcher)
    q = 'select * from source where active=1 and feed_url is not null'
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
    if deepen_pages:
        deepen_sources(conn, matcher, deepen_pages)
    if episode_pages:
        episodes_sources(conn, episode_pages)
    report(conn, birds, started)
    pieces, matches = publish(conn, birds_path)
    print(f'\npublished {pieces} pieces / {matches} matches to {birds_path}')


def deepen_sources(conn, matcher, pages=40, source=None, minutes=5):
    """Read the full text of items whose feed gave only a teaser, newest first, so a name in the article (not just its
    title and tags) can match. See writing_backfill.deepen."""
    import writing_backfill
    q, params = 'select * from source where active=1', []
    if source:
        q += ' and name=?'
        params.append(source)
    sources = [s for s in conn.execute(q + ' order by name', params) if s['kind'] in PUBLISH_KINDS]
    n = writing_backfill.deepen(conn, matcher, sources, pages, seconds=minutes * 60)
    print(f'{n} thin items read in full')
    return n


def episodes_sources(conn, pages=40, source=None, minutes=5, retry=False):
    """Find the own page of podcast episodes whose feed links only to the show's page, from the show's site. See
    writing_backfill.resolve_episodes."""
    import writing_backfill
    q, params = "select * from source where active=1 and kind='podcast'", []
    if source:
        q += ' and name=?'
        params.append(source)
    sources = conn.execute(q + ' order by name', params).fetchall()
    if retry:  # forget the ones that weren't found (a new way of finding them, or a site that has changed)
        for s in sources:
            conn.execute('update item set page_tried=null where source_id=? and page_tried=2 and page_url is null', (s['source_id'],))
        conn.commit()
    n = writing_backfill.resolve_episodes(conn, sources, pages, seconds=minutes * 60)
    print(f'{n} episode pages found')
    return n


def cmd_episodes(args):
    conn = connect()
    episodes_sources(conn, args.pages, args.source, args.minutes, args.retry)
    pieces, matches = publish(conn, BIRDS_DB)
    print(f'\npublished {pieces} pieces / {matches} matches to {BIRDS_DB}')


def cmd_deepen(args):
    conn, birds = connect(), sqlite3.connect(BIRDS_DB)
    started = iso(now())
    deepen_sources(conn, Matcher(birds), args.pages, args.source, args.minutes)
    report(conn, birds, started)
    pieces, matches = publish(conn, BIRDS_DB)
    print(f'\npublished {pieces} pieces / {matches} matches to {BIRDS_DB}')


def cmd_why(args):
    """why URL [ARTIST]: what we stored for a piece, what it matched, and what the matcher says now."""
    conn, birds = connect(), sqlite3.connect(BIRDS_DB)
    url = canonical(args.url)
    it = conn.execute('select i.*, s.name as source from item i join source s using(source_id) where url=?', (url,)).fetchone()
    if not it:
        print(f'no stored piece at {url}; it may not be in a feed or sitemap we read yet')
        return
    names = dict(birds.execute('select artist_id, name from artist'))
    body = it['body_text'] or ''
    state = {None: 'not tried', 1: 'full text read', 2: "tried; the page wouldn't give it"}.get(it['deepened'], '?')
    print(f"{it['title']}  ({it['source']}, {(it['published'] or '')[:10]})\n  tags: {', '.join(json.loads(it['tags'] or '[]')) or '-'}")
    if it['page_url']:
        print(f"  episode page: {it['page_url']}")
    print(f"  text stored: {len(body)} chars ({'a teaser only' if len(body) < THIN_CHARS else 'full'}; {state})")
    print('  hidden' if conn.execute('select 1 from item_hidden where url=?', (url,)).fetchone() else '  not hidden')
    print('stored matches:')
    for m in conn.execute('select * from match where item_id=? order by score desc', (it['item_id'],)):
        print(f"  {names.get(m['artist_id'], m['artist_id'])}: {m['score']} {m['kind']} {m['status']} [{m['signals']}]")
    live = Matcher(birds).match(title=it['title'], tags=json.loads(it['tags'] or '[]'), url=url, body=body or it['excerpt'] or '',
                                links=json.loads(it['links'] or '[]'))
    print('matcher now:')
    for m in live:
        print(f"  {names.get(m['artist_id'])}: {m['score']} {m['kind']} {m['status']} [{m['signals']}]")
    if args.artist:
        key = norm(args.artist)
        hits = [(a, n) for a, n in names.items() if key in norm(n)]
        print(f'{args.artist!r} in our artists: ' + ('; '.join(f'{n} ({a})' for a, n in hits[:8]) or 'none'))
        print(f'  appears in the stored text: {key in norm(body + " " + (it["excerpt"] or "") + " " + it["title"])}')


def cmd_add(args):
    """add URL: offer one page for matching: show what it would match, then keep it under its site's source, or under
    'Elsewhere'. If the page announces a feed, offer to follow the site."""
    import writing_backfill
    conn, birds = connect(), sqlite3.connect(BIRDS_DB)
    sync_sources(conn)
    url = canonical(args.url)
    have = conn.execute('select s.name from item i join source s using(source_id) where i.url=?', (url,)).fetchone()
    if have:
        print(f"already stored, under {have['name']}; 'python writing.py why {url}' shows what it matched")
        return
    text = sys.stdin.read() if args.text == '-' else args.text
    session = requests.Session()
    page, why = None, None
    try:
        page = writing_backfill.fetch_page(url, session)
    except requests.RequestException as e:
        why = f'could not fetch it: {e}'
    if page is None:
        why = why or "could not read it (robots.txt says no, it isn't a web page, or the site refused)"
        if not args.title:
            print(f'{why}\nto add it anyway, give --title, and if you can --date, --text (or - for stdin) and --artist NAME')
            return
        print(f'{why}; going on with what you gave: matching on the title, the address{" and your text" if text else ""}')
        page = blank_page()
    if text:
        page['body_text'], page['excerpt'] = re.sub(r'\s+', ' ', text).strip()[:MAX_BODY_CHARS], re.sub(r'\s+', ' ', text).strip()[:EXCERPT_CHARS]
    matcher = Matcher(birds)
    try:
        plan = plan_page(conn, matcher, url, page, args.source, args.kind, args.title, args.date, resolve_artists(birds, args.artist or []))
    except ValueError as e:
        print(e)
        return
    names = dict(birds.execute('select artist_id, name from artist'))
    print(f"{plan['title']}\n  {(plan['published'] or 'no date')[:10]} · {len(page['body_text'])} chars of text · would go under {plan['source']} ({plan['kind']})")
    for artist_id in plan['forced']:
        print(f"  {names.get(artist_id)}: about (you said so)")
    plan['matches'] = [m for m in plan['matches'] if m['artist_id'] not in plan['forced']]
    for m in plan['matches']:
        print(f"  {names.get(m['artist_id'])}: {m['kind']} {m['score']} {'(held for review)' if m['status'] == 'review' else ''}")
    if not plan['matches'] and not plan['forced']:
        print('  matches none of the artists we have: it would be kept, but nothing shows until one of them matches')
    if args.dry_run:
        return
    if not args.yes:
        if not sys.stdin.isatty():
            print('run it again with --yes to add it')
            return
        if input('add it? [Y/n] ').strip().lower() in ('n', 'no'):
            return
    item_id = store_page(conn, matcher, url, page, plan)
    if item_id is None:
        print('already stored')
        return
    pieces, matches = publish(conn, BIRDS_DB)
    print(f'added; published {pieces} pieces / {matches} matches to {BIRDS_DB}')
    for i, feed in enumerate(plan['feeds'], 1):
        print(f'  this page announces a feed{f" ({i})" if len(plan["feeds"]) > 1 else ""}: {feed}')
    if not plan['feeds']:
        return
    cmd = f"python writing.py source add {plan['feeds'][0]} --kind {plan['kind']}"
    if args.yes or not sys.stdin.isatty():
        print(f'  to follow the site: {cmd}')
        return
    pick = input(f"follow it (so its new posts are checked)? number to add, or n [{'1' if len(plan['feeds']) == 1 else 'n'}]: ").strip().lower()
    pick = pick or ('1' if len(plan['feeds']) == 1 else 'n')
    if not pick.isdigit() or not 1 <= int(pick) <= len(plan['feeds']):
        print(f'  not following; later: {cmd}')
        return
    try:
        entry = add_source(plan['feeds'][int(pick) - 1], None, plan['kind'])
    except (ValueError, requests.RequestException) as e:
        print(f'  not added: {e}')
        return
    sync_sources(conn)
    conn.execute('update item set source_id=(select source_id from source where name=?) where item_id=?', (entry['name'], item_id))
    conn.commit()
    pieces, matches = publish(conn, BIRDS_DB)
    print(f"  following {entry['name']!r}; the page now counts as one of its pieces. next: python writing.py run --source \"{entry['name']}\"")


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
    fixed = 67  # the columns after the name, up to the notes
    width = terminal_width(default=10_000) if sys.stdout.isatty() else 10_000  # piped: all of it
    name_w = min(32, max(14, width - fixed - 20))
    notes_w = max(0, width - fixed - name_w - 1)
    clip = lambda text, n: text if len(text) <= n else text[:max(0, n - 1)] + '…' if n > 1 else ''
    head = f"{'source':{name_w}} {'kind':10} {'check':8} {'items':>5} {'matched':>8} {'about':>6} {'artists':>7}  {'latest post':11}  notes"
    print(clip(head, width))
    for r in rows:
        check = 'OFF' if not r['active'] else f"{r['interval_hours']:.0f}h" if r['interval_hours'] else 'new'
        pct = f"{100 * r['matched_items'] / r['items']:.0f}%" if r['items'] else '-'
        notes = [n for n in (
            'not listed (news)' if r['kind'] not in PUBLISH_KINDS else '', 'members' if r['members_only'] else '',
            'roundups' if r['roundup'] else '', f"! {r['last_error']}" if r['last_error'] else '',
            'no matches yet' if r['items'] and not r['matched_items'] else '',
            'quiet: nothing new in 60+ days' if r['latest'] and r['latest'] < iso(now() - timedelta(days=60)) else '') if n]
        line = (f"{clip(r['name'], name_w):{name_w}} {r['kind']:10} {check:8} {r['items']:5} {pct:>8} {r['about_items']:6} {r['artists']:7}  "
                f"{(r['latest'] or '')[:10]:11}  ")
        print((line + clip('; '.join(notes), notes_w)).rstrip() if width >= 10_000 else clip(line + '; '.join(notes), width).rstrip())
    print('\n' + '\n'.join(textwrap.wrap(f"{len(rows)} sources, {sum(r['items'] for r in rows)} items. matched = items with an accepted match; "
                                         f"about = items about an artist we've played.", min(width, 100))))


def cmd_toggle(args):
    if args.state == 'add':
        try:
            entry = add_source(args.name, args.as_name, args.kind, args.members, args.roundup)
        except (ValueError, requests.RequestException) as e:
            print(f'not added: {e}')
            return
        conn = connect()
        sync_sources(conn)
        print(f"next: python writing.py run --source \"{entry['name']}\"   (fetch it now; the weekly run will pick it up anyway)")
        return
    if args.state == 'remove':
        name = remove_source(args.name)
        if name:
            sync_sources(connect())
            print(f'removed {name!r}: switched off, and what it gave stays stored but is no longer published (run: python writing.py publish)')
        else:
            print(f'no source named {args.name!r}')
        return
    conn = connect()
    cur = conn.execute('update source set active=? where name=?', (1 if args.state == 'on' else 0, args.name))
    conn.commit()
    print('ok' if cur.rowcount else f'no source named {args.name!r}')


def describe_artists(birds, ids):
    """{artist id: 'British glam rock band · 12 shows'}: Wikidata's short description (else what MusicBrainz knows:
    type, country, first year), then how many shows they've been at, for telling artists apart."""
    ids = list(ids)
    out = {a: [] for a in ids}
    if not ids:
        return {}
    marks = ','.join('?' * len(ids))
    def rows(sql):
        try:
            return birds.execute(sql % marks, ids).fetchall()
        except sqlite3.OperationalError:  # a database without that table
            return []
    for a, d in rows('select artist_id, description from artist_description where description is not null and artist_id in (%s)'):
        out[a].append(d[:80])
    for a, t, country, begin in rows('select artist_id, type, country, begin_date from mb_artist where mbid is not null and artist_id in (%s)'):
        if not out[a]:
            out[a].append(', '.join(x for x in (t, country, (begin or '')[:4]) if x))
    for a, n in rows('select ta.artist_id, count(distinct pt.playlist_id) from track_artist ta join playlist_track pt using (track_id) '
                     'where ta.artist_id in (%s) group by ta.artist_id'):
        out[a].append(f"{n} show{'s' if n != 1 else ''}")
    return {a: ' · '.join(x for x in parts if x) for a, parts in out.items()}


def pick_artist(birds, name):
    """The artists named NAME: an exact match (any case) if there is one, otherwise the ones whose name contains it.
    A number is an artist id, for telling same-named artists apart."""
    if name.isdigit():
        return birds.execute('select artist_id, name from artist where artist_id=?', (int(name),)).fetchall()
    exact = birds.execute('select artist_id, name from artist where lower(name)=lower(?)', (name,)).fetchall()
    return exact or birds.execute('select artist_id, name from artist where name like ? order by name', (f'%{name}%',)).fetchall()


def veto(conn, birds, name, drop_all=False, dry_run=False, force=False, interactive=False, ask=input, out=print):
    """veto NAME: this artist's name alone never matches (a 'Dillinger' who isn't The Dillinger Escape Plan). Their
    matches resting on the name alone are dropped now and never come back (drop_all: every match of theirs).
    dry_run only lists what would go. interactive goes piece by piece (y drop this one, n keep it for good, a drop this and all
    the rest and veto the artist, q stop). Otherwise it lists them and asks, unless force. -> (matches dropped, changed?)"""
    found = pick_artist(birds, name)
    if len(found) != 1:
        about = describe_artists(birds, [a for a, _ in found[:15]])
        out('no artist called that' if not found else 'which one? (give the id)\n  ' + '\n  '.join(
            f"{n} (id {a}): {about.get(a) or 'nothing known'}" for a, n in found[:15]))
        return 0, False
    aid, artist = found[0]
    pieces = conn.execute("select m.item_id, i.title, i.url, s.name source, m.signals, m.status from match m "
                          "join item i using (item_id) join source s using (source_id) "
                          "where m.artist_id=? and m.status in ('auto','review','confirmed') order by i.published desc", (aid,)).fetchall()
    held = lambda p: bool(CORROBORATED & set(p['signals'].split(',')))  # an album or a link behind it: a plain veto keeps it
    would_go = [p for p in pieces if drop_all or not held(p)]
    about = describe_artists(birds, [aid]).get(aid)
    out(f'{artist}' + (f' ({about})' if about else '') + f': {len(pieces)} matches; {len(would_go)} would be dropped' +
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
            about = describe_artists(birds, [a for a, _ in found[:15]])
            print('no artist called that' if not found else 'which one? (give the id)\n  ' + '\n  '.join(
                f"{n} (id {a}): {about.get(a) or 'nothing known'}" for a, n in found[:15]))
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
    same = {}
    for a, n in names.items():
        same.setdefault(n.lower(), []).append(a)
    names = {a: (f'{n} (id {a})' if len(same[n.lower()]) > 1 else n) for a, n in names.items()}  # told apart when they share a name
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
    y, n, x = review_loop(conn, rows, names, open_url=webbrowser.open,
                          blurbs=describe_artists(birds, {r['artist_id'] for r in rows}))
    print(f'\nconfirmed {y}, rejected {n}, never-on-the-name {x}')
    if y:
        pieces, matches = publish(conn)
        print(f'published {pieces} pieces / {matches} matches to {BIRDS_DB}')


HELP_INTRO = ('writing.py: the index of music writing',)
HELP_SECTIONS = [
    (None, [('Weekly (after load_playlist.py): python refresh_writing.py',
             'does the whole routine in one go: re-match, check the feeds that are due, a gentle slice of the backfill, publish.')]),
    ('The pieces, for doing by hand:', [
        ('run [--all] [--source NAME]', "check the feeds that are due (every one with --all, or just NAME), match what's new, publish"),
        ('rematch', 'match every stored item again; needed after new artists arrive (refresh_writing.py does it)'),
        ('backfill [--source NAME] [--minutes M]', "reach back through a site's sitemaps (or its WordPress API / paged feed): a gentle slice, "
                                                  'polite and resumable. M is the time each source gets in each phase (default 5)'),
        ('episodes [--source NAME] [--pages N] [--minutes M] [--retry]',
         "podcast feeds often link every episode to the show's page: look each episode up on the show's own site (WordPress search, "
         'or a Squarespace list) and link to its page (run does 40 a show; raise N for a backlog; --retry looks again for the ones '
         'not found)'),
        ('deepen [--source NAME] [--pages N] [--minutes M]',
         'some feeds give only a teaser, so a name inside the article is never seen: read those pieces in full, newest first, and '
         'match again (run does 40 a source; raise N to drain a backlog)'),
        ('publish', 'copy the accepted matches into birdnest.db (run, backfill and review already do this)')]),
    ('Looking:', [
        ('sources', "every source: pieces stored, matched, about artists we've played, latest post, any trouble"),
        ('report [--days N]', 'what matched lately'),
        ('why URL [ARTIST]', 'one piece: the text we hold, what it matched, what the matcher says now, and whether an artist you '
                             'expected to see is in our data and in that text')]),
    ('Deciding:', [
        ('review [--artist NAME] [--list]', "go through the borderline matches: y it's about them, n it isn't, x never match this "
                                            'artist on the name alone, s skip, o open the piece, q quit (--list just prints them)'),
        ('veto ARTIST [-n|-f|-i] [--all]',
         "an artist's name alone never matches (a 'Dillinger' who isn't The Dillinger Escape Plan): drops their matches that rest on "
         "the name, for good, after asking. -n (--dry-run) only lists them, -f (--force) doesn't ask, -i (--interactive) goes piece "
         'by piece for when only some are wrong; --all also drops matches with an album or link behind them. unveto ARTIST undoes it'),
        ('hide URL [--reason WHY]', "keep one piece off the site (junk, not writing, a duplicate you'd rather not show)"),
        ('unhide URL', 'bring it back'),
        ('duplicates', "pieces a site serves at two URLs ('/blog/x' and '/news/x'): one is kept automatically"),
        ('add URL [--source NAME] [--kind K] [--title T] [--date D] [--text TEXT|-] [--artist NAME]... [-n] [-y]',
         "offer one page for matching: shows what it would match, then keeps it under its site's source or 'Elsewhere', and offers "
         'to follow the site if the page announces a feed. A page it cannot read is added anyway if you give --title (--text and '
         '--artist say what the page says and who it is about)'),
        ('source add URL [--kind K] [--name N] [--members] [--roundup]',
         'add a site: finds its feed, notes whether the backfill can read its sitemap, writes writing_sources.json. Then: '
         'run --source NAME to fetch it now'),
        ('source remove NAME', 'take one out of the file (switched off, not published; what it gave stays stored)'),
        ('source off|on NAME', 'stop or resume checking one source without removing it')]),
]
HELP_OUTRO = 'Whatever changes birdnest.db (publish does), commit it and deploy. See the top of writing.py for how it all works.'


def terminal_width(default=100, cap=110):
    """The terminal's width when writing to one (never wider than cap, for reading), else `default`."""
    return min(cap, shutil.get_terminal_size((default, 24)).columns - 1) if sys.stdout.isatty() else default


def help_text(width=100):
    """The help, wrapped to `width`: each command and what it does side by side when there is room, else one above the other."""
    left, out = 36, [HELP_INTRO[0]]
    wrap = lambda text, indent: textwrap.wrap(text, max(40, width), initial_indent=' ' * indent, subsequent_indent=' ' * indent)
    for title, entries in HELP_SECTIONS:
        out.append('')
        if title:
            out.append(title)
        for usage, what in entries:
            if not title:  # the weekly routine: a line, then what it does
                out += [usage] + wrap(what, 2)
            elif width >= 90 and len(usage) <= left - 2:
                lines = wrap(what, left)
                out.append(f'  {usage:<{left - 2}}' + lines[0].lstrip())
                out += lines[1:]
            else:
                out.append(f'  {usage}')
                out += wrap(what, 6)
    out += ['', *textwrap.wrap(HELP_OUTRO, width)]
    return '\n'.join(out)


def cmd_help(args):
    print(help_text(terminal_width()))


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
    dp = sub.add_parser('deepen', help='read the full text of items whose feed gave only a teaser')
    dp.add_argument('--source', help='just this one')
    dp.add_argument('--pages', type=int, default=40, help='most pages to fetch per source per run')
    dp.add_argument('--minutes', type=float, default=5, help='most time each source gets')
    dp.set_defaults(fn=cmd_deepen)
    ep = sub.add_parser('episodes', help="find podcast episodes' own pages")
    ep.add_argument('--source', help='just this one')
    ep.add_argument('--pages', type=int, default=40, help='most episodes to look up per show per run')
    ep.add_argument('--minutes', type=float, default=5, help='most time each show gets')
    ep.add_argument('--retry', action='store_true', help="look again for the episodes that weren't found")
    ep.set_defaults(fn=cmd_episodes)
    wy = sub.add_parser('why', help='what we stored and matched for one piece')
    wy.add_argument('url')
    wy.add_argument('artist', nargs='?', help='an artist you expected it to match')
    wy.set_defaults(fn=cmd_why)
    sub.add_parser('rematch', help='match everything stored again').set_defaults(fn=cmd_rematch)
    rp = sub.add_parser('report', help='what matched lately')
    rp.add_argument('--days', type=int, default=7)
    rp.set_defaults(fn=cmd_report)
    sub.add_parser('publish', help='copy accepted matches into birdnest.db').set_defaults(fn=cmd_publish)
    sub.add_parser('sources', help='every source and what it yields').set_defaults(fn=cmd_sources)
    so = sub.add_parser('source', help='add, remove, turn on or off a source')
    so.add_argument('state', choices=['add', 'remove', 'on', 'off'])
    so.add_argument('name', help='its name; for add, the site or feed address')
    so.add_argument('--name', dest='as_name', help='add: what to call it (default: the site\'s own name)')
    so.add_argument('--kind', default='criticism', choices=KINDS, help='add: criticism (default), essay, interview, podcast or news (matched but not listed)')
    so.add_argument('--members', action='store_true', help='add: mostly for paying members (we still only use the public feed)')
    so.add_argument('--roundup', action='store_true', help='add: posts that cover many artists at once')
    so.set_defaults(fn=cmd_toggle)
    ad = sub.add_parser('add', help='offer one page for matching')
    ad.add_argument('url')
    ad.add_argument('--source', help="file it under this source (default: the source for its site, else 'Elsewhere')")
    ad.add_argument('--kind', default='criticism', choices=[k for k in KINDS if k != 'news'],
                    help="for 'Elsewhere': criticism (default), essay, interview or podcast")
    ad.add_argument('--title', help="when the page doesn't give one")
    ad.add_argument('--date', help='YYYY-MM-DD, when the page does not give one or has it wrong')
    ad.add_argument('--text', help="the page's text, when it can't be read or is cut off by a paywall (- reads it from stdin)")
    ad.add_argument('--artist', action='append', metavar='NAME_OR_ID', help='say it is about this artist (repeat for several)')
    ad.add_argument('-n', '--dry-run', action='store_true', help='only show what it would match')
    ad.add_argument('-y', '--yes', action='store_true', help="don't ask first (and don't offer to follow a feed)")
    ad.set_defaults(fn=cmd_add)
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
