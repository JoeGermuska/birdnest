"""Computed "factoids" about shows and the tracks in them.

Everything here is derived from the whole play history, so we load the
(small) history once into memory and answer questions from that. The
database only changes on deploy, so the cache is keyed on the file's mtime.
"""
import hashlib
import html
import json
import random
import math
import re
import os
import pickle
import sqlite3

import genre_families
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache

DB_PATH = 'birdnest.db'

THEME_NIGHT_MIN_TRACKS = 4
LONG_ABSENCE_DAYS = 365 * 2
REGULAR_MIN_SHOWS = 10
NOVELTY_WINDOW = 20  # shows on each side to compare novelty against
REPEAT_PENALTY = 0.5  # novelty: new artists' share, less this much of the share of songs heard on earlier shows
GENRE_MIN_TRACKS = 20
LINK_LABELS = [('wikipedia', 'Wikipedia'), ('musicbrainz', 'MusicBrainz'), ('discogs', 'Discogs'),
               ('bandcamp', 'Bandcamp'), ('allmusic', 'AllMusic'), ('website', 'Website'), ('wikidata', 'Wikidata')]
ALBUM_LINK_LABELS = [('wikipedia', 'Wikipedia'), ('musicbrainz', 'MusicBrainz'), ('discogs', 'Discogs'),
                     ('allmusic', 'AllMusic'), ('bandcamp', 'Bandcamp'), ('apple', 'Apple Music'), ('wikidata', 'Wikidata')]  # ignore very rare genres, whose lift is noisy


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


SOCIALS = [('instagram.com', 'Instagram'), ('bsky.app', 'Bluesky'), ('tiktok.com', 'TikTok'), ('youtube.com', 'YouTube'),
           ('soundcloud.com', 'SoundCloud'), ('facebook.com', 'Facebook'), ('threads.net', 'Threads'),
           ('threads.com', 'Threads'), ('twitter.com', 'Twitter'), ('x.com', 'X')]


def social_service(url):
    """The service a social link is on, by its host (see SOCIALS), or None for ones we don't show."""
    host = re.sub(r'^https?://(www\.|m\.)?', '', url or '').split('/')[0].lower()
    return next((name for domain, name in SOCIALS if host == domain or host.endswith('.' + domain)), None)


def flag(country):
    """A two-letter country code as its flag emoji."""
    return ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in country) if country and len(country) == 2 and country.isalpha() else ''


def mix_summary(shares, usual, tracks, lean):
    """A mix bar's segments (each with its usual share, for the tooltip) and the families that stand out:
    heavy (at least `points` more of the runtime than usual, and `ratio` times as much) and light (the reverse).
    lean: (points, ratio). Untagged runtime never counts as standing out."""
    points, ratio = lean
    order = genre_families.FAMILY_NAMES + [None]
    segments = [{'family': f, 'share': shares[f], 'usual': usual.get(f, 0),
                 'color': genre_families.COLORS.get(f, ('#ddd', '#222'))[0], 'tracks': (tracks or {}).get(f, [])}
                for f in order if shares.get(f, 0) > 0.001]
    diff = lambda f: shares.get(f, 0) - usual.get(f, 0)
    heavy = sorted((f for f in order if f and diff(f) >= points and shares.get(f, 0) >= usual.get(f, 0) * ratio),
                   key=diff, reverse=True)
    light = sorted((f for f in order if f and -diff(f) >= points and shares.get(f, 0) * ratio <= usual.get(f, 0)),
                   key=diff)
    return {'show': segments, 'heavy': [f.lower() for f in heavy], 'light': [f.lower() for f in light]}


def _label_key(name):
    """A label name for matching: case, punctuation and words like "Records" don't count."""
    name = re.sub(r'\b(records?|recordings?|music|label|ltd|llc|inc|co|company)\b', '', (name or '').lower())
    return re.sub(r'\W', '', name)


