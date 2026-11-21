# SPDX-License-Identifier: GPL-2.0-or-later
"""The internet guides (XMLTV) the user added (Preferences › EPG): web
addresses such as ``https://free-epg.de/api/epg/us.xml.gz`` and XMLTV files,
remembered so they can be shown, updated and removed again. No GTK.

Each entry is a dict ``{"kind": "url" | "file", "location", "name",
"channels", "programmes", "date", "error"}`` in the setting
``xmltv_sources``. The guide channels each source brought are kept in a
cache file, so the IPTV channel matching sees the channels of all sources
and removing a source also removes the programmes only it had.
"""

import json
import os
import time
import urllib.parse
import urllib.request

from .config import CACHE_DIR

CHANNELS_CACHE = CACHE_DIR / "xmltv_sources.json"

KINDS = ("url", "file")

# file names that say nothing about a guide ("…/epg.xml.gz", "…/guide.xml")
GENERIC_NAMES = {"", "epg", "guide", "xmltv", "tv", "index", "all", "download", "get",
                 "export", "programme", "programmes", "schedule", "listings"}


def _stem(name):
    """The name of an XMLTV file without its extensions ("us.xml.gz" → "us")."""
    for ext in (".gz", ".xz", ".zip", ".xml", ".xmltv"):
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
    return name


def default_name(kind, location):
    """A telling name for a new source: the file name, together with the
    server for a web address ("us (free-epg.de)")."""
    if kind == "file" or "://" not in location:
        return _stem(os.path.basename(location)) or location
    parts = urllib.parse.urlsplit(location)
    host = parts.hostname or parts.netloc
    stem = _stem(urllib.parse.unquote(parts.path.rstrip("/").rsplit("/", 1)[-1]))
    if stem.lower() in GENERIC_NAMES:
        return host or location
    return f"{stem} ({host})" if host else stem


def normalize_url(text):
    text = text.strip()
    if text and "://" not in text:
        text = "https://" + text
    return text


def find(sources, location):
    return next((s for s in sources if s.get("location") == location), None)


def unique_name(sources, name, location=""):
    """*name*, numbered when another source (not at *location*) already has it."""
    taken = {s.get("name", "") for s in sources if s.get("location") != location}
    out, n = name, 2
    while out in taken:
        out, n = f"{name} {n}", n + 1
    return out


def add(sources, kind, location, name=""):
    """*sources* with a new source at *location* (unchanged when it is
    already there). Returns (sources, the entry)."""
    old = find(sources, location)
    if old:
        return sources, old
    entry = {"kind": kind if kind in KINDS else "url", "location": location,
             "name": unique_name(sources, name.strip() or default_name(kind, location)),
             "channels": 0, "programmes": 0, "date": 0, "error": ""}
    return list(sources) + [entry], entry


def touch(sources, location, channels, programmes, now=None, error=""):
    """*sources* with the source at *location* marked as loaded (or failed,
    with *error*: then the last counts stay), in its place."""
    date = int(now if now is not None else time.time())
    out = []
    for s in sources:
        if s.get("location") == location:
            s = dict(s, error=error) if error else \
                dict(s, channels=int(channels), programmes=int(programmes), date=date, error="")
        out.append(s)
    return out


def rename(sources, location, name):
    """Give the source at *location* the name *name*. Returns (sources, the name used)."""
    src = find(sources, location)
    name = unique_name(sources, name.strip(), location) if name.strip() else ""
    if not src or not name:
        return sources, src.get("name", "") if src else ""
    return [dict(s, name=name) if s is src else s for s in sources], name


def forget(sources, location):
    return [s for s in sources if s.get("location") != location]


def migrate(saved):
    """LinDVB 0.x kept one XMLTV address ("xmltv_url"): it becomes the first
    entry of the list. Changes the settings dict *saved* in place."""
    url = (saved.pop("xmltv_url", "") or "").strip()
    if url and not saved.get("xmltv_sources"):
        saved["xmltv_sources"], _entry = add([], "url", normalize_url(url))


# -- loading -------------------------------------------------------------------------

def fetch(src, timeout=120):
    """The raw guide of *src* (bytes, possibly gzip compressed)."""
    loc = src.get("location", "")
    if src.get("kind") == "file":
        with open(loc, "rb") as f:
            return f.read()
    req = urllib.request.Request(loc, headers={"User-Agent": "LinDVB"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# -- the guide channels of each source ------------------------------------------

def load_channels(path=None):
    """location -> {xmltv channel id: [display names]}."""
    try:
        data = json.loads((path or CHANNELS_CACHE).read_text())
        return {str(loc): {str(k): list(v) for k, v in chans.items()}
                for loc, chans in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_channels(by_source, path=None):
    path = path or CHANNELS_CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(by_source))


def all_channels(by_source, sources):
    """The guide channels of all *sources*, for the IPTV channel matching."""
    out = {}
    for s in sources:
        for cid, names in by_source.get(s.get("location", ""), {}).items():
            have = out.setdefault(cid, [])
            have += [n for n in names if n not in have]
    return out


def orphaned(by_source, sources, location):
    """The guide channel ids only the source at *location* brought (no
    other source has them): their programmes go when it is removed."""
    others = set()
    for s in sources:
        if s.get("location") != location:
            others.update(by_source.get(s.get("location", ""), {}))
    return [cid for cid in by_source.get(location, {}) if cid not in others]
