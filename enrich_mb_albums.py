"""Match albums to MusicBrainz and store links to other representations of them
(Wikipedia, Discogs, AllMusic, Bandcamp, Apple Music, ...) in album_link,
plus the release group (the album across its editions, with its original
release date) in mb_album and the release's labels in mb_album_label.

Matching, most reliable first:
  1. MusicBrainz releases that link to the album's Spotify URL (100 per request)
  2. a barcode search for the album's UPC, accepted only when every release with
     exactly that barcode belongs to one release group
Wikipedia comes from the release group's Wikidata item.

Incremental like enrich_musicbrainz.py: albums looked up before are skipped;
not-found ones are tried again after RETRY_DAYS (load_playlist.py
retries a few each week). Barcode matches are re-checked for a Spotify link
each run. Most-played albums first,
singles last. Shares MusicBrainz's one-request-a-second with the other scripts.
    python enrich_mb_albums.py [--cap N] [path/to/birdnest.db]
"""
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

from enrich_musicbrainz import URL_BATCH, USER_AGENT, get

RETRY_DAYS = 180
CHUNK = 200
_groups = {}  # release group mbid -> its lookup, shared by duplicate albums

SCHEMA = """
create table if not exists mb_album (
    album_id integer primary key references album(album_id),
    release_mbid varchar,        -- null when checked but not matched
    release_group_mbid varchar,
    method varchar,              -- spotify-url | barcode
    rg_title varchar, rg_type varchar, rg_secondary varchar,
    first_release_date varchar,  -- the release group's: the album's first release, in any edition
    checked_at timestamp default current_timestamp
);
create table if not exists mb_album_label (
    album_id integer references album(album_id), label_mbid varchar, name varchar, catalog_number varchar
);
create table if not exists album_link (
    album_id integer references album(album_id), source varchar, url varchar
);
"""


def _digits(barcode):
    return (barcode or '').lstrip('0')


def links_from(release_rels, group_rels):
    """{source: url} from the release's and the release group's URL relations."""
    out = {}
    for r in group_rels:
        url, kind = r['url']['resource'], r['type']
        if kind in ('wikidata', 'allmusic', 'discogs') and kind not in out:
            out[kind] = url
    for r in release_rels:
        url, kind = r['url']['resource'], r['type']
        if 'bandcamp.com' in url and 'bandcamp' not in out:
            out['bandcamp'] = url
        elif 'music.apple.com' in url and 'apple' not in out:
            out['apple'] = url
        elif kind == 'discogs' and 'discogs' not in out:
            out['discogs'] = url
    return out


def barcode_match(upc):
    """(release mbid, group mbid) when the barcode leads to exactly one release group, else None."""
    result = get('release', query=f'barcode:{_digits(upc)}', limit=25) or {}
    hits = [r for r in result.get('releases', []) if _digits(r.get('barcode')) == _digits(upc)]
    groups = {r['release-group']['id'] for r in hits}
    if len(groups) != 1:
        return None
    return hits[0]['id'], groups.pop()


