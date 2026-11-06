# SPDX-License-Identifier: GPL-2.0-or-later
"""Channel search on a single-cable (Unicable / JESS) tuner.

dvbv5-scan cannot drive these setups, so LinDVB tunes each transponder itself
(lindvb.directtune), reads PAT, SDT and the PMTs from the DVR device and
builds the channels from them.
"""

import errno
import os
import select
import time

from .ca import SectionAssembler, ts_pid
from .channels import Channel, SAT_SYSTEMS, parse_dvbv5
from .tsanalysis import parse_pat, parse_pmt_full, parse_sdt, section_header

PAT_PID, SDT_PID = 0x00, 0x11
RADIO_TYPES = {0x02, 0x07, 0x0A}
TS = 188


class TransponderScan:
    """Collects PAT, SDT (actual) and the PMTs of one transponder from TS packets."""

    def __init__(self):
        self.asm = {PAT_PID: SectionAssembler(), SDT_PID: SectionAssembler()}
        self.tsid = 0
        self.onid = 0
        self.pat = None              # {sid: pmt_pid}
        self.sdt = {}
        self._sdt_sections = {}      # section number -> parsed
        self._sdt_last = None
        self.pmts = {}               # sid -> parse_pmt_full

    def wanted_pids(self):
        pids = {PAT_PID, SDT_PID}
        if self.pat:
            pids |= {p for s, p in self.pat.items() if s}
        return pids

    def feed(self, data):
        """Feed TS bytes. Returns True when the PMT PIDs became known (filters change)."""
        new_pids = False
        for off in range(0, len(data) - TS + 1, TS):
            pkt = data[off:off + TS]
            if pkt[0] != 0x47:
                continue
            pid = ts_pid(pkt)
            asm = self.asm.get(pid)
            if asm is None:
                continue
            for sec in asm.feed(pkt):
                if sec and self._section(pid, sec):
                    new_pids = True
        return new_pids

    def _section(self, pid, sec):
        tid = sec[0]
        if pid == PAT_PID and tid == 0x00 and self.pat is None:
            self.tsid = section_header(sec)[1]
            self.pat = parse_pat(sec)
            for sid, pmt_pid in self.pat.items():
                if sid and pmt_pid not in self.asm:
                    self.asm[pmt_pid] = SectionAssembler()
            return True
        if pid == SDT_PID and tid == 0x42:
            _tid, ext, _v, num, last = section_header(sec)
            if len(sec) > 9:
                self.onid = (sec[8] << 8) | sec[9]
            self._sdt_sections[num] = parse_sdt(sec)
            self._sdt_last = last
            self.sdt = {}
            for part in self._sdt_sections.values():
                self.sdt.update(part)
        elif tid == 0x02 and self.pat:
            pmt = parse_pmt_full(sec)
            if self.pat.get(pmt["sid"]) == pid:
                self.pmts[pmt["sid"]] = pmt
        return False

    @property
    def complete(self):
        if self.pat is None or self._sdt_last is None:
            return False
        if len(self._sdt_sections) <= self._sdt_last:
            return False
        return all(sid in self.pmts for sid in self.pat if sid)

    def channels(self, tp, root="", diseqc=-1):
        """Channels of the services found, with the tuning data of transponder *tp*."""
        out = []
        for sid, pmt_pid in sorted(self.pat.items() if self.pat else ()):
            if not sid:
                continue
            info = self.sdt.get(sid, {})
            pmt = self.pmts.get(sid)
            c = Channel(info.get("name") or f"Service {sid}", delsys=tp.delsys, root=root,
                        category=info.get("provider", ""), frequency=tp.frequency,
                        polarity=tp.polarity, symbol_rate=tp.symbol_rate,
                        modulation=tp.modulation, fec=tp.fec, stream_id=tp.stream_id,
                        diseqc=diseqc, sid=sid, tsid=self.tsid, onid=self.onid,
                        pmt_pid=pmt_pid, encrypted=bool(info.get("scrambled")))
            if pmt:
                c.pcr_pid = pmt["pcr_pid"]
                c.encrypted = c.encrypted or bool(pmt["ca"])
                for pid, d in pmt["streams"]:
                    kind = d.get("kind")
                    if kind == "video" and not c.video_pid:
                        c.video_pid = pid
                    elif kind == "audio":
                        c.audio_pids.append(pid)
                    elif kind == "teletext" and not c.teletext_pid:
                        c.teletext_pid = pid
                    elif kind == "subtitle":
                        c.subtitle_pids.append(pid)
                    if d.get("ca"):
                        c.encrypted = True
            c.radio = info.get("type") in RADIO_TYPES or (pmt is not None and not c.video_pid
                                                         and bool(c.audio_pids))
            if pmt is None and not info:
                continue
            out.append(c)
        return out


