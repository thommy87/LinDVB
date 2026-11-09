# SPDX-License-Identifier: GPL-2.0-or-later
"""Channel logos from a picon folder.

Picon sets (as used by Enigma2, Kodi, VDR and tvheadend) name each logo after
the channel in one of two ways:

* service reference: ``1_0_19_283D_3FB_1_C00000_0_0_0.png`` (service type,
  SID, TSID, ONID and namespace, in hex). The namespace is the orbital
  position for satellite (19.2°E -> C00000), FFFF0000 for cable and
  EEEE0000 for terrestrial.
* SNP (service name picon): the name in lower case, ``&`` -> and, ``+`` ->
  plus, ``*`` -> star and only letters and digits left: ``daserstehd.png``.

LinDVB ships no picons, because the logos belong to the broadcasters; the
user points the logo folder at a set of their own. The folder is read once
and indexed; thousands of files are fine.
"""
import os
import re
import unicodedata

EXTS = (".png", ".svg", ".jpg", ".jpeg", ".webp", ".gif")
NS_CABLE = 0xFFFF
NS_TERRESTRIAL = 0xEEEE

_REF = re.compile(r"^1_0_([0-9a-f]+)_([0-9a-f]+)_([0-9a-f]+)_([0-9a-f]+)_([0-9a-f]+)_0_0_0$", re.I)
_ORBIT = re.compile(r"(\d{1,3}(?:[.,]\d)?)\s*°?\s*([EOW])\b", re.I)


def snp_name(name):
    """The SNP file stem of a channel name: "Das Erste HD" -> "daserstehd"."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = s.lower().replace("&", "and").replace("+", "plus").replace("*", "star")
    return re.sub(r"[^a-z0-9]", "", s)


def orbital_position(text):
    """Tenths of a degree east (west is 3600 minus it) from "Astra 19.2°E", or None."""
    m = _ORBIT.search(text or "")
    if not m:
        return None
    pos = round(float(m.group(1).replace(",", ".")) * 10)
    return 3600 - pos if m.group(2).upper() == "W" else pos


def namespace(channel):
    """The upper 16 bits of the channel's Enigma2 namespace, or None if unknown."""
    d = channel.delsys or ""
    if d.startswith("dvb-c"):
        return NS_CABLE
    if d.startswith("dvb-t") or d in ("isdb-t", "atsc", "dtmb"):
        return NS_TERRESTRIAL
    if channel.is_sat:
        return orbital_position(f"{channel.root} {channel.category}")
    return None


class Index:
    """The logos of one folder by service reference and by SNP name."""

    def __init__(self, folder):
        self.folder = folder
        self.refs = {}          # (sid, tsid, onid, ns) -> file
        self.refs_any = {}      # (sid, tsid, onid) -> file, for an unknown position
        self.names = {}         # snp stem / casefolded stem -> file
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            entries = []
        for e in entries:
            stem, ext = os.path.splitext(e)
            if ext.lower() not in EXTS:
                continue
            m = _REF.match(stem)
            if m:
                _t, sid, tsid, onid, ns = (int(x, 16) for x in m.groups())
                self.refs.setdefault((sid, tsid, onid, ns >> 16), e)
                self.refs_any.setdefault((sid, tsid, onid), e)
                continue
            self.names.setdefault(stem.casefold(), e)

    def find(self, channel):
        if channel.sid:
            ns = namespace(channel)
            key = (channel.sid, channel.tsid, channel.onid)
            e = self.refs.get((*key, ns)) if ns is not None else self.refs_any.get(key)
            if e:
                return os.path.join(self.folder, e)
        for stem in (snp_name(channel.name), snp_name(channel.tvg_name)):
            e = self.names.get(stem) if stem else None
            if e:
                return os.path.join(self.folder, e)
        return None


_cache = {}


def index(folder):
    """The index of *folder*, read again when the folder changed."""
    try:
        mtime = os.stat(folder).st_mtime
    except OSError:
        return None
    hit = _cache.get(folder)
    if hit is None or hit[0] != mtime:
        hit = (mtime, Index(folder))
        _cache[folder] = hit
    return hit[1]


def find(channel, folders):
    """Path of the picon of *channel* in the first folder that has one, or None."""
    for folder in folders:
        idx = index(folder) if folder else None
        path = idx.find(channel) if idx else None
        if path:
            return path
    return None
