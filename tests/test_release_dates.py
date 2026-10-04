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


def test_best_release_picks_the_believable_earliest():
    from factoids import best_release as best
    rec, alb, sp = 'musicbrainz', 'musicbrainz-album', 'spotify'
    assert best([('1967-03', rec), (None, alb), ('2004-01-01', sp)]) == ('1967-03', rec)        # a reissue
    assert best([(None, rec), ('1969', alb), ('2006-01-01', sp)]) == ('1969', alb)               # the album knows
    assert best([('1975', rec), ('1969', alb), ('2010', sp)]) == ('1975', rec)                   # a bonus track
    assert best([('2010-05-01', rec), (None, alb), ('1999', sp)]) == ('1999', sp)                # MusicBrainz missed one
    assert best([('1985', rec), (None, alb), ('1900-01-01', sp)]) == ('1985', rec)               # Spotify's placeholder
    assert best([('1972', rec), (None, alb), ('1972-10-11', sp)]) == ('1972', rec)               # a bare year comes first
    assert best([('2020-06-26', rec), (None, alb), ('2020-05-05', sp)]) == ('2020-05-05', sp)    # the single came out first
    assert best([(None, rec), ('2026-10-02', alb), ('2026-09-20', sp)]) == ('2026-09-20', sp)    # same year: Spotify's day
    assert best([(None, rec), (None, alb), ('0000', sp)]) is None
