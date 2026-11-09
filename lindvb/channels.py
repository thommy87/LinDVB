# SPDX-License-Identifier: GPL-2.0-or-later
"""Channel model, channel database, and import/export of common list formats.

Supported formats:
  * dvbv5 channel files (output of dvbv5-scan, read/write)
  * VDR channels.conf (read)
  * Enigma2 lamedb version 4 (read)
  * M3U / M3U8 playlists for IPTV (read/write)
  * CSV (read/write, LinDVB's own column layout)
  * DVBViewer channel lists (*.ini, read) so existing lists can be migrated
"""

import configparser
import csv
import io
import json
import os
import re
import urllib.parse
from dataclasses import asdict, dataclass, field, fields

from .config import CONFIG_DIR

# Internet channels from a DVB-I list that name a content guide take their
# programme guide from it, ahead of XMLTV (the app sets this from the
# "dvbi_guide" setting).
DVBI_GUIDE_FIRST = True

SAT_SYSTEMS = ("dvb-s", "dvb-s2", "turbo", "isdb-s")


@dataclass
class Channel:
    name: str
    delsys: str = "dvb-s2"          # dvbsrc nick, or "iptv"
    root: str = ""                   # e.g. satellite or network name
    category: str = ""               # provider / bouquet
    frequency: int = 0               # kHz
    polarity: str = ""               # "h" / "v" (satellite)
    symbol_rate: int = 0             # kBd
    modulation: str = "auto"         # dvbsrc nick
    fec: str = "auto"
    bandwidth_hz: int = 0
    stream_id: int = -1
    diseqc: int = -1                 # -1 = none, 0..3 = committed port
    sid: int = 0
    tsid: int = 0
    onid: int = 0
    pmt_pid: int = 0
    pcr_pid: int = 0
    video_pid: int = 0
    audio_pids: list = field(default_factory=list)
    teletext_pid: int = 0
    subtitle_pids: list = field(default_factory=list)
    ait_pid: int = 0                 # HbbTV application signalling (learnt from the PMT)
    encrypted: bool = False
    radio: bool = False
    url: str = ""                    # for IPTV
    tvg_id: str = ""                 # IPTV: tvg-id / tvg-name from the M3U
    tvg_name: str = ""
    xmltv_id: str = ""               # IPTV: matched XMLTV guide channel
    xmltv_manual: bool = False       # the match was chosen by the user
    favourite: bool = False
    number: int = 0
    vchannel: str = ""               # ATSC virtual channel "7.1" (major.minor)
    # DVB-I (see lindvb.dvbi): the service's unique identifier, its logical
    # channel number, the LCN flags, the logo and all service instances of
    # the service (the tuning fields above hold the instance in use)
    dvbi_id: str = ""
    lcn: int = 0
    selectable: bool = True
    visible: bool = True
    logo_url: str = ""
    logo: str = ""                   # downloaded logo file
    dvbi_instances: list = field(default_factory=list)
    dvbi_alt_names: list = field(default_factory=list)
    dvbi_cg: str = ""                # content guide service reference
    dvbi_guide: dict = field(default_factory=dict)   # content guide endpoints
    dvbi_periods: list = field(default_factory=list)  # availability (pop-up channel)
    dvbi_oos_logo: str = ""          # out-of-service banner image URL
    dvbi_apps: list = field(default_factory=list)    # linked HbbTV applications (url, type, role)

    @property
    def is_iptv(self):
        return self.delsys == "iptv"

    @property
    def is_sat(self):
        return self.delsys in SAT_SYSTEMS

    @property
    def key(self):
        """Stable identifier used for favourites, EPG and timers."""
        if self.dvbi_id:
            return "dvbi:" + self.dvbi_id
        if self.is_iptv:
            return "url:" + self.url
        return f"{self.delsys}:{self.frequency}:{self.polarity}:{self.sid}"

    @property
    def mux_key(self):
        return f"{self.delsys}:{self.frequency}:{self.polarity}:{self.stream_id}"

    @property
    def epg_key(self):
        if self.is_iptv:
            if self.dvbi_id and DVBI_GUIDE_FIRST and self.dvbi_guide.get("schedule"):
                return "dvbi:" + self.dvbi_id
            if self.xmltv_id:
                return "xmltv:" + self.xmltv_id
            if self.dvbi_id:
                return "dvbi:" + self.dvbi_id
            return "iptv:" + self.name.lower()
        return f"{self.tsid}:{self.sid}"

    @property
    def epg_links(self):
        """All guide keys of the channel, ``epg_key`` first: the broadcast EIT,
        the DVB-I content guide (when it is loaded) and XMLTV. The EPG store
        merges them by the source priority (lindvb.epgmerge)."""
        keys = [self.epg_key]
        if self.dvbi_id and DVBI_GUIDE_FIRST and self.dvbi_guide.get("schedule"):
            keys.append("dvbi:" + self.dvbi_id)
        if self.xmltv_id:
            keys.append("xmltv:" + self.xmltv_id)
        return list(dict.fromkeys(keys))

    def pids(self):
        p = [0, 1, 16, 17, 18, 20, self.pmt_pid, self.pcr_pid, self.video_pid,
             self.teletext_pid, self.ait_pid, *self.audio_pids, *self.subtitle_pids]
        return sorted({x for x in p if 0 <= x < 8191} | {0})

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


