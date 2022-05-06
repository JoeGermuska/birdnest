from spotclient import Client
import models
from collections import defaultdict

session = models.get_session(create_all=True)

gdict = defaultdict(list)

for g in session.query(models.Genre).all():
    gdict[g.name].append(g)

print(f"Found {len(gdict)} unique genres")

for gname, glist in gdict.items():
    if len(glist) > 1:
        main = glist[0]
        rest = glist[1:]
        for bogus in rest:
            for artist in bogus.artists:
                artist.genre_objs.remove(bogus)
                artist.genre_objs.append(main)
            session.delete(bogus)

print(f"down to {session.query(models.Genre).count()}")
session.commit()
