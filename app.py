from flask import Flask, request, render_template, abort, send_from_directory, redirect, make_response, jsonify, url_for, session
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from models import Artist, Database, Genre, Playlist
import factoids
import radio
import genre_families
from datetime import date
from collections import Counter
import os
import random
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
@app.route('/')
def index():
    history = factoids.get_history()
    playlists = sorted(history.shows.values(), key=lambda s: s['date'], reverse=True)
    return render_template("index.html", playlists=playlists)

@app.route('/search')
def search():
    terms = (request.args.get('q') or '').strip()
    tracks, artists, genres = [], [], []
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
    return render_template("search_results.html", tracks=tracks, artists=artists, genres=genres, terms=terms)

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

@app.route('/todo/wikidata')
def wikidata_todo():
    """Unlisted: probable Wikidata items for unlinked artists (see wikidata_candidates.py), for adding
    their Spotify IDs to Wikidata by hand."""
    history = factoids.get_history()
    con = app.session.connection().connection
    if not con.execute("select 1 from sqlite_master where name = 'wikidata_candidate'").fetchone():
        abort(404)
    artists = {}
    for artist_id, spotify_id, name, tier, *cand in con.execute("""
            select c.artist_id, a.spotify_id, a.name, c.tier, c.qid, c.label, c.description, c.wikipedia, c.spotify_ids
            from wikidata_candidate c join artist a using(artist_id)
            where c.artist_id not in (select artist_id from artist_link)"""):
        a = artists.setdefault(artist_id, {'spotify_id': spotify_id, 'name': name, 'tier': tier, 'candidates': [],
                                           'shows': len(history.artist_shows.get(artist_id, ()))})
        a['candidates'].append(dict(zip(('qid', 'label', 'description', 'wikipedia', 'spotify_ids'), cand)))
    rows = sorted(artists.values(), key=lambda a: (a['tier'] != 'likely', -a['shows'], a['name']))
    return render_template('wikidata_todo.html', rows=rows)

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
        return "Invalid playlist URL", 400
    history = factoids.get_history()
    pid = history.pid_by_date.get(playlist_date)
    if pid is None:
        return f"No playlist for {date_str}", 404
    prev_date, next_date = history.neighbors(pid)
    return render_template("playlist.html", date=playlist_date, show=history.shows[pid],
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

@app.route('/radio')
def radio_page():
    """A proposed set from the sequencer, e.g. /radio?artist=<spotify id>&adventure=30&seed=123"""
    history = factoids.get_history()
    args = request.args
    if 'seed' not in args:  # pin the randomness in the URL, so a set can be revisited and shared
        return redirect(url_for('radio_page', **args, seed=random.randrange(10 ** 6)))
    seed = args.get('seed', type=int, default=0)
    adventure = min(100, max(0, args.get('adventure', type=int, default=30)))
    n = min(60, max(5, args.get('n', type=int, default=20)))
    start = start_label = start_url = None
    start_kind = 'random'
    if args.get('artist'):
        artist = next((a for a in history.artists.values() if a['spotify_id'] == args['artist']), None)
        if artist:
            start, start_label = radio.start_for_artist(artist['artist_id']), artist['name']
            start_url = url_for('artist', spotify_id=artist['spotify_id'])
            start_kind = 'artist'
    elif args.get('track'):
        cat = radio.catalog()
        start = cat.rec_by_spotify.get(args['track'])
        if start is not None:
            start_label = f"{cat.artist_names(start)}, {cat.title(start)}"
            start_kind = 'track'
    elif args.get('show'):
        try:
            pid = history.pid_by_date.get(date.fromisoformat(args['show']))
        except ValueError:
            pid = None
        if pid is not None:
            d = date.fromisoformat(args['show'])
            start, start_label = radio.start_for_show(pid), f"the show of {d:%B} {d.day}, {d.year}"
            start_url = url_for('show_playlist', date_str=args['show'])
            start_kind = 'show'
    if start is None:
        start = radio.random_start(random.Random(seed))
    picks = radio.sequence(start, n, adventure / 100, seed)
    for p in picks:
        p['spotify_id'] = p['track']['spotify_url'].rsplit('/', 1)[-1]
    start_label = start_label or f"{', '.join(a['name'] for a in picks[0]['artists'])}, “{picks[0]['track']['name']}”"
    if not start_url and picks[0]['artists'][0]['spotify_id']:
        start_url = url_for('artist', spotify_id=picks[0]['artists'][0]['spotify_id'])
    minutes = sum(p['track']['duration_ms'] or 0 for p in picks) // 60000
    return render_template('radio.html', picks=picks, start_label=start_label, start_url=start_url, start_kind=start_kind,
                           adventure=adventure, n=n, seed=seed,
                           minutes=minutes, spotify_ready=_spotify_configured(),
                           logged_in=bool(_spotify_token()),
                           saved=session.pop('saved_playlist', None) if app.secret_key else None,
                           save_error=session.pop('save_error', None) if app.secret_key else None)


# playing in the page (Web Playback SDK, Premium only) and saving sets as playlists
SPOTIFY_SCOPES = ('streaming user-read-email user-read-private user-read-playback-state '
                  'user-modify-playback-state playlist-modify-private')


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
    return redirect(auth.get_authorize_url())


@app.route('/spotify/login')
def spotify_login():
    if not _spotify_configured():
        abort(404)
    session['after_login'] = _local_path(request.args.get('next'), url_for('radio_page'))
    return redirect(_spotify_auth().get_authorize_url())


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
        picks = radio.sequence(start, n, adventure, seed, played=played)
    out = []
    for p in picks:
        p['spotify_id'] = p['track']['spotify_url'].rsplit('/', 1)[-1]
        out.append({'spotify_id': p['spotify_id'], 'html': render_template('_radio_pick.html', p=p)})
    return jsonify({'picks': out})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = bool(os.environ.get('FLASK_DEBUG', False))
    app.run(debug=debug, host='0.0.0.0', port=port)

    