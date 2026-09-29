"""Record what each show's playlist image is (the cover_caption table), from the "Birds" section of the
Conference of the Birds Google Doc, which kept a caption per week before they went into the Spotify
playlist descriptions ("Playlist image: ..."). The site uses these only when a description has none.

Download the doc as Markdown (File > Download > Markdown), then:

    python load_captions.py PATH [--commit]

Captions are headings like

    ### 5/21/2020: Livingstone's turaco *Tauraco livingstonii* (via [Nnedi Okorafor](https://...))

and may run onto the lines after. Links keep their text; Markdown emphasis is dropped. A year that's out of
order with its neighbors is taken for a typo and replaced. Without --commit, reports what would be loaded.
Loading replaces the caption for each matched show, so rerunning after fixing the doc is the way to correct it.
"""
import re
import sys
from datetime import date

import load_djs
import models

HEADING_RE = re.compile(r'^#{2,3}\s*\**\s*(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})\s*:?\s*(?P<text>.*)$')
SECTION_RE = re.compile(r'^#{1,2}\s*\**Birds\**\s*$')


def clean(text):
    """Markdown to plain text: links keep their text, images and emphasis go."""
    t = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    t = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', t)
    t = re.sub(r'\(\s*more\s*\)', '', t)  # "(more)" links to further reading
    t = re.sub(r'ð[\x80-\xff]+', '', t)  # emoji mangled to Latin-1 by some exports
    t = t.replace('\\_', '_').replace('\\', '').replace('*', '')
    t = re.sub(r'\s+', ' ', t)
    t = re.sub(r'\s+([,.;:)])', r'\1', t).replace('( ', '(').replace('()', '')
    t = re.sub(r',\(', ', (', t)
    return t.strip(' :')


def parse(text):
    """[(date, caption, warnings)] in the doc's order (oldest first) from the Birds section, or the whole
    text if there's no such heading. Headings without a caption are skipped."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if SECTION_RE.match(l.strip())), -1) + 1
    raw, i = [], start  # [(month, day, year, caption)]
    while i < len(lines):
        m = HEADING_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        parts, j = [m['text']], i + 1
        while j < len(lines) and lines[j].strip() and not lines[j].lstrip().startswith(('#', '- ', '* ')):
            parts.append(lines[j])
            j += 1
        i = j
        raw.append((int(m['m']), int(m['d']), int(m['y']), clean(' '.join(parts))))
    # A year that puts an entry before the one above it, or leaps it more than a year past the one above and
    # after the one below, is a typo (1/18/2023 between two 2024 entries; 7/11/3034): take the year that fits.
    out, prev = [], None
    for k, (month, day, year, caption) in enumerate(raw):
        d, warnings = date(year, month, day), []
        nxt = None
        if k + 1 < len(raw):
            nm, nd, ny, _ = raw[k + 1]
            nxt = date(ny, nm, nd)
        if prev and (d < prev or (nxt and d > nxt and nxt >= prev and (d - prev).days > 366)):
            fits = [date(y, month, day) for y in (prev.year, prev.year + 1)
                    if prev <= date(y, month, day) and (not nxt or nxt < prev or date(y, month, day) <= nxt)]
            if fits:
                warnings.append(f"{d} is out of order; taking {fits[0]}")
                d = fits[0]
        if caption:
            out.append((d, caption, warnings))
        prev = d
    return out


def import_doc(path, commit=False):
    session = models.get_session()
    dates = load_djs.playlist_dates(session)
    with open(path, encoding='utf-8') as f:
        found = parse(f.read())
    entries = [load_djs.LogEntry(line=c, month=d.month, day=d.day, year=d.year, djs=[], date=d, warnings=w)
               for d, c, w in found]
    matched, unmatched = load_djs.match_playlists(entries, dates)
    print(f"{len(entries)} captions in {path}; {len(matched)} matched to playlists")
    load_djs.report(entries, matched, unmatched)
    if commit:
        for e in entries:
            if id(e) in matched:
                session.merge(models.CoverCaption(playlist_id=matched[id(e)], caption=e.line))
        session.commit()
        print("committed")


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if a != '--commit']
    if len(args) != 1:
        sys.exit(__doc__)
    import_doc(args[0], commit='--commit' in sys.argv)
