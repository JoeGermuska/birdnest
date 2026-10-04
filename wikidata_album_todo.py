"""Find album items on Wikidata that don't have our Spotify album ID yet, for the
to-do at /todo/wikidata/albums.

The items come from MusicBrainz (enrich_mb_albums.py): the album's release
group links to its Wikidata item, so the match is as good as MusicBrainz's.
An album is to-do when none of its Spotify ids (one album can have several;
see factoids.History.album_group) is on the item as Spotify album ID (P2205).
Items with a different Spotify ID still get a row: an album can have more than
one. Replaces the wikidata_album_todo table; the page checks Wikidata live too.

Wikidata also wants two more things on an item with a Spotify album ID, so each
row records what's missing:
- distribution format (P437): music streaming (Q15982450), a mandatory constraint
- a default label (language code mul), suggested only when it's clear: the
  item's title (P1476) if it has one, else its label when every language has
  the same one. Otherwise it's left to whoever edits the item.
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
STREAMING = 'Q15982450'  # music streaming, for distribution format (P437)
DETAILS = """SELECT ?item (GROUP_CONCAT(DISTINCT ?fmt) AS ?formats) (GROUP_CONCAT(DISTINCT STR(?label); separator="\\u001f") AS ?labels)
       (SAMPLE(?mul) AS ?mullabel) (GROUP_CONCAT(DISTINCT STR(?title); separator="\\u001f") AS ?titles) WHERE {
  VALUES ?item { %s }
  OPTIONAL { ?item p:P437 ?fst . ?fst ps:P437 ?fmt . }
  OPTIONAL { ?item rdfs:label ?label }
  OPTIONAL { ?item rdfs:label ?mul FILTER(LANG(?mul) = "mul") }
  OPTIONAL { ?item wdt:P1476 ?title }
} GROUP BY ?item"""


def default_label(labels, titles):
    """(suggested default label, why) or (None, None) when it isn't clear: several different titles (an
    original and a translated release title, say) or labels that differ by language."""
    if len(titles) == 1:
        return titles[0], 'its title (P1476)'
    if titles:
        return None, None
    if len(labels) == 1:
        return labels[0], 'the same in every language'
    return None, None

SCHEMA = """
drop table if exists wikidata_album_todo;
create table wikidata_album_todo (
    album_id integer references album(album_id),  -- the album's page id (see History.album_group)
    qid varchar,
    spotify_ids varchar,  -- Spotify album IDs the item already has, space-separated
    needs_streaming integer,  -- 1 when distribution format: music streaming is missing
    needs_label integer,      -- 1 when there's no default (mul) label
    suggest_label varchar, suggest_why varchar  -- a default label, when it's clear (see default_label)
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
    todo = {q for q, canons in items.items()
            if any(not {history.albums[a]['spotify_id'] for a in history.album_members[c]} & has[q] for c in canons)}
    details = {}
    wanted = sorted(todo)
    for i in range(0, len(wanted), BATCH):
        for b in run_query(('%s', lambda q: f"wd:{q}"), wanted[i:i + BATCH], query=DETAILS):
            v = {k: x['value'] for k, x in b.items()}
            details[v['item'].rsplit('/', 1)[-1]] = {
                'streaming': STREAMING in v.get('formats', ''),
                'mul': bool(v.get('mullabel')),
                'labels': [l for l in v.get('labels', '').split('\u001f') if l],
                'titles': [t for t in v.get('titles', '').split('\u001f') if t]}
    rows = []
    for qid, canons in items.items():
        for canon in canons:
            ours = {history.albums[a]['spotify_id'] for a in history.album_members[canon]}
            if not ours & has[qid]:
                d = details.get(qid, {'streaming': False, 'mul': False, 'labels': [], 'titles': []})
                label, why = (None, None) if d['mul'] else default_label(d['labels'], d['titles'])
                rows.append((canon, qid, ' '.join(sorted(has[qid])) or None, int(not d['streaming']), int(not d['mul']),
                             label, why))
    con = sqlite3.connect(db_path, timeout=60)
    with con:
        con.executescript(SCHEMA)
        con.executemany("insert into wikidata_album_todo values (?, ?, ?, ?, ?, ?, ?)", rows)
    print(f"{len(rows)} albums to add to {len(items)} Wikidata items checked")


if __name__ == '__main__':
    main(*sys.argv[1:])
