"""Backfill: reach back past what a feed shows by reading a source's sitemaps.

A sitemap lists every page on a site, and most article URLs carry the artist and album in their slug
('takaat-is-noise-vol-3'), so we match the slugs first, locally, and fetch only the pages that look like they are
about an artist we've played. Progress is kept in writing_crawl.db (which sitemaps are read, which candidate pages
are fetched) so a run can stop and carry on.

Gentle by design: robots.txt is respected (and its Crawl-delay if longer), no more than one request every
HOST_DELAY seconds to a host, small default budgets per source per run so the archive fills in over weeks, and a
source is left alone for the rest of a run after a 429 or 503, or MAX_ERRORS failures. A site with no usable sitemap
("backfill": "feed" in writing_sources.json) is paged through its WordPress API (100 posts a request) or its feed
(?paged=N) instead.

    python writing.py backfill [--source NAME] [--sitemaps N] [--pages N] [--feed-requests N]
"""
import difflib
import gzip
import html
import json
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import quote, urljoin, urlparse

import requests

import writing
from writing import USER_AGENT, canonical, iso, now, parse_date

SCHEMA = """
create table if not exists backfill_sitemap (
    source_id integer, sitemap_url text, fetched_at text, n_urls integer, primary key (source_id, sitemap_url));
create table if not exists backfill_feed (
    source_id integer primary key, method text, next_page integer, done integer default 0, updated_at text);
create table if not exists backfill_candidate (
    source_id integer, url text, lastmod text, score real, fetched integer default 0, primary key (source_id, url));
"""
SKIP_PATH = re.compile(r'/(tags?|categor(?:y|ies)|authors?|topics?|labels?|page|search|artists?|genres?|feed|comments?|'
                       r'wp-content|wp-json|attachment)/', re.I)
MUSIC_LINK = re.compile(r'bandcamp\.com|spotify\.com|musicbrainz\.org|discogs\.com', re.I)
MAX_SITEMAP_BYTES, MAX_PAGE_BYTES = 40_000_000, 2_000_000
HOST_DELAY, MAX_ERRORS = 2.0, 3  # seconds between requests to one host; failures before leaving a source for the run
SOURCE_SECONDS = 300  # most time one source gets in each phase of a run, so a site that asks for a long Crawl-delay gets a small slice
_lock, _robots, _last, _delay = threading.Lock(), {}, {}, {}


class Throttled(Exception):
    """The site asked us to slow down (429, 503): leave it alone for the rest of this run."""


def polite(url, session, stream=False, accept=None):
    host = urlparse(url).netloc
    with _lock:
        gap = _delay.get(host, HOST_DELAY)
        wait = _last.get(host, 0) + gap - time.monotonic()
        _last[host] = max(time.monotonic(), _last.get(host, 0) + gap)
    if wait > 0:
        time.sleep(wait)
    r = session.get(url, headers={'User-Agent': USER_AGENT, **({'Accept': accept} if accept else {})}, timeout=30, stream=stream)
    if r.status_code in (429, 503):
        raise Throttled(f'{r.status_code} from {host}' + (f" (Retry-After {r.headers['Retry-After']})" if r.headers.get('Retry-After') else ''))
    return r


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
            wanted = rp.crawl_delay(USER_AGENT) or rp.crawl_delay('*')
            if wanted and wanted > HOST_DELAY:
                _delay[u.netloc] = float(wanted)
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


def note_delay(src, url, log, said):
    d = _delay.get(urlparse(url).netloc)
    if d and not said:
        log(f"  {src['name']}: robots.txt asks for {d:.0f}s between requests; it will get fewer pages this run")
        return True
    return said


