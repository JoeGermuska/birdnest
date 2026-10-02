from flask import Flask, request, render_template, abort, send_from_directory, redirect, make_response, jsonify, url_for, session
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from models import Artist, Database, Genre, Playlist
import factoids
import make_tiles
import radio
import genre_families
from datetime import date
from collections import Counter
import itertools
import os
import random
import secrets
import hashlib
import json
from urllib.parse import urlparse 

app = Flask(__name__,
    static_folder='static'
    )
# behind fly.io's proxy: trust X-Forwarded-Proto so external URLs (Spotify's redirect) are https
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
# signs the session cookie, which holds a listener's Spotify token; saving to Spotify is off without it
app.secret_key = os.environ.get('FLASK_SECRET_KEY')

# from https://towardsdatascience.com/use-flask-and-sqlalchemy-not-flask-sqlalchemy-5a64fafe22a4
SQLALCHEMY_DATABASE_URL = 'sqlite:///birdnest.db'
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={
    "check_same_thread": False
})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

app.session = scoped_session(SessionLocal)

# load the (pre-built) analysis cache at startup rather than on the first request
factoids.get_history()

# one public host, since Spotify only knows birdsconferring.com's OAuth callback (and the session cookie is per host)
CANONICAL_HOST = 'birdsconferring.com'
HOST_ALIASES = {'birdsconferring.fly.dev', 'www.birdsconferring.com'}

@app.before_request
def canonical_host():
    if request.host in HOST_ALIASES:
        return redirect(request.url.replace(f'//{request.host}', f'//{CANONICAL_HOST}', 1), code=301)

_static_hashes = {}

@app.url_defaults
def static_version(endpoint, values):
    """url_for('static', ...) gets ?v=<content hash>, so those URLs can be cached for good (static_cache below)."""
    if endpoint != 'static' or 'v' in values:
        return
    path = os.path.join(app.static_folder, values['filename'])
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        return
    if key not in _static_hashes:
        with open(path, 'rb') as f:
            _static_hashes[key] = hashlib.md5(f.read()).hexdigest()[:10]
    values['v'] = _static_hashes[key]

@app.after_request
def static_cache(response):
    if request.endpoint == 'static' and request.args.get('v') and response.status_code == 200:
        response.cache_control.public = True
        response.cache_control.max_age = 365 * 86400
        response.cache_control.immutable = True
        response.cache_control.no_cache = None
    return response

@app.context_processor
def radio_bar():
    # the site-wide player bar; checking the session is enough here, /spotify/token refreshes as needed
    return {'radio_available': _spotify_configured(), 'radio_listener': _spotify_configured() and 'token_info' in session}

@app.errorhandler(404)
@app.errorhandler(500)
def error_page(e):
    # in the site's own template, so moving through an error page keeps the radio and its scripts intact
    return render_template('error.html', code=getattr(e, 'code', 500)), getattr(e, 'code', 500)

@app.route('/robots.txt')
def robots():
    # Birds radio has a URL for every seed, and search for every query: endless, and each one costs real work.
    # (Pages already carry noindex; this keeps polite crawlers from fetching what they'd never index.)
    body = "User-agent: *\nDisallow: /radio\nDisallow: /search\nDisallow: /autocomplete\nDisallow: /spotify/\nDisallow: /todo/\nCrawl-delay: 5\n"
    response = make_response(body)
    response.mimetype = 'text/plain'
    return response

@app.route('/')
def index():
    history = factoids.get_history()
    pids = sorted(history.show_tracks, key=history.show_dates.get, reverse=True)
    tile = lambda p: {**history.shows[p], 'caption': history.show_caption(p)['image']}
    latest = pids[0]
    # the wall, with a note card after every few shows
    notes, wall = history.home_notes(), []
    for i, p in enumerate(pids):
        if i and i % 9 == 4 and notes:
            wall.append({'note': notes.pop(), 'tilt': random.choice((-1.5, -0.8, 0.6, 1.2))})
        wall.append({'show': tile(p)})
    return render_template("index.html", latest=tile(latest), latest_caption=history.show_caption(latest),
                           room=history.show_room(latest), mix=history.show_mix(latest),
                           facts=history.show_factoids(latest), stats=history.show_stats(latest),
                           this_week=[tile(p) for p in history.same_week(latest)], wall=wall, n_shows=len(pids))


