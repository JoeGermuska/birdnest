"""Look up artists on Wikidata and store links to other representations of
them (Wikipedia, MusicBrainz, Discogs, ...) in artist_link.

Artists are found by Spotify artist ID, or by the Wikidata id MusicBrainz has
for them (see enrich_musicbrainz.py); links MusicBrainz knows about fill in
anything Wikidata lacks.

Incremental: only artists not looked up before are queried (wikidata_checked
records who has been). Pass --all to re-query everyone, e.g. to pick up
pages created on Wikipedia since.
    python enrich_wikidata.py [--all] [path/to/birdnest.db]
"""
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

ENDPOINT = 'https://query.wikidata.org/sparql'
USER_AGENT = 'birdnest/0.1 (https://github.com/JoeGermuska/birdnest)'
BATCH = 200

# source name -> (SPARQL variable expression, URL template for the raw value)
QUERY = """
SELECT ?key ?item (SAMPLE(?article) AS ?wikipedia) (SAMPLE(?mbid) AS ?musicbrainz)
       (SAMPLE(?discogs) AS ?discogs_id) (SAMPLE(?bandcamp) AS ?bandcamp_id)
       (SAMPLE(?allmusic) AS ?allmusic_id) (SAMPLE(?website) AS ?official)
WHERE {
  %s
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }
  OPTIONAL { ?item wdt:P434 ?mbid }
  OPTIONAL { ?item wdt:P1953 ?discogs }
  OPTIONAL { ?item wdt:P3283 ?bandcamp }
  OPTIONAL { ?item wdt:P1728 ?allmusic }
  OPTIONAL { ?item wdt:P856 ?website }
}
GROUP BY ?key ?item
"""

LINKS = {
    'wikipedia': lambda v: v,
    'wikidata': lambda v: v.replace('http://', 'https://'),
    'musicbrainz': lambda v: f"https://musicbrainz.org/artist/{v}",
    'discogs': lambda v: f"https://www.discogs.com/artist/{v}",
    'bandcamp': lambda v: f"https://{v}.bandcamp.com/",
    'allmusic': lambda v: f"https://www.allmusic.com/artist/{v}",
    'website': lambda v: v,
}
COLUMNS = {'wikipedia': 'wikipedia', 'wikidata': 'item', 'musicbrainz': 'musicbrainz',
           'discogs': 'discogs_id', 'bandcamp': 'bandcamp_id', 'allmusic': 'allmusic_id', 'website': 'official'}


# (binding clause, how to write one key in its VALUES list)
BY_SPOTIFY = ('VALUES ?key { %s } ?item wdt:P1902 ?key .', '"{}"'.format)
BY_QID = ('VALUES ?item { %s } BIND(STRAFTER(STR(?item), "/entity/") AS ?key)', 'wd:{}'.format)

# MusicBrainz relationship type -> our source name
MB_SOURCES = {'discogs': 'discogs', 'bandcamp': 'bandcamp', 'allmusic': 'allmusic', 'official homepage': 'website'}


def run_query(binding, keys, query=QUERY):
    clause, fmt = binding
    values = ' '.join(fmt(k) for k in keys)
    body = urllib.parse.urlencode({'query': query % (clause % values)}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        'User-Agent': USER_AGENT, 'Accept': 'application/sparql-results+json'})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)['results']['bindings']
        except Exception as e:
            print(f"  retrying after {e}")
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError('Wikidata query failed')


def fetch(binding, key_to_artist, links):
    keys = list(key_to_artist)
    for i in range(0, len(keys), BATCH):
        for b in run_query(binding, keys[i:i + BATCH]):
            artist_id = key_to_artist[b['key']['value']]
            if artist_id in links:  # one key mapped to several items; keep the first
                continue
            links[artist_id] = {source: LINKS[source](b[column]['value'])
                                for source, column in COLUMNS.items() if column in b}
        print(f"  {min(i + BATCH, len(keys))}/{len(keys)}, {len(links)} artists linked")
        time.sleep(1)


def main(db_path='birdnest.db', refresh_all=False):
    con = sqlite3.connect(db_path)
    con.execute("""create table if not exists artist_link (
        artist_id integer references artist(artist_id), source varchar, url varchar)""")
    if not con.execute("select 1 from sqlite_master where name='wikidata_checked'").fetchone():
        with con:
            con.execute("""create table wikidata_checked (
                artist_id integer primary key references artist(artist_id), checked_at timestamp)""")
            # artists that already have links were looked up by an earlier full run
            con.execute("""insert into wikidata_checked
                           select distinct artist_id, current_timestamp from artist_link""")
    has_mb = con.execute("select 1 from sqlite_master where name='mb_artist'").fetchone()

    todo = dict(con.execute("select spotify_id, artist_id from artist where spotify_id is not null" +
                            ("" if refresh_all else
                             " and artist_id not in (select artist_id from wikidata_checked)")))
    targets = set(todo.values())
    print(f"{len(targets)} artists to look up on Wikidata")
    if not targets:
        return
    links = {}  # artist_id -> {source: url}

    fetch(BY_SPOTIFY, todo, links)

    if has_mb:
        qids = {q: a for a, q in con.execute("select artist_id, wikidata from mb_artist where wikidata is not null")
                if a in targets and a not in links}
        fetch(BY_QID, qids, links)
        for artist_id, mbid in con.execute("select artist_id, mbid from mb_artist where mbid is not null"):
            if artist_id in targets:
                links.setdefault(artist_id, {}).setdefault('musicbrainz', LINKS['musicbrainz'](mbid))
        for artist_id, mb_type, url in con.execute("select artist_id, type, url from mb_artist_url"):
            if artist_id in targets and mb_type in MB_SOURCES:
                links.setdefault(artist_id, {}).setdefault(MB_SOURCES[mb_type], url)

    rows = [(a, source, url) for a, sources in links.items() for source, url in sources.items()]
    with con:
        con.executemany("delete from artist_link where artist_id = ?", [(a,) for a in targets])
        con.executemany("insert into artist_link values (?, ?, ?)", rows)
        con.executemany("insert or replace into wikidata_checked values (?, current_timestamp)",
                        [(a,) for a in targets])
    print(f"{len(rows)} links for {len(links)} of {len(targets)} artists")


if __name__ == '__main__':
    args = sys.argv[1:]
    main(*[a for a in args if a != '--all'], refresh_all='--all' in args)