def scan_source(src, matcher, done, max_sitemaps, known, log, seconds=SOURCE_SECONDS):
    """Phase A, in a thread: walk the sitemaps not yet read, match slugs, return (sitemaps read, candidates)."""
    session = requests.Session()
    deadline, said = time.monotonic() + seconds, False
    queue, seen, read, cands, errors = list(sitemap_roots(src, session)), set(), [], {}, 0
    while queue and len(read) < max_sitemaps and errors < MAX_ERRORS and time.monotonic() < deadline:
        sm = queue.pop(0)
        if sm in done or sm in seen:
            continue
        seen.add(sm)
        try:
            if not allowed(sm, session):
                log(f"  {src['name']}: robots.txt disallows {sm}")
                continue
            said = note_delay(src, sm, log, said)
            entries = read_sitemap(sm, session)
        except Throttled as e:
            log(f"  ! {src['name']}: {e}; leaving it for now")
            break
        except Exception as e:
            errors += 1
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
    """-> {title, published, tags, excerpt, body_text, links, feeds} or None when robots.txt says no or it isn't HTML."""
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
    return {'title': title, 'published': parse_date(date), 'tags': tags, 'feeds': writing.feed_links(raw, url), 'body_text': re.sub(r'\s+', ' ', body)[:writing.MAX_BODY_CHARS],
            'excerpt': re.sub(r'\s+', ' ', first('og:description', 'description'))[:writing.EXCERPT_CHARS], 'links': links}


def slug_title(url):
    return re.sub(r'[-_]+', ' ', urlparse(url).path.rstrip('/').rsplit('/', 1)[-1]).strip()


def fetch_candidates(src, urls, log, seconds=SOURCE_SECONDS):
    """Phase B, in a thread: fetch these pages, as many as the time budget allows. -> {url: page dict or None}"""
    session = requests.Session()
    out, errors, deadline, said = {}, 0, time.monotonic() + seconds, False
    for url in urls:
        if time.monotonic() > deadline:
            break
        try:
            said = note_delay(src, url, log, said)
            out[url] = fetch_page(url, session, src['name'])
        except Throttled as e:
            log(f"  ! {src['name']}: {e}; stopping here for this run")
            break
        except Exception as e:
            errors += 1
            log(f"  ! {src['name']}: {url}: {type(e).__name__}: {str(e)[:80]}")
            out[url] = None
            if errors >= MAX_ERRORS:
                log(f"  ! {src['name']}: {MAX_ERRORS} failures; stopping here for this run")
                break
    return out


def _wp_posts(root, page, session):
    """One page (100 posts) of a WordPress site's own API -> list of items, or None when there is no such page."""
    url = (f'{root}/wp-json/wp/v2/posts?per_page=100&page={page}&orderby=date&order=desc'
           '&_fields=link,title,date_gmt,excerpt')
    if not allowed(url, session):
        raise RuntimeError('robots.txt disallows the API')
    r = polite(url, session)
    if r.status_code == 400:  # past the last page
        return None
    r.raise_for_status()
    out = []
    for p in r.json():
        out.append({'url': canonical(p['link']), 'title': plain_html(p['title']['rendered']),
                    'published': parse_date((p.get('date_gmt') or '') + 'Z' if p.get('date_gmt') else None),
                    'tags': [], 'excerpt': plain_html(p['excerpt']['rendered'])[:writing.EXCERPT_CHARS],
                    'body_text': plain_html(p['excerpt']['rendered'])[:writing.MAX_BODY_CHARS], 'links': []})
    return out


def plain_html(s):
    return writing.plain(s)


def page_feed(src, method, next_page, max_requests, log):
    """Phase C, in a thread: walk back through a site's archive a few requests at a time.
    -> (method, next_page, done, items). Methods: 'wp-json' (100 posts a request), 'paged' (the feed's ?paged=N)."""
    session = requests.Session()
    root = '{0.scheme}://{0.netloc}'.format(urlparse(src['home_url'] or src['feed_url']))
    items, failed = [], False
    methods = [method] if method else ['wp-json', 'paged']
    for m in methods:
        page, first_urls, got = next_page or 1, None, 0
        try:
            for _ in range(max_requests):
                if m == 'wp-json':
                    batch = _wp_posts(root, page, session)
                else:
                    sep = '&' if '?' in src['feed_url'] else '?'
                    url = f"{src['feed_url']}{sep}paged={page}"
                    if not allowed(url, session):
                        raise RuntimeError('robots.txt disallows the feed')
                    r = polite(url, session, accept='application/rss+xml, application/atom+xml, application/xml')
                    batch = None if r.status_code in (400, 404) else writing.parse_feed(r.content, url) if r.ok else r.raise_for_status()
                    if batch and page > 1 and first_urls and batch[0]['url'] == first_urls:
                        batch = None  # the site ignores ?paged: page 2 is page 1 again
                    if batch and page == 1:
                        first_urls = batch[0]['url']
                if not batch:
                    return m, page, True, items
                items += batch
                got += len(batch)
                page += 1
            return m, page, False, items
        except Throttled as e:
            log(f"  ! {src['name']}: {e}; leaving it for now")
            return m, next_page or 1, False, items
        except Exception as e:
            log(f"  ! {src['name']}: {m}: {type(e).__name__}: {str(e)[:80]}")
            if got or method:  # worked before, or the method is settled: try again next time
                return m, page, False, items
            if not (isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code in (401, 403, 404)):
                failed = True  # not a plain 'no such thing': maybe a bad moment, so don't write the site off
    return ('none', 1, False, items) if failed else ('none', 1, True, items)