BROWSE_SORTS = {'newest': 'Newest', 'oldest': 'Oldest', 'new': 'Most new for its time', 'shuffle': 'Shuffle'}


@app.route('/shows')
def browse():
    """Every show as tiles, sorted and filtered by the query: sort, dj (slug), family (genre family it leans toward)."""
    history = factoids.get_history()
    shows = history.browse()
    sort = request.args.get('sort') if request.args.get('sort') in BROWSE_SORTS else 'newest'
    dj = request.args.get('dj') if request.args.get('dj') in history.dj_by_slug else None
    families = [f for f in genre_families.FAMILY_NAMES if sum(f in s['leans'] for s in shows) >= 5]
    family = request.args.get('family') if request.args.get('family') in families else None
    if dj:
        shows = [s for s in shows if history.dj_by_slug[dj] in s['djs']]
    if family:
        shows = [s for s in shows if family in s['leans']]
    seed = request.args.get('seed', type=int)
    if sort == 'shuffle':
        seed = seed if seed is not None else random.randrange(10 ** 6)
        shows = sorted(shows, key=lambda s: s['date'])
        random.Random(seed).shuffle(shows)
    else:
        key = {'newest': lambda s: s['date'], 'oldest': lambda s: s['date'], 'new': lambda s: s['lift']}[sort]
        shows = sorted(shows, key=key, reverse=sort != 'oldest')

    def tile(s):
        t = {**history.shows[s['pid']], 'caption': history.show_caption(s['pid'])['image']}
        if sort == 'new':
            t['label'] = f"{s['date']:%b} {s['date'].day}, {s['date'].year} · novelty {round(s['novelty'] * 100)}"
        return t
    if sort in ('newest', 'oldest'):
        groups = [(y, [tile(s) for s in g]) for y, g in itertools.groupby(shows, key=lambda s: s['date'].year)]
    else:
        groups = [(None, [tile(s) for s in shows])]

    current = {'sort': sort, 'dj': dj, 'family': family}
    def link(**change):  # this view with some controls changed; a fresh shuffle each time it's asked for
        q = {k: v for k, v in {**current, **change}.items() if v and not (k == 'sort' and v == 'newest')}
        if q.get('sort') == 'shuffle':
            q['seed'] = random.randrange(10 ** 6)
        return url_for('browse', **q)
    djs = sorted(((history.djs[i]['slug'], history.djs[i]['name'], len(p)) for i, p in history.dj_shows.items()),
                 key=lambda d: (-d[2], d[1]))
    return render_template('shows.html', groups=groups, count=len(shows), current=current, link=link,
                           sorts=BROWSE_SORTS, djs=djs, dj_name=dj and history.djs[history.dj_by_slug[dj]]['name'],
                           families=[(f, genre_families.COLORS[f][0]) for f in families])


@app.route('/shows/random')
def random_show():
    history = factoids.get_history()
    return redirect(url_for('show_playlist', date_str=history.show_dates[random.choice(list(history.show_tracks))]))

@app.route('/search')
def search():
    terms = (request.args.get('q') or '').strip()
    tracks, artists, genres, albums = [], [], [], []
    if terms:
        db = Database(init_client=False)
        history = factoids.get_history()
        words = terms.lower().split()
        tracks = db.search_tracks(app.session, terms)
        artists = sorted((a for a in history.artists.values()
                          if all(w in (a['name'] or '').lower() for w in words) and history.artist_shows.get(a['artist_id'])),
                         key=lambda a: -len(history.artist_shows[a['artist_id']]))
        for a in artists:
            a['shows'] = len(history.artist_shows[a['artist_id']])
        genres = sorted(g for g in history.genre_artists if all(w in g for w in words))
        albums = sorted(({'spotify_id': history.albums[a]['spotify_id'], 'name': history.albums[a]['name'],
                          'artists': ', '.join(history.artists[x]['name'] for x in history.album_artists[a] if x in history.artists),
                          'shows': len(pids)}
                         for a, pids in history.album_shows.items()
                         if all(w in (history.albums[a]['name'] or '').lower() for w in words)),
                        key=lambda a: -a['shows'])
    return render_template("search_results.html", tracks=tracks, artists=artists, genres=genres, albums=albums, terms=terms)

