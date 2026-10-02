"""Look up each track's ISRC on MusicBrainz and store the recording's first
release date in mb_recording. Spotify's album date is often a reissue's or a
compilation's; MusicBrainz's first-release-date is the earliest release it
knows of for the recording itself ("Black Satin": 1972, whatever the reissue).

An ISRC can map to several MusicBrainz recordings (usually the same recording
entered twice, sometimes a mistake); we keep the one with the earliest date and
how many there were. The track_release view picks the earlier of that date and
Spotify's album date, so a recording MusicBrainz knows only from a later
release doesn't come out newer than the album it's on.

Incremental like enrich_musicbrainz.py: tracks already looked up are skipped,
except not-found ones are tried again after RETRY_DAYS (load_playlist.py
retries a few each week). Most-played first;
one request per second, so a full backfill takes a couple of hours.
    python enrich_mb_recordings.py [path/to/birdnest.db]
"""
import sqlite3
import sys
import urllib.error

from enrich_musicbrainz import get

RETRY_DAYS = 180

SCHEMA = """
create table if not exists mb_recording (
    track_id integer primary key references track(track_id),
    mbid varchar,               -- null when the ISRC isn't on MusicBrainz
    first_release_date varchar, -- YYYY, YYYY-MM or YYYY-MM-DD
    disambiguation varchar,     -- MusicBrainz's note, e.g. "live", "single version"
    n_recordings integer,       -- recordings sharing the ISRC
    checked_at timestamp default current_timestamp
);
drop view if exists track_release;
create view track_release as
select t.track_id,
       case when m.first_release_date is not null
                 and (al.release_date is null or al.release_date like '0000%' or m.first_release_date <= al.release_date)
            then m.first_release_date
            when al.release_date not like '0000%' then al.release_date end as released,
       case when m.first_release_date is not null
                 and (al.release_date is null or al.release_date like '0000%' or m.first_release_date <= al.release_date)
            then 'musicbrainz' when al.release_date not like '0000%' then 'spotify' end as source
from track t left join album al using(album_id) left join mb_recording m using(track_id);
"""


def pick(recordings):
    """(mbid, first_release_date, disambiguation, n) for the recording with the earliest date."""
    if not recordings:
        return None, None, None, 0
    best = min(recordings, key=lambda r: r.get('first-release-date') or '9999')
    return best['id'], best.get('first-release-date') or None, best.get('disambiguation') or None, len(recordings)


def main(db_path='birdnest.db', track_ids=None, retries=None):
    """New tracks (only track_ids, when given: load_playlist.py passes the night's), plus misses due a retry
    (at most `retries` of them, oldest first; all when None)."""
    con = sqlite3.connect(db_path, timeout=60)  # the web app or tests may be reading
    con.executescript(SCHEMA)
    only = f"and t.track_id in ({','.join(str(int(t)) for t in track_ids)})" if track_ids is not None else ''
    new = con.execute(f"""
        select t.track_id, t.isrc_id from track t
        left join mb_recording m using(track_id) left join playlist_track pt using(track_id)
        where t.isrc_id is not null and m.track_id is null {only}
        group by t.track_id order by count(pt.playlist_id) desc""").fetchall() if track_ids != [] else []
    due = con.execute(f"""
        select t.track_id, t.isrc_id from track t join mb_recording m using(track_id)
        where t.isrc_id is not null and m.mbid is null and m.checked_at < datetime('now', '-{RETRY_DAYS} days')
        order by m.checked_at limit ?""", (-1 if retries is None else retries,)).fetchall()
    todo = new + due
    print(f"{len(todo)} tracks to look up")
    found = 0
    for n, (track_id, isrc) in enumerate(todo, 1):
        try:
            result = get(f"isrc/{isrc.upper()}") or {}
        except urllib.error.HTTPError as e:
            if e.code != 400:  # 400: not a well-formed ISRC
                raise
            result = {}
        mbid, first, disamb, count = pick(result.get('recordings') or [])
        found += mbid is not None
        with con:
            con.execute("""insert or replace into mb_recording
                (track_id, mbid, first_release_date, disambiguation, n_recordings) values (?, ?, ?, ?, ?)""",
                        (track_id, mbid, first, disamb, count))
        if n % 200 == 0:
            print(f"  {n}/{len(todo)}, {found} found")
    print(f"done: {found} of {len(todo)} found")


if __name__ == '__main__':
    main(*sys.argv[1:])
