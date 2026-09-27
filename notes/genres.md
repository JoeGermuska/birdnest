# Genres: thinking out loud (not started)

Prompted by the Grateful Dead showing up as philly soul / motown / northern soul.

## What's going on
- `artist_genre` keeps every tag Spotify ever gave an artist (deliberately: Spotify has been retiring its
  finer, Every Noise-era vocabulary). `artist_genre_current` has only what Spotify lists now (refresh_artists.py).
  Grateful Dead: 15 stored tags incl. five soul ones; current = jam band, psychedelic rock, acid rock.
- 3,102 of 4,879 tagged artists have stored tags Spotify no longer lists; mostly retired fine-grained genres,
  not errors, so dropping them would lose real (and much-loved) detail.
- Artist-level tags are the deeper problem: the Dead did cover Motown. Tags describe some of what an artist did.

## Owner's leanings
- Keep the quirky Every Noise-era genres.
- No appetite for policing; occasional corrections are fine.
- Open to experimenting.

## Sources, for reference
- Spotify current tags: thin and shrinking.
- MusicBrainz genres: community-voted with counts (bad tags get outvoted); we have MBIDs for ~4,500 artists.
  Recording-level tags exist but are sparse.
- Wikidata genre (P136): curated, coarse.
- Discogs genres/styles: per release (album/single), every track inherits them; good taxonomy, needs a token.
- Last.fm tags: broad, weighted, noisy; needs a key.
- Audio features (6,575 tracks) could cluster by sound, but they're about sound, not genre.

## Experiment to try first: genre per play, from context
Keep each artist's full tag pool. For each play, weight the artist's tags by how well they match the tags of the
tracks around it in that show (and maybe the rest of the show). The Dead in a Stax/Motown stretch read as soul
that night; in a jam stretch, psychedelic rock. Uses only our data, keeps the quirky tags, no policing. Feed it
into the show mix, the genre map and the radio's family weighting, and compare before/after on a few shows.
Plus a tiny hand-corrections file for the plainly silly.
