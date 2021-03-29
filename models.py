# https://docs.sqlalchemy.org/en/13/orm/tutorial.html#querying
from sqlalchemy import create_engine, text, ForeignKey, Column, Table
from sqlalchemy import Integer, Float, Date, String, Boolean, JSON
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.ext.declarative import declarative_base
import re
import json
from datetime import date

from sqlalchemy.orm.base import attribute_str
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

        playlist.spotify_url = j['external_urls'].get('spotify')

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
        artist = Artist.get_or_create(session, a['id'],init_data=a)
        session.add(artist)
        return artist
        
    def search_tracks(self, session, query):
        sql = """select t.* from 
        track t, track_search ts 
        where t.track_id = ts.track_id
        and track_search match :terms"""
        return session.query(Track).from_statement(text(sql)).params(terms=query).all()


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

album_track =  Table("album_track", Base.metadata, Column("album_id", Integer, ForeignKey("album.album_id")), Column("track_id", Integer, ForeignKey("track.track_id")))

album_artist = Table("album_artist", Base.metadata, Column("album_id", Integer, ForeignKey("album.album_id")), Column("artist_id", Integer, ForeignKey("artist.artist_id")))



class Playlist(Base):
    __tablename__ = 'playlist'
    playlist_id = Column(Integer, primary_key=True)
    spotify_id = Column(String)
    spotify_url = Column(String)
    name = Column(String)
    description = Column(String)
    date = Column(Date) # not a spotify property, we have to infer from name
    tracks = relationship("Track", secondary=playlist_track, back_populates="playlists", lazy='joined')
    images = Column(JSON)
    # external_urls = String[]
    # followers = FollowersObject
    # uri = Column(String) # computable: 'spotify:playlist:${spotify_id}'

    def __repr__(self) -> str:
        return f"Playlist({self.spotify_id})"
    
    def __str__(self) -> str:
        return f"{self.name} (Playlist)"

# Index(['date', 'playlist_name', 'playlist_id', 'name', 
# 'artist', 'duration_ms',
#        'features_id', 'track_id', 'analysis_url', 'key', 'mode', 'tempo',
#        'time_signature', 'acousticness', 'danceability', 'energy',
#        'instrumentalness', 'liveness', 'loudness', 'speechiness', 'valence'],
#       dtype='object')



    def to_json(self, as_object=False):
        """Produce a JSON representation (String) of the data in this playlist 
        as an array of track-plus objects suitable for use with Vega or Altair.
        Optionally, specify `as_object=True` to have the data returned before
        being serialized to a String.
        """
        j = []
        cum_ms = 0
        playlist_json = {
            'date': f"{self.date.year}-{self.date.month:02}-{self.date.day:02}",
            'year': self.date.year,
            'month': self.date.month,
            'day': self.date.day,
        }
        for t in self.tracks:
            track_json = t.to_json(True)
            track_json.update(playlist_json)
            track_json['start_time_ms'] = cum_ms
            cum_ms += t.duration_ms
            j.append(track_json)
        if as_object:
            return j
        return json.dumps(j)

    @property
    def image_url(self):
        # spotify is inconsistent -- artist images have various dimensions. playlists have
        # a single image URL with undefined dimensions. For now we'll bank on only one.
        if self.images:
            return self.images[0]['url']
        return None

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

    albums = relationship('Album', secondary=album_artist, back_populates='artists')

    def image_url(self,pixels=None,max_size=None,min_size=None):
        """Return an image URL from this object's set of images. If pixels
        is not None, it should be an integer, and the returned image URL will be an exact match for 
        that pixel size. If there is no exact match, None will be returned.  If pixels is not set but 
        either max_size or min_size are set, then the URL for the largest image which fits the 
        constraints will be returned.
        If no kwargs are set, the first image URL found will be 
        returned"""
        images = [(i['width'],i['url']) for i in self.images]
        if pixels:
            if pixels in dict(images):
                img_dict = dict(images)
                return img_dict[pixels]
            return None
        if not max_size and not min_size:
            return images[0][1]

        for width, url in reversed(sorted(images)):
            if max_size and min_size:
                if width <= max_size and width >= min_size:
                    return url
            elif max_size and width <= max_size:
                return url
            elif min_size and width >= min_size:
                return url

        return None

    def __repr__(self) -> str:
        return f"Artist({self.spotify_id})"
    
    def __str__(self) -> str:
        return f"{self.name} (Artist)"

    @staticmethod
    def get_or_create(session, spotify_id, init_data=None):
        o = session.query(Artist).filter_by(spotify_id=spotify_id).scalar()
        if o is None:
            o = Artist(spotify_id=spotify_id)
        if init_data is None:
            return o
        # treat init_data as a SpotifyAPI ArtistObject (or simplified, be careful)
        o.name = init_data['name']

        o.spotify_url = init_data['external_urls'].get('spotify')

        try:
            o.popularity = init_data['popularity']
        except KeyError: pass
        try:
            o.followers = init_data['followers'].get('total')
        except KeyError: pass

        try:
            o.images = init_data['images']
        except KeyError: pass

        try:
            for g in init_data['genres']:
                genre = Genre.get_or_create(session, g)
                if genre not in o.genres:
                    o.genres.append(genre)
        except KeyError: pass


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
    explicit = Column(Boolean)
    album_id = Column(Integer, ForeignKey('album.album_id'))
    features = relationship('AudioFeatures', uselist=False, back_populates='track')
    artists = relationship('Artist', secondary=track_artist, back_populates='tracks', lazy='joined')
    playlists = relationship('Playlist', secondary=playlist_track, back_populates='tracks')

    def to_json(self, as_object=False):
        """Produce a JSON representation (String) of the data in this track 
        as an object (dict) with scalar values, appropriate for use in aggregating
        track info by playlist or other such.

        Optionally, specify `as_object=True` to have the data returned before
        being serialized to a String.
        """
        d = {
            'track_id': self.spotify_id,
            'name': self.name,
            'duration_ms': self.duration_ms,
            'explicit': self.explicit,
            'popularity': self.popularity,
            'explicit': self.explicit,
            'album': self.album.name,
            'artists': self.artists_str(),
        }

        d.update(self.features.to_json(True))

        if as_object:
            return d
        return json.dumps(d)

    def artists_str(self) -> str:
        return ', '.join(a.name for a in self.artists)    

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

