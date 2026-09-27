# Plan: design pass + ways into the archive

Combined from `notes/design-pass.md` (paused design pass) and `notes/home-page-options.md` (ways in), 2026-09-26.
They overlap on the home page, tile layout, nav and `static/style.css`, so the home page redesign and the new
ways in are done together, and the design pass fits around them.

1. **Groundwork** (small, blocks nothing)
   - [x] Home tiles are real links (13ba9fa).
   - [x] `<!DOCTYPE html>`, `<meta charset>` and `<meta name="viewport">` in `_base.html`, and the banner wraps on phones.
   - [x] Player bar fits on phones: one compact row, and the page reserves its real height (measured).
   - [x] Pages no longer widen past a phone screen (show page header wraps; track tables scroll in their own box for now).
2. **Home page**: tile wall as a CSS grid, plus option A (latest show big, then "this week in past years") and
   option B (note cards mixed into the wall). Owner chose both.
3. **Banner**: compact `_nav.html`, working at phone width; room for any new entry points.
4. **Design pass on the other pages**: shared colors/type tokens, **dark and light mode** (built on those tokens), tables on phones, before/after screenshots.
5. **Later**: option C (sort/filter the wall) as its own browse page; Apple Music in the radio player.

## Birds radio player: next features (owner's requests, not started)

- [x] **Like**: ♥ on the player saves the playing track to Liked Songs (and shows if it's already there). Maybe on each row later.
  Needs the `user-library-modify` scope (and `user-library-read` to show whether it's already liked), so
  everyone logs in again once.
- [x] **Add to my queue playlist** (＋ on the player; choose the playlist in the Tracks drawer). Maybe on each row later. Original idea: many of us keep a Spotify playlist of candidates for the next show. Let each
  listener **choose a queue** once (pick from their own playlists; needs `playlist-read-private` and
  `playlist-modify-public`/`-private`), then a "+ queue" button on the player and on track rows adds to it.
  Remember the choice in the browser (localStorage) since the deployed database can't take writes; show the
  chosen playlist's name on the player so it's clear where tracks go.
- Later: **Play on Spotify** (remote-control mode): besides playing in the page, a "Play on…" device picker so
  the radio drives the listener's Spotify app, phone, speakers etc. via Spotify Connect. Pros: works where the
  in-browser player can't (iPhone Safari), real background audio and lock-screen controls, accurate "now playing"
  across devices, music survives closing the tab. Cons: the page still has to be open to feed the next tracks (no
  push events for other devices, so poll /me/player every few seconds; queue one or two ahead); Spotify's queue
  can't be edited, so pre-queued tracks survive a retune; tracks the listener queues themselves mix in, and the
  page must stop feeding when they switch to other listening. Default to it on iPhones. A server-side feeder
  would survive closed tabs but needs persistent per-user token storage, which the deployment doesn't have.
  (Today, switching playback to the Spotify app keeps going by accident: the page's one queued-ahead track plays
  there, and its state events keep the feeding alive.)
- Later: Apple Music (see the ISRC-based plan discussed earlier: `track_provider` table, MusicKit JS adapter).
