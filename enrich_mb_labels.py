"""Labels from MusicBrainz: the label each album originally came out on, and
what MusicBrainz knows about each label.

Spotify's label is usually today's catalog owner (Rhino, Legacy, UMC). For
each album matched to a MusicBrainz release group (enrich_mb_albums.py), the
original label is the label on the group's earliest release: "This Is Clarence
Carter" is Atlantic (1968, SD-8192), not Rhino Atlantic. Only releases from the
earliest year count, so an undated or unlabeled first pressing doesn't hand the
album to a reissue label decades later.

Then each label seen (original, or on the release we matched) is looked up:
type (original production, reissue, imprint, ...), country and area, years,
links (Wikidata, Discogs, Bandcamp, official site; Wikipedia via Wikidata),
and its relations to other labels (imprints, ownership, distribution).

Tables: mb_album_original (album_id -> earliest release and its label),
mb_label, mb_label_url, mb_label_rel. Incremental: albums and labels looked
up before are skipped. Shares MusicBrainz's one-request-a-second with the
other scripts.
    python enrich_mb_labels.py [--cap N] [path/to/birdnest.db]
"""
import sqlite3
import sys

from enrich_mb_albums import wikipedia_for
from enrich_musicbrainz import get

NO_LABEL = '157afde4-4bf5-4039-8ad2-5a15acc85176'  # MusicBrainz's "[no label]"
PAGE = 100
MAX_PAGES = 5  # release groups with more than 500 releases (a few famous albums): the first 500 will do
URL_TYPES = {'wikidata', 'discogs', 'bandcamp', 'official site', 'wikipedia'}

SCHEMA = """
create table if not exists mb_album_original (
    album_id integer primary key references album(album_id),
    release_mbid varchar, released varchar,       -- the release group's earliest release with a label
    label_mbid varchar, label_name varchar, catalog_number varchar,
    checked_at timestamp default current_timestamp
);
create table if not exists mb_label (
    mbid varchar primary key, name varchar, type varchar, country varchar, area varchar,
    begin_date varchar, end_date varchar, disambiguation varchar,
    checked_at timestamp default current_timestamp
);
create table if not exists mb_label_url (mbid varchar, type varchar, url varchar);
create table if not exists mb_label_rel (
    mbid varchar, type varchar, direction varchar,   -- e.g. 'label ownership', 'backward': mbid is owned by target
    target_mbid varchar, target_name varchar, begin_date varchar, end_date varchar
);
"""


def earliest_labeled(releases, home=None):
    """(release, label-info) for the release from the group's earliest year that has a real label, or (None, None).
    Within that year: a release from the artist's home country first (a same-year foreign edition is usually a
    licensee: Bill Withers on Sussex in the US, not A&M in Italy), then the more precise date (a bare "1971" says
    less than "1971-05"), then the earlier. Official releases only, unless a group has none."""
    official = [r for r in releases if r.get('status') == 'Official'] or releases
    dated = [r for r in official if r.get('date')]
    if not dated:
        return None, None
    year = min(r['date'][:4] for r in dated)
    first_year = sorted((r for r in dated if r['date'][:4] == year),
                        key=lambda r: (bool(home) and r.get('country') != home, len(r['date']) == 4, r['date']))
    for r in first_year:
        for li in r.get('label-info') or []:
            if li.get('label') and li['label'].get('id') != NO_LABEL:
                return r, li
    return None, None


def group_releases(rg):
    releases, offset = [], 0
    for _ in range(MAX_PAGES):
        page = get('release', **{'release-group': rg, 'inc': 'labels', 'limit': PAGE, 'offset': offset}) or {}
        releases += page.get('releases', [])
        offset += PAGE
        if offset >= page.get('release-count', 0):
            break
    return releases


