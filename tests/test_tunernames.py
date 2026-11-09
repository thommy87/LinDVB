# SPDX-License-Identifier: GPL-2.0-or-later
import os

from lindvb import tunernames as tn

PCI = """# pci.ids
14f1  Conexant Systems, Inc.
\t8852  CX23885 PCI Video and Audio Decoder
\t\t0070 7911  WinTV-HVR1250
\t\t153b 117e  Cinergy T PCIe Dual
153b  TERRATEC Electronic GmbH
0070  Hauppauge computer works Inc.
C 04  Multimedia controller
"""
USB = """2040  Hauppauge
\t0264  WinTV-soloHD
2013  PCTV Systems
\t025f  PCTV 292e
C 00  (Defined at Interface level)
"""


def _sysfs(tmp, adapter, attrs, usb=False):
    dev = tmp / "devices" / "pci0000:00" / "0000:00:1c.0" / f"dev{adapter}"
    leaf = dev / "1-1:1.0" if usb else dev
    leaf.mkdir(parents=True)
    for k, v in attrs.items():
        (dev / k).write_text(v + "\n")
    cls = tmp / "class" / "dvb" / f"dvb{adapter}.frontend0"
    cls.mkdir(parents=True)
    os.symlink(leaf, cls / "device")


def _ids(tmp):
    (tmp / "pci.ids").write_text(PCI)
    (tmp / "usb.ids").write_text(USB)
    return {"pci_ids": (str(tmp / "pci.ids"),), "usb_ids": (str(tmp / "usb.ids"),)}


def test_short_vendor():
    assert tn.short_vendor("TERRATEC Electronic GmbH") == "TERRATEC"
    assert tn.short_vendor("Conexant Systems, Inc.") == "Conexant"
    assert tn.short_vendor("PCTV Systems") == "PCTV"


def test_pci_card_name(tmp_path):
    kw = _ids(tmp_path)
    _sysfs(tmp_path, 0, {"vendor": "0x14f1", "device": "0x8852",
                         "subsystem_vendor": "0x153b", "subsystem_device": "0x117e"})
    _sysfs(tmp_path, 1, {"vendor": "0x14f1", "device": "0x8852",
                         "subsystem_vendor": "0x0070", "subsystem_device": "0x7911"})
    assert tn.sysfs_name(0, root=str(tmp_path), **kw) == "TERRATEC Cinergy T PCIe Dual"
    assert tn.sysfs_name(1, root=str(tmp_path), **kw) == "Hauppauge WinTV-HVR1250"


def test_usb_card_name(tmp_path):
    kw = _ids(tmp_path)
    _sysfs(tmp_path, 0, {"idVendor": "2040", "idProduct": "0264", "manufacturer": "HCW",
                         "product": "soloHD"}, usb=True)
    _sysfs(tmp_path, 1, {"idVendor": "2013", "idProduct": "025f"}, usb=True)
    _sysfs(tmp_path, 2, {"idVendor": "abcd", "idProduct": "0001", "manufacturer": "Acme",
                         "product": "Acme DVB-T2 Stick"}, usb=True)
    assert tn.sysfs_name(0, root=str(tmp_path), **kw) == "Hauppauge WinTV-soloHD"
    assert tn.sysfs_name(1, root=str(tmp_path), **kw) == "PCTV 292e"
    assert tn.sysfs_name(2, root=str(tmp_path), **kw) == "Acme DVB-T2 Stick"


def test_falls_back_to_the_chip(tmp_path):
    kw = _ids(tmp_path)
    _sysfs(tmp_path, 0, {"vendor": "0x14f1", "device": "0x8852",
                         "subsystem_vendor": "0x1234", "subsystem_device": "0x5678"})
    assert tn.sysfs_name(0, root=str(tmp_path), **kw) == ""
    assert tn.card_name(0, root=str(tmp_path), info={"chip": "Montage M88DS3103"},
                        **kw) == "Montage M88DS3103"
    assert tn.card_name(9, root=str(tmp_path), info={}, **kw) == ""


def test_reception_type_of_combined_tuners():
    from lindvb import tunerprofiles as tp
    combo = tp.new_profile(0, 0, "Combo", ["dvb-c-a", "dvb-t", "dvb-t2"])
    assert tp.families(combo) == ["ter", "cab"]
    assert tp.supports(combo, "dvb-t2") and tp.supports(combo, "dvb-c")
    combo["use"] = "cab"
    assert tp.supports(combo, "dvb-c") and not tp.supports(combo, "dvb-t2")
    sat = tp.new_profile(1, 0, "Sat", ["dvb-s", "dvb-s2"])
    sat["use"] = "cab"                     # ignored: the tuner has one type only
    assert tp.supports(sat, "dvb-s2")
    assert tp.display_name(sat) == "Sat"
    sat["label"] = "Wohnzimmer"
    assert tp.display_name(sat) == "Wohnzimmer"
