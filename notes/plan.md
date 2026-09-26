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
