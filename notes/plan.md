# Plan

Current as of 2026-10-18. Earlier versions of this file were a design pass plus "ways in" work; both are done, and their notes are in `notes/archive/`. The music-writing index has its own document, `notes/reading.md`. Genres: `notes/genres.md` (backburner).

## Next, roughly in order

1. **Reading, day to day** (`notes/reading.md`): work down the review queue (`python writing.py review`, 440 waiting), let the weekly `refresh_writing.py` drain the backfill (~36,000 candidate pages, slowly on purpose), watch `python writing.py sources` for sources that never yield.
2. **Reading, small features** (each an evening): "writing about what we played" on show pages; a latest-reading strip on the home page; a ✎ marker on the artists ranking.
3. **Labels, the rest** (below): compilation and reissue share per show; imprint grouping; indie vs. major share over time; DJ label fingerprints; Chicago labels.
4. **More linking to-do lists** (below): artists MusicBrainz doesn't know; labels.
5. **Genres**: the per-play genre experiment, and the audio-features idea (`notes/genres.md`).
6. **Radio**: "Play on Spotify" (Spotify Connect) and Apple Music, if wanted.

## Done: the site (September-October 2026)

- [X]  Groundwork: home tiles are real links; `<!DOCTYPE>`/charset/viewport in `_base.html`; banner wraps on phones; player bar fits phones and the page reserves its height; pages no longer widen past a phone screen.
- [X]  Home page: tile wall as a grid, the latest show big, "this week in past years", note cards mixed in; lightened 2026-10-04 (latest show, this week in past years, 10 random shows with two note cards). "This week" is laid out for 6 past years; revisit after the next anniversary.
- [X]  Banner: a paper masthead (`_nav.html`), two rows on phones; type is Alegreya Sans for titles, labels and controls and David Libre for reading, as `--font-display`/`--font-body` and color tokens on `:root` in `style.css`.
- [X]  Design pass on the other pages: tokens for colors, fonts and small sizes; dark mode redefines the color tokens (follows the system; the banner's switch picks the other); links are ink; track tables become cards on phones.
- [X]  Browse page `/shows`: "In the room" is additive (DJ chips; several means the shows they were all in), "Heavy on" genre chips, a Sort by menu (songs, length, median release year, new artists, repeats, room), shuffle with no seed in the link (crawlers once found an endless supply of seeded URLs; robots.txt blocks Amazonbot and GPTBot outright).
- [X]  Cover captions for older shows: `load_captions.py` loads the Google Doc's "Birds" section (5/2020 to 10/2024) into `cover_caption`; 269 of 284 shows had one. Missing: 2020-04-07, 04-16, 04-23, 2020-12-10, 2024-11-21, 2025-01-02, 01-30, 02-27, 03-13, 10-01, 10-30, 2026-01-08, 04-02, 04-16, 06-11. Rerun from a Markdown download of the doc to fix a caption or get the emoji back (one in 12/23/2021 was lost).
- [X]  The show page's "usual mix" row is words ("heavy on soul, funk & blues", "a typical mix for its time", `mix_summary`, `SHOW_LEAN`); DJ pages dropped their mix and song age comparisons (nothing says who picked what).
- [X]  `/heath` (show sizes, song lengths, back-to-backs, six-minute radio), linked from Heath's DJ page only.
- [X]  Speed: tile-size cover images, inline SVG icons, long caching of versioned static files, robots.txt off the endless radio/search URLs, gunicorn `--max-requests` and access logs. The Fly machine was OOM-killed again on 2026-10-08 under crawler load and is now **512 MB** (`fly scale memory 512`).

## Birds radio player

- [X]  Like (♥ saves to Liked Songs); add to my queue playlist (＋; chosen in the Tracks drawer, remembered in the browser); genre radio, show and artist radio lean toward that circle; idle ▶ starts a random set; the player drives each track itself (2001df9). Two engines: `sdk` (Web Playback SDK, logged-in listeners) and `embed` (Embed iFrame API, everyone else; Spotify's development mode allows only 5 authorized accounts).
- Later: **Play on Spotify** (remote-control mode, a "Play on…" device picker via Spotify Connect). Works where the in-browser player can't (iPhone Safari), real background audio and lock-screen controls. But the page still has to be open to feed the next tracks (poll /me/player every few seconds, queue one or two ahead); Spotify's queue can't be edited; a server-side feeder would survive closed tabs but needs per-user token storage the deployment doesn't have. Since 2001df9 the player no longer queues ahead, so switching playback to the Spotify app stops the radio after that track.
- Later: Apple Music in the player (ISRC-based: a `track_provider` table, a MusicKit JS adapter).

## Albums, labels, artist pages

Spotify's `label` mostly names today's catalog owner, not the label a record came out on (2,965 label strings, 1,893 used once), so label rankings come from MusicBrainz instead.

- [X]  Album details from Spotify (`refresh_albums.py`): release date, type, track count, UPC, ℗/© lines.
- [X]  First release dates from MusicBrainz by ISRC (`enrich_mb_recordings.py`, `mb_recording`); `factoids.best_release` picks each track's earliest date. 46% of plays got their year from MusicBrainz at the 2026-10-02 backfill.
- [X]  Song age: Rankings > Song age, an Era row on show pages, Oldest song / Hot off the press facts.
- [X]  Album pages and rankings (`/album/<spotify id>`, Rankings > Albums, album radio); duplicate Spotify copies and editions in one release group count as one album. Outside links from `enrich_mb_albums.py`.
- [X]  **Labels from MusicBrainz**: UPC to release to label (1,824 labels, original label for 3,776 albums), label pages (`/label/<mbid>`, label radio), Rankings > Labels. Still open: group label strings under parents/imprints; indie vs. major share over time; DJ label fingerprints; Chicago labels.
- [ ]  Compilation and reissue share per show.
- [X]  Artist pages: Wikidata's short description, home area with a flag and years (MusicBrainz), social links.
- [X]  To-do lists under the 🤓 (Wikidata artists, Wikidata albums, MusicBrainz albums); bulk mode.

## Linking to-do lists: ways to grow them

Slow and steady is fine (`TODO_PAGES` in `app.py`, one shared template; each new list needs a candidate table built at load time plus a live check from the browser). The weekly load picks up what people fix (`enrich_wikidata.main(no_data=True)`; `enrich_mb_albums.py` re-checks barcode matches for a Spotify link). The slow jobs (artist candidates for /todo/wikidata, refreshing every artist's links and descriptions) are in `maintenance.py`, every month or two. **Idea, not built:** make the candidate search incremental, so artists who appear after a show don't wait for the monthly run to show up on /todo/wikidata (search only artists not searched before; `--all` keeps the full rebuild).

- Artists whose Wikidata item we reached through MusicBrainz but which has no Spotify artist ID (P1902): near-certain.
- Artists MusicBrainz doesn't know at all (no Spotify link on any MusicBrainz artist).
- Labels: Wikidata items without a MusicBrainz label ID (P966), and labels with no Wikidata item.
- QuickStatements export of reviewed rows (once the account is autoconfirmed).

## /heath, ideas for more

Song length by release decade (the MusicBrainz dates are in now); per-DJ pace; back-to-backs by the same album or label.

## Reading, in one paragraph

An index of music writing from ~43 hand-picked feeds, matched to artists and albums, published as titles, dates and links (`/reading`, a page per publication, Rankings > Publications, Further reading on artist and album pages). Local crawl state in `writing_crawl.db`, matches published into `birdnest.db`. Full design, matching rules, routine and open items: `notes/reading.md`.

## Operations

| when | what | notes |
|---|---|---|
| weekly, after each show | `python load_playlist.py`, then `python load_djs.py add "LINE" --commit`, then `python refresh_writing.py` | the last needs no Spotify credentials; don't run them at the same time (both write `birdnest.db`) |
| every month or two | `python maintenance.py` | the slow Wikidata jobs |
| after any of the above | commit `birdnest.db`, `fly deploy --ha=false` | |
| occasionally | `python drift_check.py` | read-only canary for Spotify API restrictions |

- Fly apps: `birdsconferring` (the site, one 512 MB machine, volume-less; the database ships in the image). The Bluesky sampler (`birds-music-sampler`) was scaled to zero on 2026-10-18; see `~/src/music-sampler/README.md`.
- Running unattended (a scheduled job on the laptop, or in the cloud) was considered and parked: the DJ line and a look at the diff before deploying are a few minutes of weekly judgment worth keeping.
- Known weak spot: `artist_link` has at least one wrong row (Juana Molina's Bandcamp points at `daily.bandcamp.com`).

## Loose ends

- Spotify: the app is in development mode (add each friend to its user list).
- In dark mode using search, the highlight for the suggestion may still be too hard to see (not rechecked).
- `README.md` is the original scratch list of links and technologies; it could use a rewrite or a delete.
- Search suggestions: choosing one that maps to a single page (an artist, say) should go straight to that page, not run a text search. That means the suggestions need to carry what they point at. Some, like "Peter Gabriel; Peter Gabriel", are probably an artist/album pair where the name matches several distinct albums, so those need untangling first (one suggestion per artist, one per album, or a clear label for each).
