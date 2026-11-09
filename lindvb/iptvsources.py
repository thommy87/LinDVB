# SPDX-License-Identifier: GPL-2.0-or-later
"""The IPTV lists the user imported (Preferences › Internet): remembered so
they can be updated or removed again. No GTK."""

import time
import urllib.parse


def xtream_url(server, user, password):
    """The M3U address of an Xtream Codes login (all live channels)."""
    server = server.strip().rstrip("/")
    if "://" not in server:
        server = "http://" + server
    q = urllib.parse.urlencode({"username": user.strip(), "password": password,
                                "type": "m3u_plus", "output": "ts"})
    return f"{server}/get.php?{q}"


def display_location(src):
    """Where a list comes from, without an Xtream password."""
    loc = src.get("location", "")
    if src.get("kind") == "xtream":
        parts = urllib.parse.urlsplit(loc)
        user = urllib.parse.parse_qs(parts.query).get("username", [""])[0]
        return f"{parts.netloc} ({user})" if user else parts.netloc
    return loc


def remember(sources, kind, location, root, count, now=None):
    """*sources* with the list at *location* added or refreshed (new list)."""
    entry = {"kind": kind, "location": location, "root": root, "count": int(count),
             "date": int(now if now is not None else time.time())}
    return [s for s in sources if s.get("location") != location] + [entry]


def forget(sources, location):
    return [s for s in sources if s.get("location") != location]


def own_channels(channels, root):
    """The channels a list brought: internet streams in its group."""
    return [c for c in channels if c.is_iptv and not c.dvbi_id and c.root == root]


def replace(db, root, new):
    """Update the list *root* with the channels *new*: changed channels keep
    their favourite mark, number and guide match; channels no longer in the
    list are removed. Returns (added, updated, removed)."""
    keep = {c.key for c in new}
    gone = [c for c in own_channels(db.channels, root) if c.key not in keep]
    if gone:
        drop = {id(c) for c in gone}
        db.channels[:] = [c for c in db.channels if id(c) not in drop]
    added, updated = db.merge(new)
    return added, updated, len(gone)


def remove(db, root):
    """Remove the channels of the list *root*; returns how many."""
    gone = {id(c) for c in own_channels(db.channels, root)}
    db.channels[:] = [c for c in db.channels if id(c) not in gone]
    db.renumber()
    return len(gone)
