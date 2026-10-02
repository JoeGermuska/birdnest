"""Small copies of the show images for tiles (home page, /shows), in static/images/tiles/.

The originals run up to 2000px and 1MB; a tile is a few hundred pixels wide, and every image goes through the
app's single worker, so a page of full-size tiles kept its threads busy feeding slow downloads. Tiles are
TILE_PX square WebP, center-cropped like the CSS (object-fit: cover). Built at image build (Dockerfile) and after
loading a show (load_playlist.py); only missing or out-of-date tiles are made. Not committed (.gitignore).
    python make_tiles.py
"""
import os
import sys

from PIL import Image, ImageOps

TILE_PX = 480  # about twice the widest grid tile
IMAGES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'images')
TILES = os.path.join(IMAGES, 'tiles')
EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')


def tile_path(date_str):
    return os.path.join(TILES, f"{date_str}.webp")


def main(images=IMAGES):
    os.makedirs(TILES, exist_ok=True)
    made = 0
    for name in sorted(os.listdir(images)):
        stem, ext = os.path.splitext(name)
        if ext.lower() not in EXTENSIONS or not stem[:4].isdigit():
            continue
        src, dst = os.path.join(images, name), tile_path(stem)
        if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            continue
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert('RGB')
            ImageOps.fit(im, (TILE_PX, TILE_PX), Image.LANCZOS).save(dst, 'WEBP', quality=80, method=6)
        made += 1
    print(f"made {made} tiles in {TILES}")


if __name__ == '__main__':
    main(*sys.argv[1:])
