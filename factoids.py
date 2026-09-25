"""Computed "factoids" about shows and the tracks in them.

Everything here is derived from the whole play history, so we load the
(small) history once into memory and answer questions from that. The
database only changes on deploy, so the cache is keyed on the file's mtime.
"""
import math
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

DB_PATH = 'birdnest.db'

THEME_NIGHT_MIN_TRACKS = 4
LONG_ABSENCE_DAYS = 365 * 2
REGULAR_MIN_SHOWS = 10
NOVELTY_WINDOW = 20  # shows on each side to compare novelty against
GENRE_MIN_TRACKS = 20  # ignore very rare genres, whose lift is noisy


@dataclass
class TrackNotes:
    """Per-row annotations for a track as it appears in one show."""
    debut_artists: list = field(default_factory=list)
    returning: list = field(default_factory=list)  # (artist_name, last_date, days)
    play_number: int = 1  # 1 = first time this recording was played
    total_plays: int = 1
    other_dates: list = field(default_factory=list)


def _years_months(days):
    years, rem = divmod(days, 365)
    months = rem // 30
    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months and years < 3:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    return ' '.join(parts) or f"{days} days"


def _compact(n):
    for size, suffix in ((1_000_000, 'M'), (1_000, 'k')):
        if n >= size:
            return f"{n / size:.1f}".rstrip('0').rstrip('.') + suffix
    return str(n)


def _ordinal(n):
    suffix = 'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"


