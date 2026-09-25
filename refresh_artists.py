"""Refresh artist data from Spotify without losing history.

- popularity and followers are appended to artist_snapshot (one row per fetch),
  and the artist row keeps the latest values
- Spotify's current genre tags go to artist_genre_current; artist_genre, which
  holds every tag seen when an artist was played, is never changed (Spotify has
  been retiring its older, finer-grained genre vocabulary)
- names, images and URLs are updated in place

Each artist is due once its latest snapshot is more than STALE_DAYS old; a run
refreshes due artists oldest-first, up to a cap, so weekly loads spread the
work and every artist ends up refreshed about monthly. Interrupted runs just
leave the rest due. The first run seeds artist_snapshot from the values already
stored, dated to each artist's last play (roughly when they were fetched).

    python refresh_artists.py [--all] [--cap 2000] [path/to/birdnest.db]
"""
import json
import sqlite3
import sys
from datetime import date, timedelta

STALE_DAYS = 30
DEFAULT_CAP = 2000
BATCH = 500  # commit every this many artists

SCHEMA = """
create table if not exists artist_snapshot (
    artist_id integer references artist(artist_id),
    fetched_at date, popularity integer, followers integer,
    source varchar  -- 'api', or 'seed' for values carried over from before snapshots existed
);
create index if not exists artist_snapshot_artist on artist_snapshot(artist_id, fetched_at);
create table if not exists artist_genre_current (
    artist_id integer references artist(artist_id), genre varchar, fetched_at date
);
"""


def seed(con):
    """Carry the stored popularity/followers into the snapshot table, dated to last play."""
    con.execute("""
        insert into artist_snapshot (artist_id, fetched_at, popularity, followers, source)
        select a.artist_id, max(p.date), a.popularity, a.followers, 'seed'
        from artist a join track_artist ta using(artist_id) join playlist_track using(track_id)
        join playlist p using(playlist_id)
        where a.popularity is not null or a.followers is not null
        group by a.artist_id""")


def due_artists(con, cap, refresh_all):
    cutoff = (date.today() - timedelta(days=STALE_DAYS)).isoformat()
    rows = con.execute("""
        select a.artist_id, a.spotify_id, max(s.fetched_at) latest
        from artist a left join artist_snapshot s using(artist_id)
        where a.spotify_id is not null
        group by a.artist_id
        having ? or latest is null or latest < ?
        order by latest is not null, latest""", (refresh_all, cutoff)).fetchall()
    return rows if refresh_all else rows[:cap]


def apply(con, artist_id, a, today):
    followers = (a.get('followers') or {}).get('total')
    con.execute("insert into artist_snapshot values (?, ?, ?, ?, 'api')",
                (artist_id, today, a.get('popularity'), followers))
    con.execute("""update artist set name = ?, spotify_url = ?, images = ?,
                   popularity = coalesce(?, popularity), followers = coalesce(?, followers)
                   where artist_id = ?""",
                (a['name'], (a.get('external_urls') or {}).get('spotify'), json.dumps(a.get('images') or []),
                 a.get('popularity'), followers, artist_id))
    if 'genres' in a:
        con.execute("delete from artist_genre_current where artist_id = ?", (artist_id,))
        con.executemany("insert into artist_genre_current values (?, ?, ?)",
                        [(artist_id, g, today) for g in a['genres']])


def main(db_path='birdnest.db', refresh_all=False, cap=DEFAULT_CAP, force_ids=(), client=None):
    """force_ids: artist_ids to refresh now whether or not they're due (e.g. tonight's artists)."""
    con = sqlite3.connect(db_path)
    if not con.execute("select 1 from sqlite_master where name='artist_snapshot'").fetchone():
        con.executescript(SCHEMA)
        with con:
            seed(con)
        print(f"seeded artist_snapshot with {con.execute('select count(*) from artist_snapshot').fetchone()[0]} rows")
    con.executescript(SCHEMA)

    todo = {aid: sid for aid, sid, _ in due_artists(con, cap, refresh_all)}
    if force_ids:
        marks = ','.join('?' * len(force_ids))
        todo.update(con.execute(f"""select artist_id, spotify_id from artist where artist_id in ({marks})
            and spotify_id is not null and artist_id not in
                (select artist_id from artist_snapshot where fetched_at = ?)""",
            [*force_ids, date.today().isoformat()]).fetchall())
    n_artists = con.execute("select count(*) from artist").fetchone()[0]
    print(f"refreshing {len(todo)} artists from Spotify")
    if not todo:
        return
    if client is None:
        from spotclient import Client
        client = Client()

    today = date.today().isoformat()
    items = list(todo.items())
    done = 0
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        # the API answers in request order; pair by position
        fetched = client.artists(sid for _, sid in batch)
        with con:
            for (artist_id, _), a in zip(batch, fetched):
                if a:
                    apply(con, artist_id, a, today)
                    done += 1
        print(f"  {min(i + BATCH, len(items))}/{len(items)}")
    left = len(due_artists(con, n_artists, False))
    print(f"refreshed {done}; {left} still due")


if __name__ == '__main__':
    args = sys.argv[1:]
    cap = DEFAULT_CAP
    if '--cap' in args:
        i = args.index('--cap')
        cap = int(args[i + 1])
        del args[i:i + 2]
    main(*[a for a in args if a != '--all'], refresh_all='--all' in args, cap=cap)
