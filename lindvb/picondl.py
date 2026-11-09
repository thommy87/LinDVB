# SPDX-License-Identifier: GPL-2.0-or-later
"""Download picons for the channels in the list, one logo at a time.

The picons project (github.com/picons/picons) keeps every logo once, in full
resolution, plus two small index files that say which logo a channel uses:

* ``srp.index``: ``SID_TSID_ONID_NAMESPACE=logo`` (hex, as in Enigma2
  service references), e.g. ``283D_3FB_1_C00000=daserstehd``
* ``snp.index``: ``snpname=logo``, e.g. ``daserstehd=daserstehd``

The logo itself is ``build-source/logos/<logo>.default.svg`` (or ``.png``).
Instead of cloning the whole repository (several hundred MB) LinDVB fetches
the two index files (about 1 MB) and then only the logos of the user's own
channels, and saves each one under a name ``picons.Index`` finds: the service
reference for a matched channel, else its SNP name. Channels that already
have a logo in the folder are left alone.
"""
import os
import urllib.error
import urllib.request

from . import picons

BASE = "https://raw.githubusercontent.com/picons/picons/master/build-source/"
USER_AGENT = "LinDVB (https://github.com/thommy87/lindvb)"


class Error(Exception):
    pass


def fetch(url, timeout=20):
    """Body of *url*, or None for 404. Raises Error for other failures."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise Error(f"HTTP {e.code}: {url}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Error(f"{getattr(e, 'reason', e)}") from None


def parse_srp(text):
    """(sid, tsid, onid, ns >> 16) -> logo and (sid, tsid, onid) -> logo."""
    exact, any_ns = {}, {}
    for line in text.splitlines():
        key, _sep, logo = line.strip().partition("=")
        parts = key.split("_")
        if not logo or len(parts) != 4:
            continue
        try:
            sid, tsid, onid, ns = (int(p, 16) for p in parts)
        except ValueError:
            continue
        exact.setdefault((sid, tsid, onid, ns >> 16), logo)
        any_ns.setdefault((sid, tsid, onid), logo)
    return exact, any_ns


def parse_snp(text):
    out = {}
    for line in text.splitlines():
        key, _sep, logo = line.strip().partition("=")
        if key and logo:
            out.setdefault(key, logo)
    return out


def service_ref(channel, ns):
    """Enigma2 picon file stem of *channel* for namespace upper bits *ns*."""
    return "1_0_%X_%X_%X_%X_%X0000_0_0_0" % (2 if channel.radio else 1, channel.sid,
                                            channel.tsid, channel.onid, ns or 0)


def plan(channels, srp, snp):
    """[(channel, logo, file stem)] for the channels the indexes know."""
    exact, any_ns = srp
    out = []
    for c in channels:
        if c.sid:
            ns = picons.namespace(c)
            key = (c.sid, c.tsid, c.onid)
            logo = exact.get((*key, ns)) if ns is not None else any_ns.get(key)
            if logo:
                out.append((c, logo, service_ref(c, ns)))
                continue
        for name in (c.name, c.tvg_name):
            stem = picons.snp_name(name)
            if stem and stem in snp:
                out.append((c, snp[stem], stem))
                break
    return out


def download(channels, folder, fetcher=fetch, progress=None, cancelled=None):
    """Fetch the picons of *channels* that have no logo in *folder* yet.

    *progress(done, total)* is called after every channel. Returns
    (downloaded, already there, not in the picon set). Raises Error when the
    index files cannot be read."""
    os.makedirs(folder, exist_ok=True)
    idx = picons.index(folder)
    todo, have = [], 0
    for c in channels:
        if idx and idx.find(c):
            have += 1
        else:
            todo.append(c)
    if not todo:
        return 0, have, 0
    srp_text = fetcher(BASE + "srp.index")
    snp_text = fetcher(BASE + "snp.index")
    if not srp_text or not snp_text:
        raise Error("picon index not found")
    jobs = plan(todo, parse_srp(srp_text.decode("utf-8", "replace")),
                parse_snp(snp_text.decode("utf-8", "replace")))
    got = {}            # logo -> (data, ext), each logo fetched once
    done = 0
    for i, (_c, logo, stem) in enumerate(jobs):
        if cancelled and cancelled():
            break
        if logo not in got:
            got[logo] = None
            for ext in (".svg", ".png"):
                data = fetcher(f"{BASE}logos/{logo}.default{ext}")
                if data:
                    got[logo] = (data, ext)
                    break
        if got[logo]:
            data, ext = got[logo]
            path = os.path.join(folder, stem + ext)
            if not os.path.exists(path):
                tmp = path + ".part"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            done += 1
        if progress:
            progress(i + 1, len(jobs))
    return done, have, len(todo) - done
