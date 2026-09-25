"""Record who was in the room for each show (the show_dj table).

Lines look like the ones in the show log:

    Thursday, October 1st (DJs: Heath, Joe, Chris)
    Thursday, April 24th (DJs: Patrick, Joe, Rob, Ezra) (Ezra's first time!)

DJs are listed in the order they joined. A trailing "(Name's first time!)" is
kept as a note on that DJ. Years are optional. Lines without a "(DJs: ...)"
list (cancelled shows, asides) are skipped.

    python load_djs.py import PATH [--commit]   # the whole log, newest first
    python load_djs.py add "LINE" [--commit]    # one show
    python load_djs.py add 2026-10-01 Heath, Joe, Chris [--commit]

Without --commit, reports what would be loaded. Loading a show replaces its
DJs, so rerunning with a corrected line is the way to fix mistakes.
"""
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

import models

MATCH_WINDOW_DAYS = 3  # the logged date is sometimes a day or so off the playlist's

MONTHS = {m: i for i, m in enumerate(['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                                      'september', 'october', 'november', 'december'], 1)}
WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

DATE_RE = re.compile(r'^\s*(?:(?P<weekday>[a-z]+),?\s+)?(?P<month>[a-z]+)\s+(?P<day>\d{1,2})[a-z]*'
                     r'(?:,+\s*(?P<year>2[0o]\d\d))?', re.I)
ISO_DATE_RE = re.compile(r'^\s*(?P<iso>\d{4}-\d{2}-\d{2})\s+(?P<djs>.*)$')
DJS_RE = re.compile(r'\(DJs\s*[:;]\s*(?P<djs>[^)]*)\)', re.I)
NOTE_RE = re.compile(r'\((?P<note>[^)]*)\)')
POSSESSIVE_RE = re.compile(r"^(?P<name>\w+)[’']s\b")


@dataclass
class LogEntry:
    line: str
    month: int
    day: int
    year: int  # None if the line didn't say
    djs: list
    weekday: str = None
    lineno: int = None
    date: date = None
    notes: dict = field(default_factory=dict)  # dj name -> note
    warnings: list = field(default_factory=list)


def parse_names(s):
    return [n.strip() for n in re.split(r'[,.]', s.replace('…', '')) if n.strip()]


def parse_line(line):
    """Parse one log line into a LogEntry (date not yet resolved), or None."""
    line = line.strip()
    if m := ISO_DATE_RE.match(line):
        d = date.fromisoformat(m['iso'])
        return LogEntry(line, d.month, d.day, d.year, parse_names(m['djs']))
    djs_m = DJS_RE.search(line)
    date_m = DATE_RE.match(line)
    if not djs_m or not date_m or date_m['month'].lower() not in MONTHS:
        return None
    year = int(date_m['year'].replace('o', '0')) if date_m['year'] else None
    e = LogEntry(line, MONTHS[date_m['month'].lower()], int(date_m['day']), year,
                 parse_names(djs_m['djs']), weekday=date_m['weekday'])
    if len(e.djs) != len(set(e.djs)):
        e.warnings.append(f"duplicate names: {e.djs}")
    for note_m in NOTE_RE.finditer(line[djs_m.end():]):
        note = note_m['note'].strip()
        who = POSSESSIVE_RE.match(note)
        # "Matt's first time" -> the one DJ in this show whose name starts with "Matt"
        matches = [n for n in e.djs if who and n.split()[0].startswith(who['name'])]
        if len(matches) == 1:
            e.notes[matches[0]] = note
        else:
            e.warnings.append(f"can't tell who this note is about: {note!r}")
    return e


def resolve_date(e, after=None, near=None):
    """Fill in e.date. Without a year, take the first year that falls after `after`,
    or else the year that puts the date closest to `near`."""
    if e.year:
        e.date = date(e.year, e.month, e.day)
    elif after:
        e.date = date(after.year, e.month, e.day)
        if e.date <= after:
            e.date = date(after.year + 1, e.month, e.day)
    else:
        near = near or date.today()
        e.date = min((date(y, e.month, e.day) for y in (near.year - 1, near.year, near.year + 1)),
                     key=lambda d: abs(d - near))
    if after and e.date <= after:
        e.warnings.append(f"out of order: {e.date} is not after {after}")
    if e.weekday and WEEKDAYS[e.date.weekday()] != e.weekday.lower():
        e.warnings.append(f"{e.date} is a {WEEKDAYS[e.date.weekday()].title()}, not a {e.weekday}")


def parse_log(path):
    """Parse a whole log (newest first), returning LogEntries oldest first."""
    with open(path, encoding='utf-8') as f:
        lines = list(enumerate(f, 1))
    entries, prev = [], None
    for lineno, line in reversed(lines):
        if e := parse_line(line):
            e.lineno = lineno
            resolve_date(e, after=prev)
            entries.append(e)
            prev = e.date
    return entries


def match_playlists(entries, playlist_dates):
    """Pair each entry with a playlist_id by date, allowing a few days' slop.
    Returns ({id(entry): playlist_id}, [entries with no playlist])."""
    by_date = {d: pid for pid, d in playlist_dates.items()}
    matched, unmatched, claimed = {}, [], set()
    for e in entries:
        for offset in sorted(range(-MATCH_WINDOW_DAYS, MATCH_WINDOW_DAYS + 1), key=abs):
            pid = by_date.get(e.date + timedelta(days=offset))
            if pid is not None and pid not in claimed:
                if offset:
                    e.warnings.append(f"matched to playlist dated {e.date + timedelta(days=offset)}")
                matched[id(e)] = pid
                claimed.add(pid)
                break
        else:
            unmatched.append(e)
    return matched, unmatched


def save(session, entries, matched):
    """Replace the DJs for each matched show."""
    djs = {dj.name: dj for dj in session.query(models.DJ)}
    for e in entries:
        pid = matched.get(id(e))
        if pid is None:
            continue
        session.query(models.ShowDJ).filter_by(playlist_id=pid).delete()
        for seq, name in enumerate(e.djs):
            if name not in djs:
                djs[name] = models.DJ(name=name, slug=models.slugify(name))
                session.add(djs[name])
            session.add(models.ShowDJ(playlist_id=pid, dj=djs[name], sequence=seq, note=e.notes.get(name)))
    session.flush()
    # a corrected typo can leave a DJ with no shows
    for dj in session.query(models.DJ).filter(~models.DJ.show_djs.any()):
        session.delete(dj)


def playlist_dates(session):
    return dict(session.query(models.Playlist.playlist_id, models.Playlist.date)
                .filter(models.Playlist.date != None))


def report(entries, matched, unmatched):
    for e in entries:
        where = f"line {e.lineno} " if e.lineno else ''
        for w in e.warnings:
            print(f"  {where}({e.date}): {w}")
    for e in unmatched:
        print(f"  no playlist near {e.date}: {e.line}")


def import_log(path, commit=False):
    session = models.get_session()
    dates = playlist_dates(session)
    entries = parse_log(path)
    matched, unmatched = match_playlists(entries, dates)
    print(f"{len(entries)} shows with DJs in {path}; {len(matched)} matched to playlists")
    report(entries, matched, unmatched)
    for pid, d in sorted(dates.items(), key=lambda kv: kv[1]):
        if pid not in matched.values():
            print(f"  playlist {d} has no DJs in the log")
    counts = Counter(n for e in entries for n in e.djs)
    print(f"{len(counts)} distinct names: " + ', '.join(f"{n} ({c})" for n, c in sorted(counts.items())))
    if commit:
        save(session, entries, matched)
        session.commit()
        print("committed")


def add(line, commit=False, session=None):
    """Record the DJs for one show. Returns True if it matched a playlist."""
    session = session or models.get_session()
    e = parse_line(line)
    if e is None:
        print(f"couldn't parse {line!r}; expected e.g. 'Thursday, October 1st (DJs: Heath, Joe)'")
        return False
    dates = playlist_dates(session)
    resolve_date(e, near=max(dates.values(), default=None))
    matched, unmatched = match_playlists([e], dates)
    report([e], matched, unmatched)
    if not matched:
        return False
    print(f"{e.date}: {' → '.join(e.djs)}")
    if commit:
        save(session, [e], matched)
        session.commit()
        print("committed")
    return True


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if a != '--commit']
    commit = '--commit' in sys.argv
    if len(args) >= 2 and args[0] == 'import':
        import_log(args[1], commit)
    elif len(args) >= 2 and args[0] == 'add':
        add(' '.join(args[1:]), commit)
    else:
        print(__doc__)
