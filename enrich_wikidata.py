"""Look up artists on Wikidata by Spotify artist ID and store links to other
representations of them (Wikipedia, MusicBrainz, Discogs, ...) in artist_link.

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
SELECT ?sp ?item (SAMPLE(?article) AS ?wikipedia) (SAMPLE(?mbid) AS ?musicbrainz)
       (SAMPLE(?discogs) AS ?discogs_id) (SAMPLE(?bandcamp) AS ?bandcamp_id)
       (SAMPLE(?allmusic) AS ?allmusic_id) (SAMPLE(?website) AS ?official)
WHERE {
  VALUES ?sp { %s }
  ?item wdt:P1902 ?sp .
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }
  OPTIONAL { ?item wdt:P434 ?mbid }
  OPTIONAL { ?item wdt:P1953 ?discogs }
  OPTIONAL { ?item wdt:P3283 ?bandcamp }
  OPTIONAL { ?item wdt:P1728 ?allmusic }
  OPTIONAL { ?item wdt:P856 ?website }
}
GROUP BY ?sp ?item
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


def run_query(spotify_ids):
    values = ' '.join(f'"{s}"' for s in spotify_ids)
    body = urllib.parse.urlencode({'query': QUERY % values}).encode()
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


def main(db_path='birdnest.db'):
    con = sqlite3.connect(db_path)
    con.execute("""create table if not exists artist_link (
        artist_id integer references artist(artist_id), source varchar, url varchar)""")
    ids = dict(con.execute("select spotify_id, artist_id from artist where spotify_id is not null"))
    rows = []
    spotify_ids = list(ids)
    for i in range(0, len(spotify_ids), BATCH):
        batch = spotify_ids[i:i + BATCH]
        seen = set()
        for b in run_query(batch):
            artist_id = ids[b['sp']['value']]
            if artist_id in seen:  # one Spotify id mapped to several items; keep the first
                continue
            seen.add(artist_id)
            for source, column in COLUMNS.items():
                if column in b:
                    rows.append((artist_id, source, LINKS[source](b[column]['value'])))
        print(f"{min(i + BATCH, len(spotify_ids))}/{len(spotify_ids)} artists, {len(rows)} links")
        time.sleep(1)
    with con:
        con.execute("delete from artist_link")
        con.executemany("insert into artist_link values (?, ?, ?)", rows)
    matched = con.execute("select count(distinct artist_id) from artist_link").fetchone()[0]
    print(f"matched {matched} of {len(ids)} artists")


if __name__ == '__main__':
    main(*sys.argv[1:])
