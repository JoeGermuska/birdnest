"""The occasional jobs: everything the weekly load_playlist.py doesn't do because it's slow. Run it every month or
two (it takes a while, mostly the Wikidata name searches), then commit birdnest.db and deploy as usual.

1. Refresh every artist's outside links from Wikidata and MusicBrainz's ids (picks up Wikipedia articles and
   other links added since we last looked; the weekly load only checks artists with no Wikidata item yet).
2. Rebuild the artist candidates for /todo/wikidata (name searches on Wikidata for artists with no links).
3. Rebuild the album list for /todo/wikidata/albums and the web app's analysis cache.

Everything else (new shows' artists, albums, tracks; a few MusicBrainz retries; to-do work people have done)
is handled by load_playlist.py.
    python maintenance.py [path/to/birdnest.db]
"""
import sys
import time

import enrich_wikidata
import factoids
import wikidata_album_todo
import wikidata_candidates


def main(db_path='birdnest.db'):
    for label, step in [
        ("artist links from Wikidata", lambda: enrich_wikidata.main(db_path, refresh_all=True)),
        ("artist candidates for /todo/wikidata", lambda: wikidata_candidates.main(db_path)),
        ("album list for /todo/wikidata/albums", lambda: wikidata_album_todo.main(db_path)),
        ("analysis cache", lambda: factoids.build_cache(db_path)),
    ]:
        start = time.time()
        print(f"== {label}")
        step()
        print(f"   ({time.time() - start:.0f}s)")


if __name__ == '__main__':
    main(*sys.argv[1:])
