# SPDX-License-Identifier: GPL-2.0-or-later
"""Which EPG source wins: merging the guide of one channel from several sources.

LinDVB reads the programme guide from up to four sources:

  * ``eit``      -- the broadcast EIT of the tuned transponder (DVB and ATSC)
  * ``provider`` -- the private guide of a platform (MediaHighway 1/2, Freesat)
  * ``dvbi``     -- the DVB-I content guide from the internet
  * ``xmltv``    -- an XMLTV guide from the internet

They often cover the same programmes with slightly different times, or one
of them has gaps. Each source is kept on its own in the store and merged for
display here, so the guide neither shows a programme twice nor jumps between
sources depending on which one arrived last.

Automatic priority (the default):

  1. Now and next come from the broadcast (EIT first, then the provider
     guide, then DVB-I, then XMLTV): the signal is the first to know when a
     programme runs late or the schedule changes.
  2. Further ahead the most complete source wins: the one reaching the most
     days ahead, weighted by how many programmes have a description.
  3. Gaps are filled from the other sources, and programmes of the winning
     source without description, genre, year etc. take them from the same
     programme in another source.

A custom priority (globally or per channel) replaces 1. and 2. by a fixed
order; 3. still applies. Everything here is pure Python and unit tested.
"""

import bisect
import dataclasses
import time

from .i18n import N_

SOURCES = ("eit", "provider", "dvbi", "xmltv")
BROADCAST = ("eit", "provider")          # from the signal: first for now/next
DEFAULT_ORDER = list(SOURCES)
SOURCE_LABELS = {
    "eit": N_("Broadcast (EIT)"),
    "provider": N_("Provider EPG (MediaHighway / Freesat)"),
    "dvbi": N_("DVB-I content guide"),
    "xmltv": N_("XMLTV (internet)"),
}

# A programme of a lower source fits into a gap when it overlaps the ones
# already taken by at most this much (different sources round times apart).
SLACK = 120
SLACK_SHARE = 0.1
# The same programme in two sources: overlap of at least this share of both.
SAME_SHARE = 0.5
# Fields a programme can borrow from the same programme in another source.
DETAIL_FIELDS = ("subtitle", "description", "genre", "country", "year",
                 "programme_id", "image", "min_age", "access")


def source_for_key(key):
    """The source of a store key written without one (older caches)."""
    if key.startswith("xmltv:"):
        return "xmltv"
    if key.startswith("dvbi:"):
        return "dvbi"
    return "eit"


def normalize_order(order):
    """*order* with unknown sources dropped and missing ones appended."""
    out = [s for s in dict.fromkeys(order or []) if s in SOURCES]
    return out + [s for s in DEFAULT_ORDER if s not in out]


def _overlap(a, b):
    return max(0, min(a.end, b.end) - max(a.start, b.start))


def completeness(events, now):
    """How much of the guide a source has: days ahead x (1 + share with a
    description), rounded to half days so small differences do not flip it."""
    future = [e for e in events if e.end > now]
    if not future:
        return 0.0
    days = (max(e.end for e in future) - now) / 86400
    detail = sum(1 for e in future if e.description) / len(future)
    return round(days * (1 + detail) * 2) / 2


def auto_order(buckets, now):
    """Sources by completeness, ties in the default order."""
    return sorted(buckets, key=lambda s: (-completeness(buckets[s], now),
                                          DEFAULT_ORDER.index(s) if s in DEFAULT_ORDER else 99))


def now_next_from(buckets, now):
    """The broadcast's programme now and the one after it: from the EIT,
    else the provider guide, when it has a programme on air."""
    for src in BROADCAST:
        evs = sorted(buckets.get(src) or [], key=lambda e: e.start)
        cur = next((e for e in evs if e.start <= now < e.end), None)
        if cur is None:
            continue
        nxt = next((e for e in evs if e.start >= cur.end - SLACK and e is not cur), None)
        return src, [e for e in (cur, nxt) if e is not None]
    return None, []


