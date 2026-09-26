# Home page: other ways into the archive (options, not yet built)

Status: proposal only, 2026-09-26. Nothing implemented. See `notes/plan.md` for where this fits.

## Decided so far
- Tiles show the cover caption on hover, where the show description has one
  (`Playlist image: ...` in `history.shows[pid]['description']`, HTML-escaped; parse after
  `html.unescape`). Only ~162 of 284 shows have one, and many aren't birds, so captions are
  hover text only, not a browse axis.

## What the data says (as of the 2026-09-24 show)
- 284 shows, 2020-04-07 to 2026-09-24; 253 on Thursdays, 29 Wednesdays. Weekly rhythm is strong:
  "same week in past years" (±3 days of the calendar date) hit 2020, 2021, 2022, 2023, 2024
  for this week, so an "on this week" row is almost never empty.
- 31 DJs (Joe 274 shows, Chris 190, Heath 170, Rob 147, ...); 6,540 artists played.
- Good hooks already computed in `factoids.History`: `show_factoids` (Welcome back, Heard it before,
  Regulars, First sighting, Range, Album session), `show_room`, `show_mix` (genre-family bar),
  `novelty_rank`, `ranking('tracks'|'returns'|...)`. Examples: Amen Dunes back after 6 years (9/24);
  "Planet Caravan" played 5 times; 8/20 was all-new artists.
- `/radio?show=<date>` already starts the radio from a show.

## Options
A. **This week's show up top, then "this week in..."**: latest show as a large tile with room,
   mix bar, 1-2 factoids, "radio from this show"; a row of the same week in past years; then the
   full wall. Cheapest; almost entirely reuses existing code.
B. **Notes in the wall**: tile-sized cards mixed into the grid every few tiles: long returns,
   most repeated tracks, most novel show, "pull out a random show", "start the radio". Varies per
   visit. Keeps the wall's look while breaking date order.
C. **Sort and filter the wall**: chips for shuffle / oldest / most new artists, who was in the
   room (DJ), genre family; year dividers ("2025 · 34 shows"). Client-side over data attributes
   on tiles; must bind on `turbo:load` and be idempotent. More of a browse tool than a way in;
   could be its own page.
D. **Almanac**: one row per year, columns aligned by week of year, this week highlighted, sideways
   scroll; gaps show skipped weeks. Striking, but the tiles have to shrink (conflicts with "keep big tiles").

Recommendation: A + B together; C later as a separate browse page.

## Overlap with the paused "Design pass" session
Its notes: `notes/design-pass.md`. It planned to redo the
home tile layout (index.html, _playlist_tile.html, .playlist-tile CSS) and compact the nav, so it touches
the same files as options A-C; both touch static/style.css. Tiles are now plain `<a href>` links (13ba9fa); any new cards should be plain links too.

## Constraints to remember
- Keep the big tiles (`templates/_playlist_tile.html`, `.playlist-tile` in `static/style.css`).
- Loose, personal look, not product-polished.
- Turbo Drive: page scripts run on `turbo:load` and must be safe to re-run.
- Python env: `~/.virtualenvs/birdnest` (`$WORKON_HOME/birdnest`).
