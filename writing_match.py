"""Match pieces of music writing (a title, tags, a URL, some body text and links) to artists and albums in the dataset.

The signals, strongest first: the artist and one of their albums or tracks both named in the title or the URL slug;
a feed tag that is exactly the artist's name; a link in the piece to the artist's Bandcamp, Spotify, MusicBrainz or
Discogs page; the artist named in the title or slug; the artist named in the body. A name made only of common
English words ("Real Estate", "Status Quo", "Low") is ambiguous and counts for much less on its own.

Scores combine like independent odds: 1 - prod(1 - s). A piece that is about the artist (title, slug, tag) is accepted
at AUTO and above and goes to a person between REVIEW and AUTO. A mention (body or link only) is kept at MENTION and above.
"""
import os
import re
import unicodedata
from collections import defaultdict
from urllib.parse import urlparse

AUTO, REVIEW, MENTION = 0.75, 0.35, 0.6
SCORES = {  # (signal, ambiguous name?) -> score
    ('pair_title', False): 0.95, ('pair_title', True): 0.85,
    ('pair_slug', False): 0.85, ('pair_slug', True): 0.7,
    ('tag', False): 0.9, ('tag', True): 0.55,
    ('link', False): 0.95, ('link', True): 0.95,
    ('title', False): 0.75, ('title', True): 0.35,
    ('slug', False): 0.6, ('slug', True): 0.25,
    ('body_long', False): 0.6, ('body_long', True): 0.2,   # a name of 2+ words and 10+ letters
    ('body', False): 0.45, ('body', True): 0.15,
    ('body_pair', False): 0.8, ('body_pair', True): 0.5,
}
PAIR_SIGNALS = {'pair_title', 'pair_slug', 'body_pair'}
ABOUT_SIGNALS = {'pair_title', 'pair_slug', 'tag', 'title', 'slug'}  # the rest only say the piece mentions them


def norm(s):
    s = unicodedata.normalize('NFKD', (s or '').lower().replace('&', ' and '))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[\W_]+', ' ', re.sub(r"[’']", '', s)).strip()


def norm_album(s):
    """An album title as a writer would give it: without '(Deluxe Edition)' or '- 2011 Remaster'."""
    s = re.sub(r'\([^)]*\)|\[[^\]]*\]', ' ', s or '')
    s = re.sub(r'\s+-\s+(?:\d{4}\s+)?(?:remaster|deluxe|expanded|anniversary|bonus|special|mono|stereo|live).*$', '', s, flags=re.I)
    return norm(s)


# the system word list is old: add words that are everyday now, or that name an artist without being a name
EXTRA_WORDS = {'internet', 'online', 'digital', 'electronic', 'original', 'soundtrack', 'cast', 'orchestra', 'ensemble',
               'quartet', 'trio', 'weather', 'news', 'phone', 'email', 'streaming', 'podcast', 'album', 'single'}


def load_words(path='/usr/share/dict/words'):
    try:
        words = {w.strip().lower() for w in open(path, errors='ignore') if w.strip().isalpha()}
    except OSError:
        return set()
    return words | EXTRA_WORDS


def is_ambiguous(key, words):
    toks = key.split()
    if toks and toks[0] == 'the':
        toks = toks[1:]
    if not toks:
        return True
    if len(''.join(toks)) <= 4:
        return True
    if not words:
        return len(toks) == 1 and len(toks[0]) <= 6
    return all(t in words or t.rstrip('s') in words for t in toks)  # 'powers' is as common as 'power'


# Bandcamp's own subdomains: a data row pointing an artist at one (it happens) would match every piece that links there
NOT_ARTIST_HOSTS = {'daily.bandcamp.com', 'get.bandcamp.com', 'blog.bandcamp.com', 'help.bandcamp.com', 'forum.bandcamp.com',
                    'bandcamp.com', 'music.bandcamp.com'}
