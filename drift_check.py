"""Read-only check of how much stored Spotify data has drifted from what the API
returns now. Nothing is written to the database.

Samples artists evenly across the year they were last played (so long-unplayed
artists are represented), plus a sample of tracks, fetches them from Spotify,
and reports changes in names, popularity, followers and genres. Also flags
fields the API no longer returns at all, which would suggest your app's access
has been restricted.

    python drift_check.py [--artists 200] [--tracks 100] [--seed 1] [--out drift.json] [--db birdnest.db]
"""
import argparse
import json
import random
import sqlite3
import statistics
from collections import Counter, defaultdict


def sample_artists(con, n, rng):
    rows = con.execute("""
        select a.artist_id, a.spotify_id, a.name, a.popularity, a.followers, max(p.date) last_played
        from artist a join track_artist ta using(artist_id) join playlist_track using(track_id)
        join playlist p using(playlist_id)
        where a.spotify_id is not null group by a.artist_id""").fetchall()
    by_year = defaultdict(list)
    for r in rows:
        by_year[r['last_played'][:4]].append(r)
    per_year = max(1, n // len(by_year))
    picked = []
    for year in sorted(by_year):
        picked += rng.sample(by_year[year], min(per_year, len(by_year[year])))
    genres = defaultdict(set)
    for aid, g in con.execute("select artist_id, g.name from artist_genre join genre g using(genre_id)"):
        genres[aid].add(g)
    return [dict(r, genres=genres[r['artist_id']]) for r in picked]


def sample_tracks(con, n, rng):
    rows = con.execute("""select t.track_id, t.spotify_id, t.name, t.popularity, t.isrc_id, t.duration_ms,
                                 max(p.date) last_played
                          from track t join playlist_track using(track_id) join playlist p using(playlist_id)
                          where t.spotify_id is not null group by t.track_id""").fetchall()
    return [dict(r) for r in rng.sample(rows, min(n, len(rows)))]


def median(xs):
    return statistics.median(xs) if xs else None


def compare_artists(stored, fetched):
    report = {'sampled': len(stored), 'missing': [], 'fields_absent': Counter(), 'renamed': [],
              'followers_ratio_by_year': defaultdict(list), 'popularity_delta_by_year': defaultdict(list),
              'genres': Counter(), 'genres_lost': Counter(), 'genres_gained': Counter(),
              'tags_stored': 0, 'tags_now': 0}
    for s in stored:
        f = fetched.get(s['spotify_id'])
        if f is None:
            report['missing'].append(s['name'])
            continue
        year = s['last_played'][:4]
        for field in ('popularity', 'followers', 'genres', 'images'):
            if field not in f or f[field] is None:
                report['fields_absent'][field] += 1
        if f.get('name') and f['name'] != s['name']:
            report['renamed'].append((s['name'], f['name']))
        now_followers = (f.get('followers') or {}).get('total')
        if now_followers is not None and s['followers']:
            report['followers_ratio_by_year'][year].append(now_followers / s['followers'])
        if f.get('popularity') is not None and s['popularity'] is not None:
            report['popularity_delta_by_year'][year].append(f['popularity'] - s['popularity'])
        if 'genres' in f:
            old, new = s['genres'], set(f['genres'] or [])
            report['tags_stored'] += len(old)
            report['tags_now'] += len(new)
            if not old and not new:
                report['genres']['none before or now'] += 1
            elif old == new:
                report['genres']['unchanged'] += 1
            elif old and not new:
                report['genres']['lost all'] += 1
            elif not old:
                report['genres']['gained (had none)'] += 1
            elif old - new and new - old:
                report['genres']['some lost, some gained'] += 1
            elif old - new:
                report['genres']['lost some'] += 1
            else:
                report['genres']['gained some'] += 1
            report['genres_lost'].update(old - new)
            report['genres_gained'].update(new - old)
    return report


def compare_tracks(stored, fetched):
    report = {'sampled': len(stored), 'missing': [], 'renamed': [], 'isrc_changed': 0, 'duration_changed': 0,
              'popularity_absent': 0, 'popularity_delta': []}
    for s in stored:
        f = fetched.get(s['spotify_id'])
        if f is None:
            report['missing'].append(s['name'])
            continue
        if f.get('name') != s['name']:
            report['renamed'].append((s['name'], f.get('name')))
        if (f.get('external_ids') or {}).get('isrc') != s['isrc_id']:
            report['isrc_changed'] += 1
        if f.get('duration_ms') != s['duration_ms']:
            report['duration_changed'] += 1
        if f.get('popularity') is None:
            report['popularity_absent'] += 1
        elif s['popularity'] is not None:
            report['popularity_delta'].append(f['popularity'] - s['popularity'])
    return report


def print_report(a, t):
    print(f"\n== Artists ({a['sampled']} sampled across years last played) ==")
    print(f"not returned by the API: {len(a['missing'])}" + (f"  e.g. {a['missing'][:5]}" if a['missing'] else ''))
    if a['fields_absent']:
        print(f"fields missing from API responses: {dict(a['fields_absent'])}  <- possible API restriction")
    print(f"renamed: {len(a['renamed'])}" + ''.join(f"\n   {o} -> {n}" for o, n in a['renamed'][:10]))
    print("\nfollowers now / stored (median, by year last played):")
    for y in sorted(a['followers_ratio_by_year']):
        r = a['followers_ratio_by_year'][y]
        doubled = sum(1 for x in r if x >= 2) / len(r)
        print(f"   {y}: {median(r):5.2f}x   ({doubled:.0%} at least doubled, n={len(r)})")
    print("popularity now - stored (median, by year last played):")
    for y in sorted(a['popularity_delta_by_year']):
        d = a['popularity_delta_by_year'][y]
        print(f"   {y}: {median(d):+5.1f}   (range {min(d):+d} to {max(d):+d}, n={len(d)})")
    print(f"\ngenres: {dict(a['genres'])}")
    print(f"genre tags: {a['tags_stored']} stored -> {a['tags_now']} now")
    if a['genres_lost']:
        print(f"most often dropped: {a['genres_lost'].most_common(8)}")
    if a['genres_gained']:
        print(f"most often added: {a['genres_gained'].most_common(8)}")

    print(f"\n== Tracks ({t['sampled']} sampled) ==")
    print(f"not returned: {len(t['missing'])}, renamed: {len(t['renamed'])}, ISRC changed: {t['isrc_changed']}, "
          f"duration changed: {t['duration_changed']}, popularity missing: {t['popularity_absent']}")
    for o, n in t['renamed'][:5]:
        print(f"   {o} -> {n}")
    if t['popularity_delta']:
        print(f"popularity now - stored: median {median(t['popularity_delta']):+.1f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--artists', type=int, default=200)
    parser.add_argument('--tracks', type=int, default=100)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--out', help='also write the full report as JSON')
    parser.add_argument('--db', default='birdnest.db')
    args = parser.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rng = random.Random(args.seed)
    artists = sample_artists(con, args.artists, rng)
    tracks = sample_tracks(con, args.tracks, rng)

    from spotclient import Client  # imported late so the comparison logic can be tested without credentials
    client = Client()
    # the API answers in request order; pair by position so merged/relinked ids still line up
    fetched_artists = dict(zip((a['spotify_id'] for a in artists), client.artists(a['spotify_id'] for a in artists)))
    fetched_tracks = dict(zip((t['spotify_id'] for t in tracks), client.tracks(t['spotify_id'] for t in tracks)))

    a_report = compare_artists(artists, fetched_artists)
    t_report = compare_tracks(tracks, fetched_tracks)
    print_report(a_report, t_report)
    if args.out:
        with open(args.out, 'w') as f:
            json.dump({'artists': a_report, 'tracks': t_report}, f, indent=1, default=list)
        print(f"\nfull report written to {args.out}")


if __name__ == '__main__':
    main()
