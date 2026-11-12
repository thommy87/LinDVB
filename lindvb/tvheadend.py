# SPDX-License-Identifier: GPL-2.0-or-later
"""Tvheadend servers as a channel source (Preferences › Internet).

Tvheadend (https://tvheadend.org) runs the tuners on another computer and
hands out what they receive over HTTP. LinDVB is only a client of it: the
user enters the server, a user name and a password once, and LinDVB fetches

* the channel list: ``/playlist/channels.m3u?profile=pass`` (an M3U with the
  channel number, logo and the channel's UUID as ``tvg-id``),
* the channel tags: ``/playlist/e2/tags`` (Enigma2 format, the tags and the
  channels in each), which become the channel groups and tell radio apart,
* the guide: ``/xmltv/channels`` (XMLTV whose channel ids are the same UUIDs),
* the streams: ``/stream/channel/<uuid>?profile=pass``, the unchanged MPEG
  transport stream of the service.

Tvheadend asks for HTTP digest authentication (MD5, or SHA-256 / SHA-512-256
when the server is set so) or plain (basic) authentication. Python's urllib
only knows MD5 digest and stumbles over servers that offer several
algorithms, so this module answers the challenge itself.

The servers are the IPTV lists of kind ``"tvheadend"`` in the setting
``iptv_sources``; their user name and password are kept there too::

    {"kind": "tvheadend", "location": "http://192.168.1.5:9981", "root": "Tvheadend",
     "user": "alex", "password": "…", "count": 120, "date": 1700000000}

Channels from a server carry its address in ``Channel.tvh_server``.

No GTK, no GStreamer.
"""

import base64
import hashlib
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PORT = 9981
PROFILE = "pass"                 # the unchanged transport stream of the service
USER_AGENT = "LinDVB"
UUID_RE = re.compile(r"^[0-9a-f]{32}$")

# servers known to the app (set by configure()): base address -> (user, password)
_servers = {}


class TvheadendError(Exception):
    pass


# --------------------------------------------------------------------------
# addresses
# --------------------------------------------------------------------------

def base_url(server, port=None):
    """``http://host:port`` of a server the user typed ("tvh", "tvh:9981",
    "https://tvh.example.org/tvheadend/"); the port defaults to 9981."""
    server = (server or "").strip().rstrip("/")
    if not server:
        raise TvheadendError("No server given.")
    if "://" not in server:
        server = "http://" + server
    parts = urllib.parse.urlsplit(server)
    if not parts.hostname:
        raise TvheadendError(f"Not a server address: {server}")
    host = parts.hostname
    if ":" in host:                               # IPv6
        host = f"[{host}]"
    if port is None:
        port = parts.port
    if port is None and parts.scheme == "http":
        port = DEFAULT_PORT
    netloc = f"{host}:{port}" if port else host
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", ""))


def playlist_url(base):
    return f"{base}/playlist/channels.m3u?profile={PROFILE}"


def tags_url(base):
    return f"{base}/playlist/e2/tags?profile={PROFILE}"


def xmltv_url(base):
    return f"{base}/xmltv/channels"


def stream_url(base, uuid):
    return f"{base}/stream/channel/{uuid}?profile={PROFILE}"


def display_location(src):
    """Where a server list comes from, for the user: host and user name."""
    parts = urllib.parse.urlsplit(src.get("location", ""))
    user = src.get("user", "")
    return f"{parts.netloc} ({user})" if user else parts.netloc


def default_name(base):
    host = urllib.parse.urlsplit(base).hostname or ""
    return f"Tvheadend {host}".strip()


# --------------------------------------------------------------------------
# the servers the user added
# --------------------------------------------------------------------------

def servers(settings):
    """The Tvheadend entries of the IPTV lists."""
    return [s for s in (settings.get("iptv_sources") or []) if s.get("kind") == "tvheadend"]


def configure(settings):
    """Remember the servers and their logins, for credentials()."""
    _servers.clear()
    for s in servers(settings):
        if s.get("location"):
            _servers[s["location"].rstrip("/")] = (s.get("user", ""), s.get("password", ""))


def server_for(url):
    """The base address of the configured server *url* belongs to, or ""."""
    if not url:
        return ""
    for base in sorted(_servers, key=len, reverse=True):
        if url == base or url.startswith(base + "/"):
            return base
    return ""


def credentials(url):
    """(user, password) for an address on a configured server, else None."""
    base = server_for(url)
    if not base:
        return None
    user, password = _servers[base]
    return (user, password) if user else None


def set_login(src, uri):
    """A GStreamer HTTP source (souphttpsrc) playing a Tvheadend stream:
    give it the server's login (libsoup answers basic and MD5 digest)."""
    login = credentials(uri)
    if login and src.find_property("user-id") is not None:
        src.set_property("user-id", login[0])
        src.set_property("user-pw", login[1])


