from flask import Flask, request, render_template, abort, send_from_directory, redirect
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from models import Artist, Database, Genre, Playlist
from datetime import date
from collections import Counter
import os
import json
import requests
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
@app.route('/')
def index():
    playlists = app.session.query(Playlist).order_by(Playlist.date.desc()).all()
    return render_template("index.html", playlists=playlists)

@app.route('/search')
def search():
    db = Database()
    terms = request.args.get('q')
    if terms:
        tracks = db.search_tracks(app.session, terms)
    else:
        tracks = None
    return render_template("search_results.html", tracks=tracks, terms=terms)

@app.route('/genre/<genre_name>')
def genre(genre_name):
    genre_obj = app.session.query(Genre).filter(Genre.name == genre_name).first()
    if not genre_obj:
        abort(404)
    genre_obj.artists.sort(key=lambda a: -1 * a.popularity) # reverse popularity sort
    return render_template('genre.html',genre_name=genre_name,genre_obj=genre_obj)

@app.route('/genres')
def genres():
    genres = sorted(set(x.name for x in app.session.query(Genre).order_by(Genre.name)))
    return render_template('genres.html', genres=genres)

@app.route('/artist/<spotify_id>')
def artist(spotify_id):
    artist = app.session.query(Artist).filter(Artist.spotify_id == spotify_id).first()
    if not artist:
        abort(404)
    return render_template('artist.html',artist=artist)

@app.route('/artists')
def artists():
    artists = app.session.query(Artist).all()
    from collections import Counter
    artist_count = ((a.name,len(a.tracks)) for a in artists)
    return render_template("artists.html",artist_count_json=json.dumps(artist_count))


@app.route('/playlist/<date_str>')
def show_playlist(date_str):
    try:
        (year,month,day) = map(int,date_str.split('-',3))
        playlist_date = date(year,month,day)
    except Exception:
        return "Invalid playlist URL", 400 
    playlist = app.session.query(Playlist).filter(Playlist.date == playlist_date).scalar()
    if playlist is None:
        return f"No playlist for {date_str}", 404
    return render_template("playlist.html", playlist=playlist)

@app.route('/image/<date_str>')
def playlist_image(date_str):
    try:
        (year,month,day) = map(int,date_str.split('-',3))
        playlist_date = date(year,month,day)
    except Exception:
        return "Invalid date format", 400
    
    playlist = app.session.query(Playlist).filter(Playlist.date == playlist_date).scalar()
    
    # First, try the original Spotify image
    if playlist and playlist.image_url:
        try:
            response = requests.head(playlist.image_url, timeout=5)
            if response.status_code == 200:
                return redirect(playlist.image_url)
        except:
            pass
    
    # Second, try date-based local file (YYYY-MM-DD format)
    for ext in ['.jpg', '.png', '.jpeg', '.webp']:
        filename = f"{date_str}{ext}"
        file_path = os.path.join(app.static_folder, 'images', filename)
        if os.path.exists(file_path):
            return send_from_directory(os.path.join(app.static_folder, 'images'), filename)
    
    # Finally, serve the generic placeholder
    placeholder_path = os.path.join(app.static_folder, 'images', 'vinyl-placeholder.png')
    if os.path.exists(placeholder_path):
        return send_from_directory(os.path.join(app.static_folder, 'images'), 'vinyl-placeholder.png')
    
    # If no placeholder exists, return a 404
    abort(404)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = bool(os.environ.get('FLASK_DEBUG', False))
    app.run(debug=debug, host='0.0.0.0', port=port)

    