@app.route('/autocomplete')
def autocomplete():
    db = Database(init_client=False)
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])

    suggestions = db.get_autocomplete_suggestions(app.session, query)
    return jsonify(suggestions)

@app.route('/genre/<genre_name>')
def genre(genre_name):
    history = factoids.get_history()
    profile = history.genre_profile(genre_name)
    if not profile:
        abort(404)
    return render_template('genre.html', genre_name=genre_name, profile=profile, history=history)

@app.route('/genres/map')
def genre_map():
    return render_template('genre_map.html', tree=factoids.get_history().genre_tree(),
                           colors=genre_families.COLORS)

@app.route('/genres')
def genres():
    genres = sorted(set(x.name for x in app.session.query(Genre).order_by(Genre.name)))
    return render_template('genres.html', genres=genres)

@app.route('/artist/<spotify_id>')
def artist(spotify_id):
    artist = app.session.query(Artist).filter(Artist.spotify_id == spotify_id).first()
    if not artist:
        abort(404)
    history = factoids.get_history()
    return render_template('artist.html', artist=artist, profile=history.artist_profile(artist.artist_id),
                           history=history)

@app.route('/album/<spotify_id>')
def album(spotify_id):
    history = factoids.get_history()
    canon = history.album_by_spotify.get(spotify_id)
    if canon is None:
        abort(404)
    if history.albums[canon]['spotify_id'] != spotify_id:  # another copy or edition of the album: one page for all
        return redirect(url_for('album', spotify_id=history.albums[canon]['spotify_id']))
    profile = history.album_profile(canon)
    if not profile:
        abort(404)
    return render_template('album.html', profile=profile, history=history)

TODO_PAGES = [
    {'endpoint': 'wikidata_todo', 'tab': 'Artists on Wikidata', 'title': 'Wikidata to-do: artists', 'kind': 'artist',
     'check': 'wikidata', 'property': 'P1902', 'source': 'Wikidata', 'account': 'a Wikipedia account',
     'how': 'Artists with no links whose name matches a musician or group on Wikidata, most-played first, then the ones '
            'with several possible matches. Open the Wikidata item, check it\'s the same act, and add the Spotify ID as '
            '<em>Spotify artist ID</em> (P1902).'},
    {'endpoint': 'wikidata_album_todo', 'tab': 'Albums on Wikidata', 'title': 'Wikidata to-do: albums', 'kind': 'album',
     'check': 'wikidata', 'property': 'P2205', 'source': 'Wikidata', 'account': 'a Wikipedia account',
     'how': 'Albums whose Wikidata item (found through MusicBrainz, which links the two) doesn\'t have our Spotify ID yet, '
            'most-played first. Open the item and add the Spotify ID as <em>Spotify album ID</em> (P2205). An item can '
            'have more than one, so add ours even when it already has another.'},
    {'endpoint': 'musicbrainz_album_todo', 'tab': 'Albums on MusicBrainz', 'title': 'MusicBrainz to-do: albums', 'kind': 'album',
     'check': 'musicbrainz', 'source': 'MusicBrainz', 'account': 'a MusicBrainz account',
     'how': 'Albums we matched to a MusicBrainz release by barcode alone, most-played first. Open the release, check it\'s '
            'the same album (title, tracks, barcode), and under <em>External links</em> add the Spotify URL '
            '(MusicBrainz files it as <em>stream for free</em>).'},
]


def _todo(endpoint, rows):
    page = next(p for p in TODO_PAGES if p['endpoint'] == endpoint)
    return render_template('todo.html', page=page, pages=TODO_PAGES, rows=rows)


