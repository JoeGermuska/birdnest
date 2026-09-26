# Plan: design pass + ways into the archive

Combined from `notes/design-pass.md` (paused design pass) and `notes/home-page-options.md` (ways in), 2026-09-26.
They overlap on the home page, tile layout, nav and `static/style.css`, so the home page redesign and the new
ways in are done together, and the design pass fits around them.

1. **Groundwork** (small, blocks nothing)
   - [x] Home tiles are real links (13ba9fa).
   - [x] `<!DOCTYPE html>`, `<meta charset>` and `<meta name="viewport">` in `_base.html`, and the banner wraps on phones.
   - [ ] Player bar fits on phones: compact layout, and reserve its real height instead of a fixed 72px.
2. **Home page**: tile wall as a CSS grid, plus option A (latest show big, then "this week in past years") and
   option B (note cards mixed into the wall). Owner chose both.
3. **Banner**: compact `_nav.html`, working at phone width; room for any new entry points.
4. **Design pass on the other pages**: shared colors/type tokens, **dark and light mode** (built on those tokens), tables on phones, before/after screenshots.
5. **Later**: option C (sort/filter the wall) as its own browse page; Apple Music in the radio player.

## Birds radio player: next features (owner's requests, not started)

- **Like**: a ♥ on the player (and maybe on each row) that saves the track to the listener's Liked Songs.
  Needs the `user-library-modify` scope (and `user-library-read` to show whether it's already liked), so
  everyone logs in again once.
- **Add to my queue playlist**: many of us keep a Spotify playlist of candidates for the next show. Let each
  listener **choose a queue** once (pick from their own playlists; needs `playlist-read-private` and
  `playlist-modify-public`/`-private`), then a "+ queue" button on the player and on track rows adds to it.
  Remember the choice in the browser (localStorage) since the deployed database can't take writes; show the
  chosen playlist's name on the player so it's clear where tracks go.
- Later: Apple Music (see the ISRC-based plan discussed earlier: `track_provider` table, MusicKit JS adapter).
