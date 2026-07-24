#!/usr/bin/env python3
"""
Find near-duplicate / visually similar images in static/images using
perceptual hashing (pHash).

Two modes:

  Corpus-wide clustering (default) -- flags near-duplicate groups across
  the whole directory:
    python phash_report.py                     # threshold 10, opens report.html
    python phash_report.py --threshold 6        # stricter (only very close matches)
    python phash_report.py --threshold 16       # looser (more, fuzzier matches)

  Target mode -- ranks the whole corpus by closeness to ONE image, which
  can be an existing file in static/images or a brand new candidate
  elsewhere on disk (check before you add it):
    python phash_report.py --target static/images/2025-05-01.jpeg
    python phash_report.py --target ~/Desktop/candidate.jpg --top 20

Caches hashes in .phash_cache.json (next to this script) keyed by file
path + mtime + size, so re-runs after adding a few new images are fast.
"""
import argparse
import html as html_escaping
import json
import os
import webbrowser
from pathlib import Path

from PIL import Image
import imagehash

from captions import load_captions, caption_for_key

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_IMAGES_DIR = REPO_ROOT / "static" / "images"
DEFAULT_CACHE = SCRIPT_DIR / ".phash_cache.json"
DEFAULT_OUTPUT = SCRIPT_DIR / "report.html"
DEFAULT_TARGET_OUTPUT = SCRIPT_DIR / "phash_query_report.html"
EXTENSIONS = {".jpg", ".jpeg", ".png"}
HASH_BITS = 64  # imagehash.phash default hash_size=8 -> 8x8 bits


def load_cache(cache_path):
    if cache_path.exists():
        with open(cache_path) as f:
            return json.load(f)
    return {}


def save_cache(cache_path, cache):
    with open(cache_path, "w") as f:
        json.dump(cache, f)


def compute_hashes(images_dir, cache):
    files = sorted(
        p for p in images_dir.rglob("*") if p.suffix.lower() in EXTENSIONS
    )
    hashes = {}
    updated = False
    for path in files:
        key = str(path.relative_to(images_dir))
        stat = path.stat()
        fingerprint = f"{stat.st_mtime_ns}:{stat.st_size}"
        entry = cache.get(key)
        if entry and entry.get("fingerprint") == fingerprint:
            hashes[key] = imagehash.hex_to_hash(entry["hash"])
            continue
        try:
            with Image.open(path) as img:
                h = imagehash.phash(img)
        except Exception as e:
            print(f"  skip {key}: {e}")
            continue
        cache[key] = {"fingerprint": fingerprint, "hash": str(h)}
        hashes[key] = h
        updated = True
    return hashes, updated


def cluster(hashes, threshold):
    keys = list(hashes.keys())
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    pair_distances = {}
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            dist = hashes[a] - hashes[b]
            if dist <= threshold:
                union(a, b)
                pair_distances[(a, b)] = dist

    groups = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)

    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        min_dist = min(
            (pair_distances.get((a, b), pair_distances.get((b, a)))
             for i, a in enumerate(members) for b in members[i + 1:]
             if (a, b) in pair_distances or (b, a) in pair_distances),
            default=None,
        )
        clusters.append((min_dist, members))

    clusters.sort(key=lambda c: (c[0] if c[0] is not None else 999))
    return clusters


def resolve_target(target_arg, images_dir, hashes):
    target_path = Path(target_arg).expanduser().resolve()
    if not target_path.exists():
        raise SystemExit(f"Target image not found: {target_path}")
    try:
        key = str(target_path.relative_to(images_dir.resolve()))
    except ValueError:
        key = None
    if key is not None and key in hashes:
        return key, hashes[key], target_path
    print(f"Hashing target image {target_path} ...")
    with Image.open(target_path) as img:
        h = imagehash.phash(img)
    return None, h, target_path


def rank_by_target(hashes, target_hash, exclude_key, top_n):
    scores = []
    for key, h in hashes.items():
        if key == exclude_key:
            continue
        scores.append((h - target_hash, key))
    scores.sort(key=lambda x: x[0])
    return scores[:top_n]


def caption_html(key, captions):
    caption = caption_for_key(key, captions)
    if not caption:
        return ""
    return f'<div class="caption">{html_escaping.escape(caption)}</div>'


