# SPDX-License-Identifier: GPL-2.0-or-later
"""The IPTV lists the user imported (Preferences › Internet): remembered so
they can be updated or removed again. No GTK."""

import time
import urllib.parse


XTREAM_OUTPUTS = ("ts", "m3u8")     # MPEG-TS or HLS streams

# file names that say nothing about a list ("…/get.php", "…/index.m3u8")
GENERIC_NAMES = {"", "get", "index", "playlist", "list", "channels", "tv", "live",
                 "iptv", "m3u", "m3u8", "tv_channels", "stream", "streams", "download"}


def xtream_url(server, user, password, output="ts"):
    """The M3U (m3u_plus) address of an Xtream Codes login; *output* is "ts"
    for MPEG-TS or "m3u8" for HLS streams."""
    server = server.strip().rstrip("/")
    if "://" not in server:
        server = "http://" + server
    q = urllib.parse.urlencode({"username": user.strip(), "password": password,
                                "type": "m3u_plus",
                                "output": output if output in XTREAM_OUTPUTS else "ts"})
    return f"{server}/get.php?{q}"


def display_location(src):
    """Where a list comes from, without an Xtream password."""
    loc = src.get("location", "")
    if src.get("kind") == "xtream":
        parts = urllib.parse.urlsplit(loc)
        user = urllib.parse.parse_qs(parts.query).get("username", [""])[0]
        return f"{parts.netloc} ({user})" if user else parts.netloc
    return loc


def is_vod(url):
    """True for the films and series an Xtream m3u_plus list carries next to
    the live channels (".../movie/user/pass/1.mkv", ".../series/...")."""
    path = urllib.parse.urlsplit(url).path
    return "/movie/" in path or "/series/" in path


def live_only(channels):
    """The live channels of an Xtream list (without films and series)."""
    return [c for c in channels if not is_vod(c.url)]


def default_name(kind, location, root):
    """A telling name for a new list: the file name, or for Xtream logins and
    generic file names ("get.php", "index.m3u8") the server."""
    parts = urllib.parse.urlsplit(location) if "://" in location else None
    if kind == "xtream" and parts:
        return display_location({"kind": kind, "location": location})
    if parts and root.strip().lower() in GENERIC_NAMES:
        user = urllib.parse.parse_qs(parts.query).get("username", [""])[0]
        host = parts.hostname or parts.netloc
        return f"{host} ({user})" if user else (host or root or "IPTV")
    return root or "IPTV"


def find(sources, location):
    return next((s for s in sources if s.get("location") == location), None)


def unique_name(sources, name, location=""):
    """*name*, numbered when another list (not at *location*) already has it."""
    taken = {s.get("root", "") for s in sources if s.get("location") != location}
    out, n = name, 2
    while out in taken:
        out, n = f"{name} {n}", n + 1
    return out


def rename(db, sources, location, name):
    """Give the list at *location* the name *name* (also the channel group of
    its channels). Returns (sources, the name used)."""
    src = find(sources, location)
    name = unique_name(sources, name.strip(), location)
    if not src or not name:
        return sources, src.get("root", "") if src else ""
    old = src.get("root", "")
    for c in own_channels(db.channels, old):
        c.root = name
    return [dict(s, root=name) if s is src else s for s in sources], name


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
