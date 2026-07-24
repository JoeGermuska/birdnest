"""Look up the "Playlist image: ..." caption for a given static/images file,
by matching the file's date-based stem against playlist.date in birdnest.db.
Not every playlist has one -- only ~160 of 274 rows include the phrase.
"""
import html
import re
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "birdnest.db"

CAPTION_RE = re.compile(r"Playlist image:\s*(.+)$", re.IGNORECASE | re.DOTALL)


def load_captions(db_path=DEFAULT_DB):
    if not Path(db_path).exists():
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT date, description FROM playlist").fetchall()
    finally:
        conn.close()

    captions = {}
    for date, description in rows:
        if not description:
            continue
        text = html.unescape(description)
        m = CAPTION_RE.search(text)
        if m:
            captions[date] = m.group(1).strip()
    return captions


def caption_for_key(key, captions):
    """key is a path relative to static/images, e.g. '2021-02-25.jpg'."""
    return captions.get(Path(key).stem)
