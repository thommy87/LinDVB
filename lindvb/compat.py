# SPDX-License-Identifier: GPL-2.0-or-later
"""Start-up checks for the libraries LinDVB needs, with hints that fit the system.

``require_gi()`` runs before anything imports GTK: without GTK 4 (or with one
older than MIN_GTK) LinDVB cannot start, so it stops with a plain message
naming the package to install instead of a Python traceback.

``missing_plugins()`` runs after ``Gst.init()``: a missing GStreamer plugin
does not stop LinDVB, but live TV then fails in ways that are hard to read, so
the window says which package to install.
"""

import os
import sys
from pathlib import Path

MIN_GTK = (4, 10)        # Gtk.FileDialog, Gtk.AlertDialog, Gtk.UriLauncher

# Package names by distribution family: GTK 4 with PyGObject, then the
# GStreamer plugin sets (see PLUGINS).
PACKAGES = {
    "debian": {"gtk": "python3-gi gir1.2-gtk-4.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0",
               "base": "gstreamer1.0-plugins-base", "bad": "gstreamer1.0-plugins-bad",
               "libav": "gstreamer1.0-libav",
               "install": "sudo apt install"},
    "fedora": {"gtk": "python3-gobject gtk4 gstreamer1 gstreamer1-plugins-base",
               "base": "gstreamer1-plugins-base",
               "bad": "gstreamer1-plugins-bad-free gstreamer1-plugins-bad-free-extras",
               "libav": "gstreamer1-plugin-libav",
               "install": "sudo dnf install",
               "note": "H.264/HEVC need the full codecs from RPM Fusion (gstreamer1-plugin-libav)."},
    "arch": {"gtk": "python-gobject gtk4 gstreamer gst-plugins-base",
             "base": "gst-plugins-base", "bad": "gst-plugins-bad", "libav": "gst-libav",
             "install": "sudo pacman -S"},
    "suse": {"gtk": "python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Gst-1_0",
             "base": "gstreamer-plugins-base", "bad": "gstreamer-plugins-bad",
             "libav": "gstreamer-plugins-libav",
             "install": "sudo zypper install",
             "note": "H.264/HEVC need the full codecs from Packman (gstreamer-plugins-libav)."},
}

# Elements live TV and recording rely on -> plugin set they come in.
PLUGINS = {
    "playbin": "base",
    "tsdemux": "bad",
    "dvbsrc": "bad",
    "avdec_h264": "libav",
}


def bundled():
    """'Flatpak', 'Snap' or 'AppImage' when LinDVB brings its own libraries, else ''."""
    if os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").exists():
        return "Flatpak"
    if os.environ.get("SNAP"):
        return "Snap"
    if os.environ.get("APPIMAGE") or os.environ.get("APPDIR"):
        return "AppImage"
    return ""


def distro_family(os_release="/etc/os-release"):
    """'debian', 'fedora', 'arch', 'suse' or '' from /etc/os-release (ID and ID_LIKE)."""
    try:
        text = Path(os_release).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    ids = []
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in ("ID", "ID_LIKE"):
            ids += value.strip().strip('"').lower().split()
    for i in ids:
        if i in ("debian", "ubuntu"):
            return "debian"
        if i in ("fedora", "rhel", "centos"):
            return "fedora"
        if i in ("arch", "archlinux"):
            return "arch"
        if i.startswith("opensuse") or i in ("suse", "sles"):
            return "suse"
    return ""


def install_hint(groups, family=None):
    """A command line (and a note) that installs the plugin sets ``groups``."""
    family = distro_family() if family is None else family
    pk = PACKAGES.get(family)
    if not pk:
        return ""
    hint = pk["install"] + " " + " ".join(dict.fromkeys(pk[g] for g in groups))
    if "libav" in groups and pk.get("note"):
        hint += "\n" + pk["note"]
    return hint


def gtk_too_old(version, minimum=MIN_GTK):
    return tuple(version[:2]) < tuple(minimum)


def require_gi():
    """Load the GObject bindings for GTK 4 and GStreamer, or exit with a hint."""
    problem, not_installed = "", False
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gtk
    except (ImportError, ValueError) as e:
        problem = f"LinDVB needs GTK 4 and GStreamer with their Python bindings ({e})."
        not_installed = True
    else:
        version = (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version())
        if gtk_too_old(version):
            problem = ("LinDVB needs GTK {}.{} or newer; this system has GTK {}.{}.{}."
                       .format(*MIN_GTK, *version))
    if not problem:
        return
    where = bundled()
    if where:
        problem += f"\nThe {where} should bring these: please report this at https://github.com/thommy87/LinDVB/issues"
    else:
        pk = PACKAGES.get(distro_family())
        if pk and not_installed:
            problem += f"\nInstall them with:\n  {pk['install']} {pk['gtk']}"
        problem += ("\nOn older distributions use the LinDVB Flatpak, which runs everywhere:"
                    "\n  https://github.com/thommy87/LinDVB#install")
    sys.stderr.write(problem + "\n")
    sys.exit(1)


def missing_plugins(find=None):
    """GStreamer elements from PLUGINS that are not installed, by plugin set."""
    if find is None:
        from gi.repository import Gst
        find = Gst.ElementFactory.find
    missing = {}
    for element, group in PLUGINS.items():
        if find(element) is None:
            missing.setdefault(group, []).append(element)
    return missing


def plugin_report(missing, family=None, where=None):
    """(title, detail) for a dialog about ``missing`` (from missing_plugins()), or None."""
    if not missing:
        return None
    from .i18n import _
    elements = ", ".join(e for group in missing.values() for e in group)
    detail = _("LinDVB cannot find these GStreamer elements: {elements}. "
               "Live TV or some channels may not play.").format(elements=elements)
    where = bundled() if where is None else where
    if where:
        detail += "\n\n" + _("The {package} should include them. Please report this as a bug.").format(
            package=where)
    else:
        hint = install_hint(list(missing), family)
        if hint:
            detail += "\n\n" + _("Install them with:") + "\n" + hint
        else:
            detail += "\n\n" + _("Install the GStreamer plugins base, bad and libav from your distribution.")
    return _("GStreamer plugins are missing"), detail