class History:
    def __init__(self, db_path=DB_PATH):
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row

        self.show_dates = {}  # playlist_id -> date
        for r in con.execute("select playlist_id, date from playlist where date is not null"):
            self.show_dates[r['playlist_id']] = date.fromisoformat(r['date'])

        self.artists = {r['artist_id']: dict(r) for r in con.execute(
            "select artist_id, name, spotify_id, followers, popularity from artist")}

        self.track_artists = defaultdict(list)
        for r in con.execute("select track_id, artist_id from track_artist"):
            self.track_artists[r['track_id']].append(r['artist_id'])

        self.tracks = {r['track_id']: dict(r) for r in con.execute(
            """select t.track_id, t.name, t.duration_ms, t.isrc_id, t.album_id, al.name album, al.label
               from track t left join album al using(album_id)""")}

        # genres by *name* -- the genre table has duplicate rows per name
        self.artist_genres = defaultdict(set)
        for r in con.execute(
                "select ag.artist_id, g.name from artist_genre ag join genre g using(genre_id)"):
            self.artist_genres[r['artist_id']].add(r['name'])

        self.show_tracks = defaultdict(list)  # playlist_id -> [track_id] in order
        for r in con.execute("select playlist_id, track_id from playlist_track order by playlist_id, sequence"):
            if r['playlist_id'] in self.show_dates:
                self.show_tracks[r['playlist_id']].append(r['track_id'])
        con.close()

        # Recordings can appear under several Spotify track ids (single vs. album,
        # compilations); ISRC groups them so repeat counts are honest.
        def recording_key(track_id):
            return self.tracks[track_id]['isrc_id'] or f"t{track_id}"
        self.recording_key = recording_key

        self.recording_dates = defaultdict(list)
        self.artist_dates = defaultdict(list)
        genre_track_counts = Counter()
        total_track_plays = 0
        for pid in sorted(self.show_tracks, key=self.show_dates.get):
            d = self.show_dates[pid]
            for tid in self.show_tracks[pid]:
                self.recording_dates[recording_key(tid)].append(d)
                total_track_plays += 1
                genres = set()
                for aid in self.track_artists[tid]:
                    self.artist_dates[aid].append(d)
                    genres |= self.artist_genres[aid]
                genre_track_counts.update(genres)
        for dates in (*self.recording_dates.values(), *self.artist_dates.values()):
            dates[:] = sorted(set(dates))
        self.genre_track_counts = genre_track_counts
        self.total_track_plays = total_track_plays

        self.pid_by_date = {d: pid for pid, d in self.show_dates.items()}
        played = sorted(self.show_dates[p] for p in self.show_tracks)
        self.first_date, self.last_date = played[0], played[-1]
        self.show_artists = {pid: {a for t in tids for a in self.track_artists[t]}
                             for pid, tids in self.show_tracks.items()}
        self.artist_shows = defaultdict(set)
        for pid, aids in self.show_artists.items():
            for a in aids:
                self.artist_shows[a].add(pid)
        self.plays_by_year = Counter(self.show_dates[p].year for p, tids in self.show_tracks.items() for _ in tids)
        self.genre_artists = defaultdict(set)
        for aid, genres in self.artist_genres.items():
            for g in genres:
                self.genre_artists[g].add(aid)
        # rank artists by number of shows (1 = most shows; ties share a rank)
        self.artist_rank = {a: 1 + sum(1 for a2 in self.artist_shows if len(self.artist_shows[a2]) > len(v))
                            for a, v in self.artist_shows.items()}

        # Novelty: share of a show's artists who had never been played before.
        # It trends down as the pool of already-played artists grows, so rank
        # each show against a sliding window of neighboring shows.
        self.novelty = {pid: self._debut_share(pid) for pid in self.show_tracks}
        ordered = sorted(self.novelty, key=self.show_dates.get)
        self.novelty_peers, self.novelty_rank = {}, {}
        for i, pid in enumerate(ordered):
            lo = max(0, min(i - NOVELTY_WINDOW, len(ordered) - 2 * NOVELTY_WINDOW - 1))
            peers = [self.novelty[p] for p in ordered[lo:lo + 2 * NOVELTY_WINDOW + 1]]
            share = self.novelty[pid]
            below = sum(1 for x in peers if x < share) + 0.5 * (sum(1 for x in peers if x == share) - 1)
            self.novelty_peers[pid] = sorted(peers)
            self.novelty_rank[pid] = below / (len(peers) - 1) if len(peers) > 1 else 0.5

    def _debut_share(self, pid):
        d = self.show_dates[pid]
        aids = {a for t in self.show_tracks[pid] for a in self.track_artists[t]}
        if not aids:
            return 0
        return sum(1 for a in aids if self.artist_dates[a][0] == d) / len(aids)

    def track_notes(self, playlist_id):
        """Return {track_id: TrackNotes} for one show."""
        d = self.show_dates[playlist_id]
        notes = {}
        for tid in self.show_tracks[playlist_id]:
            n = TrackNotes()
            dates = self.recording_dates[self.recording_key(tid)]
            n.play_number = dates.index(d) + 1
            n.total_plays = len(dates)
            n.other_dates = [x for x in dates if x != d]
            for aid in self.track_artists[tid]:
                a_dates = self.artist_dates[aid]
                i = a_dates.index(d)
                if i == 0:
                    n.debut_artists.append(self.artists[aid]['name'])
                elif (d - a_dates[i - 1]).days >= LONG_ABSENCE_DAYS:
                    n.returning.append((self.artists[aid]['name'], a_dates[i - 1], (d - a_dates[i - 1]).days))
            notes[tid] = n
        return notes

    def show_stats(self, playlist_id):
        """Plain playlist-level metadata."""
        tids = self.show_tracks[playlist_id]
        return {
            'tracks': len(tids),
            'runtime_min': sum(self.tracks[t]['duration_ms'] or 0 for t in tids) // 60000,
            'artists': len({a for t in tids for a in self.track_artists[t]}),
            'novelty': self.novelty.get(playlist_id, 0),
            'novelty_rank': self.novelty_rank.get(playlist_id, 0.5),
            'novelty_peers': self.novelty_peers.get(playlist_id, []),
        }

    def show_factoids(self, playlist_id):
        """Return a list of {'label', 'text', 'kind'} dicts for one show."""
        d = self.show_dates[playlist_id]
        tids = self.show_tracks[playlist_id]
        if not tids:
            return []
        facts = []

        def add(kind, label, text):
            facts.append({'kind': kind, 'label': label, 'text': text})

        aids = [a for t in tids for a in self.track_artists[t]]
        distinct = set(aids)

        # Album session: a run of consecutive tracks from one album
        best_run, run, run_start = 0, 0, 0
        for i, t in enumerate(tids):
            if i and self.tracks[t]['album_id'] == self.tracks[tids[i - 1]]['album_id']:
                run += 1
            else:
                run = 1
            if run > best_run:
                best_run, run_start = run, i - run + 1
        album_session = best_run >= THEME_NIGHT_MIN_TRACKS
        if album_session:
            first = self.tracks[tids[run_start]]
            by = ', '.join(self.artists[a]['name'] for a in self.track_artists[tids[run_start]])
            add('theme', 'Album session', f"{first['album']} by {by}: {best_run} tracks in a row, "
                                          f"starting at #{run_start + 1}")

        # Theme night: one artist dominating (not just because of an album session)
        for aid, n in Counter(aids).most_common(1):
            if n >= THEME_NIGHT_MIN_TRACKS and not album_session:
                add('theme', 'Theme night?', f"{self.artists[aid]['name']} accounts for {n} of {len(tids)} tracks")

        # Repeats of a recording
        repeats = []
        for t in tids:
            dates = self.recording_dates[self.recording_key(t)]
            if len(dates) > 1 and dates[0] < d:
                repeats.append((dates.index(d) + 1, self.tracks[t]['name']))
        if repeats:
            repeats.sort(reverse=True)
            listed = ', '.join(f"“{name}” ({_ordinal(k)} time)" for k, name in repeats[:3])
            more = f" and {len(repeats) - 3} more" if len(repeats) > 3 else ''
            add('repeat', 'Heard it before', listed + more)

        # Long-lost artists
        gaps = []
        for aid in distinct:
            a_dates = self.artist_dates[aid]
            i = a_dates.index(d)
            if i > 0 and (d - a_dates[i - 1]).days >= LONG_ABSENCE_DAYS:
                gaps.append(((d - a_dates[i - 1]).days, self.artists[aid]['name']))
        if gaps:
            days, name = max(gaps)
            add('return', 'Welcome back', f"{name}, first time in {_years_months(days)}")

        # Regulars, with their running tally, and first sightings of future regulars
        regulars, firsts = [], []
        for aid in distinct:
            a_dates = self.artist_dates[aid]
            if len(a_dates) >= REGULAR_MIN_SHOWS:
                k = a_dates.index(d) + 1
                (firsts if k == 1 else regulars).append((k, len(a_dates), self.artists[aid]['name']))
        if regulars:
            regulars.sort(reverse=True)
            add('regular', 'Regulars', ', '.join(f"{name} ({_ordinal(k)} show)" for k, _, name in regulars[:4]))
        if firsts:
            firsts.sort(key=lambda x: -x[1])
            add('regular', 'First sighting', ', '.join(f"{name} (went on to {total} shows)" for _, total, name in firsts[:3]))

        # Genre lean: which genres are most over-represented tonight vs. all time
        tonight = Counter()
        for t in tids:
            genres = set()
            for aid in self.track_artists[t]:
                genres |= self.artist_genres[aid]
            tonight.update(genres)
        leans = []
        for g, n in tonight.items():
            # baseline is every *other* show, so a theme night doesn't define its own normal
            elsewhere = self.genre_track_counts[g] - n
            if n < 3 or elsewhere < GENRE_MIN_TRACKS:
                continue
            lift = (n / len(tids)) / (elsewhere / (self.total_track_plays - len(tids)))
            if lift > 1.5:
                leans.append((n * math.log(lift), g, lift))
        if leans:
            leans.sort(reverse=True)
            add('genre', 'Leaning', ', '.join(f"{g} ({lift:.1f}×)" for _, g, lift in leans[:3]))

        # Label concentration
        labels = Counter(self.tracks[t]['label'] for t in tids if self.tracks[t]['label'])
        for label, n in labels.most_common(1):
            if n >= 3:
                add('label', 'Label of the night', f"{label} ({n} tracks)")

        # Obscure-to-famous range
        known = [a for a in distinct if self.artists[a]['followers'] is not None]
        if len(known) > 1:
            lo = min(known, key=lambda a: self.artists[a]['followers'])
            hi = max(known, key=lambda a: self.artists[a]['followers'])
            add('range', 'Range', f"from {self.artists[lo]['name']} ({_compact(self.artists[lo]['followers'])} followers) "
                                  f"to {self.artists[hi]['name']} ({_compact(self.artists[hi]['followers'])})")

        return facts


    def timeline_x(self, d, width):
        """Horizontal position of date d on a timeline spanning the whole history."""
        span = (self.last_date - self.first_date).days or 1
        return round((d - self.first_date).days / span * width, 1)

    def year_x(self, year, width):
        return self.timeline_x(max(self.first_date, date(year, 1, 1)), width)

    def artist_profile(self, artist_id):
        pids = sorted(self.artist_shows.get(artist_id, ()), key=self.show_dates.get)
        if not pids:
            return None
        shows = []
        for pid in pids:
            items = [(i + 1, t) for i, t in enumerate(self.show_tracks[pid]) if artist_id in self.track_artists[t]]
            shows.append({'date': self.show_dates[pid], 'tracks': [
                {'position': pos, 'name': self.tracks[t]['name'], 'album': self.tracks[t]['album'],
                 'others': [self.artists[a] for a in self.track_artists[t] if a != artist_id]}
                for pos, t in items]})
        dates = [s['date'] for s in shows]
        facts = []

        def add(kind, label, text):
            facts.append({'kind': kind, 'label': label, 'text': text})

        n = len(pids)
        rank = self.artist_rank[artist_id]
        if n >= 3:
            add('regular', 'Standing', f"#{rank} most-played artist" if rank <= 50 else f"played at {n} shows")
        if len(dates) > 1:
            gaps = [((b - a).days, a, b) for a, b in zip(dates, dates[1:])]
            days, a, b = max(gaps)
            if days >= 180:
                add('return', 'Longest absence', f"{_years_months(days)}, between {a} and {b}")

        recordings = Counter(self.recording_key(t) for pid in pids for t in self.show_tracks[pid]
                             if artist_id in self.track_artists[t])
        top_key, top_n = recordings.most_common(1)[0]
        if top_n > 1:
            name = next(self.tracks[t]['name'] for t in self.tracks if self.recording_key(t) == top_key)
            add('repeat', 'Most played', f"“{name}” ({top_n} times)")

        collaborators = Counter(a['name'] for s in shows for t in s['tracks'] for a in t['others'])
        if collaborators:
            add('stat', 'Credited with', ', '.join(name for name, _ in collaborators.most_common(5)))

        # Artists who turn up in the same shows more than chance would suggest
        if n >= 4:
            together = Counter(a for pid in pids for a in self.show_artists[pid] if a != artist_id)
            total_shows = len(self.show_tracks)
            scored = []
            for a, k in together.items():
                if k < 3 or a in {x['artist_id'] for s in shows for t in s['tracks'] for x in t['others']}:
                    continue
                lift = k / (n * len(self.artist_shows[a]) / total_shows)
                if lift > 2:
                    scored.append((k * math.log(lift), a, k))
            if scored:
                scored.sort(reverse=True)
                add('genre', 'Often shares a show with',
                    ', '.join(f"{self.artists[a]['name']} ({k}×)" for _, a, k in scored[:4]))

        return {'shows': shows, 'n_shows': n, 'n_plays': sum(len(s['tracks']) for s in shows),
                'first': dates[0], 'last': dates[-1], 'facts': facts,
                'genres': sorted(self.artist_genres.get(artist_id, ()))}

    def genre_profile(self, name):
        aids = self.genre_artists.get(name)
        if not aids:
            return None
        artists = []
        for a in aids:
            pids = sorted(self.artist_shows.get(a, ()), key=self.show_dates.get)
            if pids:
                artists.append({**self.artists[a], 'shows': len(pids), 'first': self.show_dates[pids[0]],
                                'last': self.show_dates[pids[-1]]})
        artists.sort(key=lambda x: (-x['shows'], x['name']))

        # share of each year's track plays that carry this genre
        by_year = Counter()
        for pid, tids in self.show_tracks.items():
            for t in tids:
                if any(a in aids for a in self.track_artists[t]):
                    by_year[self.show_dates[pid].year] += 1
        years = [{'year': y, 'plays': by_year[y], 'share': by_year[y] / self.plays_by_year[y]}
                 for y in sorted(self.plays_by_year)]

        # related genres: most over-represented among this genre's artists
        n_artists = len(self.artist_genres)
        related = []
        co = Counter(g for a in aids for g in self.artist_genres[a] if g != name)
        for g, k in co.items():
            if k < 3:
                continue
            lift = k / (len(aids) * len(self.genre_artists[g]) / n_artists)
            related.append((k * math.log(lift) if lift > 1 else 0, g, k))
        related.sort(reverse=True)

        return {'artists': artists, 'years': years,
                'n_plays': sum(by_year.values()),
                'n_shows': len({pid for a in aids for pid in self.artist_shows.get(a, ())}),
                'related': [(g, k) for score, g, k in related[:12] if score > 0]}


@lru_cache(maxsize=1)
def _history_for(db_path, mtime):
    return History(db_path)


def get_history(db_path=DB_PATH):
    return _history_for(db_path, os.path.getmtime(db_path))
