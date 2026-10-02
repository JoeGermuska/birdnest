# Plan: design pass + ways into the archive

Combined from `notes/design-pass.md` (paused design pass) and `notes/home-page-options.md` (ways in), 2026-09-26.
They overlap on the home page, tile layout, nav and `static/style.css`, so the home page redesign and the new
ways in are done together, and the design pass fits around them.

1. **Groundwork** (small, blocks nothing)
   - [X]  Home tiles are real links (13ba9fa).
   - [X]  `<!DOCTYPE html>`, `<meta charset>` and `<meta name="viewport">` in `_base.html`, and the banner wraps on phones.
   - [X]  Player bar fits on phones: one compact row, and the page reserves its real height (measured).
   - [X]  Pages no longer widen past a phone screen (show page header wraps; track tables scroll in their own box for now).

2. [X]  **Home page**: tile wall as a CSS grid, plus option A (latest show big, then "this week in past years") and
   option B (note cards mixed into the wall). Done (8821c76).
3. [X]  **Banner**: a paper masthead (`_nav.html`), two rows on phones with the links scrolling sideways if they outgrow it.
   With it, new type: Alegreya Sans for titles, labels and controls, David Libre for reading (quote marks from
   Alegreya), as `--font-display`/`--font-body` and a few color tokens on `:root` in `style.css`.

4. [X]  **Design pass on the other pages**: colors, fonts and small type sizes are tokens on `:root` in `style.css`;
   **dark mode** redefines the color tokens (follows the system; the banner's switch picks the other); links are ink
   instead of browser blue; track tables become cards on phones. Still open: images with white borders glare a bit in
   dark mode; `templates/_tracks_table.html` isn't used anywhere.
5. **Browse page** (`/shows`, 0e12cfa): option C as its own page. Built, but the filters need rethinking (Joe, 2026-09-28):
   - "Most new for its time" doesn't mean much to a listener. (Novelty now also counts repeated songs a little
     against a show: `REPEAT_PENALTY` in `factoids.py`.)
   - "In the room" should maybe be additive: pick several people, see the shows they were all in.
   - "Leans <genre>" (a family at 1.5x its usual share) doesn't obviously make anyone want to filter by it.
6. [X]  **Cover captions for the older shows**: `load_captions.py` loads the Google Doc's "Birds" section (one heading
   per week, 5/2020 to 10/2024) into `cover_caption`; `show_caption` uses it when the Spotify description has no
   "Playlist image:". 269 of 284 shows now have one. Still none for 2020-04-07, 04-16, 04-23 (before the doc's
   list), 2020-12-10, 2024-11-21, 2025-01-02, 01-30, 02-27, 03-13, 10-01, 10-30, 2026-01-08, 04-02, 04-16, 06-11.
   Rerun from a Markdown download of the doc (File > Download > Markdown) to fix a caption or get the emoji back
   (one in 12/23/2021 was lost in the export used).
7. **Later**: Apple Music in the radio player.

## Birds radio player

