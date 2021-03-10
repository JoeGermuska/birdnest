from sqlalchemy import create_engine, ForeignKey, Column, Table
from sqlalchemy import Integer, Float, Date, String, Boolean, JSON
from sqlalchemy.orm import relation, sessionmaker, relationship, backref
from sqlalchemy.ext.declarative import declarative_base
import re
from datetime import date
from spotclient import Client

PLAYLIST_DATE_PATTERN = re.compile('^.*(?P<year>20\d{2})-(?P<month>\d{2})-(?P<day>\d{2}).*$')

def get_session(sqlite_filepath='birdnest.db',create_all=True):
    engine = create_engine(f"sqlite:///{sqlite_filepath}")
    if create_all:
        Base.metadata.create_all(engine)
    sessionfactory = sessionmaker()
    sessionfactory.configure(bind=engine)
    return sessionfactory()

class Database(object):
    engine = None
    api_client = None

    def __init__(self,sqlite_filepath='birdnest.db',create_all=True):
        self.api_client = Client()

    def insert_playlist_from_json(self,session, j):
        """Given JSON matching Spotify's PlaylistObject, fully update the database.
           Note that "fully" is fluid. There is more data available from the API than is available
           in the input to this param. Should this make further calls? Or leave that for another
           process?
        """
        # consider replace/overwrite/error semantics
        playlist = Playlist.get_or_create(session,j['id'])
        playlist.name = j['name']
        playlist.description = j['description']
        try:
            playlist.images = j['images']
        except KeyError: pass

        if match := PLAYLIST_DATE_PATTERN.match(playlist.name): # := walrus requires Python 3.8
            playlist.date = date(*(map(int,match.groups())))

        for t in self.api_client.playlist_tracks(j['id'],full=True):
            playlist.tracks.append(self.insert_track_from_json(session,t))

        session.add(playlist)

        # fill in artists
        artist_ids = set()
        for t in playlist.tracks:
            for a in t.artists:
                artist_ids.add(a.spotify_id)
        self.fill_in_artists(session,artist_ids)

        self.fill_in_audio_features(session, playlist.tracks)

        return playlist

    def fill_in_artists(self, session, artist_ids):
        artists = []
        for a in self.api_client.artists(artist_ids):
            artist = self.insert_artist_from_json(session, a)
            artists.append(artist)
        return artists

    def fill_in_audio_features(self, session, tracks):
        d = dict((t.spotify_id, t) for t in tracks if t.features is None)
        features = self.api_client.audio_features(d.keys())
        for f in features:
            track = d[f['id']]
            f['spotify_id'] = f['id']

            # get rid of the stuff that doesnt' map to a property of AudioFeatures
            for k in ['id', 'spotify_id', 'type', 'uri', 'track_href', 'duration_ms']:
                del f[k]

            af = AudioFeatures(**f)
            track.features = af
            session.add(track)

    def insert_track_from_json(self, session, t):
        track = Track.get_or_create(session,t['id'])
        track.name = t['name']
        track.duration_ms = t['duration_ms']
        track.explicit = t['explicit']
        track.popularity = t['popularity']
        track.isrc_id = t['external_ids'].get('isrc')
        track.spotify_url = t['external_urls'].get('spotify')
        track.preview_url = t['preview_url']
        for a in t['artists']: # what if they're already there?
            artist = self.insert_artist_from_json(session,a)
            if artist not in track.artists:
                track.artists.append(artist)
        return track

    def insert_artist_from_json(self, session, a):
        artist = Artist.get_or_create(session, a['id'])
        artist.name = a['name']
        try:
            artist.popularity = a['popularity']
        except KeyError: pass
        try:
            artist.followers = a['followers'].get('total')
        except KeyError: pass

        try:
            artist.images = a['images']
        except KeyError: pass

        try:
            for g in a['genres']:
                genre = Genre.get_or_create(session, g)
                if genre not in artist.genres:
                    artist.genres.append(genre)
        except KeyError: pass
        session.add(artist)
        return artist
        

Base = declarative_base()

# class PersistentSpotifyObject(Base):
#     # for this to work, we need PersistentSpotifyObject to see __tablename__ from subclasses
#     @classmethod
#     def get_or_create(cls, session, spotify_id):
#         o = session.query(cls).filter_by(spotify_id=spotify_id).scalar()
#         if o is None:
#             o = cls(spotify_id=spotify_id)
#         return o    


playlist_track = Table( "playlist_track", Base.metadata, Column("playlist_id", Integer, ForeignKey("playlist.playlist_id")), Column("track_id", Integer, ForeignKey("track.track_id")))

track_artist = Table("track_artist", Base.metadata, Column("track_id", Integer, ForeignKey("track.track_id")), Column("artist_id", Integer, ForeignKey("artist.artist_id")))

