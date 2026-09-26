"""Birds radio: sequence tracks from the show's own history.

The sequencer knows only our data -- which tracks the DJs played back to back,
which shared a show, genre families -- never a streaming service, so its output
can be played or saved anywhere. Each pick comes with the reason it was made.

Knobs:
  adventure  0..1. Low stays close: tracks that actually followed the current
             one, or shared its show, in its genre family. High wanders:
             looser connections and wildcards from other families.

Try it from the command line:
    python radio.py [artist name] [--adventure 0.3] [--n 20] [--seed 1]
"""
import math
import random
import sys
from collections import Counter, defaultdict
from functools import lru_cache

import factoids

ARTIST_COOLDOWN = 8  # tracks before an artist can come back
SHOW_COOLDOWN = 3    # steps before the same show counts fully as a connection again
WILDCARDS = 30       # random tracks considered at each step, for adventure


class Catalog:
    """Play history reshaped around recordings (ISRCs), for sequencing."""

    def __init__(self, history):
        h = self.history = history
        self.rec_track = {}           # recording -> a track_id to play it by (the most played)
        self.rec_shows = defaultdict(set)
        self.rec_artists = {}
        track_plays = Counter(t for tids in h.show_tracks.values() for t in tids)
        for t, n in track_plays.most_common():
            rec = h.recording_key(t)
            if rec not in self.rec_track and h.tracks[t]['spotify_url']:
                self.rec_track[rec] = t
                self.rec_artists[rec] = h.track_artists[t]
        self.plays = Counter()
        # (from, to) -> [playlist_id]: the DJs' own transitions
        self.follows = defaultdict(lambda: defaultdict(list))
        self.artist_follows = defaultdict(lambda: defaultdict(list))
        for pid, tids in h.show_tracks.items():
            recs = [h.recording_key(t) for t in tids]
            for r in recs:
                self.plays[r] += 1
                self.rec_shows[r].add(pid)
            for (t1, r1), r2 in zip(zip(tids, recs), recs[1:]):
                self.follows[r1][r2].append(pid)
                for a in h.track_artists[t1]:
                    self.artist_follows[a][r2].append(pid)
        self.show_recs = {pid: [h.recording_key(t) for t in tids] for pid, tids in h.show_tracks.items()}
        self.family = {rec: self._family(t) for rec, t in self.rec_track.items()}
        self.by_family = defaultdict(list)
        for rec, fam in self.family.items():
            self.by_family[fam].append(rec)
        self.all_recs = sorted(self.rec_track)
        # any Spotify track id we have -> its recording, for continuing from what's playing
        self.rec_by_spotify = {t['spotify_url'].rsplit('/', 1)[-1]: h.recording_key(tid)
                               for tid, t in h.tracks.items() if t['spotify_url']}

    def _family(self, track_id):
        h = self.history
        fams = Counter(h.family_of[g] for a in h.track_artists[track_id] for g in h.artist_genres[a])
        return fams.most_common(1)[0][0] if fams else None

    def track(self, rec):
        return self.history.tracks[self.rec_track[rec]]

    def title(self, rec):
        return f"“{self.track(rec)['name']}”"

    def artist_names(self, rec):
        return ', '.join(self.history.artists[a]['name'] for a in self.rec_artists[rec])

    def date(self, pids):
        return max(self.history.show_dates[p] for p in pids).isoformat()


@lru_cache(maxsize=2)
def _catalog(history_id):
    return Catalog(factoids.get_history())


def catalog():
    return _catalog(id(factoids.get_history()))


def candidates(cat, rec, adventure, rng):
    """{candidate recording: [(score, reason, playlist_ids)]} for what could follow rec."""
    h = cat.history
    close, loose = 1 - 0.8 * adventure, 0.2 + adventure
    out = defaultdict(list)
    for nxt, pids in cat.follows[rec].items():
        out[nxt].append((8 * close * len(pids), f"followed {cat.title(rec)} on {cat.date(pids)}", pids))
    for a in cat.rec_artists[rec]:
        for nxt, pids in cat.artist_follows[a].items():
            out[nxt].append((3 * close * len(pids), f"followed {h.artists[a]['name']} on {cat.date(pids)}", pids))
    for pid in cat.rec_shows[rec]:
        others = cat.show_recs[pid]
        for nxt in others:
            out[nxt].append((6 * close / math.sqrt(len(others)),
                             f"in the same show as {cat.title(rec)}, {cat.date([pid])}", [pid]))
    # looser: anything from shows where this recording's artists were played
    for a in cat.rec_artists[rec]:
        for pid in h.artist_shows[a]:
            others = cat.show_recs[pid]
            for nxt in others:
                out[nxt].append((2 * loose / len(others),
                                 f"{h.artists[a]['name']} was in the same show, {cat.date([pid])}", [pid]))
    fam = cat.family[rec]
    pool = cat.by_family[fam] if fam and adventure <= 0.5 else cat.all_recs
    for nxt in rng.sample(pool, min(WILDCARDS, len(pool))):
        other = cat.family[nxt]
        out[nxt].append((0.4 * adventure, f"a wildcard from {other}" if other else "a wildcard", []))
    return out


