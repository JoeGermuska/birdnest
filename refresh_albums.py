"""Fill in album details from Spotify's full album object: release date, album
type (album/single/compilation), track count, UPC and the ℗/© lines. Also
updates name, label and images in place.

New albums get these when they're loaded (Album.get_or_create); this script
backfills albums stored before that, or ones whose fetch failed. Albums are due
until they have a fetched_at; --all refetches everything.

Spotify's release date is the date of the release Spotify serves, which for
catalog music is often a reissue or compilation; enrich_mb_recordings.py gets
each recording's first release date from MusicBrainz.

    python refresh_albums.py [--all] [path/to/birdnest.db]
"""
import json
import sqlite3
import sys
from datetime import date

BATCH = 200  # commit every this many albums

COLUMNS = {
    'release_date': 'varchar', 'release_date_precision': 'varchar', 'album_type': 'varchar',
    'total_tracks': 'integer', 'upc': 'varchar', 'copyright_p': 'varchar', 'copyright_c': 'varchar',
    'fetched_at': 'varchar',
}


def album_fields(a):
    """The detail columns present in a Spotify album object (simplified objects lack some)."""
    out = {k: a[k] for k in ('release_date', 'release_date_precision', 'album_type', 'total_tracks') if k in a}
    if 'external_ids' in a:
        out['upc'] = (a['external_ids'] or {}).get('upc')
    if 'copyrights' in a:
        by_type = {}
        for c in a['copyrights'] or []:
            by_type.setdefault(c.get('type'), c.get('text'))
        out['copyright_p'], out['copyright_c'] = by_type.get('P'), by_type.get('C')
        out['fetched_at'] = date.today().isoformat()
    return out


def ensure_columns(con):
    have = {row[1] for row in con.execute("pragma table_info(album)")}
    for name, kind in COLUMNS.items():
        if name not in have:
            con.execute(f"alter table album add column {name} {kind}")


def apply(con, album_id, a):
    fields = {'name': a['name'], 'images': json.dumps(a.get('images') or []), **album_fields(a)}
    if a.get('label'):
        fields['label'] = a['label']
    sets = ', '.join(f"{k} = ?" for k in fields)
    con.execute(f"update album set {sets} where album_id = ?", [*fields.values(), album_id])


def main(db_path='birdnest.db', refresh_all=False, client=None):
    con = sqlite3.connect(db_path)
    ensure_columns(con)
    # most-played first, so an interrupted run covers the albums that matter
    todo = con.execute("""
        select a.album_id, a.spotify_id from album a
        left join track t using(album_id) left join playlist_track pt using(track_id)
        where a.spotify_id is not null and (? or a.fetched_at is null)
        group by a.album_id order by count(pt.playlist_id) desc""", (refresh_all,)).fetchall()
    print(f"refreshing {len(todo)} albums from Spotify")
    if not todo:
        return
    if client is None:
        from spotclient import Client
        client = Client()
    done = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        # the API answers in request order (None for an unknown id); pair by position
        fetched = client.albums(sid for _, sid in batch)
        with con:
            for (album_id, _), a in zip(batch, fetched):
                if a:
                    apply(con, album_id, a)
                    done += 1
        print(f"  {min(i + BATCH, len(todo))}/{len(todo)}")
    print(f"refreshed {done} of {len(todo)}")


if __name__ == '__main__':
    args = sys.argv[1:]
    main(*[a for a in args if a != '--all'], refresh_all='--all' in args)
