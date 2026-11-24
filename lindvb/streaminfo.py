# SPDX-License-Identifier: GPL-2.0-or-later
"""What an internet stream offers, for the channel analysis of IPTV channels.

``probe(url)`` fetches the address once (in a worker thread) and reports the
HTTP status, server, content type and the time to the first byte; for HLS it
reads the master playlist (variants with bandwidth, resolution and codecs,
audio and subtitle renditions) and one media playlist (target duration,
segment count, live or on demand); for DASH the representations of the MPD.
The pure parsers are unit tested.

Some servers send their playlists gzip-compressed whether the client asked
for it or not (PBS on Amazon S3 / CloudFront stores them that way and
answers ``Content-Encoding: gzip`` to every request). ``decode_body`` unpacks
such an answer; ``data_master`` turns the playlist into one GStreamer can
start from (see lindvb.ipstream.hls_handover).
"""

import base64
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib

USER_AGENT = "LinDVB"
LIMIT = 512 * 1024

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(line):
    return {k: v.strip('"') for k, v in _ATTR.findall(line.split(":", 1)[1] if ":" in line else "")}


def decode_body(data, encoding=""):
    """*data* of an HTTP answer as it was before the server compressed it
    (``Content-Encoding`` gzip or deflate, or gzip's magic bytes without the
    header). A body cut short (only the first part read) unpacks as far as
    it goes; data that is not compressed comes back unchanged."""
    enc = (encoding or "").strip().lower()
    if enc in ("gzip", "x-gzip") or data[:2] == b"\x1f\x8b":
        modes = (16 + zlib.MAX_WBITS,)
    elif enc == "deflate":
        modes = (zlib.MAX_WBITS, -zlib.MAX_WBITS)       # zlib, or raw deflate
    else:
        return data
    for wbits in modes:
        try:
            return zlib.decompressobj(wbits).decompress(data)
        except zlib.error:
            pass
    return data


def is_compressed(data, encoding=""):
    """True when the server compressed the body (see decode_body)."""
    enc = (encoding or "").strip().lower()
    return enc in ("gzip", "x-gzip", "deflate") or data[:2] == b"\x1f\x8b"


_URI_ATTR = re.compile(r'URI="([^"]*)"')


def data_master(text, base):
    """An HLS playlist *text* fetched from *base*, as a ``data:`` address of a
    master playlist with absolute addresses, or None when *text* is no HLS
    playlist.

    GStreamer's HTTP source does not unpack a compressed answer, so the
    adaptive demuxer cannot read a gzip-compressed first playlist ("Invalid
    manifest"). Its own downloads (the variant playlists, the segments) are
    unpacked. So LinDVB fetches the first playlist itself and hands it over
    unpacked: a master playlist with its addresses made absolute (a data:
    address has no place to resolve them against), a media playlist (it is
    fetched again and again while a live stream runs) as the only variant of
    a master playlist that points at it."""
    if not parse_hls(text):
        return None
    if parse_hls(text)["kind"] == "media":
        out = f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n{base}\n"
    else:
        lines = []
        for ln in text.splitlines():
            st = ln.strip()
            if st and not st.startswith("#"):
                ln = urllib.parse.urljoin(base, st)
            elif st.startswith("#") and 'URI="' in st:
                ln = _URI_ATTR.sub(lambda m: f'URI="{urllib.parse.urljoin(base, m.group(1))}"', st)
            lines.append(ln)
        out = "\n".join(lines) + "\n"
    return "data:application/vnd.apple.mpegurl;base64," + \
        base64.b64encode(out.encode("utf-8")).decode("ascii")


# ISO 639-2 languages with two codes (bibliographic, terminological)
_639_2_B_T = (
    ("alb", "sqi"), ("arm", "hye"), ("baq", "eus"), ("bur", "mya"), ("chi", "zho"),
    ("cze", "ces"), ("dut", "nld"), ("fre", "fra"), ("geo", "kat"), ("ger", "deu"),
    ("gre", "ell"), ("ice", "isl"), ("mac", "mkd"), ("mao", "mri"), ("may", "msa"),
    ("per", "fas"), ("rum", "ron"), ("slo", "slk"), ("tib", "bod"), ("wel", "cym"))
_639_2_PAIRS = {**dict(_639_2_B_T), **{t: b for b, t in _639_2_B_T}}
_LANGUAGE_ATTR = re.compile(r'LANGUAGE="([^"]*)"')
_NAME_ATTR = re.compile(r'NAME="([^"]*)"')


def _other_language(lang, n):
    """The *n*-th other spelling of *lang* (n >= 1) that names the same
    language: the other ISO 639-2 code, then a private subtag."""
    other = _639_2_PAIRS.get(lang.lower())
    if other and n == 1:
        return other
    return f"{lang}-x-{n + 1}"


