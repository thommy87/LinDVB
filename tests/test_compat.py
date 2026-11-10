# SPDX-License-Identifier: GPL-2.0-or-later
"""Start-up checks: distribution detection and the hints for missing libraries."""

from lindvb import compat


def write(tmp_path, text):
    p = tmp_path / "os-release"
    p.write_text(text)
    return str(p)


def test_distro_family(tmp_path):
    cases = {
        'ID=ubuntu\nID_LIKE=debian\n': "debian",
        'ID=linuxmint\nID_LIKE="ubuntu debian"\n': "debian",
        'ID=fedora\n': "fedora",
        'ID="almalinux"\nID_LIKE="rhel centos fedora"\n': "fedora",
        'ID=cachyos\nID_LIKE=arch\n': "arch",
        'ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n': "suse",
        'ID=nixos\n': "",
    }
    for text, family in cases.items():
        assert compat.distro_family(write(tmp_path, text)) == family, text
    assert compat.distro_family(str(tmp_path / "missing")) == ""


def test_gtk_too_old():
    assert compat.gtk_too_old((4, 8, 3))
    assert not compat.gtk_too_old((4, 10, 0))
    assert not compat.gtk_too_old((4, 18, 1))


def test_missing_plugins_grouped():
    present = {"playbin"}
    missing = compat.missing_plugins(lambda name: object() if name in present else None)
    assert missing == {"bad": ["tsdemux", "dvbsrc"], "libav": ["avdec_h264"]}
    assert compat.missing_plugins(lambda name: object()) == {}


def test_install_hint():
    hint = compat.install_hint(["bad", "libav"], "debian")
    assert hint == "sudo apt install gstreamer1.0-plugins-bad gstreamer1.0-libav"
    assert "RPM Fusion" in compat.install_hint(["libav"], "fedora")
    assert "RPM Fusion" not in compat.install_hint(["bad"], "fedora")
    assert compat.install_hint(["bad"], "") == ""


def test_plugin_report():
    assert compat.plugin_report({}) is None
    title, detail = compat.plugin_report({"bad": ["dvbsrc"]}, family="arch", where="")
    assert "dvbsrc" in detail and "sudo pacman -S gst-plugins-bad" in detail
    title, detail = compat.plugin_report({"bad": ["dvbsrc"]}, family="arch", where="Flatpak")
    assert "Flatpak" in detail and "pacman" not in detail
