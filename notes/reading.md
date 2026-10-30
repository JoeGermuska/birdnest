# Reading: the index of music writing

Status as of 2026-10-18. The site has a **Reading** page (`/reading`), a page per publication (`/reading/<slug>`), a **Publications** tab under Rankings, and a "Further reading" section on artist and album pages. It lists writing about the artists and albums we've played, from a hand-picked list of publications and writers. We link out; the writing is theirs. How it works, what we decided, and what's open.

## Why it exists

It started as "could we surface conversations about music from Bluesky?" and ended up somewhere better: an index of real music writing (criticism, essays, interviews) tied to the artists and albums in our data. The trigger was finding a good review of something we were listening to (a closer listen's piece on Takaat) and wanting that kind of page, not a database record, linked from our page for the artist. Writing is a much better fit than social posts: the sources are a finite, curated list; most publish RSS or a sitemap; and a review usually names the artist and the album.

## Bluesky exploration (October 2026), parked

A sampler on Fly read the Jetstream for ten days (Oct 5-14, ~35M posts; now stopped, see `~/src/music-sampler`). What it said:

- About 3.5M posts a day, 45% replies, ~22% with a link. Only ~0.5% (about 17k a day) carry a strong music signal (a link to Spotify/Bandcamp/Apple Music/etc. or a hashtag like #nowplaying); another ~63k a day link only to YouTube.
- About half of the strong music posts come from ~110 automated accounts (radio stations posting #nowplaying). ~23k accounts posted music at all; only ~4,700 on 3+ days and ~1,400 on 7+ of the 10 days.
- Birds artists in that stream: ~105 posts a day match by exact link (Bandcamp resolves best, ~6%), a few thousand a day by name in a link card or text, of which many are mainstream and bots. Name matching with no other signal is about half wrong ("Directions", "Skeletons", "Real Estate").
- Only ~8% of music posts are replies, and we recorded replies only when they carried a music signal themselves, so real conversation couldn't be measured.
- Verdict: too thin and too noisy to be a feed on its own. Ideas that survived: Constellation (a backlink index) for "who on Bluesky linked this page" on demand; a rank-and-annotate lens rather than a gate; people over posts. None built. The data is archived in `~/src/music-sampler/archive/` (gzipped JSONL).

## How it works

```
writing_sources.json  ->  writing.py  ->  writing_crawl.db  ->  (publish)  ->  birdnest.db  ->  the site
 (the list of feeds)      (fetch, match)   (local, NOT committed)               (writing, writing_source,
                                                                                 writing_match tables)
```

- **`writing_sources.json`** (committed): one entry per source: name, home_url, feed_url, kind (`criticism`, `essay`, `interview`, `news`, `podcast`), optional `members_only`, `roundup`, and `backfill: "feed"` for sites with no usable sitemap. Edit it or use `writing.py source add URL` / `source remove NAME`. A source dropped from the file is switched off, not deleted.
- **`writing_crawl.db`** (local, gitignored): every item ever seen (url, title, date, tags, a short excerpt, body text, links), per-source crawl state (etag, next check, failures), the backfill's progress, your review verdicts, vetoes and hidden pieces. It is large and private-ish, and it's what lets a newly played artist be matched to old pieces.
- **Teasers.** Some feeds (PopMatters, Loud and Quiet, Chicago Reader, Stereogum, Bandcamp Daily...) carry only a sentence, so a name that appears only inside the article was never seen (an interview tagged "João Donato" that also named Jobim matched only Donato). `writing.py deepen` (40 pages a source in each weekly `run`, newest first) fetches the full page of any item holding under 1,000 characters, once (`item.deepened`: 1 read, 2 no text), and matches it again. The 40 a week is deliberately gentle, so a big pile of waiting teasers (a newly added source, say) takes weeks; to work through it faster in one sitting run `python writing.py deepen --source NAME --pages 300` (about 10 minutes, 2 s a request). A source whose first 20 pages mostly held nothing more than its feed is left alone, with a message. `writing.py why URL [ARTIST]` shows what we hold for a piece and why it did or didn't match.
- **`birdnest.db`** gets only what's published: `writing` (url, title, date, source, members flag), `writing_source` (slug, name, kind, home url) and `writing_match` (which piece is about or mentions which artist and album). Titles, dates and links only; no excerpt or text is published.
- **News** sources (Stereogum, Gorilla vs. Bear, Spin, Paste, Avant Music News) are fetched and matched but never published; they're not what we're listing.
- **Members:** only Aquarium Drunkard is marked. Its public feed gives a title, artist tags and an excerpt, which is enough to match; we never log in or go past a paywall. The mark just says "open it while signed in".

## The routine

- **Weekly, after `load_playlist.py`:** `python refresh_writing.py` re-matches everything stored against the artists as they are now, checks the feeds that are due, runs one gentle slice of the archive backfill, publishes into `birdnest.db`, rebuilds the cache. Then commit `birdnest.db` and deploy. `--no-backfill` for the quick version. Don't run it at the same time as `load_playlist.py`: both write `birdnest.db`. No Spotify credentials needed.
- **By hand:** `python writing.py help` lists everything. The ones you'll use: `sources` (the whole set and what each yields), `review` (the borderline matches), `veto ARTIST`, `hide URL`, `duplicates`, `report`, `run`, `rematch`, `backfill`, `deepen`, `why`, `publish`, `source add|remove|on|off`.
- **Pacing:** each feed is checked again after about half the typical gap between its posts (12 hours to 2 weeks). Failures back off.
- **Gentleness** (see `writing_backfill.py`): `robots.txt` is respected, with `Crawl-delay` if longer; at most one request per 2 seconds to a host; small default budgets per source per run (15 sitemap files, 50 pages, 5 API/feed requests, 5 minutes per phase) so an archive fills in over weeks; a 429 or 503 leaves a site alone for the run; a User-Agent that names the site and carries no email.

## Matching (`writing_match.py`)

A piece is matched from its title, URL slug, feed tags, body text (or excerpt) and the links in it. Signals and scores (combined as independent odds, `1 - prod(1 - s)`):

| signal | meaning | score (distinctive name / ambiguous name) |
|---|---|---|
| `pair_title`, `pair_slug` | the artist and one of their albums or tracks both named | 0.95 / 0.85, 0.85 / 0.70 |
| `link` | a link to the artist's Bandcamp, Spotify, MusicBrainz or Discogs page | 0.95 |
| `tag` | a feed tag that is exactly the artist's name | 0.90 / 0.55 |
| `title`, `slug` | the artist named | 0.75 / 0.35, 0.60 / 0.25 |
| `body_pair` | in the body, with one of their albums | 0.80 / 0.50 |
| `body_long`, `body` | in the body only | 0.60 / 0.20 (kept as "mentions" at 0.60+) |

A piece "about" an artist has a title, slug or tag signal; the rest are "mentions". At 0.75 and above an "about" match is accepted; between 0.35 and 0.75 it goes to review; below that it's dropped. Mentions are kept at 0.60+.

Rules that came out of reading real results:

- **Ambiguous names.** A name made only of everyday words ("Real Estate", "Status Quo", "Low") counts for much less, and only if the text writes it as the artist does (capitalized), never from a URL slug. The word list is the system's plus a few modern additions. A capitalized name of two or more words with at least one unusual word ("Uncle Tupelo", "Joni Mitchell") is not ambiguous in titles, slugs and tags (but still is in body text).
- **The longest known name claims its words**: "Prince" inside "Bonnie 'Prince' Billy" is not Prince. And if a piece names "Gia Margaret" anywhere, a lone "Margaret" in it means her, not the artist called Margaret.
- **...unless they're one musician.** A leader and his band ("Brad Mehldau", "Brad Mehldau Trio", "Nick Cave and the Bad Seeds"), two rows for one person (same MusicBrainz or Wikidata id: "Shabaka", "Shabaka Hutchings"), and a group named for a person ("Santana", "Carlos Santana") are not rivals; a piece about either links to both. A first name that belongs to a different artist ("Marta" in "Marta Del Grandi") does not.
- **Same-named artists** (21 pairs in the data, like two "Sweet"s): a name alone points at the better known one (more shows here, then followers). The other matches only through something specific to them: an album or track, a link. If one namesake's album is named, the others drop out for that piece.
- **Bandcamp's own pages** (`daily.bandcamp.com`) never count as an artist's page, even if the data says so (Juana Molina's Bandcamp link once did; that row in `artist_link` is still wrong).
- **Duplicates.** One piece served at two URLs of a site ("/blog/x" and "/news/x", "/reviews/x/x") is published once: same source, same last path segment, same title, dates within a week; the shortest URL stays. A review and an interview that share a slug stay separate because their titles differ.

## Deciding by hand

- `writing.py review` walks the borderline matches, biggest piles first, with each artist described (Wikidata's short description, else MusicBrainz's type, country and year, plus how many shows): `y` it's about them (confirmed, published), `n` no (rejected for good), `x` never match this artist on the name alone (a veto), `s` skip, `o` open, `q` quit. Rejected and confirmed verdicts survive any re-match.
- `writing.py veto ARTIST` (also by id for namesakes: `veto 981`): `--dry-run`, `--force`, `--interactive` (piece by piece, `n` keeps a piece for good), `--all` (drop even matches with an album or link behind them). `unveto` undoes it.
- `writing.py hide URL` keeps one piece off the site (junk, not writing); `unhide` brings it back.

## Where we are (2026-10-18)

- 43 sources (32 criticism, 6 essay, 5 news); 37 with published pieces. 6,154 published pieces, 13,400 matches (the crawl db holds 15,053 accepted matches, counting news sources, which aren't published), 440 matches waiting in review (183 rejected, 6 confirmed so far).
- 9,500 items stored; ~36,000 backfill candidate pages are still queued for the listed sources (best-scored first; the news sources' leftovers are no longer fetched).
- A few sources only ever show recent posts (no sitemap, no archive paging): Bandcamp Daily, Brainwashed, Musique Machine, The Wire, Under the Radar, Tiny Mix Tapes (server errors). Fix by hand if wanted: a sitemap address, or accept recent-only.

## Open

- Drain the backfill (slowly, by design) and keep an eye on `writing.py sources` for sources that never yield.
- Work down the review queue; `x` the artists whose bare names keep coming up.
- Ideas shelved, in rough order of appeal: "writing about what we played" on show pages; a latest-reading strip on the home page; a ✎ marker on the artists ranking and a "Written about" ranking (needs the backfill's depth); a Reading search. (A "played but not written about" list was rejected: we only see the sources we monitor.)
- Matching, bigger options we decided against for now: a catalogue of all MusicBrainz artist names, so names we've never played can claim words too; a small model trained on the review verdicts; asking a language model about the borderline band only (pennies a week, but the judgment stops being ours).
- Bluesky ideas parked: Constellation lookups per artist; people-over-posts.

## Files

`writing.py` (everything), `writing_match.py` (matching), `writing_backfill.py` (sitemaps, API/feed paging, polite fetching), `refresh_writing.py` (the weekly script), `writing_sources.json`, `tests/test_writing.py`; `factoids.py` (`reading_for`, `reading_index`, `reading_source`, `_rank_publications`), `templates/reading.html`, `reading_source.html`, `_reading.html`; routes `/reading`, `/reading/<slug>`, `/rankings/publications` in `app.py`.

## Podcasts

- A podcast is a source of kind `podcast`: the same feed, matching and publishing, with the show notes as the text. They are listed apart from the writing: `/podcasts` and `/podcasts/<slug>`, a "Podcast episodes" section after "Further reading" on artist and album pages, and left out of Rankings > Publications. Episodes link to the episode's page; no audio is fetched or stored.
- Many hosts give every episode the show's page as its link (or none): `parse_feed` then makes the URL unique with `#guid`.
- Talk shows that name many artists in passing are `roundup`. Added from an Overcast OPML export; to add one: `python writing.py source add FEEDURL --kind podcast`.

## Adding a single page

- `python writing.py add URL` offers one page for matching: it reads the page (politely, robots.txt respected), shows the title, date and what it would match, and asks before keeping it. `-n` only shows, `-y` doesn't ask, `--title` and `--date` fill in what the page doesn't give, `--source NAME` files it under a source we follow.
- Otherwise it goes under the source for its site if we follow one, else under `Elsewhere` (`Elsewhere (podcasts)`, etc. with `--kind`): a source with no feed and no archive (`feed_url` null, `backfill: none`) that `add` creates in `writing_sources.json`. Matching, review, veto, `hide` and publishing work as for any piece.
- A page it can't read (robots.txt, a login, a paywall) is still added if you give `--title`: it matches on the title, the URL and whatever you supply, `--text TEXT` (or `-` to read it from stdin; also for a page that's cut off by a paywall) and `--artist NAME_OR_ID` (repeatable) for "this is about them" (a confirmed match, so a rematch keeps it; an ambiguous name needs the id). Only the title, date and link are published, as always.
- If the page announces a feed in its `<head>` and we don't follow the site, it offers to add it as a source (the page then moves to that source). With `-y` or without a terminal it only prints the `source add` command.

## Podcast episode pages

- Many podcast hosts link every episode to the show's page, so a published episode would land on the show. `writing.py episodes` (also run weekly by `refresh`, 40 a show) looks each such episode up on the show's own site and keeps the page it finds in `item.page_url`; `publish` uses it in place of the stored `page#guid` URL. `page_tried`: 1 found, 2 not (each is tried once).
- Two methods. A show whose site has no search API but is Squarespace (`/episodes?format=json`, found from the biggest section of its sitemap, 20 an item per page) is matched from that list of every episode by title, in one go (Switched On Pop: 485 of 530). WordPress sites work: the REST search (`/wp-json/wp/v2/search`), asked with the title's words and then its first five, and only a result on the same host whose title matches (`same_title`) is used. A show whose site has no such API is marked at once; one whose first 20 lookups mostly find nothing is left alone (same rule as `deepen`). Both `episodes` and `deepen` work in rounds: the first tries 20 (the probe); a show that passed carries on in the same run, up to `--pages`; one that failed is dropped with a message. Anything stopped early or left over says so ("run it again").
- First probe (October 2026): works for Heat Rocks (maximumfun.org, WordPress search) and Switched On Pop (Squarespace list). No usable API or list on Talkhouse, Sound Opinions, Life of the Record, MOJO Record Club, Phantom Power, Sound Expertise, Fela Kuti; Rock's Backpages disallows it in robots.txt; Goldmine has the API but its episodes aren't found there. Open: other ways in for those (a site's own search page or JSON, the episode list on the show's page, Spotify/Apple links). Each episode is tried once, so after adding a new way in (or when a site changes) run `python writing.py episodes --source NAME --retry`: it forgets only the ones not found, never one already found.