artist_genre = Table('artist_genre', 
                     Base.metadata, 
                     Column("artist_id", Integer, ForeignKey("artist.artist_id")), 
                     Column("genre_id", Integer, ForeignKey("genre.genre_id"))
                )

class Playlist(Base):
    __tablename__ = 'playlist'
    playlist_id = Column(Integer, primary_key=True)
    spotify_id = Column(String)
    name = Column(String)
    description = Column(String)
    date = Column(Date) # not a spotify property, we have to infer from name
    tracks = relationship("Track", secondary=playlist_track, back_populates="playlists")
    images = Column(JSON)
    # external_urls = String[]
    # followers = FollowersObject
    # uri = Column(String) # computable: 'spotify:playlist:${spotify_id}'

    def __repr__(self) -> str:
        return f"Playlist({self.spotify_id})"
    
    def __str__(self) -> str:
        return f"{self.name} (Playlist)"

    @staticmethod
    def get_or_create(session, spotify_id):
        o = session.query(Playlist).filter_by(spotify_id=spotify_id).scalar()
        if o is None:
            o = Playlist(spotify_id=spotify_id)
        return o

class Artist(Base):
    __tablename__ = 'artist'
    artist_id = Column(Integer, primary_key=True)
    name = Column(String)
    spotify_id = Column(String)
    spotify_url = Column(String)
    images = Column(JSON)
    # uri = Column(String) # computable: 'spotify:artist:${spotify_id}'
    popularity = Column(Integer) # changes over time
    # followers is in a struct with a nullable URL (when is it not null?)
    followers = Column(Integer) # changes over time

    genres = relationship('Genre', secondary=artist_genre, back_populates='artists')
    # external_urls: String[] - spotify among others. 
    # Spotify can be computed: https://open.spotify.com/artist/${spotify_id}
    # images: {url,w,h}[]
    # m2m:
    tracks = relationship(
        "Track", secondary=track_artist, back_populates="artists"
    )

    def __repr__(self) -> str:
        return f"Artist({self.spotify_id})"
    
    def __str__(self) -> str:
        return f"{self.name} (Artist)"

    @staticmethod
    def get_or_create(session, spotify_id):
        o = session.query(Artist).filter_by(spotify_id=spotify_id).scalar()
        if o is None:
            o = Artist(spotify_id=spotify_id)
        return o


class Track(Base):
# https://developer.spotify.com/documentation/web-api/reference/#object-trackobject
    __tablename__ = 'track'
    track_id = Column(Integer, primary_key=True)
    spotify_id = Column(String)
    name = Column(String)
    duration_ms = Column(Integer)
    explicit = Column(Boolean)
    popularity = Column(Integer)
    isrc_id = Column(String)
    spotify_url = Column(String)
    preview_url = Column(String)
    features = relationship('AudioFeatures', uselist=False, back_populates='track')
    artists = relationship('Artist', secondary=track_artist, back_populates='tracks')
    playlists = relationship('Playlist', secondary=playlist_track, back_populates='tracks')

    def artists_str(self) -> str:
        return ','.join(a.name for a in self.artists)    

    def __repr__(self) -> str:
        return f"Track({self.spotify_id})"
    
    def __str__(self) -> str:
        return f"{self.name} by {self.artists_str}"

    @staticmethod
    def get_or_create(session, spotify_id):
        o = session.query(Track).filter_by(spotify_id=spotify_id).scalar()
        if o is None:
            o = Track(spotify_id=spotify_id)
        return o

class Genre(Base):
    __tablename__ = 'genre'
    genre_id = Column(Integer, primary_key=True)
    name = Column(String)
    artists = relationship('Artist', secondary=artist_genre, back_populates='genres')

    @staticmethod
    def get_or_create(session, name):
        o = session.query(Genre).filter_by(name=name).scalar()
        if o is None:
            o = Genre(name=name)
        return o

class AudioFeatures(Base):
    # https://developer.spotify.com/documentation/web-api/reference/#object-audiofeaturesobject
    __tablename__ = 'audio_features'
    features_id = Column(Integer, primary_key=True)
    track_id = Column(Integer, ForeignKey('track.track_id'))
    track = relationship('Track', uselist=False, back_populates="features")
    analysis_url = Column(String)
    key = Column(Integer)
    mode = Column(Integer) 
    tempo  = Column(Integer)
    time_signature = Column(Integer)
    acousticness = Column(Float)
    danceability = Column(Float)
    energy = Column(Float)
    instrumentalness = Column(Float)
    liveness = Column(Float)
    loudness = Column(Float)
    speechiness = Column(Float)
    valence = Column(Float)
