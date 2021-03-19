"""A modest wrapper for the spotipy client that deals with the paginated API"""

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

class Client():
    
    _allbirds = []
    _sp = None

    def __init__(self, user='joegermuska',scopes=None):
        self.user = user
        if scopes: # anonymous
            self._sp = spotipy.Spotify(auth_manager=SpotifyOAuth(username=self.user,scope=scopes))
        else:
            self._sp = spotipy.Spotify(client_credentials_manager=SpotifyClientCredentials())

    def get_playlists(self,user=None):
        if user is None:
            user = self.user
        return self._sp.user_playlists(user)

    def random_playlist(self):
        from random import choice
        return choice(self.allbirds())

    def allbirds(self,refresh=False):
        if refresh or len(self._allbirds) == 0:
            playlists = self._sp.user_playlists(self.user)
            while playlists:
                for p in playlists['items']:
                    if 'conference of the birds' in p['name'].lower() and 'jqbx' in p['name'].lower():
                        self._allbirds.append(p)
                if playlists['next']:
                    playlists = self._sp.next(playlists)
                else:
                    playlists = None                  
        return self._allbirds

                  
    def playlist_tracks(self, playlist_id, full=False):
        the_tracks = []
        tracks = self._sp.playlist_tracks(playlist_id)
        while tracks:
            for i in tracks['items']:
                t = i['track']
                if full:
                    the_tracks.append(t)
                else:
                    the_tracks.append({
                      # fill it in
                      'artist': ', '.join([a['name'] for a in t['artists']]),
                      'title': t['name'],
                      'album': t['album']['name'],
                      'url': t['external_urls']['spotify']
                    })
            if tracks['next']:
                tracks = self._sp.next(tracks)
            else:
                tracks = None
        return the_tracks       

    def artist(self, artist_id):
        return self._sp.artist(artist_id)

    def artists(self, artist_ids):
        # max of 50
        artists = []
        ids = list(artist_ids)
        while len(ids) > 0:
            result = self._sp.artists(ids[:50])
            artists.extend(result['artists'])
            ids = ids[50:]
        return artists

    def tracks(self, track_ids):
        # max of 50
        tracks = []
        ids = list(track_ids)
        while len(ids) > 0:
            result = self._sp.tracks(ids[:50])
            tracks.extend(result['tracks'])
            ids = ids[50:]
        return tracks

    def albums(self, album_ids):
        # max of 20
        albums = []
        ids = list(album_ids)
        while len(ids) > 0:
            result = self._sp.albums(ids[:20])
            albums.extend(result['albums'])
            ids = ids[20:]
        return albums

    def audio_features(self, track_ids):
        # max 100
        features = []
        ids = list(track_ids)
        while len(ids) > 0:
            features.extend(self._sp.audio_features(ids[:100]))
            ids = ids[100:]
        return features
