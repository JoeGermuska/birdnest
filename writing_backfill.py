"""Backfill: reach back past what a feed shows by reading a source's sitemaps.

A sitemap lists every page on a site, and most article URLs carry the artist and album in their slug
('takaat-is-noise-vol-3'), so we match the slugs first, locally, and fetch only the pages that look like they are
about an artist we've played. Progress is kept in writing_crawl.db (which sitemaps are read, which candidate pages
are fetched) so a run can stop and carry on. robots.txt is respected, one request a second per host.

    python writing.py backfill [--source NAME] [--sitemaps N] [--pages N]
"""
import gzip
import html
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests

import writing
from writing import USER_AGENT, canonical, iso, now, parse_date

SCHEMA = """
create table if not exists backfill_sitemap (
    source_id integer, sitemap_url text, fetched_at text, n_urls integer, primary key (source_id, sitemap_url));
create table if not exists backfill_candidate (
    source_id integer, url text, lastmod text, score real, fetched integer default 0, primary key (source_id, url));
"""
SKIP_PATH = re.compile(r'/(tags?|categor(?:y|ies)|authors?|topics?|labels?|page|search|artists?|genres?|feed|comments?|'
                       r'wp-content|wp-json|attachment)/', re.I)
MUSIC_LINK = re.compile(r'bandcamp\.com|spotify\.com|musicbrainz\.org|discogs\.com', re.I)
MAX_SITEMAP_BYTES, MAX_PAGE_BYTES = 40_000_000, 2_000_000
_lock, _robots, _last = threading.Lock(), {}, {}


def polite(url, session, stream=False, accept=None):
    host = urlparse(url).netloc
    with _lock:
        wait = _last.get(host, 0) + 1.0 - time.monotonic()
        _last[host] = max(time.monotonic(), _last.get(host, 0) + 1.0)
    if wait > 0:
        time.sleep(wait)
    return session.get(url, headers={'User-Agent': USER_AGENT, **({'Accept': accept} if accept else {})}, timeout=30, stream=stream)


def allowed(url, session):
    u = urlparse(url)
    root = f'{u.scheme}://{u.netloc}'
    with _lock:
        rp = _robots.get(root)
    if rp is None:
        rp = robotparser.RobotFileParser()
        try:
            r = polite(root + '/robots.txt', session)
            rp.parse(r.text.splitlines()) if r.status_code < 400 else rp.parse([])
        except requests.RequestException:
            rp.parse([])
        with _lock:
            _robots[root] = rp
    return rp.can_fetch(USER_AGENT, url)


def sitemap_roots(src, session):
    """The sitemap URLs a site announces in robots.txt, else the usual places."""
    root = '{0.scheme}://{0.netloc}'.format(urlparse(src['home_url'] or src['feed_url']))
    found = []
    try:
        r = polite(root + '/robots.txt', session)
        if r.status_code < 400:
            found = re.findall(r'(?im)^sitemap:\s*(\S+)', r.text)
    except requests.RequestException:
        pass
    return found or [root + p for p in ('/sitemap.xml', '/sitemap_index.xml', '/wp-sitemap.xml')]


def read_sitemap(url, session):
    """-> [(kind, loc, lastmod)] with kind 'sitemap' (another sitemap) or 'url'."""
    r = polite(url, session, stream=True)
    r.raise_for_status()
    data = r.raw.read(MAX_SITEMAP_BYTES, decode_content=True)
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    out = []
    for el in ET.fromstring(data):
        kind = 'sitemap' if el.tag.endswith('sitemap') else 'url' if el.tag.endswith('url') else None
        if not kind:
            continue
        loc = lastmod = None
        for c in el:
            name = c.tag.split('}')[-1]
            if name == 'loc':
                loc = (c.text or '').strip()
            elif name == 'lastmod':
                lastmod = (c.text or '').strip()
        if loc:
            out.append((kind, loc, parse_date(lastmod)))
    return out


def scan_source(src, matcher, done, max_sitemaps, known, log):
    """Phase A, in a thread: walk the sitemaps not yet read, match slugs, return (sitemaps read, candidates)."""
    session = requests.Session()
    queue, seen, read, cands = list(sitemap_roots(src, session)), set(), [], {}
    while queue and len(read) < max_sitemaps:
        sm = queue.pop(0)
        if sm in done or sm in seen:
            continue
        seen.add(sm)
        try:
            if not allowed(sm, session):
                log(f"  {src['name']}: robots.txt disallows {sm}")
                continue
            entries = read_sitemap(sm, session)
        except Exception as e:
            log(f"  ! {src['name']}: {sm}: {type(e).__name__}: {str(e)[:80]}")
            continue
        n = 0
        for kind, loc, lastmod in entries:
            if kind == 'sitemap':
                queue.append(loc)
                continue
            n += 1
            url = canonical(loc)
            if url in known or SKIP_PATH.search(url):
                continue
            ms = matcher.match(title='', url=url)
            if ms:
                cands[url] = (lastmod, ms[0]['score'])
        if n:  # a sitemap of other sitemaps is walked, not counted as read
            read.append((sm, n))
        else:
            done = done | {sm}
    return read, cands