def distinct_languages(text):
    """(*text*, []) or (a copy of the master playlist *text*, [names]) in
    which the audio renditions *names* got a spelling of their language of
    their own.

    hlsdemux2 (GStreamer 1.24) offers one audio rendition with an address
    per language: a second one with the same LANGUAGE, under another name, is
    left out (3sat: "Audio-Deskription" next to "TV Ton", both "deu", so the
    audio description never reached the buffer). With another spelling of
    the language ("ger" for "deu") it offers both. The same name in another
    group (a copy for another variant) gets the same spelling; renditions
    without an address (the variant's own sound) do not count."""
    spelling = _respellings(text)
    if not spelling:
        return text, []
    out = []
    for ln in text.splitlines():
        key = _rendition_key(_audio_rendition(ln))[:2]
        if key in spelling:
            ln = _LANGUAGE_ATTR.sub(f'LANGUAGE="{spelling[key]}"', ln, count=1)
        out.append(ln)
    return "\n".join(out) + "\n", [name for _lang, name in spelling]


def _audio_rendition(line):
    """The attributes of an audio rendition with an address, else {}."""
    st = line.strip()
    a = _attrs(st) if st.startswith("#EXT-X-MEDIA:") else {}
    return a if a.get("TYPE") == "AUDIO" and a.get("URI") else {}


def _rendition_key(attrs):
    """(language in lower case, name, language as written) of a rendition."""
    lang = attrs.get("LANGUAGE", "").strip()
    return lang.lower(), attrs.get("NAME", ""), lang


def _respellings(text):
    """{(language, name): spelling} for distinct_languages."""
    first = {}                    # language -> the first name with it
    spelling = {}
    for ln in text.splitlines():
        lang, name, written = _rendition_key(_audio_rendition(ln))
        if not lang:
            continue
        if first.setdefault(lang, name) != name and (lang, name) not in spelling:
            n = sum(1 for k in spelling if k[0] == lang) + 1
            spelling[(lang, name)] = _other_language(written, n)
    return spelling


def handover_master(text, base, compressed):
    """``(data: address, why)`` of the HLS playlist *text* (fetched from
    *base*) to hand GStreamer instead of its address, or ``(None, "")`` when
    GStreamer reads the address as it is: a compressed playlist (see
    data_master), audio renditions hlsdemux2 would leave out (see
    distinct_languages)."""
    fixed, renamed = distinct_languages(text) if parse_hls(text).get("kind") == "master" \
        else (text, [])
    whys = (["the server sends the playlist compressed"] if compressed else []) + \
        ([f"audio renditions sharing a language ({', '.join(renamed)}) get a spelling "
          "of their own"] if renamed else [])
    if not whys:
        return None, ""
    return data_master(fixed, base), "; ".join(whys)


def hls_stream_name(stream_id, kind):
    """The rendition name hlsdemux2 puts into its stream ids
    ("<hash>/audio-Klare Sprache / Originalton", ".../text-Deutsch") for
    *kind* "audio" or "text", or "". The name may contain slashes."""
    sid = stream_id or ""
    mark = "/" + kind + "-"
    i = sid.find(mark)
    if i >= 0:
        return sid[i + len(mark):]
    return sid[len(kind) + 1:] if sid.startswith(kind + "-") else ""


def _is_variant_sound(stream_id):
    sid = stream_id or ""
    return "/main-audio-" in sid or sid.startswith("main-audio-")


def without_variant_sound(streams):
    """*streams* (Gst.Stream of hlsdemux2) without the sound the variant
    carries itself (".../main-audio-N") when the playlist gives the sounds
    as renditions with addresses of their own (".../audio-NAME"): a player
    then ignores the variant's sound (RFC 8216, 4.3.4.2.1). hlsdemux2 offers
    it next to them (3sat: "Track 1" and an unnamed track in the menu)."""
    ids = [st.get_stream_id() for st in streams]
    if not any(hls_stream_name(i, "audio") for i in ids):
        return list(streams)
    return [st for st, i in zip(streams, ids) if not _is_variant_sound(i)]


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
                              "channels": a.get("CHANNELS", ""), "group": a.get("GROUP-ID", ""),
                              "uri": a.get("URI", ""), "instream_id": a.get("INSTREAM-ID", ""),
                              "characteristics": a.get("CHARACTERISTICS", "")})
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
    encoding = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            first = resp.read(1)
            out["latency"] = time.monotonic() - t0
            out["status"] = resp.status
            out["server"] = resp.headers.get("Server", "")
            out["content_type"] = resp.headers.get("Content-Type", "")
            out["final_url"] = resp.geturl()
            encoding = resp.headers.get("Content-Encoding", "")
            if encoding:
                out["content_encoding"] = encoding
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
    text = decode_body(body, encoding).decode("utf-8", "replace").lstrip("\ufeff")
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
