# SPDX-License-Identifier: GPL-2.0-or-later
"""Catch-up: programmes of the last days from the provider's archive.

Many IPTV providers keep the last days of their channels on the server and
hand out a programme from its start on request. The channel list says so:

* M3U lists: ``catchup="default|append|shift|flussonic|xc"`` with
  ``catchup-days="7"`` (or ``tvg-rec`` / ``timeshift``) and for "default"
  and "append" a ``catchup-source`` template, e.g.
  ``catchup-source="?utc={utc}&lutc={lutc}"`` (the format Kodi's IPTV Simple
  client reads).
* Xtream Codes: the server's ``player_api.php`` names the channels with an
  archive (``tv_archive``) and how many days it keeps
  (``tv_archive_duration``); a programme is
  ``/timeshift/<user>/<password>/<minutes>/<YYYY-MM-DD:HH-MM>/<id>.ts``.

The archive is a recording on the server: it plays like a file, so jumping
around in it works, and it does not use the local timeshift buffer.

No GTK, no GStreamer.
"""

import json
import re
import time
import urllib.parse
import urllib.request

KINDS = ("default", "append", "shift", "flussonic", "xc")
DEFAULT_DAYS = 1                 # catchup given without a number of days
USER_AGENT = "LinDVB"

# /live/<user>/<password>/<id>.ts, /<user>/<password>/<id> (Xtream live streams)
XTREAM_LIVE = re.compile(r"^(?:/live)?/(?P<user>[^/]+)/(?P<password>[^/]+)/"
                         r"(?P<id>\d+)(?:\.(?P<ext>ts|m3u8))?$")


def kind_from_attrs(attrs):
    """(kind, days, source) from the attributes of an #EXTINF line."""
    kind = (attrs.get("catchup") or attrs.get("catchup-type") or "").strip().lower()
    days = 0
    for key in ("catchup-days", "tvg-rec", "timeshift"):
        v = (attrs.get(key) or "").strip()
        if v.isdigit() and int(v) > 0:
            days = int(v)
            break
    source = (attrs.get("catchup-source") or "").strip()
    if kind in ("1", "true", "yes") or (not kind and source):
        kind = "default" if "://" in source else "append" if source else ""
    if kind in ("flussonic-hls", "flussonic-ts", "fs"):
        kind = "flussonic"
    if kind not in KINDS:
        kind = ""
    if kind and not days:
        days = DEFAULT_DAYS
    return kind, days, source


def xtream_parts(url):
    """(base address, user, password, stream id, extension) of an Xtream
    live stream address, or None."""
    p = urllib.parse.urlsplit(url or "")
    m = XTREAM_LIVE.match(p.path)
    if not m or p.scheme not in ("http", "https"):
        return None
    return (f"{p.scheme}://{p.netloc}", urllib.parse.unquote(m["user"]),
            urllib.parse.unquote(m["password"]), m["id"], m["ext"] or "ts")


def available(channel, start, end, now=None):
    """True when the provider's archive has the programme (start..end): it
    has begun and is not older than the channel's catch-up days."""
    if not channel.catchup or not channel.url:
        return False
    now = time.time() if now is None else now
    return start < now and start >= now - channel.catchup_days * 86400


