#!/bin/bash
# Build a .deb of LinDVB for Ubuntu 24.04 LTS and newer (and Debian 13).
# LinDVB is pure Python; GTK 4, GStreamer and PyGObject come from the system
# packages named in Depends, so the package is architecture independent.
# Usage: packaging/build-deb.sh [output directory]   (default: build/)
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(realpath -m "${1:-$SRC/build}")"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$SRC/pyproject.toml")"
PKG="$OUT/deb/lindvb_${VERSION}_all"

rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/lib/python3/dist-packages" \
         "$PKG/usr/share/applications" "$PKG/usr/share/metainfo" \
         "$PKG/usr/share/icons/hicolor/scalable/apps" "$PKG/usr/share/doc/lindvb"

# the program
cp -r "$SRC/lindvb" "$PKG/usr/lib/python3/dist-packages/"
find "$PKG/usr/lib/python3/dist-packages" -name __pycache__ -prune -exec rm -rf {} +
find "$PKG/usr/lib/python3/dist-packages" -type f -exec chmod 644 {} +
cat > "$PKG/usr/bin/lindvb" <<'SH'
#!/usr/bin/python3
import sys
from lindvb.app import main
sys.exit(main())
SH
chmod 755 "$PKG/usr/bin/lindvb"

# desktop integration
install -m644 "$SRC/data/io.github.thommy87.LinDVB.desktop" "$PKG/usr/share/applications/"
install -m644 "$SRC/data/io.github.thommy87.LinDVB.metainfo.xml" "$PKG/usr/share/metainfo/"
install -m644 "$SRC/data/lindvb.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/"
for d in "$SRC"/data/icons/hicolor/*/apps; do
    size="$(basename "$(dirname "$d")")"
    install -Dm644 "$d/lindvb.png" "$PKG/usr/share/icons/hicolor/$size/apps/lindvb.png"
done

# documentation
install -m644 "$SRC/README.md" "$SRC/THIRD-PARTY-NOTICES.md" "$PKG/usr/share/doc/lindvb/"
cat > "$PKG/usr/share/doc/lindvb/copyright" <<COPY
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: LinDVB
Source: https://github.com/thommy87/LinDVB

Files: *
Copyright: 2026 thommy87
License: GPL-2.0-or-later
 On Debian systems the full text of the GNU General Public License version 2
 is in /usr/share/common-licenses/GPL-2. Third-party components and their
 licences are listed in /usr/share/doc/lindvb/THIRD-PARTY-NOTICES.md.
COPY

SIZE="$(du -sk "$PKG/usr" | cut -f1)"
cat > "$PKG/DEBIAN/control" <<CTRL
Package: lindvb
Version: $VERSION
Architecture: all
Maintainer: thommy87 <https://github.com/thommy87/LinDVB>
Installed-Size: $SIZE
Section: video
Priority: optional
Homepage: https://github.com/thommy87/LinDVB
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0,
 gir1.2-gstreamer-1.0, gir1.2-gst-plugins-base-1.0, gir1.2-gst-plugins-bad-1.0,
 gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad,
 gstreamer1.0-libav, gstreamer1.0-gl, dvb-tools
Recommends: dtv-scan-tables, gstreamer1.0-plugins-ugly, gstreamer1.0-gtk4,
 gstreamer1.0-pipewire | gstreamer1.0-pulseaudio, gir1.2-webkit-6.0, gir1.2-soup-3.0
Description: Watch and record DVB and internet TV
 LinDVB plays and records satellite, cable, terrestrial and internet TV using
 the Linux DVB API and GStreamer. It has timeshift, timer and series
 recordings, a programme guide, teletext, channel search and a channel editor.
 .
 HbbTV (red button) needs gir1.2-webkit-6.0; gstreamer1.0-gtk4 gives faster
 video output.
CTRL

# compile the Python files on install, remove them on removal
cat > "$PKG/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v py3compile >/dev/null 2>&1; then
    py3compile -p lindvb /usr/lib/python3/dist-packages/lindvb || true
fi
SH
cat > "$PKG/DEBIAN/prerm" <<'SH'
#!/bin/sh
set -e
find /usr/lib/python3/dist-packages/lindvb -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
SH
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm"

dpkg-deb --root-owner-group --build "$PKG" "$OUT/lindvb_${VERSION}_all.deb"
rm -rf "$OUT/deb"
echo "Built $OUT/lindvb_${VERSION}_all.deb"