def sequence(start, n=20, adventure=0.3, seed=None, played=()):
    """[{'rec', 'track', 'reason'}] starting with recording start. With played (recordings
    already heard, oldest first), continue after start instead: start itself is left out and
    nothing in played repeats."""
    cat = catalog()
    rng = random.Random(seed)
    picks = [{'rec': start, 'reason': 'where we start'}]
    used = {start, *played}
    recent_artists = [a for r in [*played, start][-ARTIST_COOLDOWN:] for a in cat.rec_artists.get(r, [])]
    if played:
        n += 1
    recent_shows = []  # per step, the shows behind that pick's connection
    while len(picks) < n:
        cur = picks[-1]['rec']
        fam = cat.family[cur]
        scored = []
        for rec, parts in candidates(cat, cur, adventure, rng).items():
            if rec in used or rec not in cat.rec_track:
                continue
            if set(cat.rec_artists[rec]) & set(recent_artists[-ARTIST_COOLDOWN:]):
                continue
            # connections through a show we just drew from count for less, so a set
            # doesn't simply replay one night in order
            cooling = set().union(*recent_shows[-SHOW_COOLDOWN:])
            parts = [(s * (0.15 if cooling & set(pids) else 1), reason, pids) for s, reason, pids in parts]
            score = sum(s for s, _, _ in parts)
            if fam and cat.family[rec] == fam:
                score *= 1 + 2 * (1 - adventure)
            best = max(parts, key=lambda p: p[0])
            scored.append((score, rec, best[1], best[2]))
        if not scored:  # dead end: jump anywhere
            rec = rng.choice([r for r in cat.all_recs if r not in used])
            scored = [(1, rec, 'a fresh start', [])]
        # low adventure mostly takes the strongest connection; high flattens the odds
        power = 1 / (0.35 + 1.5 * adventure)
        weights = [s ** power for s, *_ in scored]
        _, rec, reason, pids = rng.choices(scored, weights)[0]
        picks.append({'rec': rec, 'reason': reason})
        recent_shows.append(set(pids))
        used.add(rec)
        recent_artists += cat.rec_artists[rec]
    if played:
        picks = picks[1:]
    for p in picks:
        p['track'] = cat.track(p['rec'])
        p['track_id'] = cat.rec_track[p['rec']]
        p['artists'] = [cat.history.artists[a] for a in cat.rec_artists[p['rec']]]
        p['family'] = cat.family[p['rec']]
    return picks


def start_for_artist(artist_id):
    """The artist's most played recording."""
    cat = catalog()
    recs = [r for r, arts in cat.rec_artists.items() if artist_id in arts]
    return max(recs, key=lambda r: (cat.plays[r], r)) if recs else None


def start_for_show(playlist_id):
    cat = catalog()
    return next((r for r in cat.show_recs.get(playlist_id, []) if r in cat.rec_track), None)


def random_start(rng):
    return rng.choice(catalog().all_recs)


if __name__ == '__main__':
    args, opts = [], {'--adventure': 0.3, '--n': 20, '--seed': None}
    it = iter(sys.argv[1:])
    for a in it:
        if a in opts:
            opts[a] = type(opts[a] or 0)(next(it))
        else:
            args.append(a)
    h = factoids.get_history()
    rng = random.Random(opts['--seed'])
    if args:
        name = ' '.join(args).lower()
        artist = next(a for a in h.artists.values() if (a['name'] or '').lower() == name)
        start = start_for_artist(artist['artist_id'])
    else:
        start = random_start(rng)
    for i, p in enumerate(sequence(start, int(opts['--n']), float(opts['--adventure']), opts['--seed']), 1):
        print(f"{i:2}. {', '.join(a['name'] for a in p['artists'])} – {p['track']['name']}  [{p['family']}]"
              f"\n      {p['reason']}")
