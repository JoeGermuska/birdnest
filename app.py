from flask import Flask, request, render_template, _app_ctx_stack, abort
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from models import Artist, Database, Genre, Playlist
from datetime import date
from collections import Counter

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
