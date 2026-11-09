# SPDX-License-Identifier: GPL-2.0-or-later
"""Names of DVB cards and sticks as the maker sells them.

The kernel knows the chip of a frontend ("Montage M88DS3103"); the name of the
whole card ("TerraTec Cinergy S2 PCIe Dual", "Hauppauge WinTV-dualHD") comes
from the PCI subsystem or USB ids of the device and the system's id lists
(pci.ids and usb.ids of hwdata). A USB stick usually also tells its name
itself. Without any of these the chip name is used.
"""
import ctypes
import fcntl
import os
import re
from pathlib import Path

PCI_IDS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids")
USB_IDS = ("/usr/share/hwdata/usb.ids", "/usr/share/misc/usb.ids", "/usr/share/usb.ids")

# words that make a maker's name long without saying more
_CORPORATE = re.compile(
    r"\b(inc|corp|corporation|co|ltd|limited|gmbh|ag|bv|b\.v|llc|plc|sa|srl|kg|"
    r"electronic|electronics|technologies|technology|international|computer|systems|"
    r"semiconductor|holdings|group|company|works)\b\.?", re.I)


def short_vendor(name):
    """"TERRATEC Electronic GmbH" -> "TERRATEC", "Hauppauge computer works Inc." ->
    "Hauppauge"."""
    name = name.split(",")[0].split("(")[0]
    name = _CORPORATE.sub("", name)
    return re.sub(r"\s+", " ", name).strip(" .-") or name.strip()


def _join(vendor, product):
    vendor = short_vendor(vendor or "")
    product = (product or "").strip()
    if not vendor:
        return product
    if not product:
        return vendor
    first = vendor.split()[0].lower()
    if product.lower().startswith(first):
        return product
    return f"{vendor} {product}"


# -- id lists ---------------------------------------------------------------------

def _first(paths):
    for p in paths:
        if os.path.isfile(p):
            return p
    return None


def _clean(s):
    return s.strip().strip("[]").strip()


def lookup_pci(vendor, device, subvendor=None, subdevice=None, paths=PCI_IDS):
    """(vendor name, device name, subsystem name, subvendor name) from pci.ids."""
    path = _first(paths)
    if path is None:
        return None
    vendor, device = vendor.lower(), device.lower()
    sub = f"{subvendor.lower()} {subdevice.lower()}" if subvendor and subdevice else None
    vname = dname = sname = svname = None
    in_vendor = in_device = False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip() or line.startswith("#"):
                    continue
                if line.startswith("C "):         # device classes follow
                    break
                if not line.startswith("\t"):
                    vid, _, name = line.partition(" ")
                    vid = vid.lower()
                    in_vendor = vid == vendor
                    in_device = False
                    if in_vendor:
                        vname = _clean(name)
                    if subvendor and vid == subvendor.lower():
                        svname = _clean(name)
                    continue
                if not in_vendor:
                    continue
                if line.startswith("\t\t"):
                    if in_device and sub and line[2:].lower().startswith(sub):
                        sname = _clean(line[2 + len(sub):])
                    continue
                did, _, name = line[1:].partition(" ")
                in_device = did.lower() == device
                if in_device:
                    dname = _clean(name)
    except OSError:
        return None
    return vname, dname, sname, svname


def lookup_usb(vendor, product, paths=USB_IDS):
    """(vendor name, product name) from usb.ids."""
    path = _first(paths)
    if path is None:
        return None
    vendor, product = vendor.lower(), product.lower()
    vname = pname = None
    in_vendor = False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip() or line.startswith("#"):
                    continue
                if not line.startswith("\t"):
                    if in_vendor:
                        break
                    vid, _, name = line.partition(" ")
                    if not re.fullmatch(r"[0-9a-fA-F]{4}", vid):
                        break                   # the class lists after the vendors
                    in_vendor = vid.lower() == vendor
                    if in_vendor:
                        vname = _clean(name)
                    continue
                if in_vendor and not line.startswith("\t\t"):
                    pid, _, name = line[1:].partition(" ")
                    if pid.lower() == product:
                        pname = _clean(name)
    except OSError:
        return None
    return vname, pname


# -- sysfs --------------------------------------------------------------------------

def _read(path):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _hex(s):
    return s[2:] if s.lower().startswith("0x") else s


def sysfs_name(adapter, frontend=0, root="/sys", pci_ids=PCI_IDS, usb_ids=USB_IDS):
    """The card's name from sysfs and the id lists, "" if unknown."""
    dev = Path(root) / "class" / "dvb" / f"dvb{adapter}.frontend{frontend}" / "device"
    try:
        dev = dev.resolve(strict=True)
    except OSError:
        return ""
    root_dev = Path(root) / "devices"
    for d in (dev, *dev.parents):
        if d == root_dev or d == d.parent or not str(d).startswith(str(root_dev)):
            break
        if (d / "idVendor").exists():                       # a USB device
            vid, pid = _read(d / "idVendor"), _read(d / "idProduct")
            ids = lookup_usb(vid, pid, usb_ids) if vid and pid else None
            maker, product = _read(d / "manufacturer"), _read(d / "product")
            if ids and ids[1]:
                return _join(ids[0] or maker, ids[1])
            if product:
                return _join(maker or (ids[0] if ids else ""), product)
            return ""
        if (d / "subsystem_vendor").exists() and (d / "vendor").exists():   # PCI
            ids = lookup_pci(_hex(_read(d / "vendor")), _hex(_read(d / "device")),
                             _hex(_read(d / "subsystem_vendor")),
                             _hex(_read(d / "subsystem_device")), pci_ids)
            if ids and ids[2]:
                return _join(ids[3] or "", ids[2])
            return ""
    for cand in ("product", "model"):
        name = _read(dev / cand)
        if name:
            return name
    return ""


# -- the frontend's own name (FE_GET_INFO) -------------------------------------------

class FrontendInfo(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * 128), ("type", ctypes.c_uint32),
                ("frequency_min", ctypes.c_uint32), ("frequency_max", ctypes.c_uint32),
                ("frequency_stepsize", ctypes.c_uint32),
                ("frequency_tolerance", ctypes.c_uint32),
                ("symbol_rate_min", ctypes.c_uint32), ("symbol_rate_max", ctypes.c_uint32),
                ("symbol_rate_tolerance", ctypes.c_uint32), ("notifier_delay", ctypes.c_uint32),
                ("caps", ctypes.c_uint32)]


FE_GET_INFO = (2 << 30) | (ctypes.sizeof(FrontendInfo) << 16) | (ord("o") << 8) | 61
FE_QPSK = 0          # satellite: the frequencies are in kHz, else in Hz


def frontend_info(adapter, frontend=0):
    """{"chip", "fmin", "fmax"} (frequencies in MHz) or None."""
    info = FrontendInfo()
    try:
        fd = os.open(f"/dev/dvb/adapter{adapter}/frontend{frontend}", os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        fcntl.ioctl(fd, FE_GET_INFO, info)
    except OSError:
        return None
    finally:
        os.close(fd)
    div = 1000 if info.type == FE_QPSK else 1000000
    return {"chip": info.name.decode("utf-8", "replace").strip(),
            "fmin": info.frequency_min / div, "fmax": info.frequency_max / div}


def card_name(adapter, frontend=0, info=None, **kw):
    """The name to show for a tuner: the card, else the chip, else ""."""
    name = sysfs_name(adapter, frontend, **kw)
    if name:
        return name
    info = info if info is not None else frontend_info(adapter, frontend)
    return (info or {}).get("chip", "")