def render_html(clusters, images_dir, output_path, threshold, total_images, captions):
    report_dir = output_path.parent
    rows = []
    for min_dist, members in clusters:
        thumbs = []
        for key in sorted(members):
            rel = os.path.relpath(images_dir / key, report_dir)
            thumbs.append(
                f'<figure><img src="{rel}" loading="lazy">'
                f'<figcaption>{key}{caption_html(key, captions)}</figcaption></figure>'
            )
        rows.append(
            f'<section class="cluster">'
            f'<h2>closest distance: {min_dist} &middot; {len(members)} images</h2>'
            f'<div class="thumbs">{"".join(thumbs)}</div>'
            f'</section>'
        )

    page = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Image similarity report</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #111; color: #eee; }}
  h1 {{ font-size: 1.2rem; }}
  .meta {{ color: #999; margin-bottom: 2rem; }}
  .cluster {{ border-top: 1px solid #333; padding: 1rem 0; }}
  .cluster h2 {{ font-size: 0.95rem; color: #9cf; font-weight: normal; }}
  .thumbs {{ display: flex; flex-wrap: wrap; gap: 1rem; }}
  figure {{ margin: 0; width: 220px; }}
  figure img {{ max-width: 220px; max-height: 220px; display: block; border-radius: 4px; }}
  figcaption {{ font-size: 0.75rem; color: #aaa; word-break: break-all; margin-top: 0.25rem; }}
  figcaption .caption {{ color: #9cf; font-style: italic; word-break: normal; }}
</style></head>
<body>
<h1>Image similarity report</h1>
<div class="meta">{total_images} images scanned in static/images &middot;
  Hamming distance threshold: {threshold} &middot; {len(clusters)} candidate group(s) found</div>
{"".join(rows) if rows else "<p>No matches under this threshold. Try raising --threshold.</p>"}
</body></html>"""
    output_path.write_text(page)


def render_target_html(target_path, target_key, matches, images_dir, output_path, captions):
    report_dir = output_path.parent
    if target_key is not None:
        target_rel = os.path.relpath(images_dir / target_key, report_dir)
    else:
        target_rel = os.path.relpath(target_path, report_dir)

    rows = []
    for dist, key in matches:
        rel = os.path.relpath(images_dir / key, report_dir)
        pct = round((1 - dist / HASH_BITS) * 100, 1)
        rows.append(
            f'<figure><img src="{rel}" loading="lazy">'
            f'<figcaption>distance {dist} ({pct}%) &middot; {key}'
            f'{caption_html(key, captions)}</figcaption></figure>'
        )

    target_caption = caption_html(target_key, captions) if target_key else ""

    page = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>pHash similarity report</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #111; color: #eee; }}
  h1 {{ font-size: 1.2rem; }}
  .meta {{ color: #999; margin-bottom: 1.5rem; }}
  .target {{ margin-bottom: 2rem; }}
  .target img {{ max-width: 320px; max-height: 320px; border-radius: 4px; border: 2px solid #9cf; }}
  .thumbs {{ display: flex; flex-wrap: wrap; gap: 1rem; }}
  figure {{ margin: 0; width: 220px; }}
  figure img {{ max-width: 220px; max-height: 220px; display: block; border-radius: 4px; }}
  figcaption {{ font-size: 0.75rem; color: #aaa; word-break: break-all; margin-top: 0.25rem; }}
  figcaption .caption, .target .caption {{ color: #9cf; font-style: italic; word-break: normal; }}
</style></head>
<body>
<h1>pHash similarity report</h1>
<div class="meta">Ranked by Hamming distance (lower = more alike) to target</div>
<div class="target">
  <img src="{target_rel}">
  <div>target: {target_key or target_path}</div>
  {target_caption}
</div>
<div class="thumbs">{"".join(rows)}</div>
</body></html>"""
    output_path.write_text(page)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images-dir", type=Path, default=DEFAULT_IMAGES_DIR)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--threshold", type=int, default=10,
        help="Max Hamming distance (0-64) to consider a match. Lower = stricter. Default 10. "
             "Ignored in --target mode.",
    )
    parser.add_argument(
        "--target", help="Rank the whole corpus by closeness to this image instead of clustering.",
    )
    parser.add_argument(
        "--top", type=int, default=12, help="Number of matches to show in --target mode.",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="Don't open the report in a browser when done.",
    )
    args = parser.parse_args()

    cache = load_cache(args.cache)
    print(f"Scanning {args.images_dir} ...")
    hashes, updated = compute_hashes(args.images_dir, cache)
    print(f"Hashed {len(hashes)} images ({'cache updated' if updated else 'all from cache'})")
    if updated:
        save_cache(args.cache, cache)

    captions = load_captions()

    if args.target:
        output = args.output or DEFAULT_TARGET_OUTPUT
        target_key, target_hash, target_path = resolve_target(args.target, args.images_dir, hashes)
        matches = rank_by_target(hashes, target_hash, target_key, args.top)
        render_target_html(target_path, target_key, matches, args.images_dir, output, captions)
    else:
        output = args.output or DEFAULT_OUTPUT
        clusters = cluster(hashes, args.threshold)
        print(f"Found {len(clusters)} candidate group(s) at threshold {args.threshold}")
        render_html(clusters, args.images_dir, output, args.threshold, len(hashes), captions)

    print(f"Report written to {output}")
    if not args.no_open:
        webbrowser.open(output.resolve().as_uri())


if __name__ == "__main__":
    main()
