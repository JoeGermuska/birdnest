"""Merge duplicate genre rows (one row per name, artist_genre repointed) and add the unique index that keeps it so.

Artist.get_or_create used to make a new Genre row for every artist-genre pair, so the table filled with copies
("jazz" 141 times). The loader is fixed now; this cleans up what it left behind. Safe to re-run.
    python genre_fixer.py [path/to/birdnest.db]
"""
import sqlite3
import sys


def main(db_path='birdnest.db'):
    con = sqlite3.connect(db_path)
    before = con.execute("select count(*) from genre").fetchone()[0]
    con.execute("create temp table keep as select name, min(genre_id) keep_id from genre group by name")
    con.execute("update artist_genre set genre_id = (select keep_id from keep join genre g using(name) "
                "where g.genre_id = artist_genre.genre_id)")
    con.execute("delete from genre where genre_id not in (select keep_id from keep)")
    con.execute("create unique index if not exists ix_genre_name on genre(name)")
    con.commit()
    after = con.execute("select count(*) from genre").fetchone()[0]
    print(f"genres: {before} -> {after}")
    con.close()


if __name__ == '__main__':
    main(*sys.argv[1:])