def _table(con, name):
    return con.execute("select 1 from sqlite_master where name = ?", (name,)).fetchone()


def _album_row(history, album_id, shows, **extra):
    a = history.albums[album_id]
    return {'spotify_id': a['spotify_id'], 'name': a['name'], 'url': url_for('album', spotify_id=a['spotify_id']),
            'artists': ', '.join(history.artists[x]['name'] for x in history.album_artists[album_id] if x in history.artists),
            'shows': shows, **extra}


@app.route('/todo/wikidata')
def wikidata_todo():
    """Probable Wikidata items for unlinked artists (see wikidata_candidates.py), for adding
    their Spotify IDs to Wikidata by hand."""
    history = factoids.get_history()
    con = app.session.connection().connection
    if not _table(con, 'wikidata_candidate'):
        abort(404)
    artists = {}
    for artist_id, spotify_id, name, tier, *cand in con.execute("""
            select c.artist_id, a.spotify_id, a.name, c.tier, c.qid, c.label, c.description, c.wikipedia, c.spotify_ids
            from wikidata_candidate c join artist a using(artist_id)
            where c.artist_id not in (select artist_id from artist_link)"""):
        a = artists.setdefault(artist_id, {'spotify_id': spotify_id, 'clip': spotify_id, 'name': name, 'tier': tier,
                                           'url': url_for('artist', spotify_id=spotify_id), 'candidates': [],
                                           'shows': len(history.artist_shows.get(artist_id, ()))})
        a['candidates'].append(dict(zip(('qid', 'label', 'description', 'wikipedia', 'spotify_ids'), cand)))
    rows = sorted(artists.values(), key=lambda a: (a['tier'] != 'likely', -a['shows'], a['name']))
    return _todo('wikidata_todo', rows)


@app.route('/todo/wikidata/albums')
def wikidata_album_todo():
    """Album items on Wikidata (via MusicBrainz) without our Spotify album ID (see wikidata_album_todo.py)."""
    history = factoids.get_history()
    con = app.session.connection().connection
    if not _table(con, 'wikidata_album_todo'):
        abort(404)
    rows = []
    for album_id, qid, others in con.execute("select album_id, qid, spotify_ids from wikidata_album_todo"):
        if album_id not in history.album_shows:
            continue
        wikipedia = next((h.get('wikipedia') for h in (history.album_links.get(m, {}) for m in history.album_members[album_id])
                          if h.get('wikipedia')), None)
        rows.append(_album_row(history, album_id, len(history.album_shows[album_id]),
                               clip=history.albums[album_id]['spotify_id'],
                               target={'url': f"https://www.wikidata.org/wiki/{qid}", 'text': 'Wikidata item', 'id': qid,
                                       'wikipedia': wikipedia,
                                       'others': others.split() if others else []}))
    rows.sort(key=lambda r: (-r['shows'], r['name'].lower()))
    return _todo('wikidata_album_todo', rows)


@app.route('/todo/musicbrainz/albums')
def musicbrainz_album_todo():
    """Releases matched by barcode alone (see enrich_mb_albums.py): MusicBrainz doesn't link them to Spotify yet."""
    history = factoids.get_history()
    con = app.session.connection().connection
    if not _table(con, 'mb_album'):
        abort(404)
    by_url = {history.album_group.get(a) for a, in con.execute("select album_id from mb_album where method = 'spotify-url'")}
    rows, seen = [], set()
    for album_id, mbid, title, upc in con.execute("""select m.album_id, m.release_mbid, m.rg_title, al.upc
            from mb_album m join album al using(album_id) where m.method = 'barcode'"""):
        canon = history.album_group.get(album_id)
        if canon is None or canon in by_url or canon in seen or canon not in history.album_shows:
            continue
        seen.add(canon)
        spotify_id = history.albums[album_id]['spotify_id']
        rows.append({**_album_row(history, canon, len(history.album_shows[canon])), 'spotify_id': spotify_id,
                     'clip': f"https://open.spotify.com/album/{spotify_id}",
                     'target': {'url': f"https://musicbrainz.org/release/{mbid}", 'text': f"MusicBrainz release: {title}",
                                'note': f"barcode {upc}" if upc else None}})
    rows.sort(key=lambda r: (-r['shows'], r['name'].lower()))
    return _todo('musicbrainz_album_todo', rows)