def store_item(conn, matcher, source_id, it, stamp):
    """Insert an item (unless we have it) and match it. -> True if it was new."""
    cur = conn.execute('insert or ignore into item (source_id, url, title, published, author, tags, excerpt, body_text, '
                       'links, first_seen) values (?,?,?,?,?,?,?,?,?,?)',
                       (source_id, it['url'], it['title'], it['published'], None, json.dumps(it['tags']), it['excerpt'],
                        it['body_text'], json.dumps(it['links']), stamp))
    if not cur.rowcount:
        return False
    writing.match_item(conn, matcher, cur.lastrowid, it['title'], it['tags'], it['url'], it['body_text'] or it['excerpt'], it['links'], stamp)
    return True


DEEPEN_PROBE = 20      # pages read before judging whether a source's articles hold more than its feed does
DEEPEN_MIN_GAIN = 0.1  # ...and the share of them that must have, or the source is left alone


def probe_limit(tried, budget):
    """How many to try next: until a source has had DEEPEN_PROBE tries, only enough to judge whether it is worth going on."""
    return min(budget, DEEPEN_PROBE - tried) if tried < DEEPEN_PROBE else budget


def deepen(conn, matcher, sources, pages=40, workers=4, seconds=SOURCE_SECONDS, log=print):
    """Some feeds (PopMatters, Loud and Quiet, Chicago Reader...) carry only a one-sentence teaser, so a name that is
    only inside the article never matches. Read the full page of items holding less than THIN_CHARS of text, newest
    first, at most `pages` per source, then match them again. Each is tried once (item.deepened: 1 read, 2 no text). A source
    whose first DEEPEN_PROBE pages gave more text in under DEEPEN_MIN_GAIN of cases is not tried again (podcasts' show notes
    are usually already the whole text).
    -> number of items read."""
    sources = list(sources)
    budget, stamp, read = {s['source_id']: pages for s in sources}, iso(now()), 0
    while sources:  # rounds: the first is the probe; a source that passed it carries on, up to `pages` in all
        todo, batch = {}, []
        for s in sources:
            tried, gained = conn.execute('select count(*), coalesce(sum(deepened=1), 0) from item where source_id=? and deepened in (1, 2)',
                                         (s['source_id'],)).fetchone()
            if tried >= DEEPEN_PROBE and gained < tried * DEEPEN_MIN_GAIN:
                log(f"  {s['name']}: not reading teasers in full any more (only {gained} of {tried} pages had more text than the feed)")
                continue
            rows = conn.execute(
                'select item_id, url from item where source_id=? and deepened is null and length(coalesce(body_text, \'\')) < ? '
                'and url not in (select url from item_hidden) order by published desc limit ?',
                (s['source_id'], writing.THIN_CHARS, probe_limit(tried, budget[s['source_id']]))).fetchall()
            if rows:
                todo[s['source_id']] = rows
                batch.append(s)
        by_id, sources = {s['source_id']: s for s in batch}, []
        with ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(fetch_candidates, by_id[i], [r['url'] for r in rows], log, seconds): i for i, rows in todo.items()}
            for fut in as_completed(futs):
                i = futs[fut]
                pages_read, n, done = fut.result(), 0, 0
                for r in todo[i]:
                    if r['url'] not in pages_read:
                        continue  # not reached this run (time ran out or the site said slow down): try again next time
                    done += 1
                    page = pages_read[r['url']]
                    item = conn.execute('select * from item where item_id=?', (r['item_id'],)).fetchone()
                    if page and len(page['body_text']) > len(item['body_text'] or ''):
                        links = sorted(set(json.loads(item['links'] or '[]')) | set(page['links']))[:60]
                        conn.execute('update item set body_text=?, links=?, deepened=1 where item_id=?',
                                     (page['body_text'], json.dumps(links), r['item_id']))
                        writing.match_item(conn, matcher, r['item_id'], item['title'], json.loads(item['tags'] or '[]'), item['url'],
                                           page['body_text'], links, stamp)
                        n += 1
                    else:
                        conn.execute('update item set deepened=2 where item_id=?', (r['item_id'],))
                conn.commit()
                read += n
                budget[i] -= done
                left = conn.execute('select count(*) from item where source_id=? and deepened is null and length(coalesce(body_text, \'\')) < ?',
                                    (i, writing.THIN_CHARS)).fetchone()[0]
                log(f"  {by_id[i]['name']}: read {n} of {len(todo[i])} teasers in full" + (f", {left} left" if left else ''))
                if done < len(todo[i]):
                    log(f"  {by_id[i]['name']}: stopped early (time ran out or the site said slow down): run it again to carry on")
                elif budget[i] > 0:
                    sources.append(by_id[i])  # finished its round with pages to spare: judge it again, and go on if it is worth it
                elif left:
                    log(f"  {by_id[i]['name']}: reached the {pages} pages for this run; run it again for the rest")
    return read