class _Page(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'nav', 'footer', 'header', 'aside', 'form', 'svg'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.title, self.links, self.time = {}, '', [], None
        self.in_title, self.skip, self.art, self.art_text, self.all_text = False, 0, 0, [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta' and (a.get('property') or a.get('name')):
            self.meta.setdefault((a.get('property') or a.get('name')).lower(), []).append(a.get('content') or '')
        elif tag == 'title':
            self.in_title = True
        elif tag == 'a' and a.get('href'):
            self.links.append(a['href'])
        elif tag == 'time' and a.get('datetime') and not self.time:
            self.time = a['datetime']
        if tag in self.SKIP:
            self.skip += 1
        elif tag == 'article':
            self.art += 1

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == 'article' and self.art:
            self.art -= 1

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if not self.skip and data.strip():
            self.all_text.append(data.strip())
            if self.art:
                self.art_text.append(data.strip())


def fetch_page(url, session, base_name=''):
    """-> {title, published, tags, excerpt, body_text, links} or None when robots.txt says no or it isn't HTML."""
    if not allowed(url, session):
        return None
    r = polite(url, session, stream=True, accept='text/html')
    if r.status_code >= 400 or 'html' not in r.headers.get('content-type', ''):
        return None
    raw = r.raw.read(MAX_PAGE_BYTES, decode_content=True).decode(r.encoding or 'utf-8', 'ignore')
    p = _Page()
    p.feed(raw)
    first = lambda *keys: next((p.meta[k][0] for k in keys if p.meta.get(k)), '')
    title = html.unescape(first('og:title', 'twitter:title') or p.title).strip()
    title = re.sub(r'\s*[|]\s*[^|]*$', '', title)  # 'Review | Site name'
    if base_name:
        title = re.sub(r'\s+[-–—:]\s+' + re.escape(base_name) + r'\s*$', '', title, flags=re.I)
    date = first('article:published_time', 'og:article:published_time', 'date', 'datepublished')
    if not date:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', raw)
        date = m.group(1) if m else (p.time or '')
    body = ' '.join(p.art_text if len(' '.join(p.art_text)) > 300 else p.all_text)
    tags = [t.strip() for t in p.meta.get('article:tag', []) if t.strip()]
    links = sorted({urljoin(url, h) for h in p.links if MUSIC_LINK.search(h)})[:60]
    return {'title': title, 'published': parse_date(date), 'tags': tags, 'body_text': re.sub(r'\s+', ' ', body)[:writing.MAX_BODY_CHARS],
            'excerpt': re.sub(r'\s+', ' ', first('og:description', 'description'))[:writing.EXCERPT_CHARS], 'links': links}


def slug_title(url):
    return re.sub(r'[-_]+', ' ', urlparse(url).path.rstrip('/').rsplit('/', 1)[-1]).strip()


def fetch_candidates(src, urls, log):
    """Phase B, in a thread: fetch these pages. -> {url: page dict or None}"""
    session = requests.Session()
    out = {}
    for url in urls:
        try:
            out[url] = fetch_page(url, session, src['name'])
        except Exception as e:
            log(f"  ! {src['name']}: {url}: {type(e).__name__}: {str(e)[:80]}")
            out[url] = None
    return out


def backfill(conn, matcher, sources, max_sitemaps=60, max_pages=300, workers=6, log=print):
    conn.executescript(SCHEMA)
    started = iso(now())
    # phase A: sitemaps
    known = {r[0] for r in conn.execute('select url from item')}
    jobs = {s['source_id']: {r[0] for r in conn.execute('select sitemap_url from backfill_sitemap where source_id=?', (s['source_id'],))}
            for s in sources}
    with ThreadPoolExecutor(workers) as ex:
        futs = {s['source_id']: ex.submit(scan_source, s, matcher, jobs[s['source_id']], max_sitemaps, known, log) for s in sources}
        for s in sources:
            read, cands = futs[s['source_id']].result()
            for sm, n in read:
                conn.execute('insert or replace into backfill_sitemap values (?,?,?,?)', (s['source_id'], sm, started, n))
            for url, (lastmod, score) in cands.items():
                conn.execute('insert or ignore into backfill_candidate (source_id, url, lastmod, score) values (?,?,?,?)',
                             (s['source_id'], url, lastmod, score))
            conn.commit()
            log(f"  {s['name']}: read {len(read)} sitemaps ({sum(n for _, n in read)} urls), {len(cands)} candidate pages")
    # phase B: the candidate pages, best first, up to max_pages per source
    todo = {s['source_id']: [r[0] for r in conn.execute(
        'select url from backfill_candidate where source_id=? and not fetched order by score desc, lastmod desc limit ?',
        (s['source_id'], max_pages))] for s in sources}
    stamp, new = iso(now()), 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {s['source_id']: ex.submit(fetch_candidates, s, todo[s['source_id']], log) for s in sources if todo[s['source_id']]}
        for s in sources:
            if s['source_id'] not in futs:
                continue
            pages = futs[s['source_id']].result()
            lastmods = dict(conn.execute('select url, lastmod from backfill_candidate where source_id=?', (s['source_id'],)).fetchall())
            added = 0
            for url, page in pages.items():
                title = (page or {}).get('title') or slug_title(url)
                published = (page or {}).get('published') or lastmods.get(url)
                tags, links = (page or {}).get('tags', []), (page or {}).get('links', [])
                body = (page or {}).get('body_text') or (page or {}).get('excerpt') or ''
                cur = conn.execute('insert or ignore into item (source_id, url, title, published, author, tags, excerpt, body_text, '
                                   'links, first_seen) values (?,?,?,?,?,?,?,?,?,?)',
                                   (s['source_id'], url, title, published, None, json.dumps(tags), (page or {}).get('excerpt', ''),
                                    body, json.dumps(links), stamp))
                conn.execute('update backfill_candidate set fetched=1 where source_id=? and url=?', (s['source_id'], url))
                if cur.rowcount:
                    writing.match_item(conn, matcher, cur.lastrowid, title, tags, url, body, links, stamp)
                    added += 1
            conn.commit()
            new += added
            left = conn.execute('select count(*) from backfill_candidate where source_id=? and not fetched', (s['source_id'],)).fetchone()[0]
            log(f"  {s['name']}: fetched {len(pages)} pages, {added} new items" + (f", {left} candidates left" if left else ''))
    return new