@app.route('/dj/<slug>')
def dj(slug):
    history = factoids.get_history()
    profile = history.dj_profile(slug)
    if not profile:
        abort(404)
    return render_template('dj.html', profile=profile, history=history)

@app.route('/djs')
def djs():
    return redirect(url_for('ranking', kind='djs'))

@app.route('/artists')
def artists():
    return redirect(url_for('ranking', kind='artists'))

@app.route('/heath')
def heath():
    """Show sizes and song lengths, as requested by Heath."""
    history = factoids.get_history()
    return render_template('heath.html', L=history.lengths(), B=history.back_to_back(), history=history)

@app.template_filter('mmss')
def mmss(ms):
    s = round(ms / 1000)
    return f"{s // 60}:{s % 60:02d}"

@app.route('/rankings/novelty')
def novelty():
    history = factoids.get_history()
    return render_template('novelty.html', series=history.novelty_series(), history=history,
                           highlight=request.args.get('h'), rankings=factoids.RANKINGS + ['novelty'], kind='novelty')

@app.route('/rankings/<kind>')
def ranking(kind):
    highlight = request.args.get('h')
    history = factoids.get_history()
    table = history.ranking(kind, highlight=highlight) if kind in factoids.RANKINGS else None
    if not table:
        abort(404)
    return render_template('ranking.html', kind=kind, table=table, highlight=highlight, history=history,
                           rankings=factoids.RANKINGS + ['novelty'])


@app.route('/playlist/<date_str>')
def show_playlist(date_str):
    try:
        playlist_date = date.fromisoformat(date_str)
    except ValueError:
        abort(404)
    history = factoids.get_history()
    pid = history.pid_by_date.get(playlist_date)
    if pid is None:
        abort(404)
    prev_date, next_date = history.neighbors(pid)
    ordered = sorted(history.show_tracks, key=history.show_dates.get)
    return render_template("playlist.html", date=playlist_date, show=history.shows[pid],
                           number=ordered.index(pid) + 1, n_shows=len(ordered), caption=history.show_caption(pid),
                           rows=history.show_rows(pid),
                           room=history.show_room(pid),
                           stats=history.show_stats(pid),
                           mix=history.show_mix(pid),
                           facts=history.show_factoids(pid),
                           prev_date=prev_date, next_date=next_date)

@app.route('/image/<date_str>')
def playlist_image(date_str):
    try:
        playlist_date = date.fromisoformat(date_str)
    except ValueError:
        return "Invalid date format", 400
    images_dir = os.path.join(app.static_folder, 'images')

    # Tiles ask for ?size=tile: a small square copy (make_tiles.py), when there is one
    if request.args.get('size') == 'tile' and os.path.exists(make_tiles.tile_path(date_str)):
        response = make_response(send_from_directory(make_tiles.TILES, f"{date_str}.webp"))
        response.cache_control.max_age = 86400 * 30
        return response

    # First, a local file named for the date
    for ext in ['.jpg', '.png', '.jpeg', '.webp']:
        filename = f"{date_str}{ext}"
        if os.path.exists(os.path.join(images_dir, filename)):
            response = make_response(send_from_directory(images_dir, filename))
            response.cache_control.max_age = 86400 * 30  # Cache for 30 days
            return response

    # Second, the original Spotify image; the page falls back to the placeholder if it's gone
    history = factoids.get_history()
    pid = history.pid_by_date.get(playlist_date)
    if pid is not None and history.shows[pid]['image_url']:
        return redirect(history.shows[pid]['image_url'])

    # Finally, the generic placeholder
    if os.path.exists(os.path.join(images_dir, 'vinyl-placeholder.png')):
        response = make_response(send_from_directory(images_dir, 'vinyl-placeholder.png'))
        response.cache_control.max_age = 86400 * 30
        return response
    abort(404)

