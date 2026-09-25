from spotclient import Client
import models
import enrich_musicbrainz
import refresh_artists
import enrich_wikidata
import factoids

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

# snapshot tonight's artists (so each show keeps its own follower counts) plus
# whichever artists are due for their roughly monthly refresh
refresh_artists.main(force_ids={a.artist_id for t in playlist.tracks for a in t.artists})

# look up new artists on MusicBrainz, then refresh links to other
# representations of artists (Wikipedia, Discogs, ...)
enrich_musicbrainz.main()
enrich_wikidata.main()

# pre-build the web app's analysis cache for the updated database
factoids.build_cache()