# Everyday words that make a name ambiguous when they are all it is made of ('Real Estate', 'Good Habits'). A
# capitalized name of two or more words with any word outside this set ('Uncle Tupelo', 'Joni Mitchell') is not ambiguous.
COMMON_NAME_WORDS = {
    'a', 'an', 'the', 'and', 'of', 'in', 'on', 'at', 'to', 'for', 'is', 'it', 'be', 'we', 'you', 'me', 'my', 'our', 'your',
    'all', 'new', 'old', 'big', 'little', 'small', 'great', 'good', 'bad', 'best', 'high', 'low', 'long', 'short', 'young',
    'black', 'white', 'red', 'blue', 'green', 'yellow', 'gold', 'golden', 'silver', 'dark', 'light', 'bright', 'wild',
    'dead', 'death', 'life', 'love', 'time', 'day', 'days', 'night', 'nights', 'sun', 'moon', 'star', 'stars', 'sky',
    'fire', 'water', 'earth', 'wind', 'rain', 'snow', 'ice', 'rock', 'stone', 'river', 'sea', 'ocean', 'mountain',
    'house', 'home', 'street', 'road', 'city', 'town', 'world', 'land', 'real', 'estate', 'status', 'quo', 'here', 'there',
    'now', 'then', 'once', 'again', 'first', 'last', 'one', 'two', 'three', 'four', 'five', 'zero', 'half', 'back', 'up',
    'down', 'out', 'over', 'under', 'free', 'fast', 'slow', 'hard', 'soft', 'sweet', 'sad', 'happy', 'crazy', 'lost',
    'found', 'cold', 'hot', 'heart', 'hearts', 'soul', 'souls', 'mind', 'minds', 'body', 'bodies', 'hand', 'hands',
    'eyes', 'face', 'girl', 'girls', 'boy', 'boys', 'man', 'men', 'woman', 'women', 'king', 'queen', 'prince', 'lady',
    'band', 'group', 'music', 'sound', 'sounds', 'song', 'songs', 'dance', 'party', 'club', 'orchestra', 'ensemble',
    'quartet', 'trio', 'project', 'collective', 'society', 'machine', 'machines', 'system', 'habits', 'drama', 'electronic',
    'original', 'soundtrack', 'cast', 'various', 'artists', 'internet', 'weather', 'news', 'digital', 'online',
}
# 'Brad Mehldau Trio' does not claim 'Brad Mehldau' (the leader is the same musician): what follows the shorter name is an
# ensemble word, or starts one ('and the Bad Seeds'). 'Gia Margaret' and 'Bonnie Prince Billy' do claim 'Margaret' and 'Prince'.
ENSEMBLE_WORDS = {'trio', 'quartet', 'quintet', 'sextet', 'septet', 'octet', 'nonet', 'orchestra', 'band', 'ensemble', 'arkestra',
                  'experience', 'group', 'collective', 'project', 'players', 'combo', 'choir', 'chorale', 'singers', 'allstars',
                  'unit', 'tentet', 'quartette', 'sound', 'system', 'revue', 'revival'}
BAND_STARTS = {'and', 'the', 'with', 'his', 'her', 'plus', 'featuring', 'feat', 'presents'}


def band_of(short, long):
    """Is `long` just `short` with a band attached ('brad mehldau' -> 'brad mehldau trio', 'nick cave and the bad seeds')?"""
    if not long.startswith(short + ' '):
        return False
    rest = long[len(short) + 1:].split()
    return rest[0] in BAND_STARTS or rest[-1] in ENSEMBLE_WORDS


TOO_COMMON = {'and', 'the', 'of', 'a', 'an', 'in', 'on', 'to', 'for', 'with', 'by', 'at', 'various', 'artists'}


