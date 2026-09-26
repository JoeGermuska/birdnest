"""Find probable Wikidata items for artists we have no links for, so the
matches can be reviewed by hand and their Spotify IDs added to Wikidata
(after which enrich_wikidata.py picks them up).

Each unlinked artist's name is looked up on Wikidata; items whose English label
or alias is exactly that name (with or without a leading "The") and that look
like a musician or group become candidates.
Writes a JSON list, most-played artists first, with a tier per artist:
  likely -- one musical candidate, which has no Spotify ID yet
  check  -- several musical candidates, or the one found already has a
            (different) Spotify ID
  none   -- no musical candidate found
    python wikidata_candidates.py [out.json] [path/to/birdnest.db]
"""
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

from enrich_wikidata import ENDPOINT, USER_AGENT, run_query

BATCH = 50

DETAILS = """
SELECT ?key ?label ?desc ?article ?musical ?year
       (GROUP_CONCAT(DISTINCT ?sp; separator=" ") AS ?spotify) (SAMPLE(?mb) AS ?mbid)
WHERE {
  %s
  OPTIONAL { ?item rdfs:label ?label FILTER(LANG(?label) IN ("en", "mul")) }
  OPTIONAL { ?item schema:description ?desc FILTER(LANG(?desc) = "en") }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }
  OPTIONAL { ?item wdt:P1902 ?sp }
  OPTIONAL { ?item wdt:P434 ?mb }
  OPTIONAL { ?item wdt:P571|wdt:P569 ?start }
  BIND(YEAR(?start) AS ?year)
  BIND(EXISTS { ?item wdt:P31/wdt:P279* wd:Q2088357 }                     # musical ensemble
       || EXISTS { ?item wdt:P106/wdt:P279* ?occ
                   VALUES ?occ { wd:Q639669 wd:Q177220 wd:Q36834 wd:Q753110 wd:Q130857 wd:Q183945 } }
       || EXISTS { ?item wdt:P434|wdt:P1953|wdt:P1902 [] } AS ?musical)
}
GROUP BY ?key ?label ?desc ?article ?musical ?year
"""


NAMES = """
SELECT DISTINCT ?name ?item WHERE {
  VALUES ?name { %s }
  ?item rdfs:label|skos:altLabel ?name .
}
"""


def variants(name):
    """The name as Spotify has it, with and without a leading "The", and with
    title-cased small words ("Brotherhood Of Breath") lowercased."""
    out = set()
    for n in (name, re.sub(r'(?<= )(And|Of|The|In|On|At|To|For|With|A|An)(?= )', lambda m: m[0].lower(), name)):
        bare = re.sub(r'^the ', '', n, flags=re.I)
        out |= {n, bare, f"The {bare}"}
    return out


def search(names):
    """{name: [qid]} for Wikidata items whose English or multilingual label or alias is name
    (or name with/without a leading "The")."""
    literals = {}
    for n in names:
        for v in variants(n):
            literals[v] = n
    found = {n: [] for n in names}
    keys = list(literals)
    for i in range(0, len(keys), BATCH):
        values = ' '.join(f'{json.dumps(v)}@{lang}' for v in keys[i:i + BATCH] for lang in ('en', 'mul'))
        body = urllib.parse.urlencode({'query': NAMES % values}).encode()
        req = urllib.request.Request(ENDPOINT, data=body, headers={
            'User-Agent': USER_AGENT, 'Accept': 'application/sparql-results+json'})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    bindings = json.load(resp)['results']['bindings']
                break
            except Exception as e:
                print(f"  retrying after {e}")
                time.sleep(2 ** (attempt + 2))
        else:
            raise RuntimeError('Wikidata name query failed')
        for b in bindings:
            qid = b['item']['value'].rsplit('/', 1)[-1]
            n = literals[b['name']['value']]
            if qid.startswith('Q') and qid not in found[n]:
                found[n].append(qid)
        print(f"  names {min(i + BATCH, len(keys))}/{len(keys)}")
        time.sleep(1)
    return found


def main(out='wikidata_candidates.json', db_path='birdnest.db'):
    con = sqlite3.connect(db_path)
    artists = con.execute("""
        select a.artist_id, a.name, a.spotify_id, count(distinct pt.playlist_id) shows,
               min(p.date), max(p.date)
        from artist a join track_artist ta using(artist_id) join playlist_track pt using(track_id)
             join playlist p using(playlist_id)
        where a.spotify_id is not null
          and a.artist_id not in (select artist_id from artist_link)
          and a.artist_id not in (select artist_id from mb_artist where mbid is not null)
        group by a.artist_id order by shows desc, a.name""").fetchall()
    genres = {}
    for artist_id, genre in con.execute("select artist_id, g.name from artist_genre join genre g using(genre_id)"):
        genres.setdefault(artist_id, []).append(genre)
    print(f"looking up Wikidata for {len(artists)} artists")

    by_name = search({a[1] for a in artists})
    found = {artist_id: by_name[name] for artist_id, name, *_ in artists}  # artist_id -> [qid]

    qids = sorted({q for v in found.values() for q in v})
    details = {}
    binding = ('VALUES ?item { %s } BIND(STRAFTER(STR(?item), "/entity/") AS ?key)', 'wd:{}'.format)
    for i in range(0, len(qids), BATCH):
        for b in run_query(binding, qids[i:i + BATCH], DETAILS):
            v = {k: x['value'] for k, x in b.items()}
            d = details.setdefault(v['key'], {'qid': v['key'], 'musical': False, 'spotify': []})
            d['label'] = d.get('label') or v.get('label')
            d['desc'] = d.get('desc') or v.get('desc')
            d['wikipedia'] = d.get('wikipedia') or v.get('article')
            d['mbid'] = d.get('mbid') or v.get('mbid')
            d['year'] = d.get('year') or (int(v['year']) if v.get('year') else None)
            d['musical'] = d['musical'] or v.get('musical') == 'true'
            d['spotify'] = sorted(set(d['spotify']) | set(v.get('spotify', '').split()))
        print(f"  details {min(i + BATCH, len(qids))}/{len(qids)}")
        time.sleep(1)

    rows = []
    for artist_id, name, spotify_id, shows, first, last in artists:
        cands = [details[q] for q in found[artist_id] if q in details and details[q]['musical']]
        cands = [c for c in cands if spotify_id not in c['spotify']]  # already linked; enrich will find it
        if len(cands) == 1 and not cands[0]['spotify']:
            tier = 'likely'
        elif cands:
            tier = 'check'
        else:
            tier = 'none'
        rows.append({'artist_id': artist_id, 'name': name, 'spotify_id': spotify_id, 'shows': shows,
                     'first': first[:10], 'last': last[:10], 'genres': genres.get(artist_id, [])[:4],
                     'tier': tier, 'candidates': cands})
    with open(out, 'w') as f:
        json.dump(rows, f, indent=1)
    tiers = {t: sum(1 for r in rows if r['tier'] == t) for t in ('likely', 'check', 'none')}
    print(f"wrote {out}: {tiers}")


if __name__ == '__main__':
    main(*sys.argv[1:])
