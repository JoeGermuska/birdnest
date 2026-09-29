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
6. **Cover captions for the older shows**: only ~162 of 284 shows say what the playlist image is (the "Playlist image:"
   part of the Spotify description). Earlier ones may be in Joe's Google Doc from before that habit started:
   https://docs.google.com/document/d/1cCRNWvXN7pYyKQ3JCrp8yLNofaLE1gsKcj5Ot1F-Sb0/edit?tab=t.0#heading=h.h6jguqwf34z4
   Fill the blanks from there (a small table of date -> caption, used when the description has none).
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

## Loose ends

- Wikidata to-do page: code on main, but `wikidata_candidate` isn't built; run `python wikidata_candidates.py`,
  commit birdnest.db, deploy. Bulk QuickStatements once the Wikidata account is autoconfirmed.
- Spotify: app is in development mode (add each friend to its user list)
- Genres: see notes/genres.md (backburner).