class ChannelDB:
    path = CONFIG_DIR / "channels.json"

    def __init__(self):
        self.channels: list[Channel] = []
        self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text())
            self.channels = [Channel.from_dict(d) for d in data]
        except (OSError, ValueError):
            self.channels = []

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(c) for c in self.channels], indent=1))
        tmp.replace(self.path)

    def renumber(self):
        """Number the channels 1, 2, 3 ... in list order. Channels from a DVB-I
        service list keep their logical channel number (LCN) and come first,
        sorted by it; the other channels follow after the highest LCN."""
        lcn = sorted((c for c in self.channels if c.lcn > 0), key=lambda c: c.lcn)
        if not lcn:
            for i, c in enumerate(self.channels, 1):
                c.number = i
            return
        rest = [c for c in self.channels if c.lcn <= 0]
        for c in lcn:
            c.number = c.lcn
        for i, c in enumerate(rest, lcn[-1].lcn + 1):
            c.number = i
        self.channels[:] = lcn + rest

    def dvbi_channels(self):
        return [c for c in self.channels if c.dvbi_id]

    def replace_dvbi(self, new):
        """Replace the channels of the DVB-I service list with *new* (a list
        update). Favourites and guide matches of services that stay are kept.
        Returns (added, updated, removed)."""
        old = {c.key: c for c in self.channels if c.dvbi_id}
        keep = [c for c in self.channels if not c.dvbi_id]
        added = updated = 0
        for c in new:
            prev = old.pop(c.key, None)
            if prev:
                c.favourite = prev.favourite
                if prev.xmltv_manual or not c.xmltv_id:
                    c.xmltv_id, c.xmltv_manual = prev.xmltv_id, prev.xmltv_manual
                if not c.logo:
                    c.logo = prev.logo
                updated += 1
            else:
                added += 1
        self.channels[:] = list(new) + keep
        self.renumber()
        return added, updated, len(old)

    def by_key(self, key):
        for c in self.channels:
            if c.key == key:
                return c
        return None

    def merge(self, new, update_only=False, fta_only=False, update_names=True):
        """Add or update channels. Returns (added, updated)."""
        index = {c.key: c for c in self.channels}
        added = updated = 0
        for c in new:
            if fta_only and c.encrypted:
                continue
            old = index.get(c.key)
            if old:
                fav, name, num = old.favourite, old.name, old.number
                xid, xman = old.xmltv_id, old.xmltv_manual
                old.__dict__.update(asdict(c))
                old.favourite, old.number = fav, num
                if xman or not c.xmltv_id:       # keep the guide match
                    old.xmltv_id, old.xmltv_manual = xid, xman
                if not update_names:
                    old.name = name
                updated += 1
            elif not update_only:
                self.channels.append(c)
                index[c.key] = c
                added += 1
        self.renumber()
        return added, updated

    def roots(self):
        return sorted({c.root for c in self.channels if c.root})

    def categories(self, root=None):
        return sorted({c.category for c in self.channels
                       if c.category and (root is None or c.root == root)})