def wikipedia_for(qids):
    """{qid: English Wikipedia URL} via the Wikidata API, 50 at a time."""
    out = {}
    qids = sorted(set(qids))
    for i in range(0, len(qids), 50):
        query = urllib.parse.urlencode({'action': 'wbgetentities', 'ids': '|'.join(qids[i:i + 50]),
                                        'props': 'sitelinks/urls', 'sitefilter': 'enwiki', 'format': 'json'})
        req = urllib.request.Request(f"https://www.wikidata.org/w/api.php?{query}", headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            for qid, ent in json.load(resp).get('entities', {}).items():
                link = (ent.get('sitelinks') or {}).get('enwiki')
                if link and link.get('url'):
                    out[qid] = link['url']
    return out


def main(db_path='birdnest.db', cap=None, album_ids=None, retries=None):
    """New albums (only album_ids, when given: load_playlist.py passes the night's), plus misses due a retry
    (at most `retries` of them, oldest first; all when None). First, barcode matches are checked for a
    Spotify link added since (by someone working through /todo/musicbrainz/albums)."""
    con = sqlite3.connect(db_path, timeout=60)
    con.executescript(SCHEMA)
    recheck_barcode_matches(con)
    only = f"and al.album_id in ({','.join(str(int(a)) for a in album_ids)})" if album_ids is not None else ''
    new = con.execute(f"""
        select al.album_id, al.spotify_id, al.upc, al.name from album al
        left join mb_album m using(album_id) left join track t using(album_id) left join playlist_track pt using(track_id)
        where al.spotify_id is not null and m.album_id is null {only}
        group by al.album_id order by al.album_type = 'single', count(pt.playlist_id) desc""").fetchall() if album_ids != [] else []
    due = con.execute(f"""
        select al.album_id, al.spotify_id, al.upc, al.name from album al join mb_album m using(album_id)
        where al.spotify_id is not null and m.release_mbid is null and m.checked_at < datetime('now', '-{RETRY_DAYS} days')
        order by m.checked_at limit ?""", (-1 if retries is None else retries,)).fetchall()
    todo = new + due
    if cap:
        todo = todo[:cap]
    print(f"{len(todo)} albums to look up")
    found = 0
    for i in range(0, len(todo), CHUNK):  # each chunk finishes and commits, so an interrupted run loses little
        found += lookup(con, todo[i:i + CHUNK])
        print(f"  {min(i + CHUNK, len(todo))}/{len(todo)}, {found} matched")
    print(f"done: {found} of {len(todo)} matched")


def recheck_barcode_matches(con):
    """Albums matched by barcode whose MusicBrainz release now links to the Spotify album: mark them matched by
    URL (they leave /todo/musicbrainz/albums). One request per 100 albums."""
    rows = con.execute("""select m.album_id, al.spotify_id from mb_album m join album al using(album_id)
                          where m.method = 'barcode' and al.spotify_id is not null""").fetchall()
    by_url = {f"https://open.spotify.com/album/{s}": a for a, s in rows}
    urls, linked = list(by_url), []
    for i in range(0, len(urls), URL_BATCH):
        result = get('url', resource=urls[i:i + URL_BATCH], inc='release-rels') or {}
        for u in result.get('urls', [result] if 'resource' in result else []):
            if any('release' in r for r in u.get('relations', [])) and u['resource'] in by_url:
                linked.append(by_url[u['resource']])
    with con:
        con.executemany("update mb_album set method = 'spotify-url' where album_id = ?", [(a,) for a in linked])
    if rows:
        print(f"{len(linked)} of {len(rows)} barcode matches now linked to Spotify on MusicBrainz")


def lookup(con, todo):
    # 1. releases that link to the Spotify album
    matches = {}  # album_id -> (release mbid, method)
    by_url = {f"https://open.spotify.com/album/{s}": a for a, s, _, _ in todo}
    for i in range(0, len(todo), URL_BATCH):
        urls = list(by_url)[i:i + URL_BATCH]
        result = get('url', resource=urls, inc='release-rels') or {}
        for u in result.get('urls', [result] if 'resource' in result else []):
            releases = [r['release']['id'] for r in u.get('relations', []) if 'release' in r]
            if releases and u['resource'] in by_url:
                matches[by_url[u['resource']]] = (releases[0], 'spotify-url')

    # 2. barcode search for the rest
    for album_id, _, upc, _ in todo:
        if album_id not in matches and upc:
            hit = barcode_match(upc)
            if hit:
                matches[album_id] = (hit[0], 'barcode')
    with con:
        con.executemany("insert or replace into mb_album (album_id) values (?)",
                        [(a,) for a, *_ in todo if a not in matches])

    # 3. details: the release's labels and links, its group's date and links
    for album_id, (mbid, method) in matches.items():
        rel = get(f"release/{mbid}", inc='release-groups+labels+url-rels') or {}
        rg = rel.get('release-group') or {}
        if rg.get('id') and rg['id'] not in _groups:
            _groups[rg['id']] = get(f"release-group/{rg['id']}", inc='url-rels') or {}
        group = _groups.get(rg.get('id'), {})
        links = links_from(rel.get('relations', []), group.get('relations', []))
        if rg.get('id'):
            links['musicbrainz'] = f"https://musicbrainz.org/release-group/{rg['id']}"
        with con:
            con.execute("""insert or replace into mb_album (album_id, release_mbid, release_group_mbid, method,
                           rg_title, rg_type, rg_secondary, first_release_date) values (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (album_id, mbid, rg.get('id'), method, rg.get('title'), rg.get('primary-type'),
                         ', '.join(rg.get('secondary-types') or []) or None, rg.get('first-release-date') or None))
            con.execute("delete from mb_album_label where album_id = ?", (album_id,))
            con.executemany("insert into mb_album_label values (?, ?, ?, ?)", [
                (album_id, (li.get('label') or {}).get('id'), (li.get('label') or {}).get('name'), li.get('catalog-number'))
                for li in rel.get('label-info', []) if li.get('label')])
            con.execute("delete from album_link where album_id = ?", (album_id,))
            con.executemany("insert into album_link values (?, ?, ?)", [(album_id, s, u) for s, u in links.items()])

    # 4. Wikipedia, from the release groups' Wikidata items
    rows = con.execute("select album_id, url from album_link where source = 'wikidata' and album_id not in "
                       "(select album_id from album_link where source = 'wikipedia')").fetchall()
    articles = wikipedia_for(u.rsplit('/', 1)[-1] for _, u in rows)
    with con:
        con.executemany("insert into album_link values (?, 'wikipedia', ?)",
                        [(a, articles[u.rsplit('/', 1)[-1]]) for a, u in rows if u.rsplit('/', 1)[-1] in articles])
    return len(matches)


if __name__ == '__main__':
    args = sys.argv[1:]
    cap = None
    if '--cap' in args:
        i = args.index('--cap')
        cap = int(args[i + 1])
        del args[i:i + 2]
    main(*args, cap=cap)
