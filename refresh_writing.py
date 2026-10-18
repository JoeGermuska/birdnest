"""Weekly: bring the music writing index up to date. Independent of load_playlist.py (no Spotify needed), though it's
best run after it, so the new show's artists are in birdnest.db to match against.

1. Match everything stored against the artists as they are now.
2. Check the feeds that are due, match what's new.
3. One gentle slice of the archive backfill (writing.py backfill; skip with --no-backfill).
4. Publish into birdnest.db and rebuild the web app's cache.

    python refresh_writing.py [--no-backfill]

Then commit birdnest.db and deploy. Don't run it at the same time as load_playlist.py: both write birdnest.db.
"""
import argparse

import factoids
import writing


def main(backfill=True):
    writing.refresh(rematch_first=True)
    if backfill:
        writing.backfill_sources()
    factoids.build_cache()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--no-backfill', action='store_true', help='skip the archive slice (the quick version)')
    main(backfill=not p.parse_args().no_backfill)
