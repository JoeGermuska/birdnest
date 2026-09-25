from flask import Flask, request, render_template, abort, send_from_directory, redirect, make_response, jsonify, url_for
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from models import Artist, Database, Genre, Playlist
import factoids
import genre_families
from datetime import date
from collections import Counter
import os
import json
from urllib.parse import urlparse 

app = Flask(__name__,
    static_folder='static'
    )

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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = bool(os.environ.get('FLASK_DEBUG', False))
    app.run(debug=debug, host='0.0.0.0', port=port)

    