# --------------------------------------------------------------------------
# HTTP with basic and digest authentication
# --------------------------------------------------------------------------

def _hash(algorithm):
    algo = (algorithm or "MD5").upper()
    if algo.endswith("-SESS"):
        algo = algo[:-5]
    if algo == "MD5":
        return lambda s: hashlib.md5(s.encode()).hexdigest()
    if algo == "SHA-256":
        return lambda s: hashlib.sha256(s.encode()).hexdigest()
    if algo == "SHA-512-256":
        try:
            hashlib.new("sha512_256")
        except ValueError:
            return None
        return lambda s: hashlib.new("sha512_256", s.encode()).hexdigest()
    return None


def parse_challenges(headers):
    """The authentication challenges of a 401 answer, as (scheme, params)
    pairs in the order the server sent them. *headers* is a list of
    WWW-Authenticate values (Tvheadend sends one header per algorithm)."""
    out = []
    for value in headers:
        m = re.match(r"\s*(\w+)\s*(.*)", value or "", re.S)
        if not m:
            continue
        scheme, rest = m.group(1).lower(), m.group(2)
        params = {k.lower(): (v1 if v1 or q else v2) for k, q, v1, v2 in
                  re.findall(r'(\w+)\s*=\s*(?:(")((?:[^"\\]|\\.)*)"|([^,\s]*))', rest)}
        out.append((scheme, params))
    return out


ALGORITHM_PREFERENCE = ("SHA-512-256", "SHA-256", "MD5")


def pick_challenge(challenges):
    """The challenge to answer: the strongest digest algorithm this Python
    supports, else basic. None when there is nothing we can answer."""
    digests = [(p.get("algorithm", "MD5").upper(), p) for s, p in challenges if s == "digest"]
    for algo in ALGORITHM_PREFERENCE:
        for a, p in digests:
            if a in (algo, algo + "-SESS") and _hash(a) is not None:
                return "digest", p
    for s, p in challenges:
        if s == "basic":
            return "basic", p
    return None


def basic_header(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def digest_header(params, user, password, method, uri, nc=1, cnonce=None):
    """The Authorization value answering a digest challenge (RFC 7616)."""
    algo = params.get("algorithm", "MD5")
    h = _hash(algo)
    if h is None:
        raise TvheadendError(f"Unsupported digest algorithm {algo}")
    realm, nonce = params.get("realm", ""), params.get("nonce", "")
    qops = [q.strip() for q in params.get("qop", "").split(",") if q.strip()]
    cnonce = cnonce or secrets.token_hex(8)
    ha1 = h(f"{user}:{realm}:{password}")
    if algo.upper().endswith("-SESS"):
        ha1 = h(f"{ha1}:{nonce}:{cnonce}")
    ha2 = h(f"{method}:{uri}")
    ncv = f"{nc:08x}"
    if "auth" in qops:
        response = h(f"{ha1}:{nonce}:{ncv}:{cnonce}:auth:{ha2}")
    else:
        response = h(f"{ha1}:{nonce}:{ha2}")
    parts = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"',
             f"algorithm={algo}", f'response="{response}"']
    if params.get("opaque"):
        parts.append(f'opaque="{params["opaque"]}"')
    if "auth" in qops:
        parts += ["qop=auth", f"nc={ncv}", f'cnonce="{cnonce}"']
    return "Digest " + ", ".join(parts)


