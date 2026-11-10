# Third-party notices

LinDVB is Copyright (C) 2026 thommy87 and is free software under the GNU
General Public License version 2 or (at your option) any later version
([LICENSE](LICENSE)). It contains, uses and ships the work of other projects,
listed here with their licences. Thank you to all of their authors.

## Code contained in LinDVB

| Where | From | Licence |
|---|---|---|
| `lindvb/epgsources.py` (MediaHighway 1/2 and Freesat EPG) | The section layouts and the FF packet handling follow the [VDR EEPG plugin](https://github.com/vdr-projects/vdr-plugin-eepg) by Dingo35 and Dimitar Petrovski, which is based on mhwepg by Jean-Claude Repetto and the Freesat patch by dom (suborbital.org.uk) | GPL-2.0-or-later |
| `lindvb/freesat_tables.py` | The Freesat Huffman tables from the VDR EEPG plugin (`eepg.h`), from the Freesat patch by dom | GPL-2.0-or-later |
| `lindvb/goom.py`, the "Goom HD" shaders in `lindvb/ui/gpuvisuals.py` | A port of GOOM 2k4 as shipped in [gst-plugins-good](https://gstreamer.freedesktop.org) (`gst/goom`), Copyright (C) 2000-2004 Jean-Christophe Hoelt and the GOOM authors | LGPL-2.1-or-later, used here under GPL-2.0-or-later as LGPL section 3 allows |
| `lindvb/vendor/dash.mediaplayer.min.js` | [dash.js](https://github.com/Dash-Industry-Forum/dash.js) 4.7.4, Copyright (c) 2015 Dash Industry Forum, with es6-promise, buffer, ieee754, codem-isoboxer and fromcodepoint inside | BSD-3-Clause ([licence](lindvb/vendor/dash.js-LICENSE.md)); the bundled parts MIT and BSD-3-Clause ([notices](lindvb/vendor/dash.mediaplayer.min.js.LICENSE.txt)) |

Everything else is LinDVB's own code, written from the public standards
(ETSI EN 300 468, TS 102 796 HbbTV, OIPF DAE, TS 102 809, TS 103 770 DVB-I,
EN 50221, ATSC A/65 and others). The DVBAPI opcodes in `lindvb/ca.py` are the
protocol of OSCam's `module-dvbapi`; no OSCam code is included. The readers for
dvbv5, VDR, Enigma2 and `.ini` channel lists are written from the file
formats; no code or data of other programs is included. "VDR", "Enigma2" and
"OSCam" name the programs whose files LinDVB can read and
belong to their owners.

## Libraries LinDVB uses

| Project | Licence | How |
|---|---|---|
| [Python](https://www.python.org) | PSF-2.0 | interpreter |
| [GTK 4](https://www.gtk.org), GLib, Pango, gdk-pixbuf, graphene | LGPL-2.1-or-later | through PyGObject |
| [PyGObject](https://pygobject.gnome.org), [pycairo](https://pycairo.readthedocs.io), cairo | LGPL-2.1-or-later (pycairo and cairo also MPL-1.1) | Python modules |
| [GStreamer](https://gstreamer.freedesktop.org) and plugins base, good, bad | LGPL-2.1-or-later | playback, recording, tuning |
| GStreamer plugins ugly (a52dec, mpeg2dec), dtsdec (libdca), faad (faad2) | GPL-2.0-or-later | audio and video decoders |
| [FFmpeg](https://ffmpeg.org) through gst-libav | LGPL-2.1-or-later, or GPL-2.0-or-later as distributions build it | decoders |
| [WebKitGTK](https://webkitgtk.org) | LGPL-2.1 and BSD-2-Clause | HbbTV browser |
| [libzvbi](https://github.com/zapping-vbi/zvbi) | mostly LGPL-2.0-or-later, some parts GPL-2.0-or-later and BSD | teletext |
| [libdvbcsa](https://www.videolan.org/developers/libdvbcsa.html) | GPL-2.0-or-later | software descrambling (optional) |
| [v4l-utils](https://git.linuxtv.org/v4l-utils.git) (`dvbv5-scan`, `dvbv5-zap`, libdvbv5) | programs GPL-2.0, libdvbv5 LGPL-2.1 | channel search (separate programs) |
| [libprojectM](https://github.com/projectM-visualizer/projectm) 4 and [gst-projectm](https://github.com/projectM-visualizer/gst-projectm) | LGPL-2.1 | projectM visualisation (optional) |

## Data and artwork in LinDVB and the AppImage

| Content | From | Licence |
|---|---|---|
| MilkDrop presets "Cream of the Crop" | Curated by ISOSCELES from presets by many MilkDrop authors, as [packaged by projectM](https://github.com/projectM-visualizer/presets-cream-of-the-crop) | no licence stated; redistributed as projectM does |
| MilkDrop texture pack | The textures of MilkDrop (Ryan Geiss, Nullsoft) and others, as [packaged by projectM](https://github.com/projectM-visualizer/presets-milkdrop-texture-pack) | no licence stated; redistributed as projectM does |
| `lindvb/scantables/dtv-scan-tables.json.gz` (the channel search's transponder lists) | [dtv-scan-tables](https://git.linuxtv.org/dtv-scan-tables.git) by the LinuxTV community, commit 97129f50c9b0 (2025-06-06), packed into one file by `tools/update_scantables.py`; the lists of the system's dtv-scan-tables package are used besides them | GPL-2.0 and LGPL-2.1 ([GPL](lindvb/scantables/dtv-scan-tables-COPYING), [LGPL](lindvb/scantables/dtv-scan-tables-COPYING.LGPL)) |
| Adwaita and hicolor icon themes | [GNOME](https://gitlab.gnome.org/GNOME/adwaita-icon-theme) | LGPL-3.0-or-later or CC-BY-SA-3.0 |

LinDVB's own icon, screenshots, test media and translations were made for
LinDVB and are under its licence. The AppStream metadata is CC0-1.0.

## Packages

The **AppImage** contains the libraries above from Ubuntu 24.04 and projectM
built from source. Their licence and copyright files are in
`usr/share/doc/<package>/` inside the AppImage, and
`usr/share/doc/LinDVB/PACKAGES.txt` lists every bundled Ubuntu package with
its source package and version. The source code of those packages is
available from Ubuntu (`apt-get source <package>=<version>`, or
<https://launchpad.net/ubuntu/+source/>), the projectM sources from the
commits pinned in `packaging/build-projectm.sh`. If you cannot get a source
this way, ask in the issue tracker and it will be provided for three years
after the release that contained it.

The **Snap** uses packages of the Ubuntu archive, the **Flatpak** the GNOME
runtime from Flathub plus libzvbi, GStreamer, v4l-utils and projectM (with the
presets and textures above) built from the
tagged sources in `flatpak/io.github.thommy87.LinDVB.yml`.
