"""Group Spotify's ~2,000 flat genre tags into a handful of families.

Keywords catch most genres; the rest take the family most common among the
other genres of the artists who carry them (so "permanent wave" lands in rock
and "rare groove" in soul & funk). Order matters: the first family whose
keywords match wins, so "soul jazz" is jazz and "indie rock" is rock.
"""
from collections import Counter

# (family, keywords) -- family order is also the chart color order
FAMILIES = [
    ('Jazz', ['jazz', 'bop', 'swing', 'big band', 'dixieland', 'stride', 'free improvisation', 'hammond organ',
              'tzadik', 'spiritual jazz']),
    ('Rock', ['rock', 'metal', 'punk', 'hardcore', 'grunge', 'emo', 'shoegaze', 'psych', 'garage', 'surf',
              'new wave', 'post-punk', 'mellow gold', 'british invasion', 'jam band', 'no wave', 'krautrock',
              'math', 'noise', 'slowcore', 'madchester', 'new romantic', 'darkwave', 'zolo', 'permanent wave']),
    ('Soul, funk & blues', ['soul', 'r&b', 'motown', 'doo-wop', 'quiet storm', 'new jack', 'funk', 'disco',
                            'boogie', 'go-go', 'blues', 'gospel', 'spirituals', 'rare groove', 'zydeco', 'urban contemporary']),
    ('Folk & country', ['folk', 'singer-songwriter', 'songwriter', 'country', 'americana', 'bluegrass', 'cowboy',
                        'honky', 'outlaw', 'stomp and holler', 'western', 'nashville', 'cosmic american',
                        'new weird america', 'southern gothic', 'american primitive', 'fingerstyle', 'old-time']),
    ('Pop', ['pop', 'indie', 'lo-fi', 'bedroom', 'adult standards', 'easy listening', 'lounge', 'exotica',
             'cabaret', 'vocal', 'crooner', 'bubblegum', 'lilith', 'torch song', 'christmas', 'comic', 'escape room']),
    ('Global', ['reggae', 'dub', 'ska', 'rocksteady', 'dancehall', 'calypso', 'soca', 'lovers rock', 'ragga',
                'latin', 'brazil', 'mpb', 'bossa', 'samba', 'tropicalia', 'salsa', 'cumbia', 'bolero', 'mexican',
                'cuban', 'tango', 'forro', 'colombian', 'argentin', 'chilean', 'peruvian', 'puerto', 'boogaloo',
                'nueva', 'chicha', 'afro', 'african', 'highlife', 'ethio', 'desert', 'congo', 'soukous', 'mbalax',
                'malian', 'nigerian', 'ghana', 'zambian', 'kenyan', 'arab', 'turkish', 'indian', 'bollywood',
                'japanese', 'korean', 'thai', 'anatolian', 'balkan', 'celtic', 'world', 'cambodian', 'greek',
                'iranian', 'persian', 'kora', 'gnawa', 'sega', 'wassoulou', 'fado', 'tuareg', 'chanson', 'french',
                'brass band', 'street band', 'tanzanian', 'angolan', 'semba', 'kizomba', 'moombahton']),
    ('Electronic & hip hop', ['hip hop', 'rap', 'drill', 'grime', 'boom bap', 'trap', 'turntablism', 'bboy',
                              'electronic', 'house', 'techno', 'ambient', 'idm', 'electronica', 'trip hop',
                              'downtempo', 'edm', 'dance', 'drum and bass', 'breakbeat', 'synth', 'chillwave',
                              'vaporwave', 'beats', 'glitch', 'footwork', 'big beat', 'electro', 'new rave',
                              'crank wave', 'speedrun']),
    ('Experimental & classical', ['classical', 'orchestra', 'baroque', 'opera', 'minimal', 'avant', 'experimental',
                                  'drone', 'modern composition', 'contemporary', 'choral', 'string', 'piano',
                                  'neo-classical', 'soundtrack', 'score', 'fourth world', 'new age', 'spoken word',
                                  'spectra', 'pastoral', 'melancholia', 'laboratorio']),
]
# genres the keyword rules get wrong
OVERRIDES = {'fourth world': 'Experimental & classical', 'lovers rock': 'Global', 'cajun': 'Folk & country',
             'funk rock': 'Rock'}
OTHER = 'Other'
FAMILY_NAMES = [f for f, _ in FAMILIES] + [OTHER]


def by_keyword(genre):
    if genre in OVERRIDES:
        return OVERRIDES[genre]
    for family, keywords in FAMILIES:
        if any(k in genre for k in keywords):
            return family
    return None


def assign(artist_genres):
    """artist_genres: {artist_id: set(genre names)} -> {genre: family}"""
    genres = {g for gs in artist_genres.values() for g in gs}
    families = {g: by_keyword(g) for g in genres}
    votes = {g: Counter() for g in genres if families[g] is None}
    for gs in artist_genres.values():
        for g in gs:
            if g in votes:
                votes[g].update(families[o] for o in gs if o != g and families[o])
    for g, v in votes.items():
        families[g] = v.most_common(1)[0][0] if v else OTHER
    return families
