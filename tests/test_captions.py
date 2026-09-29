"""Cover captions from the Google Doc (load_captions.py), used when a show's description doesn't say."""
from datetime import date

import load_captions

DOC = """# Birds, Conferring

## past playlists (spotify)

  - Thursday, May 14th, 2020 (DJs: Joe)

## **Birds**

At a certain point, I thought it would be fun to keep track of them, so…

### 5/14/2020: Bearded Reedling Panurus biarmicus aka “Bearded Tit”

### 5/21/2020: Livingstone's turaco *Tauraco livingstonii* (via [Nnedi Okorafor](https://twitter.com/Nnedi/status/1))

### 6/11/2020: *The Gramophone Gullfinch* *
*illustration from [The Ice Cream Cone Coot](https://www.goodreads.com/x) (1971)

A paragraph after a blank line isn't part of the caption.

## **4/8/2021: Chicks in Tutus**[ **via @birdcardigan**](https://mobile.twitter.com/x)

### 11/18/2021: Ayam Cemani from Indonesia [via @Weird\\_AnimaIs](https://x)

### 12/23/2021: merry christmas from the bird mob ð¦ð¥ via [@spacecoyotl](https://x)

### 1/4/2024: Hatched chick and eggshells

### 1/11/2024: Silver Pheasant *Lophura nycthemera*

### 1/18/2023: Female Northern Cardinal

### 

### 7/11/3034: Penguin encounters Beluga

### 10/10/2024
"""


def test_parse_headings_links_and_continuations():
    got = {d: c for d, c, _ in load_captions.parse(DOC)}
    assert got[date(2020, 5, 14)] == 'Bearded Reedling Panurus biarmicus aka “Bearded Tit”'
    assert got[date(2020, 5, 21)] == "Livingstone's turaco Tauraco livingstonii (via Nnedi Okorafor)"
    assert got[date(2020, 6, 11)] == 'The Gramophone Gullfinch illustration from The Ice Cream Cone Coot (1971)'
    assert got[date(2021, 4, 8)] == 'Chicks in Tutus via @birdcardigan'
    assert date(2024, 10, 10) not in got, 'a date with no caption is skipped'
    assert got[date(2021, 11, 18)] == 'Ayam Cemani from Indonesia via @Weird_AnimaIs', 'escaped underscores in handles'
    assert got[date(2021, 12, 23)] == 'merry christmas from the bird mob via @spacecoyotl', 'mangled emoji dropped'


def test_a_year_out_of_order_is_a_typo():
    parsed = load_captions.parse(DOC)
    d, caption, warnings = next(p for p in parsed if p[1] == 'Female Northern Cardinal')
    assert d == date(2024, 1, 18) and warnings
    assert next(p for p in parsed if p[1].startswith('Penguin'))[0] == date(2024, 7, 11)
    assert next(p for p in parsed if p[1].startswith('Silver'))[0] == date(2024, 1, 11), 'a gap in the doc is not a typo'


def test_description_caption_first_then_the_doc(app_module):
    import factoids
    h = factoids.get_history()
    with_desc = next(p for p in h.show_tracks if 'Playlist image:' in (h.shows[p]['description'] or ''))
    assert h.show_caption(with_desc)['image'] not in ('', h.cover_captions.get(with_desc))
    older = next(p for p in sorted(h.show_tracks, key=h.show_dates.get) if p in h.cover_captions
                 and 'Playlist image:' not in (h.shows[p]['description'] or ''))
    assert h.show_caption(older)['image'] == h.cover_captions[older]
