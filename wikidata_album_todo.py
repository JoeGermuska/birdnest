"""Find album items on Wikidata that don't have our Spotify album ID yet, for the
to-do at /todo/wikidata/albums.

The items come from MusicBrainz (enrich_mb_albums.py): the album's release
group links to its Wikidata item, so the match is as good as MusicBrainz's.
An album is to-do when none of its Spotify ids (one album can have several;
see factoids.History.album_group) is on the item as Spotify album ID (P2205).
Items with a different Spotify ID still get a row: an album can have more than
one. Replaces the wikidata_album_todo table; the page checks Wikidata live too.
    python wikidata_album_todo.py [path/to/birdnest.db]
"""
import sqlite3
import sys

import factoids
from enrich_wikidata import run_query

BATCH = 200
# statement values (p:/ps:) rather than truthy (wdt:), which can miss fresh edits; deprecated ones don't count
QUERY = ("SELECT ?item ?sp WHERE { VALUES ?item { %s } OPTIONAL { ?item p:P2205 ?st . ?st ps:P2205 ?sp . "
         "MINUS { ?st wikibase:rank wikibase:DeprecatedRank } } }")

SCHEMA = """
drop table if exists wikidata_album_todo;
create table wikidata_album_todo (
    album_id integer references album(album_id),  -- the album's page id (see History.album_group)
    qid varchar,
    spotify_ids varchar   -- Spotify album IDs the item already has, space-separated
);
"""


def main(db_path='birdnest.db'):
    history = factoids.History(db_path)
    items = {}  # qid -> canonical album ids
    for album_id, links in history.album_links.items():
        if 'wikidata' in links and album_id in history.album_group:
            items.setdefault(links['wikidata'].rsplit('/', 1)[-1], set()).add(history.album_group[album_id])
    has = {q: set() for q in items}
    qids = sorted(items)
    for i in range(0, len(qids), BATCH):
        for b in run_query(('%s', lambda q: f"wd:{q}"), qids[i:i + BATCH], query=QUERY):
            if 'sp' in b:
                has[b['item']['value'].rsplit('/', 1)[-1]].add(b['sp']['value'])
    rows = []
    for qid, canons in items.items():
        for canon in canons:
            ours = {history.albums[a]['spotify_id'] for a in history.album_members[canon]}
            if not ours & has[qid]:
                rows.append((canon, qid, ' '.join(sorted(has[qid])) or None))
    con = sqlite3.connect(db_path, timeout=60)
    with con:
        con.executescript(SCHEMA)
        con.executemany("insert into wikidata_album_todo values (?, ?, ?)", rows)
    print(f"{len(rows)} albums to add to {len(items)} Wikidata items checked")


if __name__ == '__main__':
    main(*sys.argv[1:])
