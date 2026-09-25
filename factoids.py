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

        new_shares = [self._debut_share(pid) for pid in self.show_tracks]
        self.avg_debut_share = sum(new_shares) / len(new_shares) if new_shares else 0

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

    def show_factoids(self, playlist_id):
        """Return a list of {'label', 'text', 'kind'} dicts for one show."""
        d = self.show_dates[playlist_id]
        tids = self.show_tracks[playlist_id]
        if not tids:
            return []
        facts = []

        def add(kind, label, text):
            facts.append({'kind': kind, 'label': label, 'text': text})

        runtime_min = sum(self.tracks[t]['duration_ms'] or 0 for t in tids) // 60000
        aids = [a for t in tids for a in self.track_artists[t]]
        distinct = set(aids)
        add('stat', 'The night', f"{len(tids)} tracks, {runtime_min // 60}h {runtime_min % 60:02}m, "
                                 f"{len(distinct)} artists")

        share = self._debut_share(playlist_id)
        debuts = sum(1 for a in distinct if self.artist_dates[a][0] == d)
        rel = 'more than' if share > self.avg_debut_share * 1.15 else (
            'fewer than' if share < self.avg_debut_share * 0.85 else 'about')
        add('stat', 'New to the nest', f"{debuts} of {len(distinct)} artists appeared for the first time "
                                       f"({share:.0%}; {rel} the usual {self.avg_debut_share:.0%})")

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

        # Regulars, with their running tally
        regulars = []
        for aid in distinct:
            a_dates = self.artist_dates[aid]
            if len(a_dates) >= REGULAR_MIN_SHOWS:
                regulars.append((a_dates.index(d) + 1, self.artists[aid]['name']))
        if regulars:
            regulars.sort(reverse=True)
            add('regular', 'Regulars', ', '.join(f"{name} ({_ordinal(k)} show)" for k, name in regulars[:4]))

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

        # Obscure and mainstream bookends
        known = [a for a in distinct if self.artists[a]['followers'] is not None]
        if known:
            lo = min(known, key=lambda a: self.artists[a]['followers'])
            hi = max(known, key=lambda a: self.artists[a]['followers'])
            add('stat', 'Deepest cut', f"{self.artists[lo]['name']} ({self.artists[lo]['followers']:,} followers)")
            add('stat', 'Most famous', f"{self.artists[hi]['name']} ({self.artists[hi]['followers']:,} followers)")

        # Label concentration
        labels = Counter(self.tracks[t]['label'] for t in tids if self.tracks[t]['label'])
        for label, n in labels.most_common(1):
            if n >= 3:
                add('stat', 'Label of the night', f"{label} ({n} tracks)")

        longest = max(tids, key=lambda t: self.tracks[t]['duration_ms'] or 0)
        ms = self.tracks[longest]['duration_ms'] or 0
        add('stat', 'Longest', f"“{self.tracks[longest]['name']}” ({ms // 60000}:{ms // 1000 % 60:02})")

        return facts


@lru_cache(maxsize=1)
def _history_for(db_path, mtime):
    return History(db_path)


def get_history(db_path=DB_PATH):
    return _history_for(db_path, os.path.getmtime(db_path))
