"""Album details from Spotify (refresh_albums.py) and first release dates from MusicBrainz (enrich_mb_recordings.py)."""
import sqlite3

import enrich_mb_recordings
import refresh_albums


def test_album_fields_full_object():
    a = {'name': 'Seafaring Strangers', 'album_type': 'compilation', 'release_date': '2017-06-16',
         'release_date_precision': 'day', 'total_tracks': 20, 'external_ids': {'upc': '825764107235'},
         'copyrights': [{'text': '2017 Numero Group', 'type': 'C'}, {'text': '℗ 2017 Numero Group', 'type': 'P'}]}
    f = refresh_albums.album_fields(a)
    assert f['album_type'] == 'compilation' and f['upc'] == '825764107235'
    assert f['copyright_p'] == '℗ 2017 Numero Group' and f['copyright_c'] == '2017 Numero Group'
    assert f['fetched_at']


def test_album_fields_simplified_object_leaves_full_fields_alone():
    f = refresh_albums.album_fields({'name': 'X', 'album_type': 'single', 'release_date': '1968'})
    assert f == {'album_type': 'single', 'release_date': '1968'}


def test_pick_takes_earliest_dated_recording():
    recs = [{'id': 'b', 'first-release-date': '2006-03-01'},
            {'id': 'a', 'first-release-date': '1968-09', 'disambiguation': 'single version'},
            {'id': 'c'}]
    assert enrich_mb_recordings.pick(recs) == ('a', '1968-09', 'single version', 3)
    assert enrich_mb_recordings.pick([]) == (None, None, None, 0)


def test_track_release_prefers_the_earlier_date():
    con = sqlite3.connect(':memory:')
    con.executescript("""
        create table album (album_id integer primary key, release_date varchar);
        create table track (track_id integer primary key, album_id integer);
        insert into album values (1, '2004-01-01'), (2, '0000'), (3, '1999');
        insert into track values (10, 1), (11, 1), (12, 2), (13, 3), (14, 3);
    """)
    con.executescript(enrich_mb_recordings.SCHEMA)
    con.executemany("insert into mb_recording (track_id, mbid, first_release_date) values (?, ?, ?)", [
        (10, 'm', '1967-03'),      # reissue: MusicBrainz knows the original
        (12, 'm', '1971'),         # Spotify's date is a placeholder
        (13, 'm', '2010-05-01'),   # MusicBrainz only knows a later release
        (14, None, None),          # not on MusicBrainz
    ])
    got = dict((t, (r, s)) for t, r, s in con.execute("select * from track_release"))
    assert got == {10: ('1967-03', 'musicbrainz'), 11: ('2004-01-01', 'spotify'), 12: ('1971', 'musicbrainz'),
                   13: ('1999', 'spotify'), 14: ('1999', 'spotify')}
