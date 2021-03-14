from flask import Flask, render_template, _app_ctx_stack
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from models import Playlist
from datetime import date

app = Flask(__name__,
    static_folder='static'
    )

# from https://towardsdatascience.com/use-flask-and-sqlalchemy-not-flask-sqlalchemy-5a64fafe22a4
SQLALCHEMY_DATABASE_URL = 'sqlite:///birdnest.db'
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={
    "check_same_thread": False
})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

app.session = scoped_session(SessionLocal,
scopefunc=_app_ctx_stack.__ident_func__)
@app.route('/')
def index():
    p = app.session.query(Playlist).order_by(Playlist.date.desc()).first()
    return render_template("playlist.html", playlist=p)

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