def _radio_set(args, seed):
    """The set a /radio URL describes: start from an artist, show or track (Spotify id / date), else at random."""
    history = factoids.get_history()
    adventure = min(100, max(0, args.get('adventure', type=int, default=30)))
    n = min(60, max(5, args.get('n', type=int, default=20)))
    start = start_label = start_url = lean = None
    start_kind = 'random'
    if args.get('artist'):
        artist = next((a for a in history.artists.values() if a['spotify_id'] == args['artist']), None)
        if artist:
            start, start_label = radio.start_for_artist(artist['artist_id']), artist['name']
            lean = ['artist', artist['artist_id']]
            start_url = url_for('artist', spotify_id=artist['spotify_id'])
            start_kind = 'artist'
    elif args.get('track'):
        cat = radio.catalog()
        start = cat.rec_by_spotify.get(args['track'])
        if start is not None:
            start_label = f"{cat.artist_names(start)}, {cat.title(start)}"
            start_kind = 'track'
    elif args.get('genre') and args['genre'] in history.genre_artists:
        start = radio.start_for_genre(args['genre'], random.Random(seed))
        if start is not None:
            start_label, start_kind = args['genre'], 'genre'
            start_url = url_for('genre', genre_name=args['genre'])
    elif args.get('dj') and args['dj'] in history.dj_by_slug:
        dj_id = history.dj_by_slug[args['dj']]
        # someone in the room nearly every week leans toward nothing in particular: then it's the whole archive
        start = radio.start_for_lean(('dj', dj_id), random.Random(seed))
        start_label, start_kind = f"{history.djs[dj_id]['name']}'s nights", 'dj'
        start_url = url_for('dj', slug=args['dj'])
        lean = ['dj', dj_id] if start is not None else None
    elif args.get('album') and args['album'] in history.album_by_spotify:
        canon = history.album_by_spotify[args['album']]
        start = radio.start_for_lean(('album', canon), random.Random(seed))
        start_label, start_kind = history.albums[canon]['name'], 'album'
        start_url = url_for('album', spotify_id=history.albums[canon]['spotify_id'])
        lean = ['album', canon] if start is not None else None
    elif args.get('length') == 'long':
        start = radio.start_for_lean(('length', 'long'), random.Random(seed))
        start_label, start_kind, start_url = 'six-minute songs', 'length', url_for('heath') + '#six'
        lean = ['length', 'long'] if start is not None else None
    elif args.get('show'):
        try:
            pid = history.pid_by_date.get(date.fromisoformat(args['show']))
        except ValueError:
            pid = None
        if pid is not None:
            d = date.fromisoformat(args['show'])
            start, start_label = radio.start_for_show(pid, random.Random(seed)), f"the show of {d:%B} {d.day}, {d.year}"
            lean = ['show', pid]
            start_url = url_for('show_playlist', date_str=args['show'])
            start_kind = 'show'
    if start is None:
        start = radio.random_start(random.Random(seed))
    genre = args['genre'] if start_kind == 'genre' else None
    if genre:
        lean = ['genre', genre]
    picks = radio.sequence(start, n, adventure / 100, seed, lean=tuple(lean) if lean else None)
    for p in picks:
        p['spotify_id'] = p['track']['spotify_url'].rsplit('/', 1)[-1]
    start_label = start_label or f"{', '.join(a['name'] for a in picks[0]['artists'])}, “{picks[0]['track']['name']}”"
    if not start_url and picks[0]['artists'][0]['spotify_id']:
        start_url = url_for('artist', spotify_id=picks[0]['artists'][0]['spotify_id'])
    return {'picks': picks, 'start_label': start_label, 'start_url': start_url, 'start_kind': start_kind, 'genre': genre, 'lean': lean,
            'adventure': adventure, 'n': n, 'seed': seed}


