# birdnest

Flask site for Conference of the Birds (deployed at birdsconferring.fly.dev).

## Running locally

- Python virtualenv: `~/.virtualenvs/birdnest` (not activated by default in agent shells; call its binaries directly).
- Run the app: `~/.virtualenvs/birdnest/bin/flask --app app run` (uses the committed `birdnest.db`).
- Screenshots: the birdnest venv has no Playwright. `~/.virtualenvs/data/bin/python` has the Playwright package, and Chromium is cached in `~/Library/Caches/ms-playwright`; use it as a client against the local server.

- In a Claude Code cloud session there's no virtualenv: `pip install -r requirements.txt` and run `python3 app.py` (PORT env var); Chromium for Playwright is at `/opt/pw-browsers/chromium`.

## Tests

- `pip install -r requirements-dev.txt`, then `pytest tests` (about 30s). `tests/test_radio.py` covers the sequencer
  and its endpoints; `tests/test_player.py` drives the radio player in Chromium against a fake Spotify
  (`tests/fake_spotify.js`: the Web API and Web Playback SDK, controllable from tests). Browser tests skip without
  Playwright/Chromium. Change the player? Add or adjust a test there first.

## Notes

- `notes/genres.md`: open questions and an experiment idea for genre tagging.
- `notes/plan.md`: the current plan for the design pass and new ways into the home page, with `notes/design-pass.md` and `notes/home-page-options.md` behind it.

## Front end

- Templates in `templates/` extend `_base.html`; shared CSS in `static/style.css`.
- Turbo Drive (vendored in `static/vendor/`) keeps Birds radio playing across pages. Page scripts must be safe to re-run on each page view: no DOMContentLoaded-only init, no top-level `let`/`const` in inline body scripts.
- Birds radio: `radio.py` sequences sets (knows nothing about Spotify); `static/radio-player.js` is the in-page Spotify player and its always-present bar (`data-turbo-permanent`). It keeps Spotify's player iframe alive by overriding Turbo's body render, so don't replace `<body>` wholesale elsewhere.