class NoLookup(Exception):
    """This site has no search we can use (no such API, not JSON, robots.txt says no): don't try its other episodes."""


def _fold(s):
    s = unicodedata.normalize('NFKD', html.unescape(s or '')).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'[^a-z0-9]+', ' ', s).strip()
    return re.sub(r'^(episode|ep|e)\s*\d+\s*', '', s)  # a site's 'Episode 55:' in front


def same_title(a, b):
    """The same episode title, allowing for a site's 'Episode 55:' in front or different punctuation and accents."""
    a, b = _fold(a), _fold(b)
    if not a or not b:
        return False
    if (a in b or b in a) and min(len(a), len(b)) >= 0.6 * max(len(a), len(b)):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.9


def wp_episode_page(root, title, show_url, session):
    """A WordPress site's own page for the episode with this title, from its search API, or None. The search wants every
    word to match, so it asks with the title's words (no punctuation or accents), then with just its first few. Raises
    NoLookup when the site has no such API, Throttled when it says slow down."""
    words = _fold(title).split()
    queries = list(dict.fromkeys([' '.join(words[:25]), ' '.join(words[:5])]))
    host = urlparse(root).netloc.removeprefix('www.')
    where = lambda u: (urlparse(u).netloc.removeprefix('www.'), urlparse(u).path.rstrip('/'))  # not the scheme: http or https
    show = where(show_url)
    for q in queries:
        url = f'{root}/wp-json/wp/v2/search?search={quote(q)}&per_page=10&_fields=title,url,subtype'
        if not allowed(url, session):
            raise NoLookup('robots.txt disallows the API')
        r = polite(url, session)
        if r.status_code in (400, 401, 403, 404, 405, 410):
            raise NoLookup(f'no search API ({r.status_code})')
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise NoLookup('not a WordPress API')
        if not isinstance(data, list):
            raise NoLookup('not a WordPress API')
        for d in data:
            found = d.get('url') or ''
            if urlparse(found).netloc.removeprefix('www.') == host and where(found) != show and same_title(d.get('title'), title):
                return found
    return None