@app.route('/radio')
def radio_page():
    """A proposed set from the sequencer, e.g. /radio?artist=<spotify id>&adventure=30&seed=123"""
    if 'seed' not in request.args:  # pin the randomness in the URL, so a set can be revisited and shared
        return redirect(url_for('radio_page', **request.args, seed=random.randrange(10 ** 6)))
    s = _radio_set(request.args, request.args.get('seed', type=int, default=0))
    minutes = sum(p['track']['duration_ms'] or 0 for p in s['picks']) // 60000
    return render_template('radio.html', **s, minutes=minutes, spotify_ready=_spotify_configured(),
                           saved=session.pop('saved_playlist', None) if app.secret_key else None,
                           save_error=session.pop('save_error', None) if app.secret_key else None)


@app.route('/radio/set')
def radio_set():
    """The same set as JSON, for the player: {'label', 'picks': [{'spotify_id', 'ms', 'html'}]}"""
    s = _radio_set(request.args, request.args.get('seed', type=int, default=random.randrange(10 ** 6)))
    return jsonify({'label': s['start_label'], 'genre': s['genre'], 'lean': s['lean'], 'picks': [_pick_json(p) for p in s['picks']]})


def _lean(body):
    """The (kind, key) a set leans toward, as the player sends it back: ['show', 123], ['genre', 'shoegaze'], ..."""
    lean = body.get('lean') or (['genre', body['genre']] if body.get('genre') else None)
    history = factoids.get_history()
    if not lean or len(lean) != 2:
        return None
    kind, key = lean
    ok = {'genre': lambda: key in history.genre_artists, 'show': lambda: key in history.show_tracks,
          'artist': lambda: key in history.artist_shows, 'dj': lambda: key in history.djs,
          'length': lambda: key == 'long', 'album': lambda: key in history.album_shows}
    return (kind, key) if kind in ok and ok[kind]() else None


def _pick_json(p):
    return {'spotify_id': p['spotify_id'], 'ms': p['track']['duration_ms'] or 0,
            'html': render_template('_radio_pick.html', p=p)}


# playing in the page (Web Playback SDK, Premium only) and saving sets as playlists
# ...plus ♥ (Liked Songs) and ＋ (adding to a playlist of your own you've chosen as your queue)
SPOTIFY_SCOPES = ('streaming user-read-email user-read-private user-read-playback-state '
                  'user-modify-playback-state playlist-modify-private playlist-modify-public '
                  'playlist-read-private playlist-read-collaborative user-library-read user-library-modify')


def _spotify_configured():
    return bool(app.secret_key and os.environ.get('SPOTIPY_CLIENT_ID') and os.environ.get('SPOTIPY_CLIENT_SECRET'))


def _spotify_token():
    """The listener's Spotify token info, refreshed if needed, or None."""
    if not _spotify_configured():
        return None
    try:
        auth = _spotify_auth()
        return auth.validate_token(auth.cache_handler.get_cached_token())
    except Exception:
        app.logger.exception('refreshing Spotify token failed')
        return None


def _local_path(url, default):
    """url if it points into this site, else default."""
    return url if url and url.startswith('/') and not url.startswith('//') else default


def _spotify_auth():
    from spotipy.cache_handler import FlaskSessionCacheHandler
    from spotipy.oauth2 import SpotifyOAuth
    return SpotifyOAuth(scope=SPOTIFY_SCOPES,
                        redirect_uri=os.environ.get('SPOTIFY_REDIRECT_URI') or url_for('spotify_callback', _external=True),
                        cache_handler=FlaskSessionCacheHandler(session), show_dialog=False)


def _send_to_spotify(auth):
    """Off to Spotify to log in, with a fresh random state that the callback must see again: a callback with any
    other state (someone else's link, say) is refused, so nobody can log a friend in to their own account."""
    session['oauth_state'] = secrets.token_urlsafe(24)
    return redirect(auth.get_authorize_url(state=session['oauth_state']))


def _save_pending_playlist(auth):
    import spotipy
    pending = session.pop('pending_playlist', None)
    if not pending:
        return
    try:
        sp = spotipy.Spotify(auth_manager=auth)
        playlist = sp.current_user_playlist_create(pending['name'], public=False, description=pending['description'])
        sp.playlist_add_items(playlist['id'], [f"spotify:track:{t}" for t in pending['tracks']])
        session['saved_playlist'] = {'name': pending['name'], 'url': playlist['external_urls']['spotify']}
    except Exception as e:
        app.logger.exception('saving playlist to Spotify failed')
        session['save_error'] = str(e)
    return pending['return_to']