# --------------------------------------------------------------------------
# dvbv5 channel files
# --------------------------------------------------------------------------

DVBV5_DELSYS = {
    "DVBS": "dvb-s", "DVBS2": "dvb-s2", "DVBC/ANNEX_A": "dvb-c-a",
    "DVBC/ANNEX_B": "dvb-c-b", "DVBC/ANNEX_C": "dvb-c-c", "DVBT": "dvb-t",
    "DVBT2": "dvb-t2", "ATSC": "atsc", "ISDBT": "isdb-t", "ISDBS": "isdb-s",
    "DTMB": "dtmb", "TURBO": "turbo",
}
DVBV5_DELSYS_REV = {v: k for k, v in DVBV5_DELSYS.items()}

DVBV5_MOD = {
    "QPSK": "qpsk", "PSK/8": "8psk", "APSK/16": "16apsk", "APSK/32": "32apsk",
    "QAM/16": "qam-16", "QAM/32": "qam-32", "QAM/64": "qam-64",
    "QAM/128": "qam-128", "QAM/256": "qam-256", "QAM/AUTO": "auto",
    "VSB/8": "8vsb", "VSB/16": "16vsb", "DQPSK": "dqpsk", "AUTO": "auto",
}
DVBV5_MOD_REV = {v: k for k, v in DVBV5_MOD.items() if k != "AUTO"}


def _ints(s):
    return [int(x) for x in s.split() if x.isdigit()]


def parse_dvbv5(text, root=""):
    """Parse a dvbv5 channel file (as written by dvbv5-scan -O DVBV5)."""
    out = []
    for block in re.split(r"^\[", text, flags=re.M)[1:]:
        head, _, body = block.partition("]")
        kv = {}
        for line in body.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k.strip().upper()] = v.strip()
        delsys = DVBV5_DELSYS.get(kv.get("DELIVERY_SYSTEM", ""), "dvb-t")
        sat = delsys in SAT_SYSTEMS
        freq = int(kv.get("FREQUENCY", 0) or 0)
        if not sat:
            freq //= 1000                       # Hz -> kHz
        vpid = (_ints(kv.get("VIDEO_PID", "")) or [0])[0]
        apids = _ints(kv.get("AUDIO_PID", ""))
        others = _ints(" ".join(v for k, v in kv.items() if k.startswith("PID_")))
        c = Channel(
            name=head.strip(), delsys=delsys, root=root,
            category=kv.get("PROVIDER", ""),
            frequency=freq,
            polarity=kv.get("POLARIZATION", "")[:1].lower() if sat else "",
            symbol_rate=int(kv.get("SYMBOL_RATE", 0) or 0) // 1000,
            modulation=DVBV5_MOD.get(kv.get("MODULATION", "AUTO"), "auto"),
            fec=kv.get("INNER_FEC", "AUTO").lower(),
            bandwidth_hz=int(kv.get("BANDWIDTH_HZ", 0) or 0),
            stream_id=int(kv.get("STREAM_ID", -1) or -1),
            diseqc=int(kv.get("SAT_NUMBER", -1) or -1),
            sid=int(kv.get("SERVICE_ID", 0) or 0),
            tsid=int(kv.get("TRANSPORT_ID", 0) or 0),
            onid=int(kv.get("NETWORK_ID", 0) or 0),
            vchannel=kv.get("VCHANNEL", "").strip(),
            video_pid=vpid, pcr_pid=vpid, audio_pids=apids,
            subtitle_pids=others, radio=vpid == 0 and bool(apids),
        )
        out.append(c)
    return out


