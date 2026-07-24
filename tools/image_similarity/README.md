# Image similarity tools

Local-only helpers for checking whether a new image is a near-duplicate of
(or looks a lot like) something already in `static/images`. Not part of the
deployed app — nothing here ships to Fly.

Both scripts run against the `birdnest` virtualenv:
`/Users/germuska/.virtualenvs/birdnest` (`workon birdnest` or call its
`bin/python` directly). Dependencies (`Pillow`, `ImageHash`, `open_clip_torch`)
are installed there — see `requirements.txt` for the list, though it's
documentation more than something you `pip install -r` fresh, since it's
already installed in the shared venv.

## phash_report.py — near-duplicate finder

Perceptual-hashes every image in `static/images`, clusters images within a
Hamming-distance threshold, and writes an HTML report with thumbnails so you
can eyeball each cluster. Good at catching exact reuse: same photo re-saved,
resized, cropped, or converted between jpg/png.

```bash
python phash_report.py                 # default threshold 10, opens report.html
python phash_report.py --threshold 6   # stricter — only near-exact matches
python phash_report.py --threshold 16  # looser — more (fuzzier) matches
```

Caveat: above a threshold of ~12-15, clusters can chain unrelated images
together transitively (A~B~C flagged as one group even if A and C aren't
alike) — treat loose thresholds as noisy and eyeball each cluster.

It also supports target mode, same idea as `clip_query.py` below: rank the
whole corpus by Hamming distance to one image instead of clustering
everything. `--target` can be an existing file in `static/images` or a
candidate elsewhere on disk you haven't added yet.

```bash
python phash_report.py --target ../../static/images/2025-05-01.jpeg
python phash_report.py --target ~/Desktop/candidate.jpg --top 20
```

Opens `phash_query_report.html` by default (cluster mode still writes
`report.html`). Note pHash's ranking is only meaningful near the top — past
the true near-duplicates it falls off a cliff into noise (unrelated images
at "65% similar" are not actually alike), unlike CLIP's ranking below, which
degrades more gracefully.

## clip_query.py — "does anything look like *this*?"

Embeds every image with CLIP (`ViT-B-32-quickgelu` / `openai` weights) and
ranks the whole corpus by cosine similarity to one target image. Catches
semantic/visual similarity that pHash can't (same illustration style,
similar composition, same species) — but it's a ranked list against a single
query, not a corpus-wide clustering, since "everything is somewhat similar to
something" makes CLIP-based clustering mostly noise at this scale.

```bash
python clip_query.py --target ../../static/images/2025-05-01.jpeg
python clip_query.py --target ~/Desktop/candidate.jpg --top 20
```

`--target` can be:
- an image already in `static/images` (excluded from its own results), or
- any file elsewhere on disk — e.g. a candidate you haven't added yet, so you
  can check before you commit to it.

First run downloads CLIP weights (one-time) and embeds the full corpus
(~1 min on CPU for ~260 images); later runs only embed new/changed files.

## captions.py — shared helper, not run directly

Both reports show the `"Playlist image: ..."` caption from `birdnest.db`
next to each thumbnail, when one exists (matched by date against the
`playlist.date` column — only ~160 of 274 rows have that phrase in their
description, so plenty of images just won't have a caption). Purely for
your own visual/textual context — it does not feed into either
similarity calculation.

## Caches

Both scripts cache their results next to the script, keyed by file path +
mtime + size, so re-runs after adding a few images only process what's new:

- `.phash_cache.json` — pHash values
- `.clip_cache.npz` — CLIP embeddings

Delete either file to force a full recompute. Both files, plus the generated
`report.html` / `clip_report.html`, are gitignored.

## A note on the venv

The `birdnest` virtualenv's Python is an x86_64 build (running under
Rosetta on Apple Silicon), which caps `torch` at 2.2.2 — the last version
with a macOS x86_64 wheel. That version predates NumPy 2.x's ABI, so numpy
in this venv is pinned to `<2` (currently 1.26.4) to keep `torch` working.
This doesn't affect `app.py`/`models.py` (neither touches numpy), only the
analysis notebooks, which are arguably more compatible with numpy 1.x anyway
given how old `pandas==1.2.3` is.
