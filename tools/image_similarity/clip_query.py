#!/usr/bin/env python3
"""
Find images in static/images that are semantically/visually similar to a
target image, using CLIP embeddings. Unlike phash_report.py (which flags
near-exact duplicates), this ranks the whole corpus by similarity to ONE
image you point it at -- either an image already in static/images, or a
brand new candidate file you haven't added yet.

Usage:
    python clip_query.py --target static/images/2025-05-01.jpeg
    python clip_query.py --target ~/Desktop/candidate.jpg --top 20

Embeddings are cached in .clip_cache.npz (next to this script), keyed by
file path + mtime + size, so re-runs after adding a few new images only
embed what's new.
"""
import argparse
import html as html_escaping
import webbrowser
from pathlib import Path

import numpy as np
import torch
from PIL import Image
import open_clip

from captions import load_captions, caption_for_key

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_IMAGES_DIR = REPO_ROOT / "static" / "images"
DEFAULT_CACHE = SCRIPT_DIR / ".clip_cache.npz"
DEFAULT_OUTPUT = SCRIPT_DIR / "clip_report.html"
EXTENSIONS = {".jpg", ".jpeg", ".png"}
MODEL_NAME = "ViT-B-32-quickgelu"
PRETRAINED = "openai"


def load_model():
    model, _, preprocess = open_clip.create_model_and_transforms(
        MODEL_NAME, pretrained=PRETRAINED
    )
    model.eval()
    return model, preprocess


def embed_image(path, model, preprocess):
    img = Image.open(path).convert("RGB")
    x = preprocess(img).unsqueeze(0)
    with torch.no_grad():
        feat = model.encode_image(x)
    feat = feat / feat.norm(dim=-1, keepdim=True)
    return feat.squeeze(0).numpy().astype(np.float32)


def load_cache(cache_path):
    if not cache_path.exists():
        return {}
    data = np.load(cache_path, allow_pickle=True)
    keys = data["keys"]
    fingerprints = data["fingerprints"]
    vectors = data["vectors"]
    return {
        k: {"fingerprint": fp, "vector": v}
        for k, fp, v in zip(keys, fingerprints, vectors)
    }


def save_cache(cache_path, cache):
    keys = list(cache.keys())
    fingerprints = [cache[k]["fingerprint"] for k in keys]
    vectors = np.stack([cache[k]["vector"] for k in keys]) if keys else np.zeros((0, 512))
    np.savez(cache_path, keys=np.array(keys), fingerprints=np.array(fingerprints), vectors=vectors)


def compute_embeddings(images_dir, cache, model, preprocess):
    files = sorted(p for p in images_dir.rglob("*") if p.suffix.lower() in EXTENSIONS)
    result = {}
    updated = False
    for i, path in enumerate(files):
        key = str(path.relative_to(images_dir))
        stat = path.stat()
        fingerprint = f"{stat.st_mtime_ns}:{stat.st_size}"
        entry = cache.get(key)
        if entry and entry["fingerprint"] == fingerprint:
            result[key] = entry["vector"]
            continue
        print(f"  embedding {key} ({i + 1}/{len(files)})")
        try:
            vector = embed_image(path, model, preprocess)
        except Exception as e:
            print(f"  skip {key}: {e}")
            continue
        cache[key] = {"fingerprint": fingerprint, "vector": vector}
        result[key] = vector
        updated = True
    return result, updated


def resolve_target(target_arg, images_dir, embeddings, model, preprocess):
    target_path = Path(target_arg).expanduser().resolve()
    if not target_path.exists():
        raise SystemExit(f"Target image not found: {target_path}")
    try:
        key = str(target_path.relative_to(images_dir.resolve()))
    except ValueError:
        key = None
    if key is not None and key in embeddings:
        return key, embeddings[key], target_path
    print(f"Embedding target image {target_path} ...")
    return None, embed_image(target_path, model, preprocess), target_path


def rank(embeddings, target_vector, exclude_key, top_n):
    scores = []
    for key, vector in embeddings.items():
        if key == exclude_key:
            continue
        sim = float(np.dot(vector, target_vector))
        scores.append((sim, key))
    scores.sort(reverse=True)
    return scores[:top_n]


def caption_html(key, captions):
    caption = caption_for_key(key, captions)
    if not caption:
        return ""
    return f'<div class="caption">{html_escaping.escape(caption)}</div>'


def render_html(target_path, target_key, matches, images_dir, output_path, captions):
    import os

    report_dir = output_path.parent
    if target_key is not None:
        target_rel = os.path.relpath(images_dir / target_key, report_dir)
    else:
        target_rel = os.path.relpath(target_path, report_dir)

    rows = []
    for sim, key in matches:
        rel = os.path.relpath(images_dir / key, report_dir)
        pct = round(sim * 100, 1)
        rows.append(
            f'<figure><img src="{rel}" loading="lazy">'
            f'<figcaption>{pct}% &middot; {key}{caption_html(key, captions)}</figcaption></figure>'
        )

    target_caption = caption_html(target_key, captions) if target_key else ""

    page = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>CLIP similarity report</title>
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
<h1>CLIP similarity report</h1>
<div class="meta">Ranked by cosine similarity to target ({MODEL_NAME} / {PRETRAINED})</div>
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
    parser.add_argument("--target", required=True, help="Path to the query image")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--images-dir", type=Path, default=DEFAULT_IMAGES_DIR)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    print("Loading CLIP model ...")
    model, preprocess = load_model()

    cache = load_cache(args.cache)
    print(f"Scanning {args.images_dir} ...")
    embeddings, updated = compute_embeddings(args.images_dir, cache, model, preprocess)
    print(f"{len(embeddings)} images embedded ({'cache updated' if updated else 'all from cache'})")
    if updated:
        save_cache(args.cache, cache)

    target_key, target_vector, target_path = resolve_target(
        args.target, args.images_dir, embeddings, model, preprocess
    )
    matches = rank(embeddings, target_vector, target_key, args.top)
    captions = load_captions()

    render_html(target_path, target_key, matches, args.images_dir, args.output, captions)
    print(f"Report written to {args.output}")
    if not args.no_open:
        webbrowser.open(args.output.resolve().as_uri())


if __name__ == "__main__":
    main()