def _fill(template, start, end, now):
    """Fill the placeholders of a catch-up template (Kodi's IPTV Simple names)."""
    duration = max(60, int(end - start))
    utc = time.gmtime(start)
    utc_end = time.gmtime(end)
    values = {
        "utc": int(start), "start": int(start), "utcend": int(end), "end": int(end),
        "lutc": int(now), "now": int(now), "timestamp": int(now),
        "duration": duration, "offset": max(0, int(now - start)),
        "Y": f"{utc.tm_year:04d}", "m": f"{utc.tm_mon:02d}", "d": f"{utc.tm_mday:02d}",
        "H": f"{utc.tm_hour:02d}", "M": f"{utc.tm_min:02d}", "S": f"{utc.tm_sec:02d}",
        "Yend": f"{utc_end.tm_year:04d}", "mend": f"{utc_end.tm_mon:02d}",
        "dend": f"{utc_end.tm_mday:02d}", "Hend": f"{utc_end.tm_hour:02d}",
        "Mend": f"{utc_end.tm_min:02d}", "Send": f"{utc_end.tm_sec:02d}",
    }

    def one(m):
        name, arg = m.group(1), m.group(2)
        if name in ("duration", "offset") and arg and arg.isdigit():
            return str(values[name] // int(arg))     # {duration:60}: in minutes
        if name in ("utc", "start", "utcend", "end", "lutc", "now", "timestamp") and arg:
            t = {"utc": start, "start": start, "utcend": end, "end": end}.get(name, now)
            fmt = "".join("%" + ch if ch in "YmdHMS" else ch for ch in arg)
            return time.strftime(fmt, time.gmtime(t))
        return str(values.get(name, m.group(0)))
    return re.sub(r"\$?\{(\w+)(?::([^}]*))?\}", one, template)


def url(channel, start, end, now=None):
    """The address of the programme start..end in the provider's archive,
    or "" when the channel has no catch-up."""
    now = time.time() if now is None else now
    end = min(end, now) if end > now else end
    kind, base = channel.catchup, channel.url
    if not kind or not base:
        return ""
    if kind == "xc":
        parts = xtream_parts(base)
        if parts is None:
            return ""
        server, user, password, sid, ext = parts
        minutes = max(1, int((end - start + 59) // 60))
        t = time.strftime("%Y-%m-%d:%H-%M", time.gmtime(start))
        return (f"{server}/timeshift/{urllib.parse.quote(user, safe='')}/"
                f"{urllib.parse.quote(password, safe='')}/"
                f"{minutes}/{t}/{sid}.{ext}")
    if kind == "shift":
        return base + ("&" if "?" in base else "?") + _fill("utc={utc}&lutc={lutc}",
                                                             start, end, now)
    if kind == "flussonic":
        p = urllib.parse.urlsplit(base)
        path, duration = p.path, max(60, int(end - start))
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".m3u8"):
            stem = name[:-5]
            path = path[:-len(name)] + f"{stem}-{int(start)}-{duration}.m3u8"
        elif name in ("mpegts", "") or name.endswith(".ts"):
            path = path[:-len(name)] + f"timeshift_abs-{int(start)}.ts"
        else:
            path = path.rstrip("/") + f"/index-{int(start)}-{duration}.m3u8"
        return urllib.parse.urlunsplit((p.scheme, p.netloc, path, p.query, p.fragment))
    source = channel.catchup_source
    if not source:
        source = "?utc={utc}&lutc={lutc}" if kind == "append" else ""
        if not source:
            return ""
    filled = _fill(source, start, end, now)
    if kind == "append" or "://" not in filled:
        if filled.startswith("?") and "?" in base:
            filled = "&" + filled[1:]
        return base + filled
    return filled


# -- Xtream Codes: which channels have an archive ------------------------------------

def xtream_api_url(m3u_url):
    """player_api.php live streams address of an Xtream m3u_plus address."""
    p = urllib.parse.urlsplit(m3u_url)
    q = urllib.parse.parse_qs(p.query)
    user, password = q.get("username", [""])[0], q.get("password", [""])[0]
    if not user:
        return ""
    query = urllib.parse.urlencode({"username": user, "password": password,
                                    "action": "get_live_streams"})
    return f"{p.scheme}://{p.netloc}/player_api.php?{query}"


def parse_xtream_archive(text):
    """{stream id: archive days} from the player_api.php live stream list."""
    try:
        items = json.loads(text)
    except ValueError:
        return {}
    out = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        try:
            archive = int(it.get("tv_archive") or 0)
            days = int(it.get("tv_archive_duration") or 0)
            sid = str(int(it.get("stream_id")))
        except (TypeError, ValueError):
            continue
        if archive and days > 0:
            out[sid] = days
    return out


def load_xtream_archive(m3u_url, timeout=30):
    """Ask the Xtream server which channels have an archive (network)."""
    api = xtream_api_url(m3u_url)
    if not api:
        return {}
    req = urllib.request.Request(api, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return parse_xtream_archive(resp.read(32 * 1024 * 1024).decode("utf-8", "replace"))


def apply_xtream_archive(channels, archive):
    """Mark the channels the server keeps an archive of (catchup "xc").
    Returns how many have one."""
    n = 0
    for c in channels:
        parts = xtream_parts(c.url)
        if parts is None:
            continue
        days = archive.get(parts[3], 0)
        if days:
            c.catchup, c.catchup_days = "xc", days
            n += 1
        elif c.catchup == "xc":
            c.catchup, c.catchup_days = "", 0
    return n
