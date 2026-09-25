"""Computed "factoids" about shows and the tracks in them.

Everything here is derived from the whole play history, so we load the
(small) history once into memory and answer questions from that. The
database only changes on deploy, so the cache is keyed on the file's mtime.
"""
import math
import re
import os
import sqlite3

import genre_families
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

DB_PATH = 'birdnest.db'

THEME_NIGHT_MIN_TRACKS = 4
LONG_ABSENCE_DAYS = 365 * 2
REGULAR_MIN_SHOWS = 10
NOVELTY_WINDOW = 20  # shows on each side to compare novelty against
GENRE_MIN_TRACKS = 20
LINK_LABELS = [('wikipedia', 'Wikipedia'), ('musicbrainz', 'MusicBrainz'), ('discogs', 'Discogs'),
               ('bandcamp', 'Bandcamp'), ('allmusic', 'AllMusic'), ('website', 'Website'), ('wikidata', 'Wikidata')]  # ignore very rare genres, whose lift is noisy


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


def _competition_ranks(values):
    """1-based ranks for already-sorted (descending) values; ties share a rank."""
    ranks, prev = [], object()
    for i, v in enumerate(values):
        ranks.append(ranks[-1] if v == prev else i + 1)
        prev = v
    return ranks


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')


class Facts(list):
    """Factoids as {'kind', 'label', 'parts', 'more'}. Parts are plain strings or
    dicts naming something linkable: {'artist': spotify_id}, {'genre': name},
    {'show': date}, each with a 'text'. 'more' points at a ranking page."""

    def add(self, kind, label, *parts, more=None):
        self.append({'kind': kind, 'label': label, 'parts': list(parts), 'more': more})


