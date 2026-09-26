# Design pass: handoff notes (paused 2026-09-26)

From a paused session (its branch has since been deleted; see `notes/plan.md` for the combined plan). No CSS or template changes were made then and no screenshots were taken. It was paused because another session is exploring new ways into the site beyond the reverse-chron tile wall, which overlaps with the home page and banner parts of this pass.

## The owner's checklist (original brief)

1. **Responsive everywhere, starting with the home page.** No horizontal scroll at 375-400px, readable text, reasonable tap targets. Wide tables (`table.show-tracks`, `table.artist-tracks`, search) should drop low-value columns, stack, or scroll inside their own container. Include the radio page and the fixed `.radio-player` bar.
2. **Streamline the banner** (`templates/_nav.html`): more compact and tidy, working at phone width.
3. **Better tile layout on the home page** (`index.html`, `_playlist_tile.html`): keep the big image tiles, improve sizing, rhythm, dates/descriptions and reflow.
4. **Harmonize across pages** (show, artist, genre, genre map, rankings, DJs, search, radio): consistent type scale, spacing, colors and components. Keep some looseness and personality: "harmonious but not too polished, not too orderly, not thirstily disorderly." It's a hobby site for friends' weekly listening show.

Process asked for: small steps, before/after screenshots (desktop and phone) of home, a show page, an artist page and radio; propose the banner and tile direction before any large rewrite.

## Overlap with the "ways into the site" work

- **Direct conflict:** home page (`index.html`, `_playlist_tile.html`, tile CSS) and the nav (`_nav.html`), since new entry points will probably land as nav links or home page sections.
- **Low conflict:** viewport and meta fixes, shared design tokens, tables, show/artist/genre/rankings/DJ/search/radio pages, player bar.
- Both will touch `static/style.css`, so keep changes grouped by section. Probably easier to land the "ways in" work first and rebase the design pass onto it.

Suggested order: groundwork and the non-home pages first; propose the banner and tiles once the entry-points work settles.

## Findings from reading the code (not yet verified in a browser)

**Base template (`_base.html`)**
- No `<meta name="viewport">`, no `<!DOCTYPE html>`, no `<meta charset>`. Phones render at about 980px and zoom out, which is the root cause of most phone problems.

**Home tiles**
- `.playlist-tile` is a flex item with `width: calc(20% - 2px)` and `flex-grow: 1`, so the last row's leftover tiles stretch wider than the rest. A CSS grid with `repeat(auto-fill, minmax(...))` would reflow cleanly.
- Tile caption font is `2.5vw`: tiny on phones, large on wide screens. The caption bar is a fixed 15% of the tile height.
- ~~Tiles were `<button>`s that did nothing after a hard refresh.~~ Fixed in 13ba9fa: tiles are now `<a href>` links.
- Only the date is shown. Show descriptions (`history.shows[...]['description']`) are available but HTML-escaped (`&#x2F;`, `&quot;`) and start with boilerplate ("What we played for each other on 9/24/2026."). The "Playlist image: …" credit after it is good caption or hover material.
- 284 shows, 2020-04-07 to 2026-09-24. Images are lazy-loaded, with a vinyl placeholder on error.

**Nav (`_nav.html`)**
- A single flex row with no wrap: big `h1` title, links with `margin-left: 24px; margin-right: auto`, and a "search:" text label plus input. It will overflow or squash at phone width. The inline autocomplete script is Turbo-safe (runs in an IIFE, guards its global listener).

**Tables**
- `table.show-tracks` has a 700px rule hiding `.label` and `th:nth-child(4)`. `artist-tracks` also has class `show-tracks`, so its 4th column (Album, using class `label`) is hidden by the same rule. That works by coincidence and is fragile.
- `search-tracks` has no narrow-screen handling (links, thumb, track, artist, played).
- `_tracks_table.html` (16 audio-feature columns) needs a scroll container if it's still used anywhere.
- `.rank-row` has `minmax(160px, 280px)` for labels on desktop, with a 700px override.

**Radio (`radio.html`, `.radio-player`)**
- Page styles are an inline `<style>` in `extra_head`. They could move into `style.css` if they're harmonized.
- The player bar is `position: fixed` with `body { padding-bottom: 72px }`. At phone width it wraps to 2-3 rows (play, next, now-playing, sync, "Keep playing", logout), taller than 72px, so it will cover the bottom of the content. Consider a compact phone layout (hide or shorten logout and the "Keep playing" text) or measure its height in JS and set a CSS variable.
- `radio-player.js` relies on these classes and IDs: `#radio-player` and its `#rp-*` parts (the bar, the Tracks drawer with the live list `#rp-list` in `#rp-scroll`), `#radio-login` (the log-in bar), `.radio-set li` with `.is-playing`, `.is-played`, `.fresh`, `.play-from`, `.pick-no`, the /radio page's `#radio-controls`, `#radio-result` (with its `data-*`), `#radio-list`, `#radio-play-set`, `.radio-nav a`, `.radio-save input[name=tracks]`, and `:root.radio-bar` / `:root.player-ready`. Restyle freely but keep those hooks. (The player bar is now always shown when Spotify is configured, and its drawer adds height when open.)

**Visual language today (inputs for harmonizing)**
- Body is Arial. David Libre (Google Fonts) is used only for show and detail `h1`.
- Greys are hard-coded throughout (#222, #333, #444, #555, #666, #777, #999). Accent red #c0392b (marks, highlights, novelty). Radio plum #7a2e5e. Warm beige surfaces #f0eee9 / #f3f1ec. Badge palette: green, purple, blue.
- The nav is a dark #333 bar, which clashes a bit with the warm beige used elsewhere.
- Recurring components: `.detail-meta`, small-caps labels (`.novelty-label`, `.factoid dt`, `.radio-start dt`), `.chip`, `.badge`, segmented toggles (`.sort-toggle`, `.year-filter`). These are good candidates for shared tokens and classes.
- Breakpoint used everywhere: 700px.

## Setup (also in CLAUDE.md)

- App: `~/.virtualenvs/birdnest/bin/flask --app app run --port 5077`.
- Screenshots: `~/.virtualenvs/data/bin/python` has Playwright, with Chromium cached. A minimal script:

```python
from playwright.sync_api import sync_playwright
PAGES = {"home": "/", "show": "/playlist/2026-09-17",
         "artist": "/artist/4TMHGUX5WI7OOm53PqSDAT",   # Grateful Dead, most-played artist
         "radio": "/radio?artist=4TMHGUX5WI7OOm53PqSDAT&seed=1"}
SIZES = {"desktop": (1280, 900), "phone": (390, 844)}
with sync_playwright() as p:
    b = p.chromium.launch()
    for s, (w, h) in SIZES.items():
        ctx = b.new_context(viewport={"width": w, "height": h}, is_mobile=s == "phone", has_touch=s == "phone")
        pg = ctx.new_page()
        for name, path in PAGES.items():
            pg.goto("http://127.0.0.1:5077" + path, wait_until="networkidle")
            print(s, name, "scrollWidth", pg.evaluate("document.documentElement.scrollWidth"), "vs", w)
            pg.screenshot(path=f"{name}-{s}.png")
```