def write_dvbv5(channels):
    """Write a dvbv5 channel file (usable with dvbv5-zap and GStreamer dvbbasebin)."""
    buf = io.StringIO()
    for c in channels:
        if c.is_iptv:
            continue
        buf.write(f"[{c.name}]\n")
        buf.write(f"\tSERVICE_ID = {c.sid}\n")
        if c.tsid:
            buf.write(f"\tTRANSPORT_ID = {c.tsid}\n")
        if c.onid:
            buf.write(f"\tNETWORK_ID = {c.onid}\n")
        if c.vchannel:
            buf.write(f"\tVCHANNEL = {c.vchannel}\n")
        if c.video_pid:
            buf.write(f"\tVIDEO_PID = {c.video_pid}\n")
        if c.audio_pids:
            buf.write(f"\tAUDIO_PID = {' '.join(map(str, c.audio_pids))}\n")
        if c.teletext_pid:
            buf.write(f"\tPID_06 = {c.teletext_pid}\n")
        buf.write(f"\tDELIVERY_SYSTEM = {DVBV5_DELSYS_REV.get(c.delsys, 'DVBT')}\n")
        if c.is_sat:
            buf.write(f"\tFREQUENCY = {c.frequency}\n")
            buf.write(f"\tPOLARIZATION = {'HORIZONTAL' if c.polarity == 'h' else 'VERTICAL'}\n")
            if c.diseqc >= 0:
                buf.write(f"\tSAT_NUMBER = {c.diseqc}\n")
        else:
            buf.write(f"\tFREQUENCY = {c.frequency * 1000}\n")
        if c.symbol_rate:
            buf.write(f"\tSYMBOL_RATE = {c.symbol_rate * 1000}\n")
        if c.bandwidth_hz:
            buf.write(f"\tBANDWIDTH_HZ = {c.bandwidth_hz}\n")
        if c.modulation in DVBV5_MOD_REV:
            buf.write(f"\tMODULATION = {DVBV5_MOD_REV[c.modulation]}\n")
        if c.fec not in ("", "auto"):
            buf.write(f"\tINNER_FEC = {c.fec.upper()}\n")
        if c.stream_id >= 0:
            buf.write(f"\tSTREAM_ID = {c.stream_id}\n")
        buf.write("\n")
    return buf.getvalue()


# --------------------------------------------------------------------------
# VDR channels.conf
#   Name;Provider:Freq:Params:Source:Srate:VPID:APID:TPID:CA:SID:NID:TID:RID
# --------------------------------------------------------------------------

def parse_vdr(text, root=""):
    out = []
    for line in text.splitlines():
        if not line or line.startswith(":") or line.count(":") < 12:
            continue
        p = line.split(":")
        name, _, prov = p[0].partition(";")
        name = name.split(",")[0]
        params, source = p[2].upper(), p[3]
        freq = int(p[1] or 0)
        m = dict(re.findall(r"([A-Z])(\d+)", params))
        if source.startswith("S"):
            delsys = "dvb-s2" if m.get("S") == "1" else "dvb-s"
            freq *= 1000                                     # MHz -> kHz
            pol = next((ch.lower() for ch in params if ch in "HVLR"), "h")
        else:
            delsys = {"C": "dvb-c-a", "T": "dvb-t2" if m.get("S") == "1" else "dvb-t",
                      "A": "atsc"}.get(source[:1], "dvb-t")
            pol = ""
            if freq < 1000000:            # VDR uses MHz, kHz or Hz depending on age
                freq *= 1000
            elif freq > 100000000:
                freq //= 1000
        vpid = int(re.split(r"[+=]", p[5])[0] or 0)
        apids = [int(re.split(r"=|@", a)[0]) for a in re.split(r"[,;]", p[6]) if a and a[0].isdigit()]
        mod = {"2": "qpsk", "5": "8psk", "6": "16apsk", "16": "qam-16", "64": "qam-64",
               "128": "qam-128", "256": "qam-256", "999": "auto"}.get(m.get("M", ""), "auto")
        out.append(Channel(
            name=name, delsys=delsys, root=root or source, category=prov,
            frequency=freq, polarity=pol, symbol_rate=int(p[4] or 0), modulation=mod,
            bandwidth_hz=int(m.get("B", 0)) * 1000000 if "B" in m else 0,
            sid=int(p[9] or 0), onid=int(p[10] or 0), tsid=int(p[11] or 0),
            video_pid=vpid, pcr_pid=vpid, audio_pids=apids,
            teletext_pid=int(p[7].split(";")[0] or 0),
            encrypted=p[8] not in ("0", ""), radio=vpid in (0, 1),
        ))
    return out