class _Taken:
    """The merged programmes so far, sorted by start, for overlap checks."""

    def __init__(self):
        self.starts = []
        self.items = []         # (event, source)
        self.longest = 0

    def overlapping(self, ev):
        # a taken programme starting before ev can still run into it
        i = bisect.bisect_left(self.starts, ev.start - self.longest)
        for j in range(i, len(self.items)):
            e, src = self.items[j]
            if e.start >= ev.end:
                break
            if _overlap(e, ev) > 0:
                yield j, e, src

    def fit(self, ev):
        """*ev* when it fits in, trimmed when a different programme runs into
        its start or end (a late running broadcast), else None."""
        over = [e for _j, e, _s in self.overlapping(ev)]
        allowed = min(SLACK, ev.duration * SLACK_SHARE)
        if sum(_overlap(e, ev) for e in over) <= allowed:
            return ev
        if any(_same(ev, e) for e in over):
            return None
        start = max([ev.start] + [e.end for e in over if e.start <= ev.start])
        end = min([ev.end] + [e.start for e in over if e.end >= ev.end])
        if end - start < ev.duration / 2 or \
                any(e.start > ev.start and e.end < ev.end for e in over):
            return None
        return dataclasses.replace(ev, start=start, duration=end - start)

    def add(self, ev, src):
        i = bisect.bisect_right(self.starts, ev.start)
        self.starts.insert(i, ev.start)
        self.items.insert(i, (ev, src))
        self.longest = max(self.longest, ev.duration)


class _Index:
    """The events of one source sorted by start, to find the same programme."""

    def __init__(self, events):
        self.events = sorted(events, key=lambda e: e.start)
        self.starts = [e.start for e in self.events]
        self.longest = max((e.duration for e in self.events), default=0)

    def same_as(self, ev):
        i = bisect.bisect_left(self.starts, ev.start - self.longest)
        for e in self.events[i:]:
            if e.start >= ev.end:
                break
            if _same(ev, e):
                yield e


def _same(a, b):
    ov = _overlap(a, b)
    if ov <= 0:
        return False
    share = min(ov / max(1, a.duration), ov / max(1, b.duration))
    if share >= 0.9:
        return True
    ta, tb = a.title.casefold().strip(), b.title.casefold().strip()
    return share >= SAME_SHARE and bool(ta and tb) and (ta.startswith(tb) or tb.startswith(ta))


def _enrich(ev, others):
    """*ev* with the details it lacks taken from the same programme elsewhere."""
    changes = {}
    for o in others:
        for f in DETAIL_FIELDS:
            if f not in changes and not getattr(ev, f) and getattr(o, f):
                changes[f] = getattr(o, f)
    return dataclasses.replace(ev, **changes) if changes else ev


def merge(buckets, order=None, now=None):
    """Merge the programmes of one channel.

    *buckets* maps a source to its events. *order* is a custom source order,
    or None for the automatic priority. Returns the events sorted by start.
    """
    buckets = {s: list(evs) for s, evs in buckets.items() if evs}
    if not buckets:
        return []
    if len(buckets) == 1:
        return sorted(next(iter(buckets.values())), key=lambda e: e.start)
    now = time.time() if now is None else now
    taken = _Taken()
    if order:
        sequence = [s for s in normalize_order(order) if s in buckets]
        sequence += [s for s in buckets if s not in sequence]
    else:
        _src, first = now_next_from(buckets, now)
        for ev in first:
            taken.add(ev, _src)
        sequence = auto_order(buckets, now)
    for src in sequence:
        for ev in sorted(buckets[src], key=lambda e: e.start):
            ev = taken.fit(ev)
            if ev is not None:
                taken.add(ev, src)
    index = {s: _Index(evs) for s, evs in buckets.items()}
    out = []
    for ev, src in taken.items:
        if all(getattr(ev, f) for f in DETAIL_FIELDS):
            out.append(ev)
            continue
        others = [e for s in sequence if s != src for e in index[s].same_as(ev)]
        out.append(_enrich(ev, others) if others else ev)
    return out
