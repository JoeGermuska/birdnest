"""Match artists to MusicBrainz by their Spotify URL and store what MusicBrainz
knows about them (type, country/area, active dates, Wikidata id, external
links) in mb_artist and mb_artist_url.

Incremental: artists already looked up are skipped, so after the first
backfill a run only touches newly added artists. Safe to interrupt; progress
is committed as it goes. MusicBrainz asks for at most one request per second.
    python enrich_musicbrainz.py [path/to/birdnest.db]
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = 'https://musicbrainz.org/ws/2'
USER_AGENT = 'birdnest/0.1 (https://github.com/JoeGermuska/birdnest)'
URL_BATCH = 100  # the url endpoint accepts up to 100 resource= params

SCHEMA = """
create table if not exists mb_artist (
    artist_id integer primary key references artist(artist_id),
    mbid varchar,          -- null when checked but not found
    type varchar, country varchar, area varchar,
    begin_date varchar, end_date varchar, wikidata varchar,
    checked_at timestamp default current_timestamp
);
create table if not exists mb_artist_url (
    artist_id integer references artist(artist_id), type varchar, url varchar
);
"""

_last_request = 0.0


def get(path, **params):
    global _last_request
    query = urllib.parse.urlencode({**params, 'fmt': 'json'}, doseq=True)
    req = urllib.request.Request(f"{API}/{path}?{query}", headers={'User-Agent': USER_AGENT})
    for attempt in range(5):
        wait = 1.05 - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code not in (429, 500, 502, 503):
                raise
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"MusicBrainz request failed: {path}")


def main(db_path='birdnest.db'):
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    # most-played artists first, so an interrupted backfill covers the ones that matter
    todo = con.execute("""
        select a.artist_id, a.spotify_id from artist a
        left join track_artist ta using(artist_id) left join playlist_track pt using(track_id)
        where a.spotify_id is not null and a.artist_id not in (select artist_id from mb_artist)
        group by a.artist_id order by count(distinct pt.playlist_id) desc""").fetchall()
    print(f"{len(todo)} artists to look up")

    # 1. batch-match Spotify URLs to MusicBrainz artist ids
    matches = {}
    for i in range(0, len(todo), URL_BATCH):
        batch = todo[i:i + URL_BATCH]
        by_url = {f"https://open.spotify.com/artist/{s}": a for a, s in batch}
        result = get('url', resource=list(by_url), inc='artist-rels') or {}
        # a single-resource lookup returns the url object itself rather than a list
        for u in result.get('urls', [result] if 'resource' in result else []):
            artists = [r['artist']['id'] for r in u.get('relations', []) if 'artist' in r]
            if artists and u['resource'] in by_url:
                matches[by_url[u['resource']]] = artists[0]
        print(f"matched {len(matches)} of {min(i + URL_BATCH, len(todo))}")
    with con:
        con.executemany("insert or replace into mb_artist (artist_id) values (?)",
                        [(a,) for a, _ in todo if a not in matches])

    # 2. fetch details for each matched artist
    for n, (artist_id, mbid) in enumerate(matches.items(), 1):
        mb = get(f"artist/{mbid}", inc='url-rels') or {}
        life = mb.get('life-span') or {}
        urls = [(r['type'], r['url']['resource']) for r in mb.get('relations', []) if r.get('url')]
        wikidata = next((u.rsplit('/', 1)[-1] for t, u in urls if t == 'wikidata'), None)
        with con:
            con.execute("delete from mb_artist_url where artist_id = ?", (artist_id,))
            con.executemany("insert into mb_artist_url values (?, ?, ?)", [(artist_id, t, u) for t, u in urls])
            con.execute("""insert or replace into mb_artist
                (artist_id, mbid, type, country, area, begin_date, end_date, wikidata)
                values (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (artist_id, mbid, mb.get('type'), mb.get('country'), (mb.get('area') or {}).get('name'),
                         life.get('begin'), life.get('end'), wikidata))
        if n % 100 == 0:
            print(f"details {n}/{len(matches)}")
    print(f"done: {len(matches)} matched of {len(todo)}")


if __name__ == '__main__':
    main(*sys.argv[1:])
