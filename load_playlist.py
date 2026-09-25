from spotclient import Client
import models
import enrich_musicbrainz
import refresh_artists
import enrich_wikidata
import factoids
import load_djs
import sys

db = models.Database()
session = models.get_session(create_all=True)

c = Client()
ab = c.allbirds()
latest = ab[0]
print(f"latest from API: {latest['name']}")

# update the database with the latest material
playlist = db.insert_playlist_from_json(session, latest)
print(f"saved playlist: {playlist.name}")

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

# look up new artists on MusicBrainz, then refresh links to other
# representations of artists (Wikipedia, Discogs, ...)
enrich_musicbrainz.main()
enrich_wikidata.main()

# pre-build the web app's analysis cache for the updated database
factoids.build_cache()