def main(db_path='birdnest.db', cap=None):
    """cap: at most this many release groups and labels (for a trial run)."""
    con = sqlite3.connect(db_path, timeout=60)
    con.executescript(SCHEMA)

    # 1. each matched album's original label, one lookup per release group (shared by its editions)
    todo = con.execute("""
        select m.album_id, m.release_group_mbid,
               (select ma.country from album_artist aa join mb_artist ma using(artist_id)
                where aa.album_id = m.album_id and ma.country is not null limit 1) home
        from mb_album m
        left join mb_album_original o using(album_id) left join track t using(album_id) left join playlist_track pt using(track_id)
        where m.release_group_mbid is not null and o.album_id is null
        group by m.album_id order by count(pt.playlist_id) desc""").fetchall()
    groups, homes = {}, {}
    for album_id, rg, home in todo:
        groups.setdefault(rg, []).append(album_id)
        homes.setdefault(rg, home)
    if cap:
        groups = dict(list(groups.items())[:cap])
    print(f"{len(todo)} albums ({len(groups)} release groups) to find original labels for")
    for n, (rg, albums) in enumerate(groups.items(), 1):
        rel, li = earliest_labeled(group_releases(rg), homes[rg])
        row = (rel['id'], rel['date'], li['label']['id'], li['label']['name'], li.get('catalog-number')) if rel else (None,) * 5
        with con:
            con.executemany("""insert or replace into mb_album_original
                (album_id, release_mbid, released, label_mbid, label_name, catalog_number) values (?, ?, ?, ?, ?, ?)""",
                            [(a, *row) for a in albums])
        if n % 200 == 0:
            print(f"  {n}/{len(groups)}")

    # 2. every label seen, original or on the release we matched
    labels = [m for m, in con.execute("""
        select label_mbid from mb_album_original where label_mbid is not null
        union select label_mbid from mb_album_label where label_mbid is not null
        except select mbid from mb_label""")]
    labels = [m for m in labels if m != NO_LABEL][:cap]
    print(f"{len(labels)} labels to look up")
    for n, mbid in enumerate(labels, 1):
        lab = get(f"label/{mbid}", inc='url-rels+label-rels') or {}
        life = lab.get('life-span') or {}
        rels = lab.get('relations', [])
        with con:
            con.execute("""insert or replace into mb_label (mbid, name, type, country, area, begin_date, end_date, disambiguation)
                           values (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (mbid, lab.get('name'), lab.get('type'), lab.get('country'), (lab.get('area') or {}).get('name'),
                         life.get('begin'), life.get('end'), lab.get('disambiguation') or None))
            con.execute("delete from mb_label_url where mbid = ?", (mbid,))
            con.executemany("insert into mb_label_url values (?, ?, ?)",
                            [(mbid, r['type'], r['url']['resource']) for r in rels if r.get('url') and r['type'] in URL_TYPES])
            con.execute("delete from mb_label_rel where mbid = ?", (mbid,))
            con.executemany("insert into mb_label_rel values (?, ?, ?, ?, ?, ?, ?)",
                            [(mbid, r['type'], r.get('direction'), r['label']['id'], r['label'].get('name'),
                              r.get('begin'), r.get('end')) for r in rels if r.get('label')])
        if n % 200 == 0:
            print(f"  labels {n}/{len(labels)}")

    # 3. Wikipedia, from the labels' Wikidata items
    rows = con.execute("""select mbid, url from mb_label_url where type = 'wikidata' and mbid not in
                          (select mbid from mb_label_url where type = 'wikipedia')""").fetchall()
    articles = wikipedia_for(u.rsplit('/', 1)[-1] for _, u in rows)
    with con:
        con.executemany("insert into mb_label_url values (?, 'wikipedia', ?)",
                        [(m, articles[u.rsplit('/', 1)[-1]]) for m, u in rows if u.rsplit('/', 1)[-1] in articles])
    found = con.execute("select count(label_mbid), count(*) from mb_album_original").fetchone()
    print(f"done: original labels for {found[0]} of {found[1]} albums; {len(articles)} new Wikipedia links")


if __name__ == '__main__':
    args = sys.argv[1:]
    cap = None
    if '--cap' in args:
        i = args.index('--cap')
        cap = int(args[i + 1])
        del args[i:i + 2]
    main(*args, cap=cap)
