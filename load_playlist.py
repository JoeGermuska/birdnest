from spotclient import Client
import models

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