@app.route('/radio/save', methods=['POST'])
def radio_save():
    if not _spotify_configured():
        abort(404)
    return_to = _local_path(request.form.get('return_to'), url_for('radio_page'))
    tracks = [t for t in request.form.get('tracks', '').split(',') if t.isalnum()][:100]
    session['pending_playlist'] = {'name': (request.form.get('name') or 'Birds radio')[:100], 'tracks': tracks,
                                   'description': f"Sequenced from Conference of the Birds history. {request.url_root.rstrip('/')}{return_to}"[:300],
                                   'return_to': return_to}
    auth = _spotify_auth()
    if auth.validate_token(auth.cache_handler.get_cached_token()):
        return redirect(_save_pending_playlist(auth))
    return _send_to_spotify(auth)


@app.route('/spotify/login')
def spotify_login():
    if not _spotify_configured():
        abort(404)
    session['after_login'] = _local_path(request.args.get('next'), url_for('radio_page'))
    return _send_to_spotify(_spotify_auth())


@app.route('/spotify/logout')
def spotify_logout():
    session.pop('token_info', None)
    return redirect(_local_path(request.args.get('next'), url_for('radio_page')))


@app.route('/spotify/token')
def spotify_token():
    """A current access token for the in-page player."""
    token = _spotify_token()
    if not token:
        return jsonify({'error': 'not logged in'}), 401
    return jsonify({'access_token': token['access_token']})


@app.route('/spotify/callback')
def spotify_callback():
    if not _spotify_configured():
        abort(404)
    auth = _spotify_auth()
    expected, state = session.pop('oauth_state', None), request.args.get('state') or ''
    if not expected or not secrets.compare_digest(expected.encode(), state.encode()):
        session.pop('pending_playlist', None)
        session['save_error'] = "that Spotify login didn't start here. Please log in again."
        return redirect(session.pop('after_login', None) or url_for('radio_page'))
    if request.args.get('code'):
        auth.get_access_token(request.args['code'], check_cache=False)
        return redirect(_save_pending_playlist(auth) or session.pop('after_login', None) or url_for('radio_page'))
    pending = session.pop('pending_playlist', None)
    session['save_error'] = f"Spotify didn't connect ({request.args.get('error', 'no code returned')})."
    return redirect(pending['return_to'] if pending else session.pop('after_login', None) or url_for('radio_page'))


@app.route('/radio/more', methods=['POST'])
def radio_more():
    """Picks to follow what's playing: {'after': spotify id, 'played': [spotify ids], 'adventure', 'n', 'seed'}
    -> {'picks': [{'spotify_id', 'html'}]}"""
    body = request.get_json(silent=True) or {}
    cat = radio.catalog()
    start = cat.rec_by_spotify.get(body.get('after'))
    if start is None:
        abort(400)
    played = [cat.rec_by_spotify[i] for i in body.get('played', []) if i in cat.rec_by_spotify] or [start]
    adventure = min(100, max(0, int(body.get('adventure', 30)))) / 100
    n, seed = min(40, max(1, int(body.get('n', 10)))), int(body.get('seed', 0))
    if body.get('random'):  # jump somewhere new, then carry on from there
        rng = random.Random(seed)
        jump = rng.choice([r for r in cat.all_recs if r not in set(played)])
        first = radio.sequence(jump, 1, adventure, seed)
        first[0]['reason'] = 'a random new start'
        picks = first + radio.sequence(jump, n - 1, adventure, seed, played=[*played, jump])
    else:
        picks = radio.sequence(start, n, adventure, seed, played=played, lean=_lean(body))
    for p in picks:
        p['spotify_id'] = p['track']['spotify_url'].rsplit('/', 1)[-1]
    return jsonify({'picks': [_pick_json(p) for p in picks]})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = bool(os.environ.get('FLASK_DEBUG', False))
    app.run(debug=debug, host='0.0.0.0', port=port)

    