def lookup_episodes(src, rows, log, seconds=SOURCE_SECONDS):
    """In a thread: look up these episodes. -> ({item_id: page url or None}, no_api reason or None)"""
    session = requests.Session()
    out, errors, deadline = {}, 0, time.monotonic() + seconds
    for r in rows:
        if time.monotonic() > deadline:
            break
        show_url = r['url'].split('#')[0]
        try:
            out[r['item_id']] = wp_episode_page('https://' + urlparse(show_url).netloc, r['title'], show_url, session)
        except NoLookup as e:
            return out, str(e)
        except Throttled as e:
            log(f"  ! {src['name']}: {e}; stopping here for this run")
            break
        except requests.RequestException as e:
            errors += 1
            log(f"  ! {src['name']}: {type(e).__name__}: {str(e)[:80]}")
            if errors >= MAX_ERRORS:
                break
    return out, None


def resolve_episodes(conn, sources, pages=40, workers=4, seconds=SOURCE_SECONDS, log=print):
    """Podcast feeds often give every episode the show's page as its link (stored as page#guid), so a visitor lands on the
    show, not the episode. Look the episode up on the show's own site, newest first, at most `pages` per show, and keep the
    page it finds (item.page_url; page_tried 1 found, 2 not). A show whose site has no usable search, or whose first
    DEEPEN_PROBE lookups mostly found nothing, is left alone. -> number of episode pages found."""
    sources = list(sources)
    budget, total = {s['source_id']: pages for s in sources}, 0
    while sources:  # rounds, as in deepen: the first is the probe, and a source that passed it carries on
        todo, batch = {}, []
        for s in sources:
            tried, found = conn.execute('select count(*), coalesce(sum(page_tried=1), 0) from item where source_id=? and page_tried in (1, 2)',
                                        (s['source_id'],)).fetchone()
            if tried >= DEEPEN_PROBE and found < tried * DEEPEN_MIN_GAIN:
                log(f"  {s['name']}: not looking up episode pages any more (only {found} of {tried} lookups found one)")
                continue
            rows = conn.execute(
                "select item_id, url, title from item where source_id=? and page_tried is null and url like '%#%' "
                'and url not in (select url from item_hidden) order by published desc limit ?',
                (s['source_id'], probe_limit(tried, budget[s['source_id']]))).fetchall()
            if rows:
                todo[s['source_id']] = rows
                batch.append(s)
        by_id, sources = {s['source_id']: s for s in batch}, []
        with ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(lookup_episodes, by_id[i], rows, log, seconds): i for i, rows in todo.items()}
            for fut in as_completed(futs):
                i = futs[fut]
                got, no_api = fut.result()
                for item_id, page in got.items():
                    if page:
                        conn.execute('update item set page_url=?, page_tried=1 where item_id=?', (page, item_id))
                    else:
                        conn.execute('update item set page_tried=2 where item_id=?', (item_id,))
                n = sum(1 for p in got.values() if p)
                total += n
                budget[i] -= len(got)
                if no_api:  # the same for every episode of this show
                    conn.execute("update item set page_tried=2 where source_id=? and page_tried is null and url like '%#%'", (i,))
                    log(f"  {by_id[i]['name']}: no episode pages from its site ({no_api})")
                else:
                    log(f"  {by_id[i]['name']}: found {n} of {len(got)} episode pages")
                conn.commit()
                left = conn.execute("select count(*) from item where source_id=? and page_tried is null and url like '%#%'", (i,)).fetchone()[0]
                if no_api or not left:
                    continue
                if len(got) < len(todo[i]):
                    log(f"  {by_id[i]['name']}: stopped early (time ran out or the site said slow down): run it again to carry on")
                elif budget[i] > 0:
                    sources.append(by_id[i])
                else:
                    log(f"  {by_id[i]['name']}: reached the {pages} lookups for this run; {left} more: run it again")
    return total