PITCH_CLASSES = [
    'C',
    'C♯',
    'D',
    'E♭',
    'E',
    'F',
    'F♯',
    'G',
    'A♭',
    'A',
    'B♭',
    'B'
]
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

    def to_json(self, as_object=False):
        """Produce a JSON representation (String) of the data in this AudioFeatures 
        as an object (dict) with scalar values, appropriate for use in aggregating
        track features by playlist or other such.

        Optionally, specify `as_object=True` to have the data returned before
        being serialized to a String.
        """
        d = {
            'key': self.key,
            'key_str': self.key_str,
            'mode': self.mode,
            'mode_str': self.mode_str,
            'tempo': self.tempo,
            'time_signature': self.time_signature,
            'acousticness': self.acousticness,
            'danceability': self.danceability,
            'energy': self.energy,
            'instrumentalness': self.instrumentalness,
            'liveness': self.liveness,
            'loudness': self.loudness,
            'speechiness': self.speechiness,
            'valence': self.valence,
        }

        if as_object:
            return d
        return json.dumps(d)


    @property
    def key_str(self):
        try:
            return PITCH_CLASSES[self.key]
        except:
            return ''

    @property
    def mode_str(self):
        if self.mode == 0:
            return 'major'
        elif self.mode == 1:
            return 'minor'
        else:
            return ''

class Album(Base):
    # https://developer.spotify.com/documentation/web-api/reference/#object-simplifiedalbumobject
    # https://developer.spotify.com/documentation/web-api/reference/#object-albumobject
    __tablename__ = 'album'
    album_id = Column(Integer, primary_key=True)
    spotify_id = Column(String)
    spotify_url = Column(String)
    name = Column(String)
    label = Column(String)
    images = Column(JSON) # not in simplified
    popularity = Column(Integer) # not in simplified
    artists = relationship('Artist', secondary=album_artist, back_populates='albums')
    tracks = relationship("Track", backref="album")

    def image_url(self,pixels=None,max_size=None,min_size=None):
        """Return an image URL from this object's set of images. If pixels
        is not None, it should be an integer, and the returned image URL will be an exact match for 
        that pixel size. If there is no exact match, None will be returned.  If pixels is not set but 
        either max_size or min_size are set, then the URL for the largest image which fits the 
        constraints will be returned.
        If no kwargs are set, the first image URL found will be 
        returned"""
        images = [(i['width'],i['url']) for i in self.images]
        if pixels:
            if pixels in dict(images):
                img_dict = dict(images)
                return img_dict[pixels]
            return None
        if not max_size and not min_size:
            return images[0][1]

        for width, url in reversed(sorted(images)):
            if max_size and min_size:
                if width <= max_size and width >= min_size:
                    return url
            elif max_size and width <= max_size:
                return url
            elif min_size and width >= min_size:
                return url

        return None

    @staticmethod
    def get_or_create(session, spotify_id, init_data=None):
        o = session.query(Album).filter_by(spotify_id=spotify_id).scalar()
        if o is None:
            o = Album(spotify_id=spotify_id)
        o.spotify_url = f"https://open.spotify.com/album/{o.spotify_id}"
        if init_data is None:
            return o
        # treat init_data as a SpotifyAPI AlbumObject (or simplified, be careful)
        o.name = init_data['name']
        o.images = init_data['images']
        try:
            # instead of init_data.get('label') which would overwrite a previous value
            # which might not be intended if we're updating from a simplified
            o.label = init_data['label']
        except KeyError: pass
        try:
            o.popularity = init_data['popularity']
        except KeyError: pass
        for a in init_data.get('artists',[]):
            o.artists.append(Artist.get_or_create(session, a['id'], init_data=a))
        session.add(o)
        return o