# --------------------------------------------------------------------------
# Enigma2 lamedb (version 4)
# --------------------------------------------------------------------------

def parse_lamedb(text, root="Enigma2"):
    lines = text.splitlines()
    tps, out = {}, []
    try:
        i = lines.index("transponders") + 1
        while lines[i] != "end":
            key = lines[i].strip()
            data = lines[i + 1].strip()
            tps[key] = data
            i += 3
        i = lines.index("services") + 1
        while lines[i] != "end":
            ref, name, prov = lines[i].split(":"), lines[i + 1], lines[i + 2]
            i += 3
            sid, ns, tsid, onid, stype = (int(x, 16) for x in ref[:5])
            tp = tps.get(f"{ns:08x}:{tsid:04x}:{onid:04x}", "")
            c = Channel(name=name, root=root, sid=sid, tsid=tsid, onid=onid,
                        category=dict(x.split(":", 1) for x in prov.split(",") if ":" in x).get("p", ""),
                        radio=stype == 2)
            vals = tp[2:].split(":")
            if tp.startswith("s") and len(vals) >= 6:
                c.frequency, c.symbol_rate = int(vals[0]), int(vals[1]) // 1000
                c.polarity = "h" if vals[2] in ("0", "2") else "v"
                s2 = len(vals) > 6 and vals[6] == "1"
                c.delsys = "dvb-s2" if s2 else "dvb-s"
                c.modulation = "8psk" if s2 and len(vals) > 7 and vals[7] == "2" else "qpsk"
            elif tp.startswith("c") and len(vals) >= 3:
                c.delsys, c.frequency, c.symbol_rate = "dvb-c-a", int(vals[0]), int(vals[1]) // 1000
                c.modulation = {"1": "qam-16", "2": "qam-32", "3": "qam-64",
                                "4": "qam-128", "5": "qam-256"}.get(vals[3] if len(vals) > 3 else "", "auto")
            elif tp.startswith("t") and vals:
                c.delsys, c.frequency = "dvb-t", int(vals[0]) // 1000
            for item in prov.split(","):
                if item.startswith("c:"):
                    kind, pid = int(item[2:4], 16), int(item[4:], 16)
                    if kind == 0:
                        c.video_pid = pid
                    elif kind in (1, 4):
                        c.audio_pids.append(pid)
                    elif kind == 2:
                        c.teletext_pid = pid
                    elif kind == 3:
                        c.pcr_pid = pid
                elif item.startswith("C:"):
                    c.encrypted = True
            out.append(c)
    except (ValueError, IndexError):
        pass
    return out


# --------------------------------------------------------------------------
# M3U
# --------------------------------------------------------------------------