class _Tp:
    """A transponder to search: the tuning fields of a Channel."""

    def __init__(self, delsys, frequency, polarity, symbol_rate, modulation="auto", fec="auto",
                 stream_id=-1):
        self.delsys, self.frequency, self.polarity = delsys, frequency, (polarity or "h").lower()
        self.symbol_rate, self.modulation, self.fec = symbol_rate, modulation, fec
        self.stream_id = stream_id
        self.diseqc = -1
        self.is_sat = delsys in SAT_SYSTEMS


def transponders(tplist):
    """The satellite transponders of a transponder list (lindvb.scan.TransponderList)."""
    out, seen = [], set()
    if tplist.transponders:
        src = [_Tp(t.delsys, t.frequency, t.polarity, t.symbol_rate, t.modulation, t.fec)
               for t in tplist.transponders]
    else:
        try:
            with open(tplist.path, encoding="utf-8", errors="replace") as f:
                chans = parse_dvbv5(f.read())
        except OSError:
            chans = []
        src = [_Tp(c.delsys, c.frequency, c.polarity, c.symbol_rate, c.modulation, c.fec,
                   c.stream_id) for c in chans]
    for t in src:
        key = (t.frequency // 1000, t.polarity, t.stream_id)
        if t.is_sat and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def scan(tuner, frontend_status, uc, lnb, tps, diseqc=-1, root="", log=print,
         cancelled=lambda: False, lock_wait=3.0, read_wait=8.0):
    """Search *tps* on the open lindvb.directtune.DirectTuner *tuner*.

    *frontend_status()* gives the frontend status dict (lindvb.festats).
    Returns the channels found.
    """
    found = []
    for i, tp in enumerate(tps, 1):
        if cancelled():
            break
        tp.diseqc = diseqc
        log(f"Scanning frequency {i}/{len(tps)}: {tp.frequency // 1000} MHz "
            f"{tp.polarity.upper()} {tp.symbol_rate} kBd")
        locked = False
        for attempt in range(2):
            try:
                tuner.tune(uc, tp, lnb)
            except (OSError, ValueError) as e:
                log(f"  tuning failed: {getattr(e, 'strerror', None) or e}")
                break
            end = time.monotonic() + lock_wait
            while time.monotonic() < end and not cancelled():
                if (frontend_status() or {}).get("lock"):
                    locked = True
                    break
                time.sleep(0.1)
            if locked:
                break
        if not locked:
            log("  no signal")
            continue
        ts = TransponderScan()
        tuner.set_pids(sorted(ts.wanted_pids()))
        end = time.monotonic() + read_wait
        while time.monotonic() < end and not ts.complete and not cancelled():
            try:
                ready, _, _ = select.select([tuner.dvr], [], [], 0.2)
                data = os.read(tuner.dvr, TS * 348) if ready else b""
            except OSError as e:
                if e.errno in (errno.EOVERFLOW, errno.EAGAIN, errno.EINTR):
                    continue
                raise
            if data and ts.feed(data):
                tuner.set_pids(sorted(ts.wanted_pids()))
        chans = ts.channels(tp, root, diseqc)
        log(f"  {len(chans)} services")
        found += chans
    tuner.set_pids([])
    return found