def best_release(dates):
    """(date, source) for when a recording first came out, from [(recording's MusicBrainz date, source), (album's
    MusicBrainz date, source), (Spotify's album date, source)], each YYYY[-MM[-DD]] or None. The earliest date wins
    (a bare year counts as that year's start), so a new single Spotify has before its album still counts on the
    night it was played. The album's date counts only when the recording's own is unknown: an album's first
    release before the recording's means the track wasn't on it then (a deluxe edition's bonus track, say).
    Spotify's placeholder dates (1900, 0000) don't count."""
    ok = lambda d: d and d[:4].isdigit() and int(d[:4]) > 1900
    rec, alb, sp = dates
    usable = [(d, s) for d, s in ([rec, sp] if ok(rec[0]) else [rec, sp, alb]) if ok(d)]
    return min(usable, key=lambda u: u[0]) if usable else None


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
        self.shows = {}  # playlist_id -> description, spotify_url
        for r in con.execute("select playlist_id, date, description, spotify_url, images from playlist "
                             "where date is not null"):
            self.show_dates[r['playlist_id']] = date.fromisoformat(r['date'])
            images = json.loads(r['images']) if r['images'] else []
            self.shows[r['playlist_id']] = {'date': self.show_dates[r['playlist_id']], 'description': r['description'],
                                            'spotify_url': r['spotify_url'],
                                            'image_url': images[0]['url'] if images else None}

        self.artists = {r['artist_id']: dict(r) for r in con.execute(
            "select artist_id, name, spotify_id, followers, popularity from artist")}

        self.track_artists = defaultdict(list)
        for r in con.execute("select track_id, artist_id from track_artist"):
            self.track_artists[r['track_id']].append(r['artist_id'])

        self.tracks = {r['track_id']: dict(r) for r in con.execute(
            """select t.track_id, t.name, t.duration_ms, t.isrc_id, t.album_id, t.spotify_url,
                      al.name album, al.label
               from track t left join album al using(album_id)""")}

        # genres by *name* -- the genre table has duplicate rows per name
        self.artist_genres = defaultdict(set)
        for r in con.execute(
                "select ag.artist_id, g.name from artist_genre ag join genre g using(genre_id)"):
            self.artist_genres[r['artist_id']].add(r['name'])

        # follower counts over time (see refresh_artists.py), oldest first
        self.snapshots = defaultdict(list)
        if con.execute("select 1 from sqlite_master where name='artist_snapshot'").fetchone():
            for r in con.execute("select artist_id, fetched_at, followers from artist_snapshot "
                                 "where followers is not null order by fetched_at"):
                self.snapshots[r['artist_id']].append((date.fromisoformat(r['fetched_at']), r['followers']))

        self.artist_links = defaultdict(dict)  # artist_id -> {source: url}; see enrich_wikidata.py
        if con.execute("select 1 from sqlite_master where name='artist_link'").fetchone():
            for r in con.execute("select artist_id, source, url from artist_link"):
                self.artist_links[r['artist_id']][r['source']] = r['url']

        # what the older shows' playlist images are, from before that went in the description (load_captions.py)
        self.cover_captions = {}
        if con.execute("select 1 from sqlite_master where name='cover_caption'").fetchone():
            self.cover_captions = {r['playlist_id']: r['caption'] for r in con.execute("select playlist_id, caption from cover_caption")}

        # who was in the room, in join order (see load_djs.py)
        self.djs = {}  # dj_id -> {'dj_id', 'name', 'slug'}
        self.show_djs = defaultdict(list)  # playlist_id -> [(dj_id, note)]
        if con.execute("select 1 from sqlite_master where name='show_dj'").fetchone():
            self.djs = {r['dj_id']: dict(r) for r in con.execute("select dj_id, name, slug from dj")}
            for r in con.execute("select playlist_id, dj_id, note from show_dj order by playlist_id, sequence"):
                if r['playlist_id'] in self.show_dates:
                    self.show_djs[r['playlist_id']].append((r['dj_id'], r['note']))

        self.show_tracks = defaultdict(list)  # playlist_id -> [track_id] in order
        for r in con.execute("select playlist_id, track_id from playlist_track order by playlist_id, sequence"):
            if r['playlist_id'] in self.show_dates:
                self.show_tracks[r['playlist_id']].append(r['track_id'])

        # albums, with what MusicBrainz knows (enrich_mb_albums.py) when it's been looked up
        has = lambda t: con.execute("select 1 from sqlite_master where name = ?", (t,)).fetchone()
        cols = {r[1] for r in con.execute("pragma table_info(album)")}
        extra = [c for c in ('release_date', 'release_date_precision', 'album_type', 'total_tracks', 'copyright_p') if c in cols]
        mb = has('mb_album')
        self.albums = {}
        for r in con.execute(f"""select al.album_id, al.spotify_id, al.spotify_url, al.name, al.label, al.images
                                 {''.join(', al.' + c for c in extra)}
                                 {', m.release_group_mbid, m.first_release_date original_date, m.rg_title' if mb else ''}
                                 from album al {'left join mb_album m using(album_id)' if mb else ''}"""):
            a = dict(r)
            images = json.loads(a.pop('images') or '[]')
            a['image_url'] = next((i['url'] for i in sorted(images, key=lambda i: i.get('width') or 0) if (i.get('width') or 0) >= 250),
                                  images[0]['url'] if images else None)
            self.albums[a['album_id']] = a
        self.album_artists = defaultdict(list)
        for r in con.execute("select album_id, artist_id from album_artist"):
            if r['artist_id'] not in self.album_artists[r['album_id']]:
                self.album_artists[r['album_id']].append(r['artist_id'])
        self.album_links = defaultdict(dict)
        self.album_mb_labels = defaultdict(list)
        if has('album_link'):
            for r in con.execute("select album_id, source, url from album_link"):
                self.album_links[r['album_id']][r['source']] = r['url']
        # when each recording first came out: the earliest believable date among MusicBrainz's for the recording,
        # MusicBrainz's for the album (its release group's first release), and Spotify's album date (see best_release)
        self.track_released = {}  # track_id -> (YYYY[-MM[-DD]], 'musicbrainz' | 'musicbrainz-album' | 'spotify')
        rec = 'm.first_release_date' if has('mb_recording') else 'null'
        alb = 'ma.first_release_date' if has('mb_album') else 'null'
        for r in con.execute(f"""select t.track_id, {rec} rec, {alb} alb, al.{'release_date' if 'release_date' in cols else 'name'} sp
                from track t left join album al using(album_id)
                {'left join mb_recording m using(track_id)' if has('mb_recording') else ''}
                {'left join mb_album ma on ma.album_id = t.album_id' if has('mb_album') else ''}"""):
            best = best_release([(r['rec'], 'musicbrainz'), (r['alb'], 'musicbrainz-album'),
                                 (r['sp'] if 'release_date' in cols else None, 'spotify')])
            if best:
                self.track_released[r['track_id']] = best
        # Labels (enrich_mb_labels.py): what MusicBrainz knows about each, and each album's original label
        self.labels = {}  # mbid -> {'mbid', 'name', 'type', 'country', 'area', 'begin', 'end', 'links', 'rels'}
        if has('mb_label'):
            for r in con.execute("select mbid, name, type, country, area, begin_date, end_date from mb_label"):
                self.labels[r['mbid']] = {'mbid': r['mbid'], 'name': r['name'], 'type': r['type'], 'country': r['country'],
                                          'area': r['area'], 'begin': r['begin_date'], 'end': r['end_date'],
                                          'links': {}, 'rels': []}
            for r in con.execute("select mbid, type, url from mb_label_url"):
                if r['mbid'] in self.labels:
                    self.labels[r['mbid']]['links'].setdefault(r['type'], r['url'])
            for r in con.execute("select mbid, type, direction, target_mbid, target_name, end_date from mb_label_rel"):
                if r['mbid'] in self.labels:
                    self.labels[r['mbid']]['rels'].append(dict(r))
        self.album_original = {}  # album_id -> {'mbid', 'name', 'catno', 'released'}
        if has('mb_album_original'):
            for r in con.execute("select album_id, label_mbid, label_name, catalog_number, released from mb_album_original "
                                 "where label_mbid is not null"):
                self.album_original[r['album_id']] = {'mbid': r['label_mbid'], 'name': r['label_name'],
                                                      'catno': r['catalog_number'], 'released': r['released']}
        # Spotify's label name -> a MusicBrainz label, when the albums we matched agree on it (for albums MusicBrainz
        # doesn't know, whose Spotify label is all we have)
        spotify_names = defaultdict(Counter)
        # about each artist, for their page: MusicBrainz's type, home and years, Wikidata's description, socials
        self.artist_about = defaultdict(dict)
        if has('mb_artist'):
            for r in con.execute("select artist_id, type, country, area, begin_date, end_date from mb_artist where mbid is not null"):
                self.artist_about[r['artist_id']].update({k: r[k] for k in ('type', 'country', 'area', 'begin_date', 'end_date')})
        if has('artist_description'):
            for r in con.execute("select artist_id, description from artist_description where description is not null"):
                self.artist_about[r['artist_id']]['description'] = r['description']
        if has('mb_artist_url'):
            for r in con.execute("select artist_id, url from mb_artist_url where type in ('social network', 'youtube', 'soundcloud')"):
                service = social_service(r['url'])
                if service:
                    self.artist_about[r['artist_id']].setdefault('socials', {}).setdefault(service, r['url'])

        if has('mb_album_label'):
            for r in con.execute("""select al.label, l.label_mbid from album al join mb_album_label l using(album_id)
                                      where al.label is not null and l.label_mbid is not null"""):
                spotify_names[_label_key(r['label'])][r['label_mbid']] += 1
        self.label_by_spotify = {}
        for key, c in spotify_names.items():
            mbid, n = c.most_common(1)[0]
            if n / sum(c.values()) >= 0.8 and mbid in self.labels and self.labels[mbid]['type'] not in NOT_RELEASING_TYPES:
                self.label_by_spotify[key] = mbid
        if has('mb_album_label'):
            for r in con.execute("select album_id, name, catalog_number from mb_album_label"):
                if (r['name'], r['catalog_number']) not in self.album_mb_labels[r['album_id']]:
                    self.album_mb_labels[r['album_id']].append((r['name'], r['catalog_number']))
        con.close()

        # Recordings can appear under several Spotify track ids (single vs. album,
        # compilations); ISRC groups them so repeat counts are honest.

        self.recording_dates = defaultdict(list)
        self.artist_dates = defaultdict(list)
        genre_track_counts = Counter()
        total_track_plays = 0
        for pid in sorted(self.show_tracks, key=self.show_dates.get):
            d = self.show_dates[pid]
            for tid in self.show_tracks[pid]:
                self.recording_dates[self.recording_key(tid)].append(d)
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

        # Albums: Spotify sometimes lists one album under several ids (regional or re-uploaded copies), and
        # MusicBrainz's release group gathers editions (remasters, deluxe); each group counts as one album,
        # under its most played id.
        self.album_plays = Counter(self.tracks[t]['album_id'] for tids in self.show_tracks.values() for t in tids
                                   if self.tracks[t]['album_id'] in self.albums)
        parent = {a: a for a in self.albums}
        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a
        firsts = {}
        for aid, a in self.albums.items():
            keys = [('name', (a['name'] or '').strip().lower(), frozenset(self.album_artists[aid]), a.get('album_type') == 'single')]
            if a.get('release_group_mbid'):
                keys.append(('rg', a['release_group_mbid']))
            for k in keys:
                if k in firsts:
                    parent[find(aid)] = find(firsts[k])
                else:
                    firsts[k] = aid
        members = defaultdict(list)
        for aid in self.albums:
            members[find(aid)].append(aid)
        self.album_group, self.album_members = {}, {}
        for ids in members.values():
            canon = max(ids, key=lambda a: (self.album_plays[a], -a))
            self.album_members[canon] = sorted(ids, key=lambda a: (-self.album_plays[a], a))
            for a in ids:
                self.album_group[a] = canon
        self.album_by_spotify = {a['spotify_id']: self.album_group[aid] for aid, a in self.albums.items() if a['spotify_id']}
        self.album_shows = defaultdict(set)
        for pid, tids in self.show_tracks.items():
            for t in tids:
                if self.tracks[t]['album_id'] in self.album_group:
                    self.album_shows[self.album_group[self.tracks[t]['album_id']]].add(pid)
        counts = sorted((len(v) for a, v in self.album_shows.items() if self.albums[a].get('album_type') != 'single'), reverse=True)
        first_at = {}
        for i, n in enumerate(counts):
            first_at.setdefault(n, i + 1)
        # rank albums (singles aside) by number of shows; ties share a rank
        self.album_rank = {a: first_at[len(v)] for a, v in self.album_shows.items() if self.albums[a].get('album_type') != 'single'}

        # Labels: each album's label is its original one (MusicBrainz), else Spotify's when that maps to a label that
        # releases music (not a reissue arm, distributor or holding company); else none
        self.album_label = {}  # album_id -> label mbid
        self.album_label_how = {}  # album_id -> 'original' | 'spotify'
        for aid, a in self.albums.items():
            group = self.album_members.get(self.album_group.get(aid), [aid])
            orig = next((self.album_original[m] for m in [aid] + group if m in self.album_original), None)
            if orig and orig['mbid'] in self.labels:
                self.album_label[aid], self.album_label_how[aid] = orig['mbid'], 'original'
            elif a.get('label') and _label_key(a['label']) in self.label_by_spotify:
                self.album_label[aid], self.album_label_how[aid] = self.label_by_spotify[_label_key(a['label'])], 'spotify'
        self.label_shows, self.label_plays = defaultdict(set), Counter()
        for pid, tids in self.show_tracks.items():
            for t in tids:
                mbid = self.album_label.get(self.tracks[t]['album_id'])
                if mbid:
                    self.label_shows[mbid].add(pid)
                    self.label_plays[mbid] += 1
        order = sorted(self.label_plays.values(), reverse=True)
        first_at = {}
        for i, n in enumerate(order):
            first_at.setdefault(n, i + 1)
        self.label_rank = {m: first_at[n] for m, n in self.label_plays.items()}

        # Song age: each play's release year, when we know it. Spotify's placeholder dates (1900, 0000) and dates
        # after the show itself don't count.
        self.play_years = {}  # playlist_id -> [(year, track_id)] for the plays with a usable date
        for pid, tids in self.show_tracks.items():
            d = self.show_dates[pid]
            self.play_years[pid] = [(y, t) for t in tids for y in [self.release_year(t, d)] if y]

        self.dj_by_slug = {dj['slug']: i for i, dj in self.djs.items()}
        self.dj_shows = defaultdict(set)
        for pid, room in self.show_djs.items():
            for i, _ in room:
                self.dj_shows[i].add(pid)
        self.dj_first = {i: min(self.show_dates[p] for p in pids) for i, pids in self.dj_shows.items()}

        self.family_of = genre_families.assign(self.artist_genres)
        self.mix = {pid: self._family_mix(pid) for pid in self.show_tracks}

        # Novelty: share of a show's artists who had never been played before, less a little (REPEAT_PENALTY)
        # for songs heard on earlier shows. It trends down as the pool of already-played artists grows, so rank
        # each show against a sliding window of neighboring shows.
        self.new_artists = {pid: self._debut_share(pid) for pid in self.show_tracks}
        self.repeats = self._repeat_shares()
        self.novelty = {pid: max(0.0, self.new_artists[pid] - REPEAT_PENALTY * self.repeats[pid]) for pid in self.show_tracks}
        ordered = sorted(self.novelty, key=self.show_dates.get)
        self.novelty_peers, self.novelty_rank = {}, {}
        for i, pid in enumerate(ordered):
            lo = max(0, min(i - NOVELTY_WINDOW, len(ordered) - 2 * NOVELTY_WINDOW - 1))
            peers = [self.novelty[p] for p in ordered[lo:lo + 2 * NOVELTY_WINDOW + 1]]
            share = self.novelty[pid]
            below = sum(1 for x in peers if x < share) + 0.5 * (sum(1 for x in peers if x == share) - 1)
            self.novelty_peers[pid] = sorted(peers)
            self.novelty_rank[pid] = below / (len(peers) - 1) if len(peers) > 1 else 0.5

    def recording_key(self, track_id):
        # Recordings can appear under several Spotify track ids (single vs. album,
        # compilations); ISRC groups them so repeat counts are honest.
        return self.tracks[track_id]['isrc_id'] or f"t{track_id}"

    def _repeat_shares(self):
        """Per show, the share of its tracks whose recording was played on an earlier show."""
        first = {}
        for pid in sorted(self.show_tracks, key=self.show_dates.get):
            for t in self.show_tracks[pid]:
                first.setdefault(self.recording_key(t), self.show_dates[pid])
        return {pid: sum(1 for t in tids if first[self.recording_key(t)] < self.show_dates[pid]) / len(tids) if tids else 0
                for pid, tids in self.show_tracks.items()}

    def _debut_share(self, pid):
        d = self.show_dates[pid]
        aids = {a for t in self.show_tracks[pid] for a in self.track_artists[t]}
        if not aids:
            return 0
        return sum(1 for a in aids if self.artist_dates[a][0] == d) / len(aids)

    def show_rows(self, playlist_id):
        """Everything the show page's track table needs, without touching the ORM."""
        notes = self.track_notes(playlist_id)
        return [{**self.tracks[t], 'artists': [self.artists[a] for a in self.track_artists[t]], 'notes': notes[t],
                 'album_page': self._album_page(t), 'label_page': self.album_label.get(self.tracks[t]['album_id']),
                 'label_name': self.labels[self.album_label[self.tracks[t]['album_id']]]['name']
                 if self.tracks[t]['album_id'] in self.album_label else self.tracks[t]['label']}
                for t in self.show_tracks[playlist_id]]

    def _album_page(self, track_id):
        """The Spotify id an album page lives at, for a track's album (None if unknown)."""
        canon = self.album_group.get(self.tracks[track_id]['album_id'])
        return self.albums[canon]['spotify_id'] if canon is not None else None

    def neighbors(self, playlist_id):
        """(previous, next) show dates."""
        ordered = sorted(self.show_tracks, key=self.show_dates.get)
        i = ordered.index(playlist_id)
        return (self.show_dates[ordered[i - 1]] if i else None,
                self.show_dates[ordered[i + 1]] if i + 1 < len(ordered) else None)

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
        """This show's family mix, and what stands out against the shows around it (see mix_summary)."""
        ordered = sorted(self.show_tracks, key=self.show_dates.get)
        i = ordered.index(playlist_id)
        lo = max(0, min(i - NOVELTY_WINDOW, len(ordered) - 2 * NOVELTY_WINDOW - 1))
        peers = ordered[lo:lo + 2 * NOVELTY_WINDOW + 1]
        usual = Counter()
        for p in peers:
            usual.update({f: v / len(peers) for f, v in self.mix[p][0].items()})
        shares, tracks = self.mix[playlist_id]
        return mix_summary(shares, usual, tracks, SHOW_LEAN)

    def _dj_part(self, dj_id):
        return {'dj': self.djs[dj_id]['slug'], 'text': self.djs[dj_id]['name']}

    def show_room(self, playlist_id):
        """Who was in the room for a show, in the order they joined."""
        d = self.show_dates[playlist_id]
        return [{**self.djs[i], 'note': note, 'first_show': self.dj_first[i] == d}
                for i, note in self.show_djs.get(playlist_id, ())]

    def _over_represented(self, pids, universe, min_k=3, limit=8):
        """Keys of `universe` (key -> set of playlist_ids) that turn up in `pids` more than
        chance would suggest, as [(key, k)] best first."""
        n, total = len(pids), len(self.show_tracks)
        together = Counter(key for key, shows in universe.items() for p in shows if p in pids)
        scored = []
        for key, k in together.items():
            lift = k / (n * len(universe[key]) / total)
            if k >= min_k and lift > 1.5:
                scored.append((k * math.log(lift), key, k))
        scored.sort(reverse=True)
        return [(key, k) for _, key, k in scored[:limit]]

    def dj_profile(self, slug):
        dj_id = self.dj_by_slug.get(slug)
        if dj_id is None:
            return None
        pids = sorted(self.dj_shows[dj_id], key=self.show_dates.get)
        dates = [self.show_dates[p] for p in pids]
        n = len(pids)
        facts = Facts()

        ordered = sorted(self.show_tracks, key=self.show_dates.get)
        streak = best = 0
        best_end = None
        for p in ordered:
            streak = streak + 1 if p in self.dj_shows[dj_id] else 0
            if streak > best:
                best, best_end = streak, self.show_dates[p]
        if best >= 3:
            start = self.show_dates[ordered[ordered.index(self.pid_by_date[best_end]) - best + 1]]
            facts.add('regular', 'Longest run', f"{best} shows in a row, ", {'show': start, 'text': str(start)},
                      " to ", {'show': best_end, 'text': str(best_end)})
        if n > 1:
            gaps = [((b - a).days, a, b) for a, b in zip(dates, dates[1:])]
            days, a, b = max(gaps)
            if days >= 180:
                facts.add('return', 'Longest absence', f"{_years_months(days)}, between ", {'show': a, 'text': str(a)},
                          " and ", {'show': b, 'text': str(b)})
        opened = sum(1 for p in pids if self.show_djs[p][0][0] == dj_id)
        if opened and n > 1:
            facts.add('stat', 'First in the room', f"{opened} of {n} shows")

        crew = Counter(i for p in pids for i, _ in self.show_djs[p] if i != dj_id)
        regulars = [{**self.djs[i], 'shows': k, 'share': k / n} for i, k in crew.most_common()]

        # what this person's shows lean toward (we can't know who picked what)
        artists = self._over_represented(set(pids), self.artist_shows) if n >= 4 else []
        return {'dj': self.djs[dj_id], 'n_shows': n, 'share': n / len(self.show_tracks),
                'first': dates[0], 'last': dates[-1], 'facts': facts, 'regulars': regulars,
                'notes': [(self.show_dates[p], note) for p in pids for i, note in self.show_djs[p]
                          if i == dj_id and note],
                'artists': [{**self.artists[a], 'shows': k, 'of': len(self.artist_shows[a])} for a, k in artists],
                'shows': [{**self.shows[p], 'room': self.show_room(p)} for p in reversed(pids)]}

    def show_stats(self, playlist_id):
        """Plain playlist-level metadata."""
        tids = self.show_tracks[playlist_id]
        return {
            'tracks': len(tids),
            'runtime_min': sum(self.tracks[t]['duration_ms'] or 0 for t in tids) // 60000,
            'artists': len({a for t in tids for a in self.track_artists[t]}),
            'novelty': self.novelty.get(playlist_id, 0),
            'new_artists': self.new_artists.get(playlist_id, 0),
            'repeats': self.repeats.get(playlist_id, 0),
            'novelty_rank': self.novelty_rank.get(playlist_id, 0.5),
            'novelty_peers': self.novelty_peers.get(playlist_id, []),
        }

    def followers_at(self, aid, d):
        """Followers as of date d: the latest snapshot on or shortly after d (it's taken
        when the show is loaded), else the nearest earlier one, else the stored value."""
        snaps = self.snapshots.get(aid)
        if not snaps:
            return self.artists[aid]['followers']
        after = [f for when, f in snaps if d <= when <= d + timedelta(days=14)]
        if after:
            return after[0]
        before = [f for when, f in snaps if when <= d]
        return before[-1] if before else snaps[0][1]

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

        # Song age: the oldest song, and anything out less than two weeks
        plays = self.play_years.get(playlist_id, [])
        if plays:
            y, t = min(plays, key=lambda p: (self.track_released[p[1]][0], p[1]))
            if d.year - y >= 30:
                facts.add('stat', 'Oldest song', f"“{self.tracks[t]['name']}” by ",
                          *_join([self._artist_part(a) for a in self.track_artists[t]]), f" ({y})",
                          more={'ranking': 'age', 'anchor': 'oldest', 'text': 'song age'})
            fresh = [t for _, t in plays if len(self.track_released[t][0]) == 10
                     and 0 <= (d - date.fromisoformat(self.track_released[t][0])).days <= FRESH_DAYS]
            if fresh:
                names = _join([f"“{self.tracks[t]['name']}”" for t in fresh[:3]])
                facts.add('stat', 'Hot off the press', *names, " and more" if len(fresh) > 3 else "",
                          f": out less than two weeks", more={'ranking': 'age', 'anchor': 'fresh', 'text': 'song age'})

        # Label concentration
        labels = Counter(self.album_label[self.tracks[t]['album_id']] for t in tids if self.tracks[t]['album_id'] in self.album_label)
        for mbid, n in labels.most_common(1):
            if n >= 3:
                facts.add('label', 'Label of the night', self.label_part(mbid), f" ({n} tracks)",
                          more={'ranking': 'labels', 'anchor': mbid, 'text': 'top labels'})

        # Obscure-to-famous range, using follower counts from around the show
        followers = {a: self.followers_at(a, d) for a in distinct}
        known = [a for a in distinct if followers[a] is not None]
        if len(known) > 1:
            lo = min(known, key=followers.get)
            hi = max(known, key=followers.get)
            facts.add('range', 'Range', "from ", self._artist_part(lo),
                      f" ({_compact(followers[lo])} followers) to ", self._artist_part(hi),
                      f" ({_compact(followers[hi])})")

        return facts

    def timeline_x(self, d, width):
        """Horizontal position of date d on a timeline spanning the whole history."""
        span = (self.last_date - self.first_date).days or 1
        return round((d - self.first_date).days / span * width, 1)

    def year_x(self, year, width):
        return self.timeline_x(max(self.first_date, date(year, 1, 1)), width)

    def artist_header(self, artist_id):
        """What an artist page says about who they are: Wikidata's description, where they're from (with a flag),
        their years (left out when the description already gives them), and social links, in SOCIALS order."""
        a = self.artist_about.get(artist_id, {})
        desc = a.get('description')
        begin, end = (a.get('begin_date') or '')[:4], (a.get('end_date') or '')[:4]
        group = a.get('type') in ('Group', 'Orchestra', 'Choir')
        years = None
        if begin and not (desc and begin in desc):
            years = f"{begin}–{end}" if end else f"formed {begin}" if group else f"born {begin}"
        order = [name for _, name in SOCIALS]
        socials = sorted((a.get('socials') or {}).items(), key=lambda kv: order.index(kv[0]))
        return {'description': desc, 'home': a.get('area'), 'flag': flag(a.get('country')), 'years': years, 'socials': socials}

    def artist_profile(self, artist_id):
        pids = sorted(self.artist_shows.get(artist_id, ()), key=self.show_dates.get)
        if not pids:
            return None
        shows = []
        for pid in pids:
            items = [(i + 1, t) for i, t in enumerate(self.show_tracks[pid]) if artist_id in self.track_artists[t]]
            shows.append({'date': self.show_dates[pid], 'tracks': [
                {'position': pos, 'name': self.tracks[t]['name'], 'album': self.tracks[t]['album'],
                 'spotify_url': self.tracks[t]['spotify_url'],
                 'others': [self.artists[a] for a in self.track_artists[t] if a != artist_id]}
                for pos, t in items]})
        dates = [s['date'] for s in shows]
        # the same plays by recording, most recently played first
        by_rec = {}
        for pid in pids:
            for t in self.show_tracks[pid]:
                if artist_id in self.track_artists[t]:
                    r = by_rec.setdefault(self.recording_key(t), {
                        'name': self.tracks[t]['name'], 'album': self.tracks[t]['album'], 'album_page': self._album_page(t),
                        'spotify_url': self.tracks[t]['spotify_url'], 'dates': [],
                        'others': [self.artists[a] for a in self.track_artists[t] if a != artist_id]})
                    r['dates'].append(self.show_dates[pid])
        track_rows = sorted(by_rec.values(), key=lambda r: r['dates'][-1], reverse=True)
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

        if n >= 4 and self.dj_shows:
            room = self._over_represented(set(pids), self.dj_shows, limit=3)
            if room:
                facts.add('stat', 'Often in the room',
                          *_join([[self._dj_part(i), f" ({k} of {n})"] for i, k in room]))

        return {'shows': shows, 'tracks': track_rows, 'n_shows': n, 'n_plays': sum(len(s['tracks']) for s in shows),
                'first': dates[0], 'last': dates[-1], 'facts': facts,
                'genres': sorted(self.artist_genres.get(artist_id, ())),
                'links': [(label, self.artist_links[artist_id][source]) for source, label in LINK_LABELS
                          if source in self.artist_links.get(artist_id, {})]}

    def album_profile(self, album_id):
        """The album page for a group (album_id is its canonical id: see album_group)."""
        pids = sorted(self.album_shows.get(album_id, ()), key=self.show_dates.get)
        if not pids:
            return None
        members = set(self.album_members[album_id])
        album = self.albums[album_id]
        artists = self.album_artists[album_id]
        shows, by_rec = [], {}
        for pid in pids:
            items = [(i + 1, t) for i, t in enumerate(self.show_tracks[pid]) if self.tracks[t]['album_id'] in members]
            shows.append({'date': self.show_dates[pid], 'tracks': [{'position': p, 'name': self.tracks[t]['name']} for p, t in items]})
            for _, t in items:
                r = by_rec.setdefault(self.recording_key(t), {
                    'name': self.tracks[t]['name'], 'spotify_url': self.tracks[t]['spotify_url'],
                    'ms': self.tracks[t]['duration_ms'], 'dates': [],
                    'others': [self.artists[a] for a in self.track_artists[t] if a not in artists]})
                r['dates'].append(self.show_dates[pid])
        track_rows = sorted(by_rec.values(), key=lambda r: (-len(r['dates']), -r['dates'][-1].toordinal()))
        n = len(pids)
        facts = Facts()

        rank = self.album_rank.get(album_id)
        if rank and n >= 2:
            facts.add('regular', 'Standing', f"#{rank} most-played album" if rank <= 50 else f"played at {n} shows",
                      more={'ranking': 'albums', 'anchor': album['spotify_id'], 'text': 'see ranking'})
        top = track_rows[0]
        if len(top['dates']) > 1:
            facts.add('repeat', 'Most played', f"“{top['name']}” ({len(top['dates'])} times)")
        released = album.get('release_date')
        original = next((self.albums[m]['original_date'] for m in self.album_members[album_id] if self.albums[m].get('original_date')), None)
        if original and released and original[:4] < released[:4]:
            facts.add('stat', 'Released', f"{original[:4]}, first (MusicBrainz); this edition {released[:4]}")
        elif released and not released.startswith('0000'):
            facts.add('stat', 'Released', released[:4])
        # the original label (linked), with its catalog number; then Spotify's label when it's someone else
        mbid = self.album_label.get(album_id)
        orig = next((self.album_original[m] for m in self.album_members[album_id] if m in self.album_original), None)
        parts = []
        if mbid:
            parts.append(self.label_part(mbid))
            if orig and orig['mbid'] == mbid:
                parts.append(f" ({orig['catno']}, {orig['released'][:4]})" if orig.get('catno') and orig.get('released')
                             else f" ({orig['released'][:4]})" if orig.get('released') else "")
        if album.get('label') and (not mbid or _label_key(album['label']) != _label_key(self.labels[mbid]['name'])):
            parts.append(f"{'; ' if parts else ''}on Spotify, {album['label']}" if parts else album['label'])
        if parts:
            facts.add('stat', 'Label', *parts)
        if n >= 3 and self.dj_shows:
            room = self._over_represented(set(pids), self.dj_shows, limit=3)
            if room:
                facts.add('stat', 'Often in the room', *_join([[self._dj_part(i), f" ({k} of {n})"] for i, k in room]))

        links = [(label, self.album_links[album_id][source]) for source, label in ALBUM_LINK_LABELS
                 if source in self.album_links.get(album_id, {})]
        if not links:  # a duplicate in the group may be the one MusicBrainz knows
            other = next((m for m in self.album_members[album_id] if self.album_links.get(m)), None)
            if other:
                links = [(label, self.album_links[other][source]) for source, label in ALBUM_LINK_LABELS
                         if source in self.album_links[other]]
        kind = {'compilation': 'Compilation', 'single': 'Single'}.get(album.get('album_type'), 'Album')
        return {'album': album, 'kind': kind, 'artists': _join([self._artist_part(a) for a in artists if a in self.artists]),
                'shows': shows, 'tracks': track_rows, 'n_shows': n, 'n_plays': sum(len(s['tracks']) for s in shows),
                'first': shows[0]['date'], 'last': shows[-1]['date'], 'facts': facts, 'links': links,
                'copyright': album.get('copyright_p'),
                'editions': [self.albums[m] for m in self.album_members[album_id] if m != album_id]}

    def label_part(self, mbid, text=None):
        return {'label': mbid, 'text': text or self.labels[mbid]['name']}

    def label_family(self, mbid):
        """(parents, children): labels this one is an imprint of or currently owned by, and the labels that are its
        imprints or that it owns and that have plays here."""
        rels = self.labels[mbid]['rels']
        kinds = ('imprint', 'label ownership')
        parents = {r['target_mbid']: r['target_name'] for r in rels
                   if r['type'] in kinds and r['direction'] == 'backward' and not r['end_date']}
        children = {r['target_mbid']: r['target_name'] for r in rels
                    if r['type'] in kinds and r['direction'] == 'forward' and r['target_mbid'] in self.label_plays}
        return parents, children

    def label_profile(self, mbid):
        pids = sorted(self.label_shows.get(mbid, ()), key=self.show_dates.get)
        if not pids:
            return None
        lab = self.labels[mbid]
        shows, albums, artists = [], {}, Counter()
        for pid in pids:
            items = [t for t in self.show_tracks[pid] if self.album_label.get(self.tracks[t]['album_id']) == mbid]
            shows.append({'date': self.show_dates[pid], 'tracks': [self.tracks[t]['name'] for t in items]})
            for t in items:
                aid = self.tracks[t]['album_id']
                canon = self.album_group.get(aid, aid)
                orig = self.album_original.get(aid) or next((self.album_original[m] for m in self.album_members.get(canon, [])
                                                             if m in self.album_original), None)
                a = albums.setdefault(canon, {
                    'album': {'album': self.albums[canon]['spotify_id'], 'text': self.albums[canon]['name']},
                    'artists': _join([self._artist_part(x) for x in self.album_artists[canon] if x in self.artists]),
                    'released': (orig or {}).get('released') or self.albums[aid].get('release_date'),
                    'catno': (orig or {}).get('catno') if orig and orig['mbid'] == mbid else None,
                    'how': self.album_label_how.get(aid), 'plays': 0, 'dates': set()})
                a['plays'] += 1
                a['dates'].add(self.show_dates[pid])
                artists.update(self.track_artists[t])
        albums = sorted(albums.values(), key=lambda a: (-len(a['dates']), -a['plays'], a['album']['text'].lower()))
        n = len(pids)
        facts = Facts()
        rank = self.label_rank[mbid]
        facts.add('regular', 'Standing', f"#{rank} most-played label" if rank <= 50 else f"{self.label_plays[mbid]} plays",
                  more={'ranking': 'labels', 'anchor': mbid, 'text': 'see ranking'})
        facts.add('stat', 'Most played', *_join([[self._artist_part(a), f" ({k})"] for a, k in artists.most_common(5)
                                                 if a in self.artists]))
        years = sorted(int(a['released'][:4]) for a in albums if a['released'] and a['released'][:4].isdigit()
                       and int(a['released'][:4]) > 1900)
        if years:
            facts.add('stat', 'Released', f"{years[0]}" if years[0] == years[-1] else f"{years[0]} to {years[-1]}")
        if n >= 4 and self.dj_shows:
            room = self._over_represented(set(pids), self.dj_shows, limit=3)
            if room:
                facts.add('stat', 'Often in the room', *_join([[self._dj_part(i), f" ({k} of {n})"] for i, k in room]))
        parents, children = self.label_family(mbid)
        links = [(label, lab['links'][src]) for src, label in LABEL_LINK_LABELS if src in lab['links']]
        links.insert(1 if links and links[0][0] == 'Wikipedia' else 0, ('MusicBrainz', f"https://musicbrainz.org/label/{mbid}"))
        return {'label': lab, 'shows': shows, 'albums': albums, 'n_shows': n, 'n_plays': self.label_plays[mbid],
                'first': shows[0]['date'], 'last': shows[-1]['date'], 'facts': facts, 'links': links,
                'parents': [self.label_part(m, t) if m in self.labels else {'text': t} for m, t in parents.items()],
                'children': [self.label_part(m) for m in sorted(children, key=lambda m: -self.label_plays[m])],
                'from_spotify': sum(1 for a in albums if a['how'] == 'spotify')}

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
        years = [{'year': y, 'plays': by_year[y], 'total': self.plays_by_year[y],
                  'share': by_year[y] / self.plays_by_year[y]}
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

    def _rank_djs(self):
        items = sorted(((i, sorted(self.show_dates[p] for p in pids)) for i, pids in self.dj_shows.items()),
                       key=lambda kv: (-len(kv[1]), self.djs[kv[0]]['name']))
        rows = self._dated_rows(items, lambda i: self.djs[i]['slug'], lambda i: [self._dj_part(i)],
                                lambda i, d: f"{self.djs[i]['name']}: {len(d)} shows, {d[0]} to {d[-1]}")
        return {'title': 'DJs', 'unit': 'shows', 'rows': rows,
                'blurb': 'Everyone who has joined the room, by the number of shows; each tick is a show.'}

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

    def _rank_albums(self):
        items = sorted(((a, sorted(self.show_dates[p] for p in pids)) for a, pids in self.album_shows.items()
                        if len(pids) >= 2 and a in self.album_rank),
                       key=lambda kv: (-len(kv[1]), (self.albums[kv[0]]['name'] or '').lower()))

        def label(a):
            return [{'album': self.albums[a]['spotify_id'], 'text': self.albums[a]['name']}, ' · ',
                    *_join([self._artist_part(x) for x in self.album_artists[a] if x in self.artists])]
        rows = self._dated_rows(items, lambda a: self.albums[a]['spotify_id'], label,
                                lambda a, d: f"{self.albums[a]['name']}: {len(d)} shows, {d[0]} to {d[-1]}")
        return {'title': 'Most played albums', 'unit': 'shows', 'rows': rows,
                'blurb': 'Albums (not singles) played at more than one show, by the number of shows; each tick is a '
                         'show. Copies of one album under different Spotify ids, and its editions, count together.'}

    def _rank_labels(self):
        artists = defaultdict(Counter)
        for pid, tids in self.show_tracks.items():
            for t in tids:
                mbid = self.album_label.get(self.tracks[t]['album_id'])
                if mbid:
                    artists[mbid].update(self.track_artists[t])
        dates = defaultdict(list)
        for pid, tids in self.show_tracks.items():
            for t in tids:
                mbid = self.album_label.get(self.tracks[t]['album_id'])
                if mbid:
                    dates[mbid].append(self.show_dates[pid])
        items = sorted(((m, sorted(d)) for m, d in dates.items() if len(d) >= 5),
                       key=lambda kv: (-len(kv[1]), self.labels[kv[0]]['name'].lower()))
        rows = self._dated_rows(items, lambda m: m, lambda m: [self.label_part(m)],
                                lambda m, d: f"{self.labels[m]['name']}: {len(d)} plays. Most played: " +
                                ', '.join(self.artists[a]['name'] for a, _ in artists[m].most_common(3) if a in self.artists))
        credited = sum(self.label_plays.values())
        return {'title': 'Top labels', 'unit': 'plays', 'rows': rows,
                'blurb': f"Labels the music first came out on, from MusicBrainz (Atlantic, not today's Rhino reissue), "
                         f"by plays; each tick is a play. {credited / self.total_track_plays:.0%} of plays have a label "
                         "we can name this way."}

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

    # ---- home page ----

    def show_caption(self, playlist_id):
        """A show's description split into its own note and the playlist image credit, without the
        "What we played for each other on <date>." boilerplate. Older shows' descriptions don't say what the
        image is; for those, the caption from the Google Doc (cover_caption), if there is one."""
        text = html.unescape(self.shows[playlist_id]['description'] or '')
        text = re.sub(r'^\s*What we played for each other on [\d/]+\.?\s*', '', text)
        note, _, image = text.partition('Playlist image:')
        image = ' '.join(image.split()).rstrip('.') or self.cover_captions.get(playlist_id, '')
        return {'note': ' '.join(note.split()), 'image': image}

    def same_week(self, playlist_id, days=3):
        """The show closest to this one's calendar date in each earlier year (within `days`), newest first."""
        d = self.show_dates[playlist_id]
        out = []
        for year in range(d.year - 1, self.first_date.year - 1, -1):
            try:
                target = d.replace(year=year)
            except ValueError:  # Feb 29
                target = d.replace(year=year, day=28)
            near = [(abs((self.show_dates[p] - target).days), p) for p in self.show_tracks
                    if self.show_dates[p].year == year and abs((self.show_dates[p] - target).days) <= days]
            if near:
                out.append(min(near)[1])
        return out

    def home_notes(self, rng=None):
        """Small cards for the home page wall: a return, a much-repeated track, a very new show, a regular,
        a DJ and a random show to pull out. Different picks each call."""
        rng = rng or random.Random()
        if not hasattr(self, '_home_pools'):
            self._home_pools = {'returns': self._rank_returns()['rows'][:60], 'tracks': self._rank_tracks()['rows'][:40],
                                'artists': self._rank_artists()['rows'][:60]}
        pools, notes = self._home_pools, []
        shows = sorted(self.show_tracks, key=self.show_dates.get)

        r = rng.choice(pools['returns'])
        a, b = r['span']
        notes.append({'kind': 'return', 'label': 'Welcome back', 'parts': r['label'] + [f" returned after {r['value']}, on "],
                      'link': {'show': b, 'text': f"{b:%B} {b.day}, {b.year}"}})
        t = rng.choice(pools['tracks'])
        notes.append({'kind': 'repeat', 'label': 'Heard it before', 'parts': t['label'] + [f", played {t['value']} times"],
                      'more': {'ranking': 'tracks', 'anchor': t['anchor']}})
        novel = sorted(shows, key=lambda p: -self.new_artists[p])[:25]
        p = rng.choice(novel)
        d = self.show_dates[p]
        notes.append({'kind': 'theme', 'label': 'All new', 'parts': [f"{round(self.new_artists[p] * 100)}% of the artists on "],
                      'link': {'show': d, 'text': f"{d:%B} {d.day}, {d.year}"}, 'tail': " had never been played before"})
        ar = rng.choice(pools['artists'])
        notes.append({'kind': 'regular', 'label': 'Regulars', 'parts': ar['label'] + [f", at {ar['value']} shows"]})
        regulars = [i for i, pids in self.dj_shows.items() if len(pids) >= 10]
        if regulars:
            i = rng.choice(regulars)
            notes.append({'kind': 'genre', 'label': 'In the room', 'parts': [self._dj_part(i), f", at {len(self.dj_shows[i])} shows"]})
        d = self.show_dates[rng.choice(shows)]
        notes.append({'kind': 'random', 'label': 'Pull one out', 'parts': [],
                      'link': {'show': d, 'text': f"{d:%B} {d.day}, {d.year}"}, 'tail': ", picked at random"})
        for n in notes:  # each reads as one run of parts
            n['parts'] = n['parts'] + ([n.pop('link')] if 'link' in n else []) + ([n.pop('tail')] if 'tail' in n else [])
        rng.shuffle(notes)
        return notes

    def browse(self):
        """Every show with what the browse page sorts and filters on: novelty against the shows around it (lift:
        its share of new artists minus their median), who was in the room, and the genre families it leans toward
        (at least 1.5 times the family's average share across the archive, and at least 12% of the show)."""
        if not hasattr(self, '_browse'):
            n, usual = len(self.mix), Counter()
            for shares, _ in self.mix.values():
                usual.update({f: v / n for f, v in shares.items() if f})
            self._browse = []
            for pid in self.show_tracks:
                peers, shares = self.novelty_peers[pid], self.mix[pid][0]
                self._browse.append({
                    'pid': pid, 'date': self.show_dates[pid], 'novelty': self.novelty[pid],
                    'lift': self.novelty[pid] - peers[len(peers) // 2],
                    'djs': {i for i, _ in self.show_djs.get(pid, ())},
                    'leans': {f for f, v in shares.items() if f and v >= max(0.12, 1.5 * usual[f])}})
        return self._browse

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

    def lengths(self):
        """Show sizes and song lengths, for /heath (Heath asked: most and fewest songs per show, longest and
        shortest songs, six-minute songs)."""
        six = SIX_MINUTES
        shows = []
        for pid, tids in self.show_tracks.items():
            ms = [self.tracks[t]['duration_ms'] or 0 for t in tids]
            shows.append({'date': self.show_dates[pid], 'n': len(tids), 'minutes': sum(ms) // 60000,
                          'avg_ms': sum(ms) / len(ms), 'six': sum(m >= six for m in ms),
                          'room': _join([self._dj_part(d) for d, _ in self.show_djs.get(pid, [])])})
        shows.sort(key=lambda s: s['date'])
        plays = [(self.show_dates[pid], t) for pid, tids in self.show_tracks.items() for t in tids
                 if self.tracks[t]['duration_ms']]
        all_ms = sorted(self.tracks[t]['duration_ms'] for _, t in plays)

        # one row per recording, with every date it was played
        recs = defaultdict(lambda: {'dates': []})
        for d, t in plays:
            r = recs[self.recording_key(t)]
            r.setdefault('track', t)
            r['dates'].append(d)
        rows = []
        for r in recs.values():
            t = self.tracks[r['track']]
            rows.append({'ms': t['duration_ms'], 'name': t['name'], 'spotify_url': t['spotify_url'],
                         'artists': _join([self._artist_part(a) for a in self.track_artists[r['track']]]),
                         'dates': sorted(set(r['dates']))})
        by_length = sorted(rows, key=lambda r: r['ms'])

        bin_ms, n_bins = 30000, 24  # half-minute bins up to 12 minutes, then one for everything longer
        bins = [0] * (n_bins + 1)
        for m in all_ms:
            bins[min(m // bin_ms, n_bins)] += 1

        years = defaultdict(lambda: {'shows': 0, 'plays': 0, 'ms': 0, 'six': 0})
        for s in shows:
            y = years[s['date'].year]
            y['shows'] += 1
            y['plays'] += s['n']
            y['six'] += s['six']
        for d, t in plays:
            years[d.year]['ms'] += self.tracks[t]['duration_ms']
        by_year = [{'year': k, 'per_show': v['plays'] / v['shows'], 'avg_ms': v['ms'] / v['plays'],
                    'six_share': v['six'] / v['plays']} for k, v in sorted(years.items())]

        by_n = sorted(shows, key=lambda s: (s['n'], s['minutes']))
        sixers = [r for r in rows if r['ms'] >= six]
        return {
            'shows': shows, 'plays': len(all_ms), 'hours': sum(all_ms) // 3600000,
            'median_ms': all_ms[len(all_ms) // 2], 'per_show': len(all_ms) / len(shows),
            'most': by_n[::-1][:10], 'fewest': by_n[:10],
            'longest_show': max(shows, key=lambda s: s['minutes']), 'shortest_show': min(shows, key=lambda s: s['minutes']),
            'slowest_show': max(shows, key=lambda s: s['avg_ms']), 'quickest_show': min(shows, key=lambda s: s['avg_ms']),
            'max_n': max(s['n'] for s in shows),
            'bins': bins, 'bin_ms': bin_ms,
            'longest': by_length[::-1][:15], 'shortest': by_length[:15],
            'six_plays': sum(m >= six for m in all_ms), 'six_recordings': len(sixers),
            'most_six': sorted(shows, key=lambda s: (-s['six'], s['date']))[:8],
            'near_six': sorted((r for r in rows if r['ms'] >= six), key=lambda r: r['ms'])[:10],
            'by_year': by_year,
        }

    def back_to_back(self):
        """Runs of two or more songs in a row in a show sharing an artist, for /heath."""
        runs = []
        for pid, tids in self.show_tracks.items():
            i = 0
            while i < len(tids):
                shared, j = set(self.track_artists[tids[i]]), i + 1
                while j < len(tids) and shared & set(self.track_artists[tids[j]]):
                    shared &= set(self.track_artists[tids[j]])
                    j += 1
                if j - i >= 2:
                    runs.append({'date': self.show_dates[pid], 'start': i + 1, 'artists': sorted(shared),
                                 'tracks': [{'name': self.tracks[t]['name'], 'spotify_url': self.tracks[t]['spotify_url']}
                                            for t in tids[i:j]]})
                i = j
        runs.sort(key=lambda r: (-len(r['tracks']), r['date']))
        by_artist = defaultdict(list)
        for r in runs:
            for a in r['artists']:
                by_artist[a].append(r['date'])
        for r in runs:
            r['artist_parts'] = _join([self._artist_part(a) for a in r['artists']])
        repeat = sorted(((a, sorted(d)) for a, d in by_artist.items() if len(d) >= 2), key=lambda kv: (-len(kv[1]), self.artists[kv[0]]['name']))
        return {'runs': runs, 'n': len(runs), 'shows': len({r['date'] for r in runs}), 'total_shows': len(self.show_tracks),
                'repeat': [{'artist': [self._artist_part(a)], 'dates': d} for a, d in repeat[:12]]}

    def release_year(self, track_id, on=None):
        """The year a track's recording first came out, or None if unknown or not believable (Spotify's 1900
        placeholder, or after `on`, the date it was played)."""
        released = self.track_released.get(track_id)
        if not released or not released[0][:4].isdigit():
            return None
        year = int(released[0][:4])
        if year <= 1900 or (on and released[0][:10] > on.isoformat()):
            return None
        return year

    def show_age(self, playlist_id):
        """Song age for a show's header: the span and median of its songs' release years, and a count per decade."""
        years = sorted(y for y, _ in self.play_years.get(playlist_id, []))
        if not years:
            return None
        decades = Counter(min(max(y // 10 * 10, OLDEST_DECADE), self.last_date.year // 10 * 10) for y in years)
        return {'first': years[0], 'last': years[-1], 'median': years[(len(years) - 1) // 2], 'known': len(years),
                'tracks': len(self.show_tracks[playlist_id]),
                'decades': [(dec, decades.get(dec, 0)) for dec in range(OLDEST_DECADE, self.last_date.year // 10 * 10 + 1, 10)]}

    def age_page(self):
        """Everything the song age page shows."""
        shows = []
        for pid in sorted(self.show_tracks, key=self.show_dates.get):
            years = sorted(y for y, _ in self.play_years[pid])
            if len(years) >= 5:
                shows.append({'date': self.show_dates[pid], 'median': years[(len(years) - 1) // 2],
                              'first': years[0], 'last': years[-1], 'known': len(years), 'tracks': len(self.show_tracks[pid])})
        for i, sh in enumerate(shows):  # the median of the shows around each one, for the trend line
            near = sorted(x['median'] for x in shows[max(0, i - NOVELTY_WINDOW):i + NOVELTY_WINDOW + 1])
            sh['trend'] = near[len(near) // 2]

        # each show year's spread of release years, in 5-year bins (older songs in the first bin), for small multiples
        first_bin, last_bin = OLDEST_DECADE, self.last_date.year // AGE_BIN * AGE_BIN
        bins = list(range(first_bin, last_bin + 1, AGE_BIN))
        by_year = defaultdict(list)
        for pid, plays in self.play_years.items():
            by_year[self.show_dates[pid].year].extend(y for y, _ in plays)
        spread = []
        for yr, ys in sorted(by_year.items()):
            ys.sort()
            c = Counter(min(max(y // AGE_BIN * AGE_BIN, first_bin), last_bin) for y in ys)
            spread.append({'year': yr, 'total': len(ys), 'median': ys[(len(ys) - 1) // 2],
                           'bins': [(b, c.get(b, 0) / len(ys), c.get(b, 0)) for b in bins]})
        peak = max(share for row in spread for _, share, _ in row['bins'])

        # the oldest recordings played, and songs played within a fortnight of coming out
        oldest, fresh = {}, []
        for pid, plays in self.play_years.items():
            d = self.show_dates[pid]
            for y, t in plays:
                released, source = self.track_released[t]
                key = self.recording_key(t)
                o = oldest.setdefault(key, {'year': y, 'released': released, 'source': source, 'dates': [], **self._track_row(t)})
                o['dates'].append(d)
                if len(released) == 10 and 0 <= (d - date.fromisoformat(released)).days <= FRESH_DAYS:
                    fresh.append({'date': d, 'days': (d - date.fromisoformat(released)).days, **self._track_row(t)})
        oldest = sorted(oldest.values(), key=lambda o: (o['released'], o['name']))[:15]
        fresh.sort(key=lambda f: (f['date'], -f['days']), reverse=True)

        overall = sorted(y for plays in self.play_years.values() for y, _ in plays)
        known = sum(len(p) for p in self.play_years.values())
        mb = sum(1 for plays in self.play_years.values() for _, t in plays if self.track_released[t][1].startswith('musicbrainz'))
        return {'shows': shows, 'spread': spread, 'bins': bins, 'peak': peak, 'oldest': oldest, 'fresh': fresh[:15], 'n_fresh': len(fresh),
                'median': overall[(len(overall) - 1) // 2], 'known': known, 'plays': self.total_track_plays,
                'mb_share': mb / known if known else 0,
                'year_min': min(s['median'] for s in shows) // 10 * 10, 'year_max': self.last_date.year}

    def _track_row(self, t):
        return {'name': self.tracks[t]['name'], 'spotify_url': self.tracks[t]['spotify_url'],
                'artists': _join([self._artist_part(a) for a in self.track_artists[t]])}

    def novelty_series(self):
        """Every show's novelty and its neighborhood median, in date order, for the novelty chart."""
        out = []
        for pid in sorted(self.novelty, key=self.show_dates.get):
            peers = self.novelty_peers[pid]
            out.append({'date': self.show_dates[pid], 'novelty': self.novelty[pid],
                        'new_artists': self.new_artists[pid], 'repeats': self.repeats[pid],
                        'median': peers[len(peers) // 2], 'rank': self.novelty_rank[pid]})
        return out


SIX_MINUTES = 6 * 60 * 1000
OLDEST_DECADE = 1950  # song age: older decades count with the 1950s ("1950s and earlier")
FRESH_DAYS = 14  # song age: played within this many days of coming out
NOT_RELEASING_TYPES = {'Reissue Production', 'Distributor', 'Holding', 'Rights Society', 'Manufacturer', 'Publisher'}
LABEL_LINK_LABELS = [('wikipedia', 'Wikipedia'), ('musicbrainz', 'MusicBrainz'), ('discogs', 'Discogs'),
                     ('bandcamp', 'Bandcamp'), ('official site', 'Website'), ('wikidata', 'Wikidata')]
SHOW_LEAN = (0.10, 1.5)  # a show's mix: a family stands out at 10 points of runtime and 1.5x (or half) its usual
AGE_BIN = 5  # song age: years per bar in each show year's histogram
RANKINGS = ['artists', 'albums', 'tracks', 'returns', 'labels', 'djs']  # novelty has its own chart page


def _signature(db_path):
    """Hash of the database file and of the code that builds History from it, so either changing rebuilds the
    cache (mtimes change on git checkout and Docker COPY, content doesn't)."""
    h = hashlib.sha1()
    for path in (db_path, __file__, genre_families.__file__):
        with open(path, 'rb') as f:
            for block in iter(lambda: f.read(1 << 20), b''):
                h.update(block)
    return h.hexdigest()


def _cache_path(db_path):
    return db_path + '.history.pickle'


def build_cache(db_path=DB_PATH):
    """Build the History and pickle it next to the database, so the web app can start
    without rebuilding it. Run after loading data (load_playlist.py does) and at image build."""
    history = History(db_path)
    with open(_cache_path(db_path), 'wb') as f:
        pickle.dump((_signature(db_path), history), f, protocol=pickle.HIGHEST_PROTOCOL)
    return history


def _load(db_path):
    try:
        with open(_cache_path(db_path), 'rb') as f:
            signature, history = pickle.load(f)
        if signature == _signature(db_path):
            return history
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError):
        pass
    try:
        return build_cache(db_path)
    except OSError:  # read-only filesystem: just build in memory
        return History(db_path)


@lru_cache(maxsize=1)
def _history_for(db_path, stat_key):
    return _load(db_path)


def get_history(db_path=DB_PATH):
    st = os.stat(db_path)
    return _history_for(db_path, (st.st_size, st.st_mtime_ns))


if __name__ == '__main__':
    import time
    start = time.time()
    build_cache()
    print(f"built {_cache_path(DB_PATH)} in {time.time() - start:.1f}s")