def _join(items, sep=', '):
    """Interleave a list of part-lists with separators."""
    out = []
    for i, item in enumerate(items):
        if i:
            out.append(sep)
        out.extend(item if isinstance(item, list) else [item])
    return out


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

        self.artist_links = defaultdict(dict)  # artist_id -> {source: url}; see enrich_wikidata.py
        if con.execute("select 1 from sqlite_master where name='artist_link'").fetchone():
            for r in con.execute("select artist_id, source, url from artist_link"):
                self.artist_links[r['artist_id']][r['source']] = r['url']

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

        self.family_of = genre_families.assign(self.artist_genres)
        self.mix = {pid: self._family_mix(pid) for pid in self.show_tracks}

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

    def _family_mix(self, pid):
        """Share of a show's runtime per genre family (each track split evenly across the
        families of its artists' genres), plus the tracks behind each family."""
        total = sum(self.tracks[t]['duration_ms'] or 0 for t in self.show_tracks[pid]) or 1
        shares, tracks = Counter(), defaultdict(list)
        for t in self.show_tracks[pid]:
            ms = self.tracks[t]['duration_ms'] or 0
            fams = {self.family_of[g] for a in self.track_artists[t] for g in self.artist_genres[a]}
            for f in fams or {None}:  # None = no genre data
                shares[f] += ms / len(fams or {None}) / total
                tracks[f].append(self.tracks[t]['name'])
        return shares, tracks

    def show_mix(self, playlist_id):
        """This show's family mix and the average mix of the shows around it."""
        ordered = sorted(self.show_tracks, key=self.show_dates.get)
        i = ordered.index(playlist_id)
        lo = max(0, min(i - NOVELTY_WINDOW, len(ordered) - 2 * NOVELTY_WINDOW - 1))
        peers = ordered[lo:lo + 2 * NOVELTY_WINDOW + 1]
        usual = Counter()
        for p in peers:
            usual.update({f: v / len(peers) for f, v in self.mix[p][0].items()})
        shares, tracks = self.mix[playlist_id]
        order = genre_families.FAMILY_NAMES + [None]

        def segments(values, with_tracks):
            return [{'family': f, 'share': values[f], 'color': genre_families.COLORS.get(f, ('#ddd', '#222'))[0],
                     'tracks': tracks[f] if with_tracks else []}
                    for f in order if values.get(f, 0) > 0.001]
        return {'show': segments(shares, True), 'usual': segments(usual, False)}

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

    def _artist_part(self, aid):
        return {'artist': self.artists[aid]['spotify_id'], 'text': self.artists[aid]['name']}

    def show_factoids(self, playlist_id):
        d = self.show_dates[playlist_id]
        tids = self.show_tracks[playlist_id]
        facts = Facts()
        if not tids:
            return facts

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
            facts.add('theme', 'Album session', f"{first['album']} by ",
                      *_join([self._artist_part(a) for a in self.track_artists[tids[run_start]]]),
                      f": {best_run} tracks in a row, starting at #{run_start + 1}")

        # Theme night: one artist dominating (not just because of an album session)
        for aid, n in Counter(aids).most_common(1):
            if n >= THEME_NIGHT_MIN_TRACKS and not album_session:
                facts.add('theme', 'Theme night?', self._artist_part(aid), f" accounts for {n} of {len(tids)} tracks")

        # Repeats of a recording
        repeats = []
        for t in tids:
            dates = self.recording_dates[self.recording_key(t)]
            if len(dates) > 1 and dates[0] < d:
                repeats.append((dates.index(d) + 1, self.tracks[t]['name'], self.recording_key(t)))
        if repeats:
            repeats.sort(reverse=True)
            parts = _join([f"“{name}” ({_ordinal(k)} time)" for k, name, _ in repeats[:3]])
            if len(repeats) > 3:
                parts.append(f" and {len(repeats) - 3} more")
            facts.add('repeat', 'Heard it before', *parts,
                      more={'ranking': 'tracks', 'anchor': _slug(repeats[0][2]), 'text': 'most repeated'})

        # Long-lost artists
        gaps = []
        for aid in distinct:
            a_dates = self.artist_dates[aid]
            i = a_dates.index(d)
            if i > 0 and (d - a_dates[i - 1]).days >= LONG_ABSENCE_DAYS:
                gaps.append(((d - a_dates[i - 1]).days, aid))
        if gaps:
            days, aid = max(gaps)
            facts.add('return', 'Welcome back', self._artist_part(aid), f", first time in {_years_months(days)}",
                      more={'ranking': 'returns', 'anchor': f"{self.artists[aid]['spotify_id']}-{d}",
                            'text': 'longest absences'})

        # Regulars, with their running tally, and first sightings of future regulars
        regulars, firsts = [], []
        for aid in distinct:
            a_dates = self.artist_dates[aid]
            if len(a_dates) >= REGULAR_MIN_SHOWS:
                k = a_dates.index(d) + 1
                (firsts if k == 1 else regulars).append((k, len(a_dates), aid))
        if regulars:
            regulars.sort(reverse=True)
            facts.add('regular', 'Regulars',
                      *_join([[self._artist_part(a), f" ({_ordinal(k)} show)"] for k, _, a in regulars[:4]]),
                      more={'ranking': 'artists', 'anchor': self.artists[regulars[0][2]]['spotify_id'],
                            'text': 'most played artists'})
        if firsts:
            firsts.sort(key=lambda x: -x[1])
            facts.add('regular', 'First sighting',
                      *_join([[self._artist_part(a), f" (went on to {total} shows)"] for _, total, a in firsts[:3]]),
                      more={'ranking': 'artists', 'anchor': self.artists[firsts[0][2]]['spotify_id'],
                            'text': 'most played artists'})

        # Label concentration
        labels = Counter(self.tracks[t]['label'] for t in tids if self.tracks[t]['label'])
        for label, n in labels.most_common(1):
            if n >= 3:
                facts.add('label', 'Label of the night', f"{label} ({n} tracks)",
                          more={'ranking': 'labels', 'anchor': _slug(label), 'text': 'top labels'})

        # Obscure-to-famous range
        known = [a for a in distinct if self.artists[a]['followers'] is not None]
        if len(known) > 1:
            lo = min(known, key=lambda a: self.artists[a]['followers'])
            hi = max(known, key=lambda a: self.artists[a]['followers'])
            facts.add('range', 'Range', "from ", self._artist_part(lo),
                      f" ({_compact(self.artists[lo]['followers'])} followers) to ", self._artist_part(hi),
                      f" ({_compact(self.artists[hi]['followers'])})")

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
        facts = Facts()
        spotify_id = self.artists[artist_id]['spotify_id']

        n = len(pids)
        rank = self.artist_rank[artist_id]
        if n >= 3:
            facts.add('regular', 'Standing', f"#{rank} most-played artist" if rank <= 50 else f"played at {n} shows",
                      more={'ranking': 'artists', 'anchor': spotify_id, 'text': 'see ranking'})
        if len(dates) > 1:
            gaps = [((b - a).days, a, b) for a, b in zip(dates, dates[1:])]
            days, a, b = max(gaps)
            if days >= 180:
                facts.add('return', 'Longest absence', f"{_years_months(days)}, between ", {'show': a, 'text': str(a)},
                          " and ", {'show': b, 'text': str(b)},
                          more={'ranking': 'returns', 'anchor': f"{spotify_id}-{b}", 'text': 'longest absences'}
                          if days >= 365 else None)

        recordings = Counter(self.recording_key(t) for pid in pids for t in self.show_tracks[pid]
                             if artist_id in self.track_artists[t])
        top_key, top_n = recordings.most_common(1)[0]
        if top_n > 1:
            name = next(self.tracks[t]['name'] for t in self.tracks if self.recording_key(t) == top_key)
            facts.add('repeat', 'Most played', f"“{name}” ({top_n} times)",
                      more={'ranking': 'tracks', 'anchor': _slug(top_key), 'text': 'most repeated'})

        collaborators = Counter(a['artist_id'] for s in shows for t in s['tracks'] for a in t['others'])
        if collaborators:
            facts.add('stat', 'Credited with', *_join([self._artist_part(a) for a, _ in collaborators.most_common(5)]))

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
                facts.add('genre', 'Often shares a show with',
                          *_join([[self._artist_part(a), f" ({k}×)"] for _, a, k in scored[:4]]))

        return {'shows': shows, 'n_shows': n, 'n_plays': sum(len(s['tracks']) for s in shows),
                'first': dates[0], 'last': dates[-1], 'facts': facts,
                'genres': sorted(self.artist_genres.get(artist_id, ())),
                'links': [(label, self.artist_links[artist_id][source]) for source, label in LINK_LABELS
                          if source in self.artist_links.get(artist_id, {})]}

    def genre_profile(self, name):
        aids = self.genre_artists.get(name)
        if not aids:
            return None
        artists = []
        for a in aids:
            pids = sorted(self.artist_shows.get(a, ()), key=self.show_dates.get)
            if pids:
                dates = [self.show_dates[p] for p in pids]
                plays = sum(1 for p in pids for t in self.show_tracks[p] if a in self.track_artists[t])
                artists.append({'anchor': self.artists[a]['spotify_id'], 'label': [self._artist_part(a)],
                                'marks': dates, 'first': dates[0], 'shows': len(pids), 'plays': plays,
                                'value': plays,
                                'tip': f"{self.artists[a]['name']}: {plays} play{'s' if plays != 1 else ''} "
                                       f"at {len(pids)} show{'s' if len(pids) != 1 else ''}, first {dates[0]}"})
        artists.sort(key=lambda x: (-x['plays'], x['first']))

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


    def ranking(self, kind, highlight=None, limit=100):
        """Return a ranking for a visual page: {'title', 'blurb', 'unit', 'rows'} where
        each row has 'anchor', 'rank', 'label' (parts), 'value', 'tip', and either
        'marks' (dates to tick) or 'span' (a date range). Rows past `limit` are
        dropped, except the highlighted one."""
        builder = getattr(self, f"_rank_{kind}", None)
        if not builder:
            return None
        table = builder()
        rows = table['rows']
        kept = rows[:limit]
        for r in rows:
            if highlight in r.get('also', ()):
                r['anchor'] = highlight
        extra = [r for r in rows[limit:] if r['anchor'] == highlight]
        table.update(rows=kept, extra=extra, total=len(rows))
        return table

    def _dated_rows(self, items, anchor, label, tip):
        """items: list of (key, sorted dates) ordered best-first."""
        ranks = _competition_ranks([len(d) for _, d in items])
        return [{'anchor': anchor(k), 'rank': r, 'label': label(k), 'value': len(dates),
                 'marks': dates, 'tip': tip(k, dates)} for r, (k, dates) in zip(ranks, items)]

    def _rank_artists(self):
        items = sorted(((a, sorted(self.show_dates[p] for p in pids)) for a, pids in self.artist_shows.items()
                        if len(pids) >= 3), key=lambda kv: (-len(kv[1]), self.artists[kv[0]]['name']))
        rows = self._dated_rows(items, lambda a: self.artists[a]['spotify_id'], lambda a: [self._artist_part(a)],
                                lambda a, d: f"{self.artists[a]['name']}: {len(d)} shows, {d[0]} to {d[-1]}")
        return {'title': 'Most played artists', 'unit': 'shows', 'rows': rows,
                'blurb': 'Artists by the number of shows they appeared in; each tick is a show.'}

    def _rank_tracks(self):
        names = {}
        for t in self.tracks:
            names.setdefault(self.recording_key(t), t)
        items = sorted(((k, v) for k, v in self.recording_dates.items() if len(v) > 1),
                       key=lambda kv: (-len(kv[1]), self.tracks[names[kv[0]]]['name']))

        def label(k):
            t = names[k]
            return [f"“{self.tracks[t]['name']}” · "] + _join([self._artist_part(a) for a in self.track_artists[t]])
        rows = self._dated_rows(items, _slug, label,
                                lambda k, d: f"{self.tracks[names[k]]['name']}: " + ', '.join(map(str, d)))
        return {'title': 'Most repeated tracks', 'unit': 'plays', 'rows': rows,
                'blurb': 'Recordings played at more than one show (the same recording on different releases '
                         'counts together); each tick is a play.'}

    def _rank_labels(self):
        dates, artists = defaultdict(list), defaultdict(Counter)
        for pid, tids in self.show_tracks.items():
            for t in tids:
                label = self.tracks[t]['label']
                if label:
                    dates[label].append(self.show_dates[pid])
                    artists[label].update(self.track_artists[t])
        items = sorted(((l, sorted(d)) for l, d in dates.items() if len(d) >= 5), key=lambda kv: (-len(kv[1]), kv[0]))
        rows = self._dated_rows(items, _slug, lambda l: [l], lambda l, d: f"{l}: {len(d)} plays. Most played: " +
                                ', '.join(self.artists[a]['name'] for a, _ in artists[l].most_common(3)))
        return {'title': 'Top labels', 'unit': 'plays', 'rows': rows,
                'blurb': "Labels as Spotify reports them (reissue imprints like Rhino and Legacy aren't merged "
                         "with their parents); each tick is a play."}

    def _rank_returns(self):
        # co-credited artists returning together on the same track share one row
        groups = defaultdict(list)
        for aid, dates in self.artist_dates.items():
            for a, b in zip(dates, dates[1:]):
                if (b - a).days >= 365:
                    track = next(t for t in self.show_tracks[self.pid_by_date[b]] if aid in self.track_artists[t])
                    groups[(a, b, track)].append(aid)
        gaps = sorted(groups.items(), key=lambda g: (-(g[0][1] - g[0][0]).days, g[0][1]))
        ranks = _competition_ranks([(b - a).days for (a, b, _), _ in gaps])
        rows = []
        for r, ((a, b, track), aids) in zip(ranks, gaps):
            aids.sort(key=lambda x: self.track_artists[track].index(x))
            names = ', '.join(self.artists[x]['name'] for x in aids)
            rows.append({'anchor': f"{self.artists[aids[0]]['spotify_id']}-{b}", 'rank': r,
                         'label': _join([self._artist_part(x) for x in aids]),
                         'value': _years_months((b - a).days), 'span': (a, b), 'marks': self.artist_dates[aids[0]],
                         'tip': f"{names}: last played {a}, back {b} with “{self.tracks[track]['name']}”",
                         'also': [f"{self.artists[x]['spotify_id']}-{b}" for x in aids[1:]]})
        return {'title': 'Longest absences', 'unit': '', 'rows': rows,
                'blurb': "Artists who came back after a year or more away. The bar is the gap; "
                         "ticks are the artist's other shows."}

    def genre_tree(self):
        """Plays per genre per year, grouped into families, for the genre map. Each play
        is split evenly across the genres of its artists, so totals match real plays."""
        family_of = genre_families.assign(self.artist_genres)
        years = sorted(self.plays_by_year)
        plays = defaultdict(lambda: defaultdict(float))  # genre -> year -> plays
        untagged = Counter()
        for pid, tids in self.show_tracks.items():
            y = self.show_dates[pid].year
            for t in tids:
                gs = set()
                for a in self.track_artists[t]:
                    gs |= self.artist_genres[a]
                if not gs:
                    untagged[y] += 1
                for g in gs:
                    plays[g][y] += 1 / len(gs)
        families = {f: [] for f in genre_families.FAMILY_NAMES}
        for g, by_year in plays.items():
            families[family_of[g]].append({'name': g, 'plays': [round(by_year.get(y, 0), 3) for y in years]})
        return {'years': years, 'untagged': [untagged[y] for y in years],
                'totals': [self.plays_by_year[y] for y in years],
                'families': [{'name': f, 'genres': gs} for f, gs in families.items() if gs]}

    def novelty_series(self):
        """Every show's novelty and its neighborhood median, in date order, for the novelty chart."""
        out = []
        for pid in sorted(self.novelty, key=self.show_dates.get):
            peers = self.novelty_peers[pid]
            out.append({'date': self.show_dates[pid], 'novelty': self.novelty[pid],
                        'median': peers[len(peers) // 2], 'rank': self.novelty_rank[pid]})
        return out


RANKINGS = ['artists', 'tracks', 'returns', 'labels']  # novelty has its own chart page


@lru_cache(maxsize=1)
def _history_for(db_path, mtime):
    return History(db_path)


def get_history(db_path=DB_PATH):
    return _history_for(db_path, os.path.getmtime(db_path))
