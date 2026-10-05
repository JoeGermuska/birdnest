# Data normalization backlog

From an assessment of `birdnest.db` (October 2026). Counts are from that day.

## Done

- **Duplicate genre rows.** `Artist.get_or_create` made a new `genre` row for every artist-genre pair (8,698 rows for
  2,014 names; "jazz" 141 times). Fixed in `models.py` (genres are looked up and reused), cleaned up with
  `genre_fixer.py` (now SQL, safe to re-run), and `genre.name` has a unique index. Covered by `tests/test_genres.py`.

## Needs a decision

### Collab credits that became fake artists

Spotify sometimes hangs a track on a combined artist ("Gene Ammons, Dexter Gordon", `/artist/4sNmOvWLi3Pcib8gjV1Xkm`:
0 popularity, 23 followers, no image, genres, links or MusicBrainz match, one track). 28 artist names read as collabs
where every part is also an artist row. Some are real bands (Bob Marley & The Wailers, Sharon Jones & The Dap-Kings);
others aren't (Martin Hannett, Steve Hopkins; Jackie Wilson & LaVern Baker; Aretha Franklin & George Benson;
Brian Auger & Julie Tippetts; Brad Mehldau & Mark Guiliana). The separator can't tell them apart ("Earth, Wind & Fire").
A usable signal: thin (few followers, no MusicBrainz match, one track).

Open question: split into per-artist credits on `track_artist` (more correct, touches `factoids.py` and the history
code), or keep the row and link it to the individual artists. Either way needs a human to confirm each one, probably
as a to-do page like the Wikidata ones. Also names with credit text in them ("deadmau5 feat. Cypress Hill",
"The Lenny Hambro Quintet featuring Eddie Costa").

### Duplicate artists

13 MusicBrainz IDs are on two artist rows each, but they are not all the same name, so this is editorial:
Yasiin Bey / Mos Def, The Jacksons / The Jackson 5, The Supremes / Diana Ross & The Supremes, Black Thought / Tariq
Trotter, Shabaka / Shabaka Hutchings, Herbert / Matthew Herbert, The English Beat / The Beat, The Crusaders / The
Jazz Crusaders, Sun Ra Arkestra / Sun Ra & His Arkestra. Exact same-name pairs on the same MBID (Kae Tempest, Yazoo,
Booker T. Jones, Joe Talbot) are the safest ones to merge first.

Also: 21 names occur twice (some different people: Air, Sweet, Steve Lacy, so don't merge by name), and near-name
variants (Kahil El Zabar / Kahil El'Zabar, The Chosen Few / Chosen Few, Honeydrippers / The Honey Drippers,
Joy-Tones / The Joy Tones). Lou Donaldson has two Spotify IDs with identical followers and no MBID on one.

A merge touches `artist_genre`, `album_artist`, `track_artist`, `artist_link`, `mb_artist`, `mb_artist_url`,
`artist_snapshot`, `artist_genre_current`, `wikidata_checked`, `wikidata_candidate` and `artist_description`. It needs
an alias (`artist_alias` or a canonical id) so old `/artist/<spotify_id>` URLs redirect, as the album route does, and
so `load_playlist.py` doesn't recreate the duplicate the next time it sees that Spotify ID.

### Album labels

About 3,000 distinct `album.label` values. 22 groups differ only by case or punctuation (Daptone Records / DAPTONE
RECORDS, Light In The Attic / Light in the Attic, `Geffen*`, "Co." / "Co"); about 90 differ by suffix (Polydor /
Polydor Records, Drag City / Drag City Records, XL / XL Recordings). Universal Music Group / LLC / Records is partly a
corporate-structure question. The app already groups through MusicBrainz label IDs (`mb_label`,
`mb_album_original`), so prefer showing and grouping by MBID and use a normalized string only where there is none.
Not touching `album.label` itself: it is Spotify's text and the refresh scripts overwrite it.

## Smaller things

- **Album editions.** 909 album names carry "(Deluxe Edition)", "(Remastered)", "[Stax Remasters]", "(Remasterizado)";
  169 artist+name pairs have several rows. `factoids.py` already groups editions (`album_group`); spot-check how much
  it misses before doing anything.
- **Tracks.** About 299 titles say "Remastered" (often " - 2015 Remastered"), 189 are live, 234 have feat/with.
  109 ISRCs are shared across track rows. Whether to treat these as one song depends on what radio and search want.
- **Artist names.** 189 are ALL CAPS, mostly real stylings (RÜFÜS DU SOL, SAULT, ODESZA). Leave alone.
- **MusicBrainz fields.** `mb_artist.area` mixes countries and cities; `type` is empty for 2,232 artists and
  `country` for 2,394. Matters only if area becomes a facet.
- **Artist links.** 1,984 `artist_link` URLs are `http://` and 3,884 end in a slash. Only matters for comparing or
  de-duplicating links; rewriting `http` to `https` isn't safe for the `website` source.
- **Playlists.** 282 descriptions hold `&#x2F;` entities (already unescaped at display in `factoids.py`); one title has
  a stray 💫 prefix.
- **Loader.** Whatever is decided above has to be done in `load_playlist.py` too, or the next weekly load recreates it.
