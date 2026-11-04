# SPDX-License-Identifier: GPL-2.0-or-later
"""Transponder lists and channel search via dvbv5-scan (libdvbv5 / v4l-utils)."""

import configparser
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .channels import DVBV5_DELSYS_REV, DVBV5_MOD_REV, CABLE_QAM, decode_text, dvbviewer_sat_flags

FEC_CODES = {"12": "1/2", "23": "2/3", "34": "3/4", "35": "3/5", "45": "4/5",
             "56": "5/6", "78": "7/8", "89": "8/9", "910": "9/10"}

# Standard locations of the dtv-scan-tables package on common distributions.
SCAN_TABLE_DIRS = ["/usr/share/dvb", "/usr/share/dvbv5", "/usr/share/dvb-scan-tables",
                   "/usr/local/share/dvb"]
# lists that ship with LinDVB (the kernel's virtual tuner vidtv, for testing)
BUNDLED_TABLES = Path(__file__).with_name("scantables")


@dataclass
class Transponder:
    delsys: str
    frequency: int           # kHz
    polarity: str = ""
    symbol_rate: int = 0     # kBd
    modulation: str = "auto"
    fec: str = "auto"
    bandwidth_hz: int = 0
    label: str = ""


@dataclass
class TransponderList:
    name: str
    kind: str                # "sat", "cable", "terrestrial", "atsc", "atsc-cable"
    transponders: list
    path: str = ""


def parse_transponder_ini(path):
    """Read a DVBViewer style transponder list ([SATTYPE] + [DVB] sections)."""
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.read_string(decode_text(Path(path).read_bytes()))
    name = cp.get("SATTYPE", "2", fallback=Path(path).stem)
    pos = int(cp.get("SATTYPE", "1", fallback="0") or 0)
    stem = Path(path).stem.upper()
    if stem.startswith("DVB-S") or pos < 3600:
        kind = "sat"
    elif stem.startswith("DVB-C"):
        kind = "cable"
    elif stem.startswith("ATSC"):
        # US cable (ATSC_America_Cable.ini) is QAM annex B, not 8VSB
        kind = "atsc-cable" if "CABLE" in stem else "atsc"
    else:
        kind = "terrestrial"
    # terrestrial lists of other TV systems: DVBViewer names them ISDB.ini, DTMB.ini
    if stem.startswith("ISDB") or "ISDB" in name.upper() or "ISBD" in name.upper():
        terr = "isdb-t"
    elif stem.startswith("DTMB") or "DTMB" in name.upper():
        terr = "dtmb"
    elif "T2" in stem:
        terr = "dvb-t2"
    else:
        terr = "dvb-t"
    tps = []
    for k, v in cp.items("DVB") if cp.has_section("DVB") else []:
        if k == "0":
            continue
        data, _, label = v.partition("|")
        f = [x.strip() for x in data.split(",") if x.strip()]
        if not f or not f[0].isdigit():
            continue
        freq = int(f[0])
        if kind == "sat" and len(f) >= 3:
            flags = int(f[4]) if len(f) > 4 and f[4].isdigit() else 1
            delsys, mod = dvbviewer_sat_flags(flags)
            tps.append(Transponder(delsys, freq, f[1][:1].lower(), int(f[2]), mod,
                                   FEC_CODES.get(f[3], "auto") if len(f) > 3 else "auto",
                                   label=label))
        elif kind == "cable" and len(f) >= 3:
            tps.append(Transponder("dvb-c-a", freq, "", int(f[2]),
                                   CABLE_QAM.get(int(f[1]), "auto"), label=label))
        elif kind == "terrestrial":
            bw = int(f[1]) if len(f) > 1 and f[1].isdigit() else 8
            tps.append(Transponder(terr, freq, bandwidth_hz=bw * 1000000, label=label))
        elif kind == "atsc":
            tps.append(Transponder("atsc", freq, modulation="8vsb", label=label))
        elif kind == "atsc-cable":
            tps.append(Transponder("dvb-c-b", freq, modulation="qam-256", label=label))
    return TransponderList(name, kind, tps, str(path))


