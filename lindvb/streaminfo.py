# SPDX-License-Identifier: GPL-2.0-or-later
"""What an internet stream offers, for the channel analysis of IPTV channels.

``probe(url)`` fetches the address once (in a worker thread) and reports the
HTTP status, server, content type and the time to the first byte; for HLS it
reads the master playlist (variants with bandwidth, resolution and codecs,
audio and subtitle renditions) and one media playlist (target duration,
segment count, live or on demand); for DASH the representations of the MPD.
The pure parsers are unit tested.
"""

import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

USER_AGENT = "LinDVB"
LIMIT = 512 * 1024

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(line):
    return {k: v.strip('"') for k, v in _ATTR.findall(line.split(":", 1)[1] if ":" in line else "")}


def parse_hls(text, base=""):
    """{"kind": "master"|"media", ...} of an HLS playlist."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        return {}
    if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        variants, media = [], []
        for i, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF"):
                a = _attrs(ln)
                uri = next((x for x in lines[i + 1:] if not x.startswith("#")), "")
                res = a.get("RESOLUTION", "")
                w, _, h = res.partition("x")
                variants.append({
                    "bandwidth": int(a.get("BANDWIDTH", 0) or 0),
                    "average": int(a.get("AVERAGE-BANDWIDTH", 0) or 0),
                    "width": int(w) if w.isdigit() else 0, "height": int(h) if h.isdigit() else 0,
                    "codecs": a.get("CODECS", ""), "fps": float(a.get("FRAME-RATE", 0) or 0),
                    "uri": urllib.parse.urljoin(base, uri) if base else uri})
            elif ln.startswith("#EXT-X-MEDIA"):
                a = _attrs(ln)
                media.append({"type": a.get("TYPE", ""), "language": a.get("LANGUAGE", ""),
                              "name": a.get("NAME", ""), "default": a.get("DEFAULT") == "YES",
                              "channels": a.get("CHANNELS", "")})
        variants.sort(key=lambda v: v["bandwidth"])
        return {"kind": "master", "variants": variants, "media": media}
    target, durations, live = 0.0, [], True
    for ln in lines:
        if ln.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target = float(ln.split(":", 1)[1])
            except ValueError:
                pass
        elif ln.startswith("#EXTINF:"):
            try:
                durations.append(float(ln.split(":", 1)[1].split(",")[0]))
            except ValueError:
                pass
        elif ln.startswith("#EXT-X-ENDLIST"):
            live = False
    seg = next((ln for ln in lines if not ln.startswith("#")), "")
    container = "fMP4" if any(ln.startswith("#EXT-X-MAP") for ln in lines) or \
        seg.split("?")[0].endswith((".m4s", ".mp4")) else \
        ("MPEG-TS" if seg.split("?")[0].endswith(".ts") else "")
    return {"kind": "media", "target": target, "segments": len(durations),
            "avg_segment": sum(durations) / len(durations) if durations else 0.0,
            "window": sum(durations), "live": live, "container": container}


def parse_mpd(text):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    reps = []
    for aset in root.iter():
        if local(aset.tag) != "AdaptationSet":
            continue
        ctype = aset.get("contentType") or aset.get("mimeType", "").split("/")[0]
        for rep in aset:
            if local(rep.tag) != "Representation":
                continue
            kind = ctype or rep.get("mimeType", "").split("/")[0]
            reps.append({"type": kind, "bandwidth": int(rep.get("bandwidth", 0) or 0),
                         "width": int(rep.get("width", 0) or 0),
                         "height": int(rep.get("height", 0) or 0),
                         "codecs": rep.get("codecs") or aset.get("codecs", ""),
                         "language": aset.get("lang", ""), "id": rep.get("id", "")})
    live = root.get("type") == "dynamic"
    return {"kind": "dash", "representations": sorted(reps, key=lambda r: (r["type"], r["bandwidth"])),
            "live": live, "min_buffer": root.get("minBufferTime", ""),
            "segment_hint": root.get("maxSegmentDuration", "")}


def probe(url, timeout=6):
    """Fetch *url* once; dict with status, latency and the parsed playlist (blocking)."""
    out = {"url": url}
    if not url.lower().startswith(("http://", "https://")):
        parts = urllib.parse.urlsplit(url)
        out["protocol"] = parts.scheme.upper() or "FILE"
        return out
    out["protocol"] = "HTTPS" if url.lower().startswith("https") else "HTTP"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            first = resp.read(1)
            out["latency"] = time.monotonic() - t0
            out["status"] = resp.status
            out["server"] = resp.headers.get("Server", "")
            out["content_type"] = resp.headers.get("Content-Type", "")
            out["final_url"] = resp.geturl()
            ctype = out["content_type"].lower()
            textual = "mpegurl" in ctype or "dash" in ctype or "xml" in ctype or \
                url.split("?")[0].endswith((".m3u8", ".mpd"))
            if textual:
                body = first + resp.read(LIMIT)
            else:
                t1 = time.monotonic()
                chunk = resp.read(256 * 1024)
                dt = time.monotonic() - t1
                if dt > 0 and chunk:
                    out["download_bps"] = len(chunk) * 8 / dt
                body = b""
    except Exception as e:          # network, TLS, HTTP errors: show them
        out["error"] = str(e)
        out["latency"] = time.monotonic() - t0
        return out
    text = body.decode("utf-8", "replace")
    if text.lstrip().startswith("#EXTM3U"):
        out["playlist"] = parse_hls(text, out.get("final_url", url))
        pl = out["playlist"]
        if pl.get("kind") == "master" and pl["variants"]:
            try:
                sub = probe(pl["variants"][-1]["uri"], timeout)
                if sub.get("playlist", {}).get("kind") == "media":
                    pl["media_playlist"] = sub["playlist"]
            except Exception:
                pass
    elif "<MPD" in text[:2000]:
        out["playlist"] = parse_mpd(text)
    return out
