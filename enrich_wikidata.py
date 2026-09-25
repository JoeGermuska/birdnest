"""Look up artists on Wikidata and store links to other representations of
them (Wikipedia, MusicBrainz, Discogs, ...) in artist_link.

Artists are found by Spotify artist ID, or by the Wikidata id MusicBrainz has
for them (see enrich_musicbrainz.py); links MusicBrainz knows about fill in
anything Wikidata lacks.

Rebuilds the table from scratch each run; it's a few dozen batched queries.
    python enrich_wikidata.py [path/to/birdnest.db]
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


def run_query(binding, keys):
    clause, fmt = binding
    values = ' '.join(fmt(k) for k in keys)
    body = urllib.parse.urlencode({'query': QUERY % (clause % values)}).encode()
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


def main(db_path='birdnest.db'):
    con = sqlite3.connect(db_path)
    con.execute("""create table if not exists artist_link (
        artist_id integer references artist(artist_id), source varchar, url varchar)""")
    has_mb = con.execute("select 1 from sqlite_master where name='mb_artist'").fetchone()
    links = {}  # artist_id -> {source: url}

    print("Wikidata by Spotify id")
    fetch(BY_SPOTIFY, dict(con.execute("select spotify_id, artist_id from artist where spotify_id is not null")), links)

    if has_mb:
        qids = {q: a for a, q in con.execute("select artist_id, wikidata from mb_artist where wikidata is not null")
                if a not in links}
        print("Wikidata by MusicBrainz's Wikidata id")
        fetch(BY_QID, qids, links)
        for artist_id, mbid in con.execute("select artist_id, mbid from mb_artist where mbid is not null"):
            links.setdefault(artist_id, {}).setdefault('musicbrainz', LINKS['musicbrainz'](mbid))
        for artist_id, mb_type, url in con.execute("select artist_id, type, url from mb_artist_url"):
            if mb_type in MB_SOURCES:
                links.setdefault(artist_id, {}).setdefault(MB_SOURCES[mb_type], url)

    rows = [(a, source, url) for a, sources in links.items() for source, url in sources.items()]
    with con:
        con.execute("delete from artist_link")
        con.executemany("insert into artist_link values (?, ?, ?)", rows)
    print(f"{len(rows)} links for {len(links)} artists")


if __name__ == '__main__':
    main(*sys.argv[1:])