def parse_m3u(text, root="IPTV"):
    out, info = [], {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
            info = {"name": line.rsplit(",", 1)[-1].strip(),
                    "category": attrs.get("group-title", ""),
                    "radio": attrs.get("radio", "") == "true",
                    "tvg_id": attrs.get("tvg-id", "").strip(),
                    "tvg_name": attrs.get("tvg-name", "").strip(),
                    "logo_url": attrs.get("tvg-logo", "").strip()}
        elif line and not line.startswith("#"):
            out.append(Channel(name=info.get("name") or line, delsys="iptv", url=line,
                               root=root, category=info.get("category", ""),
                               radio=info.get("radio", False),
                               tvg_id=info.get("tvg_id", ""), tvg_name=info.get("tvg_name", ""),
                               logo_url=info.get("logo_url", "")))
            info = {}
    return out


def write_m3u(channels):
    lines = ["#EXTM3U"]
    for c in channels:
        if c.is_iptv and c.url:
            tvg = c.xmltv_id or c.tvg_id
            attrs = f'tvg-id="{tvg}" ' if tvg else ""
            if c.tvg_name:
                attrs += f'tvg-name="{c.tvg_name}" '
            lines.append(f'#EXTINF:-1 {attrs}group-title="{c.category}",{c.name}')
            lines.append(c.url)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

CSV_FIELDS = ["name", "delsys", "root", "category", "frequency", "polarity",
              "symbol_rate", "modulation", "sid", "video_pid", "audio_pids",
              "encrypted", "radio", "url"]


def parse_csv(text, root=""):
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        d = {k: v for k, v in row.items() if k in CSV_FIELDS}
        for k in ("frequency", "symbol_rate", "sid", "video_pid"):
            d[k] = int(d.get(k) or 0)
        d["audio_pids"] = [int(x) for x in (d.get("audio_pids") or "").split() if x.isdigit()]
        for k in ("encrypted", "radio"):
            d[k] = (d.get(k) or "").lower() in ("1", "true", "yes")
        d.setdefault("root", root)
        out.append(Channel.from_dict(d))
    return out


def write_csv(channels):
    buf = io.StringIO()
    w = csv.DictWriter(buf, CSV_FIELDS)
    w.writeheader()
    for c in channels:
        d = {k: getattr(c, k) for k in CSV_FIELDS}
        d["audio_pids"] = " ".join(map(str, c.audio_pids))
        w.writerow(d)
    return buf.getvalue()


# --------------------------------------------------------------------------
# DVBViewer channel lists (*.ini) for migration
# --------------------------------------------------------------------------

CABLE_QAM = {1: "qam-16", 2: "qam-32", 3: "qam-64", 4: "qam-128", 5: "qam-256"}


def dvbviewer_sat_flags(value):
    """Decode the satellite modulation flags used in DVBViewer lists.

    The low two bits carry the modulation (1 QPSK, 2 8PSK, 3 16APSK); any
    higher bit (roll-off / pilot) only exists for DVB-S2.
    """
    mod = {1: "qpsk", 2: "8psk", 3: "16apsk"}.get(value & 3, "auto")
    s2 = mod in ("8psk", "16apsk") or bool(value & 0xFC)
    return ("dvb-s2" if s2 else "dvb-s"), mod


def parse_dvbviewer_ini(text):
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read_string(text)
    out, seen = [], {}
    for sec in cp.sections():
        if not sec.lower().startswith("channel"):
            continue
        g = cp[sec]

        def num(k, d=0):
            try:
                return int(g.get(k, d))
            except ValueError:
                return d

        tuner = num("TunerType", -1)
        vpid, apid = num("VPID"), num("APID")
        c = Channel(name=g.get("Name", sec), root=g.get("Root", ""),
                    category=g.get("Category", ""), sid=num("SID"),
                    tsid=num("StreamID"), onid=num("NetworkID"),
                    pmt_pid=num("PMTPID"), pcr_pid=num("PCRPID"),
                    video_pid=vpid, audio_pids=[apid] if apid else [],
                    teletext_pid=num("TelePID"),
                    encrypted=bool(num("Encrypted") & 1), radio=vpid == 0)
        if tuner == 1:                                   # satellite
            c.delsys, c.modulation = dvbviewer_sat_flags(num("SatModulation"))
            c.frequency = num("Frequency") * 1000
            c.polarity = g.get("Polarity", "h").lower()[:1]
            c.symbol_rate = num("Symbolrate")
            # DVBViewer: 1/2 = simple A/B (tone burst), 3..6 = DiSEqC 1.0
            # ports 1..4 (PosA/OptA, PosB/OptA, PosA/OptB, PosB/OptB)
            c.diseqc = {1: 0, 2: 1, 3: 0, 4: 1, 5: 2, 6: 3}.get(num("DiseqC"), -1)
            sub = num("SubStreamID", -1)
            c.stream_id = sub if 0 <= sub < 256 else -1
        elif tuner == 0:                                 # cable
            c.delsys, c.frequency = "dvb-c-a", num("Frequency")
            c.symbol_rate = num("Symbolrate")
            c.modulation = CABLE_QAM.get(num("Polarity"), "auto")
        elif tuner == 2:                                 # terrestrial
            c.delsys, c.frequency = "dvb-t2", num("Frequency")
            c.bandwidth_hz = (num("Symbolrate") or 8) * 1000000
        elif tuner == 3:                                 # ATSC
            c.delsys, c.frequency, c.modulation = "atsc", num("Frequency"), "8vsb"
        elif g.get("URL"):                               # internet stream
            c.delsys, c.url = "iptv", g.get("URL")
        else:
            continue
        # DVBViewer stores extra audio tracks as separate entries of one service.
        prev = seen.get((c.root, c.key))
        if prev and not c.is_iptv:
            prev.audio_pids += [p for p in c.audio_pids if p not in prev.audio_pids]
            continue
        seen[(c.root, c.key)] = c
        out.append(c)
    return out


def decode_text(raw: bytes):
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def import_file(path):
    """Detect the format of *path* and return a list of channels."""
    with open(path, "rb") as f:
        return import_data(f.read(), str(path), source=str(path))


def download_name(url):
    """The file name a downloaded list is imported under: it picks the format
    and names the channel group (".../countries/de.m3u" → "de.m3u")."""
    parts = urllib.parse.urlsplit(url)
    name = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return name or parts.netloc or "IPTV"


def is_hls_playlist(text):
    """True for the playlist of one HLS stream (a master or media playlist),
    which is a single channel and not a channel list."""
    return "#EXT-X-STREAM-INF" in text or "#EXT-X-TARGETDURATION" in text or \
        "#EXT-X-MEDIA-SEQUENCE" in text


def import_data(raw, name, source=""):
    """Detect the format of *raw* (the bytes of a file or a download called
    *name*) and return a list of channels. *source* is the web address or
    the file path it came from (needed for a single HLS stream)."""
    text = decode_text(raw)
    low = name.lower()
    stem = re.sub(r"^\d{4}_", "", name.rsplit("/", 1)[-1].rsplit(".", 1)[0])
    if is_hls_playlist(text):
        if not source:
            raise ValueError("This is the playlist of a single stream; add its address instead.")
        url = source if "://" in source else "file://" + urllib.parse.quote(
            os.path.abspath(source))
        return [Channel(name=stem or "Stream", delsys="iptv", url=url, root=stem or "IPTV")]
    if low.endswith((".m3u", ".m3u8")) or text.lstrip().startswith("#EXTM3U"):
        return parse_m3u(text, root=stem)
    if low.endswith(".csv"):
        return parse_csv(text, root=stem)
    if re.search(r"^\[channel0\]", text, re.M | re.I) or re.search(r"^TunerType=", text, re.M):
        return parse_dvbviewer_ini(text)
    if text.startswith("eDVB services"):
        return parse_lamedb(text)
    if re.search(r"^\s*DELIVERY_SYSTEM\s*=", text, re.M):
        return parse_dvbv5(text, root=stem)
    return parse_vdr(text, root="")