def _request_uri(url):
    parts = urllib.parse.urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def open_url(url, user="", password="", timeout=30, opener=None):
    """GET *url* and return the open response (the caller reads and closes
    it). Answers a 401 with the given login. Raises TvheadendError."""
    opener = opener or urllib.request.build_opener()
    headers = {"User-Agent": USER_AGENT}
    host = urllib.parse.urlsplit(url).netloc or url
    try:
        return opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise TvheadendError(f"{host}: HTTP {e.code} {e.reason}") from None
        challenges = parse_challenges(e.headers.get_all("WWW-Authenticate") or [])
        e.close()
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise TvheadendError(f"{host}: {getattr(e, 'reason', e)}") from None
    if not user:
        raise TvheadendError(f"{host}: the server asks for a user name and password.")
    choice = pick_challenge(challenges)
    if choice is None:
        raise TvheadendError(f"{host}: unsupported authentication.")
    scheme, params = choice
    headers["Authorization"] = basic_header(user, password) if scheme == "basic" else \
        digest_header(params, user, password, "GET", _request_uri(url))
    try:
        return opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code in (401, 403):
            raise TvheadendError(f"{host}: wrong user name or password, or the user "
                                 f"may not stream (HTTP {e.code}).") from None
        raise TvheadendError(f"{host}: HTTP {e.code} {e.reason}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise TvheadendError(f"{host}: {getattr(e, 'reason', e)}") from None


def fetch(url, user="", password="", timeout=30, opener=None):
    """GET *url* and return its body."""
    with open_url(url, user, password, timeout, opener) as resp:
        return resp.read()


# --------------------------------------------------------------------------
# channel list, tags and guide
# --------------------------------------------------------------------------

def parse_e2_tags(text):
    """Tag of each channel from ``/playlist/e2/tags``: {channel id: [tags]}.
    The list names a tag (``#SERVICE 1:64:…::Name``) and then the streams of
    its channels (``#SERVICE 1:0:…:http%3a//…/stream/channelid/12?…:Name``)."""
    tags, tag = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#SERVICE"):
            continue
        body = line.split(None, 1)[1] if " " in line else ""
        fields = body.split(":")
        if len(fields) >= 11 and fields[1] == "64":
            tag = ":".join(fields[11:]) or fields[-1]
            continue
        if tag is None:
            continue
        url = urllib.parse.unquote(":".join(fields[10:]))
        m = re.search(r"/stream/channelid/(\d+)", url)
        if m:
            tags.setdefault(m.group(1), []).append(tag)
    return tags


RADIO_TAG = re.compile(r"radio", re.I)


def channels_from_playlist(raw, base, tags=None, root=""):
    """The channels of the server's M3U (bytes): stream addresses by UUID,
    the guide id set, groups and radio from the tags."""
    from .channels import decode_text, parse_m3u
    tags = tags or {}
    out = []
    for c in parse_m3u(decode_text(raw), root=root or default_name(base)):
        m = re.search(r"/stream/channelid/(\d+)", c.url)
        cid = m.group(1) if m else ""
        uuid = c.tvg_id.lower()
        if UUID_RE.match(uuid):
            c.url = stream_url(base, uuid)
            c.xmltv_id = uuid
        c.tvh_server = base
        mine = tags.get(cid, [])
        if any(RADIO_TAG.search(t) for t in mine):
            c.radio = True
        groups = [t for t in mine if not RADIO_TAG.search(t) and
                  not re.search(r"^(tv|hd|sd)( channels)?$|^tv-?sender$", t, re.I)]
        if not c.category and groups:
            c.category = groups[0]
        out.append(c)
    return out


def load_channels(base, user="", password="", root="", opener=None):
    """Fetch the channel list (and the tags, when the server gives them)."""
    raw = fetch(playlist_url(base), user, password, opener=opener)
    try:
        tags = parse_e2_tags(fetch(tags_url(base), user, password, opener=opener)
                             .decode("utf-8", "replace"))
    except TvheadendError:
        tags = {}
    chans = channels_from_playlist(raw, base, tags, root)
    if not chans:
        raise TvheadendError(f"{urllib.parse.urlsplit(base).netloc}: the server has no "
                             "channels this user may watch.")
    return chans


def load_guide(base, user="", password="", language="", opener=None):
    """The server's XMLTV guide: (xmltv channels, programmes) as read_xmltv."""
    from .epg import read_xmltv
    return read_xmltv(fetch(xmltv_url(base), user, password, timeout=120, opener=opener),
                      language)


def match_guide(channels, base, xmltv_channels):
    """Point the server's channels at its guide. Normally the guide ids are
    the channel UUIDs; a server set to name its guide channels by name
    ("basic, no hash") gets them matched by name. Returns the matches."""
    from .xmltvmatch import Matcher
    m = Matcher(xmltv_channels)
    n = 0
    for c in channels:
        if c.tvh_server != base or c.xmltv_manual:
            continue
        if c.tvg_id.lower() in m.by_id:
            c.xmltv_id = m.by_id[c.tvg_id.lower()]
        else:
            cid, _how, _score = m.match(c.name, "", c.tvg_name)
            c.xmltv_id = cid or c.xmltv_id
        n += bool(c.xmltv_id and c.xmltv_id in m.channels)
    return n


def download_logos(channels, folder, user="", password="", max_age=7 * 24 * 3600,
                   opener=None):
    """Channel logos; those in the server's image cache need the login."""
    from .dvbi import logo_file
    os.makedirs(folder, exist_ok=True)
    done, count = {}, 0
    for c in channels:
        url = c.logo_url
        if not url:
            continue
        path = done.get(url) or logo_file(folder, url)
        fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age
        if url not in done and not fresh:
            login = credentials(url) or (user, password)
            try:
                data = fetch(url, *login, timeout=15, opener=opener)
                if data:
                    with open(path + ".tmp", "wb") as f:
                        f.write(data)
                    os.replace(path + ".tmp", path)
                    count += 1
            except (TvheadendError, OSError):
                pass
        done[url] = path
        c.logo = path if os.path.exists(path) else c.logo
    return count
