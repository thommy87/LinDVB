# SPDX-License-Identifier: GPL-2.0-or-later
"""Third-party credits shown in the About dialog.

The full list with licences is THIRD-PARTY-NOTICES.md in the source tree.
Section titles are marked with N_ so update-po.sh extracts them; the window
translates them when it shows the dialog.
"""

from .i18n import N_

CREDITS = (
    (N_("Contains code from"), (
        "VDR EEPG plugin (Dingo35, Dimitar Petrovski; mhwepg by Jean-Claude Repetto, "
        "Freesat patch by dom), GPL-2.0-or-later https://github.com/vdr-projects/vdr-plugin-eepg",
        "GOOM 2k4 (Jean-Christophe Hoelt and the GOOM authors), LGPL-2.1-or-later "
        "https://gstreamer.freedesktop.org",
        "dash.js 4.7.4 (DASH Industry Forum), BSD-3-Clause "
        "https://github.com/Dash-Industry-Forum/dash.js",
    )),
    (N_("Built with"), (
        "GTK https://www.gtk.org",
        "GStreamer https://gstreamer.freedesktop.org",
        "PyGObject https://pygobject.gnome.org",
        "WebKitGTK https://webkitgtk.org",
        "libzvbi https://github.com/zapping-vbi/zvbi",
        "v4l-utils / libdvbv5 https://git.linuxtv.org/v4l-utils.git",
        "dtv-scan-tables https://git.linuxtv.org/dtv-scan-tables.git",
        "libdvbcsa https://www.videolan.org/developers/libdvbcsa.html",
        "FFmpeg https://ffmpeg.org",
    )),
    (N_("Visualisations"), (
        "projectM and gst-projectm https://github.com/projectM-visualizer",
        "MilkDrop presets \"Cream of the Crop\", curated by ISOSCELES, "
        "and their authors https://github.com/projectM-visualizer/presets-cream-of-the-crop",
        "MilkDrop by Ryan Geiss and the MilkDrop texture pack "
        "https://github.com/projectM-visualizer/presets-milkdrop-texture-pack",
    )),
)