class Matcher:
    def __init__(self, conn, words=None):
        """conn: a sqlite3 connection to birdnest.db. words: common English words, for spotting ambiguous names."""
        words = load_words() if words is None else words
        self.names = {}
        self.artist_idx = defaultdict(set)    # normalized name -> artist ids
        for aid, name in conn.execute("select artist_id, name from artist where name is not null"):
            k = norm(name)
            if len(k) >= 2 and k not in TOO_COMMON:
                self.artist_idx[k].add(aid)
                self.names[aid] = name
        # 'strict': common words only (used for the body, where a name is a weak signal); the title, slug and tags also
        # accept a capitalized multi-word name with an unusual word in it
        self.ambiguous_strict = {k: is_ambiguous(k, words) for k in self.artist_idx}
        self.ambiguous = {k: amb and not self._distinctive(k) for k, amb in self.ambiguous_strict.items()}
        self.maxn = max((len(k.split()) for k in self.artist_idx), default=1)
        # what each artist made, to confirm a name by its work: aid -> {normalized title: album id (or None for a track)}
        self.work = defaultdict(dict)
        album_artists = defaultdict(set)
        for alb, aid in conn.execute("select album_id, artist_id from album_artist"):
            album_artists[alb].add(aid)
        for alb, name in conn.execute("select album_id, name from album where name is not null"):
            k = norm_album(name)
            if len(k) >= 4:
                for aid in album_artists.get(alb, ()):
                    self.work[aid][k] = alb
        track_artists = defaultdict(set)
        for tid, aid in conn.execute("select track_id, artist_id from track_artist"):
            track_artists[tid].add(aid)
        for tid, name in conn.execute("select track_id, name from track where name is not null"):
            k = norm_album(name)
            if len(k) >= 5:
                for aid in track_artists.get(tid, ()):
                    self.work[aid].setdefault(k, None)
        # two artists can share a name ('Sweet', twice): a name alone points at the one we know best, so the other
        # only matches on something that is specifically theirs (an album, a link)
        try:
            pop = {a: (shows or 0, followers or 0) for a, followers, shows in conn.execute(
                """select a.artist_id, a.followers, count(distinct pt.playlist_id) from artist a
                   left join track_artist ta using (artist_id) left join playlist_track pt using (track_id) group by a.artist_id""")}
        except Exception:  # a database without those tables (a test): the lowest id stands in
            pop = {}
        self.primary = {k: max(ids, key=lambda a: (pop.get(a, (0, 0)), -a)) for k, ids in self.artist_idx.items() if len(ids) > 1}
        # two rows for one musician ('Shabaka', 'Shabaka Hutchings'): same MusicBrainz or Wikidata id. One doesn't rival the other.
        parent = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        try:
            ids = conn.execute('select artist_id, mbid, wikidata from mb_artist where mbid is not null').fetchall()
        except Exception:  # no wikidata column (a test database)
            ids = [(a, m, None) for a, m in conn.execute('select artist_id, mbid from mb_artist where mbid is not null')]
        for aid, mbid, wd in ids:
            parent[find(aid)] = find(('mb', mbid))
            if wd:
                parent[find(aid)] = find(('wd', wd))
        self.entity = {aid: find(aid) for aid, _, _ in ids}
        # exact pages
        self.sp_artist = {s: a for s, a in conn.execute("select spotify_id, artist_id from artist where spotify_id is not null")}
        self.mb_artist = {m: a for m, a in conn.execute("select mbid, artist_id from mb_artist where mbid is not null")}
        self.discogs, self.bandcamp = {}, {}
        for src, url, aid in conn.execute("select source, url, artist_id from artist_link where source in ('discogs','bandcamp')"):
            if src == 'discogs':
                m = re.search(r'/artist/(\d+)', url)
                if m:
                    self.discogs[m.group(1)] = aid
            else:
                h = (urlparse(url).hostname or '').lower().removeprefix('www.')
                if h and h not in NOT_ARTIST_HOSTS:
                    self.bandcamp[h] = aid

    def _same_musician(self, a, b):
        return self.entity.get(a, ('a', a)) == self.entity.get(b, ('b', b))

    def _rivals(self, short_key, long_key):
        """Could the artists named `long_key` crowd out those named `short_key`? Not when they're one musician."""
        return not any(self._same_musician(a, b) for a in self.artist_idx[short_key] for b in self.artist_idx[long_key])

    def _distinctive(self, key):
        """A name of two or more words, written with capitals, that has a word outside the everyday ones."""
        for aid in self.artist_idx[key]:
            words = re.findall(r"[^\W_]+", re.sub(r"[’']", '', self.names[aid]))
            if words and words[0].lower() == 'the':
                words = words[1:]
            if len(words) >= 2 and all(w[0].isupper() or w[0].isdigit() for w in words) \
                    and any(w.lower() not in COMMON_NAME_WORDS for w in words):
                return True
        return False

    def _find(self, text, written=True, strict=False):
        """{artist id: normalized name} for every artist named in text. A name of common words only counts if the text
        writes it as the artist does ('The Internet', not 'the internet'); a URL slug has no capitals, so those never do."""
        ambiguous = self.ambiguous_strict if strict else self.ambiguous
        toks = norm(text).split()
        found, claims = {}, []  # the longest name wins its words: 'Prince' inside "Bonnie 'Prince' Billy" is not Prince
        for n in range(min(self.maxn, len(toks)), 0, -1):  # (unless the longer one is just this one's band: 'Brad Mehldau Trio')
            for i in range(len(toks) - n + 1):
                k = ' '.join(toks[i:i + n])
                if k not in self.artist_idx:
                    continue
                over = [c for c in claims if c[0] < i + n and i < c[1]]
                if over and not all(band_of(k, c[2]) or not self._rivals(k, c[2]) for c in over):
                    continue
                hit = False
                for aid in self.artist_idx[k]:
                    if ambiguous[k] and not (written and self.names[aid] in text):
                        continue
                    found.setdefault(aid, k)
                    hit = True
                if hit:
                    claims.append((i, i + n, k))
        return found

    def _pair(self, text_key, aid, name_key):
        """The album/track of this artist named in the text (other than the artist's own name), as (title, album id)."""
        padded = f' {text_key} '
        for title, alb in self.work.get(aid, {}).items():
            # not an album that is just part of the artist's own name (Dolly Parton's 'Dolly')
            if f' {title} ' not in f' {name_key} ' and f' {title} ' in padded:
                return title, alb
        return None

    def _links(self, links):
        out = set()
        for u in links:
            m = re.search(r'open\.spotify\.com/(?:intl-[a-z-]+/)?artist/([A-Za-z0-9]{22})', u)
            if m and m.group(1) in self.sp_artist:
                out.add(self.sp_artist[m.group(1)])
            m = re.search(r'musicbrainz\.org/artist/([0-9a-f-]{36})', u)
            if m and m.group(1) in self.mb_artist:
                out.add(self.mb_artist[m.group(1)])
            m = re.search(r'discogs\.com/(?:[a-z]{2}/)?artist/(\d+)', u)
            if m and m.group(1) in self.discogs:
                out.add(self.discogs[m.group(1)])
            h = (urlparse(u).hostname or '').lower().removeprefix('www.')
            if h in self.bandcamp:
                out.add(self.bandcamp[h])
        return out

    def match(self, title='', tags=(), url='', body='', links=()):
        """-> [{'artist_id', 'album_id', 'score', 'kind' ('about'|'mentions'), 'signals', 'status' ('auto'|'review')}]"""
        sig = defaultdict(dict)    # artist id -> {signal: score}
        album = {}
        namesakes = defaultdict(set)  # a shared name -> the artists with it that this text matched
        key_of = {}                   # artist id -> the name it matched under

        def add(aid, name_key, signal, pair=None, strict=False):
            if aid != self.primary.get(name_key, aid) and signal not in PAIR_SIGNALS:
                return  # a namesake: the name alone belongs to the better-known artist
            key_of[aid] = name_key
            if len(self.artist_idx.get(name_key, ())) > 1:
                namesakes[name_key].add(aid)
            s = SCORES[(signal, (self.ambiguous_strict if strict else self.ambiguous).get(name_key, False))]
            if s > sig[aid].get(signal, 0):
                sig[aid][signal] = s
            if pair and pair[1]:
                album[aid] = pair[1]

        slug = urlparse(url).path.rstrip('/').rsplit('/', 1)[-1].replace('-', ' ').replace('_', ' ')
        for text, plain, paired in ((title, 'title', 'pair_title'), (slug, 'slug', 'pair_slug')):
            key = norm(text)
            for aid, name_key in self._find(text, written=text is title).items():
                pair = self._pair(key, aid, name_key)
                add(aid, name_key, paired if pair else plain, pair)
        tag_keys = {norm(t) for t in tags}
        for k in tag_keys:
            for aid in self.artist_idx.get(k, ()):
                add(aid, k, 'tag')
        if body:
            body_key = norm(body)
            for aid, name_key in self._find(body, written=True, strict=True).items():
                pair = self._pair(body_key, aid, name_key)
                long_name = len(name_key.split()) >= 2 and len(name_key) >= 10
                add(aid, name_key, 'body_pair' if pair else 'body_long' if long_name else 'body', pair, strict=True)
        for aid in self._links(links):
            sig[aid]['link'] = SCORES[('link', False)]
        # a name that sits inside a longer artist's name found in the same piece ('Margaret' in a piece naming 'Gia
        # Margaret') is that longer name's: later 'Margaret's mean her. Only an album, track or link brings it back.
        keep = PAIR_SIGNALS | {'link'}

        def inside(ka, kb):  # ka's words sit in kb's, and kb isn't just ka with a band attached
            return len(kb) > len(ka) and f' {ka} ' in f' {kb} ' and not band_of(ka, kb)
        for a, ka in key_of.items():
            if a in sig and any(inside(ka, kb) and not self._same_musician(a, b) for b, kb in key_of.items() if b != a and b in sig):
                for signal in [x for x in sig[a] if x not in keep]:
                    del sig[a][signal]
                if not sig[a]:
                    del sig[a]
        # when one namesake's album or track is named, the text is about that one, not the others
        for aids in namesakes.values():
            named = {a for a in aids if sig[a].keys() & PAIR_SIGNALS}
            for a in aids - named if named else ():
                for signal in [x for x in sig[a] if x not in PAIR_SIGNALS | {'link'}]:
                    del sig[a][signal]
                if not sig[a]:
                    del sig[a]

        out = []
        for aid, signals in sig.items():
            miss = 1.0
            for s in signals.values():
                miss *= 1 - s
            score = round(1 - miss, 3)
            kind = 'about' if ABOUT_SIGNALS & signals.keys() else 'mentions'
            if kind == 'about':
                if score < REVIEW:
                    continue
                status = 'auto' if score >= AUTO else 'review'
            else:  # a name in the body only: a mention worth listing if the name is distinctive, otherwise noise
                if score < MENTION:
                    continue
                status = 'auto'
            out.append({'artist_id': aid, 'album_id': album.get(aid), 'score': score, 'kind': kind,
                        'signals': ','.join(sorted(signals)), 'status': status})
        return sorted(out, key=lambda m: -m['score'])
