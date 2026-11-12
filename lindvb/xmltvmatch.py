# SPDX-License-Identifier: GPL-2.0-or-later
"""Match IPTV (M3U) channels to the channels of an XMLTV guide.

Playlists and guides rarely spell channel names the same way ("DE: Das Erste
HD" in the playlist, "Das Erste" with the id "DasErste.de" in the guide). The
matcher tries, in this order:

  1. the playlist's tvg-id against the XMLTV channel id (case-insensitive)
  2. the normalised tvg-name / channel name against the normalised XMLTV
     display names and ids
  3. a fuzzy comparison of the normalised names (difflib ratio)

Normalising removes country prefixes, quality tags (HD, FHD, UHD, 4K, 1080p,
HEVC...), bracketed notes, accents, case and punctuation. "+1" style
time-shift suffixes are kept, because those are different channels.

The result of a match is stored on the channel (``Channel.xmltv_id``); a
choice the user made in the matching dialog is marked ``xmltv_manual`` so the
automatic matcher never overrides it.
"""

import difflib
import json
import re
import unicodedata

from .config import CACHE_DIR

CHANNELS_CACHE = CACHE_DIR / "xmltv_channels.json"

FUZZY_THRESHOLD = 0.82

_QUALITY = (r"uhd|fhd|hd\+?|sd|hq|lq|4k|8k|2160p|1080[pi]|720p|576[pi]|480p|"
            r"hevc|h\.?26[45]|x26[45]|avc|50\s?fps|60\s?fps|25\s?fps|backup|raw")
_QUALITY_RE = re.compile(rf"(?<![a-z0-9])(?:{_QUALITY})(?![a-z0-9])")
# "DE:", "DE |", "|DE|", "[DE]", "DE -" at the start of a name
_PREFIX_RE = re.compile(r"^\s*(?:\|?\s*[a-z]{2,3}\s*(?:\||:|-|»)\s*|\[[a-z]{2,3}\]\s*)")
_BRACKETS_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
# XMLTV ids often carry a country domain: "DasErste.de", "bbc1.uk", "ard.de@SD"
_ID_SUFFIX_RE = re.compile(r"(?:\.[a-z]{2,3})?(?:@[\w-]+)?$")
_TIMESHIFT_RE = re.compile(r"\+\s*(\d)\b")


def normalize(name):
    """Normalise a channel name for comparison."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace("&", " and ").replace("ß", "ss")
    s = _PREFIX_RE.sub("", s)
    s = _BRACKETS_RE.sub(" ", s)
    s = _TIMESHIFT_RE.sub(r" plus\1 ", s)
    s = _QUALITY_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def normalize_id(xmltv_id):
    s = (xmltv_id or "").strip().lower()
    return normalize(_ID_SUFFIX_RE.sub("", s))


class Matcher:
    """Index of XMLTV channels for repeated matching."""

    def __init__(self, xmltv_channels):
        # xmltv_channels: {id: [display names]}
        self.channels = dict(xmltv_channels)
        self.by_id = {cid.lower(): cid for cid in self.channels}
        self.by_norm = {}
        for cid, names in self.channels.items():
            for key in {normalize(n) for n in names} | {normalize_id(cid)}:
                if key:
                    self.by_norm.setdefault(key, set()).add(cid)
        self._keys = list(self.by_norm)

    def label(self, cid):
        names = self.channels.get(cid) or [cid]
        return names[0] if names[0] == cid else f"{names[0]} ({cid})"

    def match(self, name, tvg_id="", tvg_name=""):
        """Return (xmltv_id, method, score) or (None, "", 0.0)."""
        if tvg_id:
            cid = self.by_id.get(tvg_id.strip().lower())
            if cid:
                return cid, "tvg-id", 1.0
        candidates = [n for n in (tvg_name, name) if n]
        if tvg_id:
            candidates.append(_ID_SUFFIX_RE.sub("", tvg_id.strip().lower()))
        for text in candidates:
            hit = self.by_norm.get(normalize(text))
            if hit and len(hit) == 1:
                return next(iter(hit)), "name", 1.0
            if hit:                       # several guide channels share the name
                return sorted(hit)[0], "name", 0.95
        best, best_score = None, 0.0
        for text in candidates:
            norm = normalize(text)
            if len(norm) < 2:
                continue
            for key in difflib.get_close_matches(norm, self._keys, n=3, cutoff=FUZZY_THRESHOLD):
                score = difflib.SequenceMatcher(None, norm, key).ratio()
                # a different time-shift number is a different channel
                if _plus(norm) != _plus(key):
                    continue
                if score > best_score:
                    best, best_score = key, score
        if best:
            return sorted(self.by_norm[best])[0], "fuzzy", round(best_score, 2)
        return None, "", 0.0


def _plus(s):
    m = re.search(r"plus(\d)", s)
    return m.group(1) if m else ""


def auto_match(channels, xmltv_channels, overwrite=False):
    """Fill ``xmltv_id`` of IPTV channels. Returns (matched, unmatched) counts.

    Channels the user matched by hand (``xmltv_manual``) are left alone, and
    so are existing matches unless *overwrite* is set.
    """
    m = Matcher(xmltv_channels)
    matched = unmatched = 0
    for c in channels:
        # Tvheadend channels take the guide of their server (lindvb.tvheadend)
        if not c.is_iptv or c.xmltv_manual or c.tvh_server:
            continue
        if c.xmltv_id and c.xmltv_id in m.channels and not overwrite:
            matched += 1
            continue
        cid, _method, _score = m.match(c.name, c.tvg_id, c.tvg_name)
        c.xmltv_id = cid or ""
        if cid:
            matched += 1
        else:
            unmatched += 1
    return matched, unmatched


def save_channels(xmltv_channels):
    CHANNELS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CHANNELS_CACHE.write_text(json.dumps(xmltv_channels))


def load_channels():
    try:
        data = json.loads(CHANNELS_CACHE.read_text())
        return {str(k): list(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}
