from spotclient import Client
import models
import enrich_musicbrainz
import enrich_mb_albums
import enrich_mb_recordings
import refresh_albums
import refresh_artists
import enrich_wikidata
import wikidata_album_todo
import factoids
import make_tiles
import load_djs
import re
import sys

db = models.Database()
session = models.get_session(create_all=True)

c = Client()
if len(sys.argv) > 1:
    # a specific playlist: URL (https://open.spotify.com/playlist/ID?si=...), URI (spotify:playlist:ID) or ID
    m = re.search(r'playlist[/:]([A-Za-z0-9]+)', sys.argv[1])
    latest = c.playlist(m.group(1) if m else sys.argv[1])
    print(f"from API: {latest['name']}")
else:
    ab = c.allbirds()
    latest = ab[0]
    print(f"latest from API: {latest['name']}")

# update the database with the latest material
playlist = db.insert_playlist_from_json(session, latest)
print(f"saved playlist: {playlist.name}")
if playlist.date is None:
    print("WARNING: no YYYY-MM-DD in the playlist name, so it won't show up on the site")

# if that looks right, rebuild the index and commit db changes

db.rebuild_fts(session)
session.commit()

# who was in the room: Spotify doesn't know, so ask (paste the show-log line)
if not playlist.show_djs and sys.stdin.isatty():
    line = input(f"DJs for {playlist.date}, e.g. 'Thursday, October 1st (DJs: Heath, Joe)' (blank to skip): ").strip()
    if line and not load_djs.add(line, commit=True, session=session):
        print("DJs not saved; add them later with: python load_djs.py add \"LINE\" --commit")

# snapshot tonight's artists (so each show keeps its own follower counts) plus
# whichever artists are due for their roughly monthly refresh
refresh_artists.main(force_ids={a.artist_id for t in playlist.tracks for a in t.artists})

# album details (release date, type, UPC) for any album the load didn't fill in
refresh_albums.main()

# look up new artists on MusicBrainz, then refresh links to other
# representations of artists (Wikipedia, Discogs, ...); and tonight's tracks'
# first release dates and albums' links, plus a few earlier misses to retry
# (run enrich_mb_recordings.py and enrich_mb_albums.py on their own for a backlog)
enrich_musicbrainz.main()
enrich_mb_recordings.main(track_ids=[t.track_id for t in playlist.tracks], retries=100)
enrich_mb_albums.main(album_ids=list({t.album_id for t in playlist.tracks if t.album_id}), retries=50)
# no_data: also re-check artists with no Wikidata item yet, which picks up IDs added from /todo/wikidata
enrich_wikidata.main(no_data=True)
wikidata_album_todo.main()  # albums whose Wikidata item lacks our Spotify ID (/todo/wikidata/albums)

# small tile copies of any new show images (also made at image build)
make_tiles.main()

# pre-build the web app's analysis cache for the updated database
factoids.build_cache()
