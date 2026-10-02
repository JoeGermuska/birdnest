# birdnest

Flask site for Conference of the Birds (deployed at birdsconferring.fly.dev).

## Running locally

- Python virtualenv: `~/.virtualenvs/birdnest` (not activated by default in agent shells; call its binaries directly).
- Run the app: `~/.virtualenvs/birdnest/bin/flask --app app run` (uses the committed `birdnest.db`).
- Screenshots and browser tests: the birdnest venv has Playwright (from `requirements-dev.txt`), but its own Chromium build isn't downloaded. Point it at a cached one: `PLAYWRIGHT_CHROMIUM="$HOME/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"`.

- In a Claude Code cloud session there's no virtualenv: `pip install -r requirements.txt` and run `python3 app.py` (PORT env var); Chromium for Playwright is at `/opt/pw-browsers/chromium`.

## Data upkeep

- Weekly, after each show: `python load_playlist.py` (the show, its artists, albums and tracks, a few MusicBrainz
  retries, and links people added through the to-do lists under the 🤓 in the banner).
- Every month or two: `python maintenance.py` (the slow Wikidata jobs). Then commit `birdnest.db` and deploy.

## Tests

- `pip install -r requirements-dev.txt`, then `pytest tests` (about 30s). `tests/test_radio.py` covers the sequencer
  and its endpoints; `tests/test_player.py` drives the radio player in Chromium against a fake Spotify
  (`tests/fake_spotify.js`: the Web API and Web Playback SDK; `tests/fake_spotify_embed.js`: the Embed iFrame API; both controllable from tests). Browser tests skip without
  Playwright/Chromium. Change the player? Add or adjust a test there first.

## Notes

- `notes/genres.md`: open questions and an experiment idea for genre tagging.
- `notes/plan.md`: the current plan for the design pass and new ways into the home page, with `notes/design-pass.md` and `notes/home-page-options.md` behind it.

## Front end

- Templates in `templates/` extend `_base.html`; shared CSS in `static/style.css`.
- Turbo Drive (vendored in `static/vendor/`) keeps Birds radio playing across pages. Page scripts must be safe to re-run on each page view: no DOMContentLoaded-only init, no top-level `let`/`const` in inline body scripts.
- Birds radio: `radio.py` sequences sets (knows nothing about Spotify); `static/radio-player.js` is the in-page Spotify player and its always-present bar (`data-turbo-permanent`). It keeps Spotify's player iframe alive by overriding Turbo's body render, so don't replace `<body>` wholesale elsewhere.
- The player has two engines (the bar's `data-mode`): `sdk` (Web Playback SDK + Web API) for listeners logged in with Spotify, and `embed` (Spotify's Embed iFrame API, no login, previews unless logged in to Spotify in the browser) for everyone else. The Spotify app is in development mode, which allows only 5 authorized accounts, so `embed` is what the public gets.