- [X]  **Like**: ♥ on the player saves the playing track to Liked Songs (and shows if it's already there). Maybe on each row later.
  Needs the `user-library-modify` scope (and `user-library-read` to show whether it's already liked), so
  everyone logs in again once.
- [X]  **Add to my queue playlist** (＋ on the player; choose the playlist in the Tracks drawer). Shows ✓ when the
  playing track is already in it; clicking ✓ takes it back out. Maybe on each row later. Original idea: many of us keep a Spotify playlist of candidates for the next show. Let each
  listener **choose a queue** once (pick from their own playlists; needs `playlist-read-private` and
  `playlist-modify-public`/`-private`), then a "+ queue" button on the player and on track rows adds to it.
  Remember the choice in the browser (localStorage) since the deployed database can't take writes; show the
  chosen playlist's name on the player so it's clear where tracks go.
- [X]  **Genre radio**: a Birds radio link on every genre page; the set leans toward the genre (about two-thirds in
  genre at low adventure), still following show connections. Every Birds radio link now uses the radio icon.
- [X]  Idle player: ▶ with nothing loaded starts a random set.
- [X]  Show and artist radio lean toward that night / the artist's circle, like genre radio (radio.lean_recs).
- [X]  The player drives each track itself instead of using Spotify's queue (2001df9); tests in tests/.

- Later: **Play on Spotify** (remote-control mode): besides playing in the page, a "Play on…" device picker so
- the radio drives the listener's Spotify app, phone, speakers etc. via Spotify Connect. Pros: works where the
  in-browser player can't (iPhone Safari), real background audio and lock-screen controls, accurate "now playing"
  across devices, music survives closing the tab. Cons: the page still has to be open to feed the next tracks (no
  push events for other devices, so poll /me/player every few seconds; queue one or two ahead); Spotify's queue
  can't be edited, so pre-queued tracks survive a retune; tracks the listener queues themselves mix in, and the
  page must stop feeding when they switch to other listening. Default to it on iPhones. A server-side feeder
  would survive closed tabs but needs persistent per-user token storage, which the deployment doesn't have.
  (Since 2001df9 the player no longer queues ahead, so switching playback to the Spotify app now stops the radio
  after that track.)
- Later: Apple Music (see the ISRC-based plan discussed earlier: `track_provider` table, MusicKit JS adapter).

## Albums and labels (2026-10-01)

Spotify's `label` mostly names today's catalog owner (Rhino/Warner Records, Columbia/Legacy, UMC), not the label a
record came out on, and there are 2,965 distinct label strings, 1,893 of them used once. Raw label rankings mostly
show who owns the back catalog.

- [X]  **Album details from Spotify** (`refresh_albums.py`; new albums get them on load): release date, album type
  (album/single/compilation), track count, UPC, ℗/© lines. Spotify's date is the date of whatever release it serves,
  so often a reissue's.
- [ ]  **First release dates from MusicBrainz** (`enrich_mb_recordings.py`, by ISRC, into `mb_recording`); the
  `track_release` view takes the earlier of that and Spotify's album date. Backfill started 2026-10-01; about 60% of
  the most-played tracks are found. Maybe later: fall back to an artist + title search for the rest (less reliable;
  keep it marked as such).
- [ ]  **Song age**: each show's span and median age, decade mix per show/DJ/year, "oldest thing played".
- [ ]  **Labels from MusicBrainz**: UPC → release → label (with MBID, type, country, parent/imprint relations,
  Wikidata, Discogs, Bandcamp); the earliest release of each recording for its original label. Then group label
  strings under parents, and add label pages (`/label/<slug>`, label radio), DJ label fingerprints, indie vs. major
  share over time, and Chicago labels.
- [X]  **Album pages and rankings** (`/album/<spotify id>`, Rankings > Albums, album names linked from shows, artists
  and search; album radio). Duplicate Spotify copies of an album, and editions in one MusicBrainz release group,
  count as one album. Outside links (Wikipedia, MusicBrainz, Discogs, AllMusic, Bandcamp, Apple Music, Wikidata)
  from `enrich_mb_albums.py`: matched by the release's own link to the Spotify album, else by an exact barcode
  that leads to one release group (70% matched in a 40-album trial). Backfill started 2026-10-01.
- [ ]  Compilation and reissue share per show.
- [X]  To-do lists for albums, from the 🤓 in the banner: Wikidata album items lacking our Spotify album ID
  (`wikidata_album_todo.py`, P2205), and MusicBrainz releases matched only by barcode, which need the Spotify link.
  Both check live from the browser, like the artist list.

## Linking to-do lists: ways to grow them

Slow and steady is fine. The lists are under the 🤓 in the banner (`TODO_PAGES` in app.py, one shared template);
each new one needs a candidate table built at load time plus a live check from the browser. The weekly load
picks up what people fix: `enrich_wikidata.main(no_data=True)` re-checks artists with no Wikidata item, and
`enrich_mb_albums.py` re-checks barcode matches for a Spotify link on MusicBrainz. It also retries a few old
MusicBrainz misses each week (`retries=`). The slow jobs (artist candidates for /todo/wikidata, refreshing
every artist's links) are in `maintenance.py`, run every month or two.

- Artists whose Wikidata item we reached through MusicBrainz but which has no Spotify artist ID (P1902): near-certain
  matches, since MusicBrainz editors made the link.
- [X] Albums: Wikidata album items without a Spotify album ID (P2205); MusicBrainz releases matched by barcode only.
- Artists MusicBrainz doesn't know at all (no Spotify link on any MusicBrainz artist).
- Labels, once we have MusicBrainz labels: Wikidata items without a MusicBrainz label ID (P966), and labels with
  no Wikidata item at all.
- QuickStatements export of reviewed rows (once the account is autoconfirmed).

## /heath: show sizes and song lengths (2026-10-01)

Heath asked for most and fewest songs per show, longest and shortest songs, six-minute songs, and back-to-back
runs of one artist. Built at `/heath` (`History.lengths()`, `History.back_to_back()`), with six-minute radio
(`/radio?length=long`). Linked from Heath's DJ page only. Ideas for more:
- Song length by release decade, once the MusicBrainz dates are in.
- Per-DJ pace: average song length on the nights each DJ is in the room.
- Back-to-backs by the same album, or the same label.

## Speed (2026-10-01)

Thursday-night load: the 256 MB machine OOM-killed the worker twice, and Fly logged the 25-connection hard limit
over and over. Done: tile-size cover images (`make_tiles.py`; covers were up to 2000px/1MB and each download held
one of the worker's 8 threads), inline SVG icons instead of the Font Awesome kit (a blocking script on every
load), year-long caching of versioned static files, robots.txt off the endless radio/search URLs, gunicorn
`--max-requests` recycling and access logs.
- [ ]  Maybe `fly scale memory 512` if the OOM kills continue (a dollar or two a month).
- [ ]  Once /shows is good, lighten the home page: latest show, this week in past years, a few random shows,
  instead of the whole wall of covers (Joe, 2026-10-01).

## Loose ends

- Dark mode: on the show page, the "usual" row of the mix bars is hard to read.

- Wikidata to-do page: code on main, but `wikidata_candidate` isn't built; run `python wikidata_candidates.py`,
  commit birdnest.db, deploy. Bulk QuickStatements once the Wikidata account is autoconfirmed.
- Spotify: app is in development mode (add each friend to its user list)
- Genres: see notes/genres.md (backburner).