def backfill(conn, matcher, sources, max_sitemaps=15, max_pages=50, feed_requests=5, workers=4, seconds=SOURCE_SECONDS, log=print):
    conn.executescript(SCHEMA)
    started = iso(now())
    by_feed = [s for s in sources if (s['backfill'] or 'sitemap') == 'feed']
    by_map = [s for s in sources if s not in by_feed and s['backfill'] != 'none']  # 'none': single pages, no archive
    new = 0
    # phase C: sources without sitemaps, a few requests each
    state = {r['source_id']: r for r in conn.execute('select * from backfill_feed')}
    with ThreadPoolExecutor(workers) as ex:
        futs = {s['source_id']: ex.submit(page_feed, s, (state.get(s['source_id']) or {'method': None})['method'],
                                          (state.get(s['source_id']) or {'next_page': 1})['next_page'], feed_requests, log)
                for s in by_feed if not (state.get(s['source_id']) and state[s['source_id']]['done'])}
        for s in by_feed:
            if s['source_id'] not in futs:
                log(f"  {s['name']}: archive already read")
        by_id = {s['source_id']: s for s in by_feed}
        for fut in as_completed(list(futs.values())):
            s = by_id[next(i for i, f in futs.items() if f is fut)]
            method, next_page, done, items = fut.result()
            added = sum(store_item(conn, matcher, s['source_id'], it, started) for it in items)
            conn.execute('insert or replace into backfill_feed values (?,?,?,?,?)', (s['source_id'], method, next_page, int(done), started))
            conn.commit()
            new += added
            log(f"  {s['name']}: {method}, {len(items)} posts seen, {added} new" + (', archive complete' if done else f', next page {next_page}'))
    sources = by_map
    # phase A: sitemaps
    known = {r[0] for r in conn.execute('select url from item')}
    jobs = {s['source_id']: {r[0] for r in conn.execute('select sitemap_url from backfill_sitemap where source_id=?', (s['source_id'],))}
            for s in sources}
    with ThreadPoolExecutor(workers) as ex:
        futs = {s['source_id']: ex.submit(scan_source, s, matcher, jobs[s['source_id']], max_sitemaps, known, log, seconds) for s in sources}
        by_id = {s['source_id']: s for s in sources}
        for fut in as_completed(list(futs.values())):
            s = by_id[next(i for i, f in futs.items() if f is fut)]
            read, cands = fut.result()
            for sm, n in read:
                conn.execute('insert or replace into backfill_sitemap values (?,?,?,?)', (s['source_id'], sm, started, n))
            for url, (lastmod, score) in cands.items():
                conn.execute('insert or ignore into backfill_candidate (source_id, url, lastmod, score) values (?,?,?,?)',
                             (s['source_id'], url, lastmod, score))
            conn.commit()
            log(f"  {s['name']}: read {len(read)} sitemaps ({sum(n for _, n in read)} urls), {len(cands)} new candidate pages")
    # phase B: the candidate pages, best first, up to max_pages per source
    todo = {s['source_id']: [r[0] for r in conn.execute(
        'select url from backfill_candidate where source_id=? and not fetched order by score desc, lastmod desc limit ?',
        (s['source_id'], max_pages))] for s in sources}
    stamp = iso(now())
    with ThreadPoolExecutor(workers) as ex:
        futs = {s['source_id']: ex.submit(fetch_candidates, s, todo[s['source_id']], log, seconds) for s in sources if todo[s['source_id']]}
        by_id = {s['source_id']: s for s in sources}
        for fut in as_completed(list(futs.values())):
            s = by_id[next(i for i, f in futs.items() if f is fut)]
            pages = fut.result()
            lastmods = dict(conn.execute('select url, lastmod from backfill_candidate where source_id=?', (s['source_id'],)).fetchall())
            added = 0
            for url, page in pages.items():
                page = page or {}
                it = {'url': url, 'title': page.get('title') or slug_title(url), 'published': page.get('published') or lastmods.get(url),
                      'tags': page.get('tags', []), 'excerpt': page.get('excerpt', ''),
                      'body_text': page.get('body_text') or page.get('excerpt') or '', 'links': page.get('links', [])}
                added += store_item(conn, matcher, s['source_id'], it, stamp)
                conn.execute('update backfill_candidate set fetched=1 where source_id=? and url=?', (s['source_id'], url))
            conn.commit()
            new += added
            left = conn.execute('select count(*) from backfill_candidate where source_id=? and not fetched', (s['source_id'],)).fetchone()[0]
            log(f"  {s['name']}: fetched {len(pages)} pages, {added} new items" + (f", {left} candidates left" if left else ''))
    return new