def write_scan_file(tplist, diseqc=-1):
    """Write an initial tuning file in dvbv5 format for dvbv5-scan."""
    out = []
    for t in tplist.transponders:
        out.append("[CHANNEL]")
        out.append(f"\tDELIVERY_SYSTEM = {DVBV5_DELSYS_REV.get(t.delsys, 'DVBT')}")
        if tplist.kind == "sat":
            out.append(f"\tFREQUENCY = {t.frequency}")
            out.append(f"\tPOLARIZATION = {'HORIZONTAL' if t.polarity == 'h' else 'VERTICAL'}")
        else:
            out.append(f"\tFREQUENCY = {t.frequency * 1000}")
        if t.symbol_rate:
            out.append(f"\tSYMBOL_RATE = {t.symbol_rate * 1000}")
        if t.bandwidth_hz:
            out.append(f"\tBANDWIDTH_HZ = {t.bandwidth_hz}")
        if t.modulation in DVBV5_MOD_REV:
            out.append(f"\tMODULATION = {DVBV5_MOD_REV[t.modulation]}")
        out.append(f"\tINNER_FEC = {t.fec.upper() if t.fec != 'auto' else 'AUTO'}")
        if diseqc >= 0:
            out.append(f"\tSAT_NUMBER = {diseqc}")
        out.append("")
    return "\n".join(out) + "\n"


def find_transponder_lists(extra_dirs=()):
    """Return (name, path) of transponder lists found on this system."""
    found = []
    for d in [*extra_dirs, *SCAN_TABLE_DIRS, BUNDLED_TABLES]:
        p = Path(d).expanduser()
        if not p.is_dir():
            continue
        for f in sorted(p.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                found.append((f"{f.parent.name}/{f.name}", str(f)))
    return found


def load_transponder_list(path):
    text = decode_text(Path(path).read_bytes())
    if "[SATTYPE]" in text:
        return parse_transponder_ini(path)
    # dtv-scan-tables file: already in dvbv5 or legacy format, dvbv5-scan reads it as-is
    kind = "sat" if re.search(r"DVBS|^S ", text, re.M) else "terrestrial"
    return TransponderList(Path(path).name, kind, [], str(path))


def scan_command(adapter, frontend, scan_file, out_file, lnb="UNIVERSAL",
                 diseqc=-1, nit=False):
    tool = shutil.which("dvbv5-scan")
    if not tool:
        raise FileNotFoundError("dvbv5-scan not found. Install v4l-utils / dvb-tools.")
    cmd = [tool, "-a", str(adapter), "-f", str(frontend), "-O", "DVBV5", "-o", out_file]
    if lnb:
        cmd += ["-l", lnb]
    if diseqc >= 0:
        cmd += ["-S", str(diseqc)]
    if nit:
        cmd += ["-N"]
    cmd.append(scan_file)
    return cmd


def find_adapters():
    """List DVB adapters as (adapter, frontend, name) using sysfs/devfs."""
    res = []
    for fe in sorted(Path("/dev/dvb").glob("adapter*/frontend*")):
        a = int(fe.parent.name[7:])
        f = int(fe.name[8:])
        name = ""
        sysname = Path(f"/sys/class/dvb/dvb{a}.frontend{f}/device")
        for cand in ("product", "model"):
            try:
                name = (sysname / cand).read_text().strip()
                break
            except OSError:
                pass
        if not name and is_virtual_adapter(a, f):
            name = "Virtual tuner (vidtv)"
        res.append((a, f, name or f"Adapter {a}"))
    return res


def is_virtual_adapter(adapter, frontend=0):
    """True for the kernel's virtual DVB driver vidtv (a test tuner, no hardware)."""
    dev = Path(f"/sys/class/dvb/dvb{adapter}.frontend{frontend}/device")
    for link in (dev / "driver", dev):
        try:
            if "vidtv" in link.resolve().name:
                return True
        except OSError:
            pass
    try:
        return "vidtv" in (dev / "modalias").read_text()
    except OSError:
        return False
