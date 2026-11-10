#!/bin/bash
# Assemble an AppDir for LinDVB from the packages of the build system
# (meant for Ubuntu 24.04; the AppImage then runs on distributions with
# glibc 2.39 or newer). Usage: packaging/build-appdir.sh [AppDir]
set -euo pipefail
[ -n "${CI:-}" ] && set -x

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APPDIR="$(realpath -m "${1:-$SRC/build/LinDVB.AppDir}")"
PY="${PY:-python3}"
PYVER="$($PY -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
ARCH_LIB="/usr/lib/$(gcc -dumpmachine 2>/dev/null || echo x86_64-linux-gnu)"
GSTVER=1.0

rm -rf "$APPDIR"
mkdir -p "$APPDIR"/usr/{bin,lib,share}

echo "== Python $PYVER"
cp -L "$(command -v python$PYVER)" "$APPDIR/usr/bin/python3"
mkdir -p "$APPDIR/usr/lib/python$PYVER"
tar -C "/usr/lib/python$PYVER" \
    --exclude=test --exclude=tests --exclude=idlelib --exclude=tkinter --exclude=turtledemo \
    --exclude=ensurepip --exclude=lib2to3 --exclude=__pycache__ --exclude='config-*' \
    -cf - . | tar -C "$APPDIR/usr/lib/python$PYVER" -xf -
SITE="$APPDIR/usr/lib/python$PYVER/site-packages"
mkdir -p "$SITE"
for mod in gi cairo; do
    for base in /usr/lib/python3/dist-packages /usr/lib/python$PYVER/dist-packages; do
        if [ -d "$base/$mod" ]; then cp -r "$base/$mod" "$SITE/"; break; fi
    done
done
cp -r "$SRC/lindvb" "$SITE/"
find "$SITE" -name __pycache__ -prune -exec rm -rf {} +

echo "== GObject introspection typelibs"
mkdir -p "$APPDIR/usr/lib/girepository-1.0"
for t in GLib-2.0 GObject-2.0 Gio-2.0 GModule-2.0 Gtk-4.0 Gdk-4.0 Gsk-4.0 Graphene-1.0 \
         Pango-1.0 PangoCairo-1.0 cairo-1.0 HarfBuzz-0.0 freetype2-2.0 GdkPixbuf-2.0 \
         Gst-1.0 GstBase-1.0 GstVideo-1.0 GstGL-1.0 GstAudio-1.0 GstTag-1.0 GstMpegts-1.0 GstPbutils-1.0 \
         PangoFT2-1.0 Fontconfig-2.0 GioUnix-2.0 GLibUnix-2.0 \
         WebKit-6.0 JavaScriptCore-6.0 Soup-3.0; do
    if [ -f "$ARCH_LIB/girepository-1.0/$t.typelib" ]; then
        cp "$ARCH_LIB/girepository-1.0/$t.typelib" "$APPDIR/usr/lib/girepository-1.0/"
    fi
done

echo "== GStreamer plugins"
mkdir -p "$APPDIR/usr/lib/gstreamer-$GSTVER"
# Only the plugins LinDVB uses: tuning, demuxing, decoding, network sources, output.
PLUGINS="coreelements playback typefindfunctions app autodetect audioconvert audioresample
  volume audiorate videoconvertscale videoconvert videoscale videorate videofilter deinterlace
  equalizer spectrum goom pbtypes gio subparse subenc audiofx audiomixer interleave pango overlaycomposition dvb mpegtsdemux mpegtsmux teletext
  dvbsuboverlay dvdspu libav audioparsers videoparsersbad mpegpsdemux mpeg2dec mpg123 a52dec
  dtsdec faad opus vorbis ogg flac isomp4 matroska id3demux icydemux soup hls dash
  adaptivedemux2 udp rtp rtpmanager rtpmanagerbad rtsp tcp multifile png alsa pulseaudio
  pipewire gtk4 opengl ximagesink
  va nvcodec vulkan v4l2codecs video4linux2"
# va, nvcodec, vulkan, v4l2codecs, video4linux2: hardware video decoding
# (lindvb/hwaccel.py). They load the host's libva, CUDA/NVDEC, Vulkan and V4L2
# drivers, so those libraries are left out below.
for p in $PLUGINS; do
    f="$ARCH_LIB/gstreamer-$GSTVER/libgst$p.so"
    if [ -f "$f" ]; then cp "$f" "$APPDIR/usr/lib/gstreamer-$GSTVER/"; fi
done
SCANNER="$(ls "$ARCH_LIB"/gstreamer$GSTVER/gstreamer-$GSTVER/gst-plugin-scanner 2>/dev/null || true)"
[ -n "$SCANNER" ] && cp "$SCANNER" "$APPDIR/usr/lib/gst-plugin-scanner"

echo "== WebKitGTK (HbbTV)"
# The web, network and GPU processes and the injected bundle. libwebkitgtk
# finds them at a path compiled in; it is rewritten below to one relative to
# AppRun's working directory (usr/lib), which WebKit's sandbox also sees
# because it is on LD_LIBRARY_PATH.
WEBKIT_DIR="$ARCH_LIB/webkitgtk-6.0"
if [ -d "$WEBKIT_DIR" ] && [ -f "$ARCH_LIB/girepository-1.0/WebKit-6.0.typelib" ]; then
    mkdir -p "$APPDIR/usr/lib/$(basename "$ARCH_LIB")"
    cp -a "$WEBKIT_DIR" "$APPDIR/usr/lib/$(basename "$ARCH_LIB")/"
    rm -f "$APPDIR/usr/lib/$(basename "$ARCH_LIB")/webkitgtk-6.0/MiniBrowser"
    cp -L "$ARCH_LIB"/libwebkitgtk-6.0.so.4 "$ARCH_LIB"/libjavascriptcoregtk-6.0.so.1 "$APPDIR/usr/lib/"
    HAVE_WEBKIT=1
else
    echo "== WebKitGTK 6 not installed (gir1.2-webkit-6.0): the AppImage will have no HbbTV"
    HAVE_WEBKIT=0
fi

echo "== GTK runtime data"
mkdir -p "$APPDIR/usr/share/glib-2.0/schemas"
cp /usr/share/glib-2.0/schemas/org.gtk.gtk4.*.xml "$APPDIR/usr/share/glib-2.0/schemas/" 2>/dev/null || true
glib-compile-schemas "$APPDIR/usr/share/glib-2.0/schemas"
mkdir -p "$APPDIR/usr/share/icons"
cp -r /usr/share/icons/Adwaita "$APPDIR/usr/share/icons/"
cp -r /usr/share/icons/hicolor "$APPDIR/usr/share/icons/" 2>/dev/null || true
PIXBUF_DIR="$(ls -d "$ARCH_LIB"/gdk-pixbuf-2.0/2.10.0 2>/dev/null || true)"
if [ -n "$PIXBUF_DIR" ]; then
    mkdir -p "$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders"
    cp "$PIXBUF_DIR"/loaders/*.so "$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders/"
fi
GIO_MOD="$ARCH_LIB/gio/modules"
mkdir -p "$APPDIR/usr/lib/gio/modules"
cp "$GIO_MOD"/libgiognutls.so "$APPDIR/usr/lib/gio/modules/" 2>/dev/null || true

echo "== dvbv5 tools"
for tool in dvbv5-scan dvbv5-zap; do
    if command -v $tool >/dev/null; then cp "$(command -v $tool)" "$APPDIR/usr/bin/"; fi
done
for d in /usr/share/dvb /usr/share/dvbv5; do
    if [ -d "$d" ]; then cp -r "$d" "$APPDIR/usr/share/"; fi
done

echo "== shared libraries"
# Libraries that must come from the host system (C library, graphics drivers, audio servers).
EXCLUDE='^(libc|libm|libdl|libpthread|librt|libresolv|libutil|libnsl|ld-linux|libGL|libGLX|libGLdispatch|libEGL|libOpenGL|libgbm|libdrm|libva|libva-drm|libva-x11|libva-wayland|libvulkan|libcuda|libnvcuvid|libstdc\+\+|libgcc_s|libasound|libpulse|libjack|libpipewire|libudev|libsystemd|libdbus-1|libX11-xcb|libxcb|libX11|libwayland-client|libwayland-egl|libwayland-cursor|libexpat|libfontconfig|libfreetype|libz)\.so'
collect() {
    find "$APPDIR" -type f \( -name '*.so*' -o -path '*/usr/bin/*' -o -name gst-plugin-scanner \
                           -o -path '*/webkitgtk-6.0/WebKit*' \) |
        while read -r f; do ldd "$f" 2>/dev/null || true; done |
        awk '/=> \//{print $3}' | sort -u
}
for _ in 1 2 3 4; do
    collect | while read -r lib; do
        name="$(basename "$lib")"
        if [[ "$name" =~ $EXCLUDE ]] || [ -e "$APPDIR/usr/lib/$name" ]; then continue; fi
        cp -L "$lib" "$APPDIR/usr/lib/$name"
    done
done
# Python extension modules and GObject typelibs reference libraries by soname.
for t in "$ARCH_LIB"/libgtk-4.so.1 "$ARCH_LIB"/libgstmpegts-1.0.so.0 "$ARCH_LIB"/libgstvideo-1.0.so.0 \
         "$ARCH_LIB"/libgstpbutils-1.0.so.0 "$ARCH_LIB"/libgraphene-1.0.so.0 "$ARCH_LIB"/libgirepository-1.0.so.1; do
    if [ -e "$t" ] && [ ! -e "$APPDIR/usr/lib/$(basename "$t")" ]; then cp -L "$t" "$APPDIR/usr/lib/"; fi
done

if [ "$HAVE_WEBKIT" = 1 ]; then
    # "/usr/lib/<triplet>/webkitgtk-6.0" -> "././././/<triplet>/webkitgtk-6.0": the same
    # length, so every other string in the library stays where it is
    "$PY" - "$APPDIR/usr/lib/libwebkitgtk-6.0.so.4" "$ARCH_LIB/webkitgtk-6.0" <<'PYEOF'
import sys
path, old = sys.argv[1], sys.argv[2].encode()
assert old.startswith(b"/usr/lib/")
new = b"././././/" + old[len(b"/usr/lib/"):]
assert len(new) == len(old)
data = open(path, "rb").read()
n = data.count(old)
if not n:
    sys.exit("WebKit's process path not found in " + path)
open(path, "wb").write(data.replace(old, new))
print(f"patched {n} occurrence(s) of {old.decode()}")
PYEOF
fi

echo "== licences of the bundled packages"
# The licence and copyright files of every Ubuntu package the AppImage takes
# files from, and a list of those packages with their source versions, so the
# corresponding sources can be found (see THIRD-PARTY-NOTICES.md).
DOC="$APPDIR/usr/share/doc"
mkdir -p "$DOC/LinDVB"
cp "$SRC/LICENSE" "$SRC/THIRD-PARTY-NOTICES.md" "$DOC/LinDVB/"
if command -v dpkg-query >/dev/null; then
    PKGS="$( { find "$APPDIR/usr" -type f \( -name '*.so*' -o -path '*/usr/bin/*' \
                    -o -path '*/webkitgtk-6.0/*' -o -name gst-plugin-scanner \) -printf '*/%f\n' |
                 sort -u | xargs -r dpkg -S 2>/dev/null | grep -v '^diversion' |
                 cut -d: -f1 | tr ',' '\n'
               echo "python$PYVER python3-gi python3-gi-cairo python3-cairo dtv-scan-tables"
               echo "adwaita-icon-theme hicolor-icon-theme"; } | tr ' ' '\n' | sed '/^$/d' | sort -u || true)"
    : > "$DOC/LinDVB/PACKAGES.txt"
    for pkg in $PKGS; do
        dpkg-query -W "$pkg" >/dev/null 2>&1 || continue
        dpkg-query -W -f='${Package} ${Version} (source: ${source:Package} ${source:Version})\n' "$pkg" \
            >> "$DOC/LinDVB/PACKAGES.txt"
        if [ -e "/usr/share/doc/$pkg/copyright" ]; then
            mkdir -p "$DOC/$pkg"
            cp -L "/usr/share/doc/$pkg/copyright" "$DOC/$pkg/copyright"
        fi
    done
    echo "licences of $(wc -l < "$DOC/LinDVB/PACKAGES.txt") packages"
fi

echo "== desktop integration"
cp "$SRC/data/io.github.thommy87.LinDVB.desktop" "$APPDIR/"
sed -i 's/^Icon=.*/Icon=lindvb/' "$APPDIR/io.github.thommy87.LinDVB.desktop"
cp "$SRC/data/lindvb.png" "$APPDIR/lindvb.png"
ln -sf lindvb.png "$APPDIR/.DirIcon"
cp -r "$SRC/data/icons/hicolor/." "$APPDIR/usr/share/icons/hicolor/"
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp "$SRC/data/lindvb.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/lindvb.svg"
mkdir -p "$APPDIR/usr/share/metainfo"
cp "$SRC/data/io.github.thommy87.LinDVB.metainfo.xml" "$APPDIR/usr/share/metainfo/"
cp "$SRC/LICENSE" "$APPDIR/usr/share/LICENSE.LinDVB"

# The oldest C library the bundled programs and libraries run on: the build system's.
MIN_GLIBC="$(getconf GNU_LIBC_VERSION | awk '{print $2}')"
[ -n "$MIN_GLIBC" ]
cat > "$APPDIR/AppRun" <<EOF
#!/bin/bash
HERE="\$(dirname "\$(readlink -f "\$0")")"
# Built on glibc $MIN_GLIBC: on an older (or a non-glibc) system the bundled
# libraries fail with a loader error, so say so plainly and point to Flathub.
MIN_GLIBC=$MIN_GLIBC
HOST_GLIBC="\$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print \$2}')"
[ -n "\$HOST_GLIBC" ] || HOST_GLIBC="\$(ldd --version 2>&1 | head -n1 | grep -oE '[0-9]+\\.[0-9]+\$')"
if [ -z "\$HOST_GLIBC" ] || [ "\$(printf '%s\\n' "\$MIN_GLIBC" "\$HOST_GLIBC" | sort -V | head -n1)" != "\$MIN_GLIBC" ]; then
    msg="This LinDVB AppImage needs a Linux system with glibc \$MIN_GLIBC or newer (this system: \${HOST_GLIBC:-no glibc}).
Older systems such as Debian 12, Ubuntu 22.04 or RHEL 9 can use the Flatpak instead:
https://github.com/thommy87/LinDVB#install"
    echo "\$msg" >&2
    if [ -z "\${LINDVB_NO_GUI_ERROR:-}" ] && { [ -n "\${DISPLAY:-}" ] || [ -n "\${WAYLAND_DISPLAY:-}" ]; }; then
        if command -v zenity >/dev/null; then zenity --error --title=LinDVB --text="\$msg" 2>/dev/null
        elif command -v kdialog >/dev/null; then kdialog --title LinDVB --error "\$msg" 2>/dev/null
        elif command -v notify-send >/dev/null; then notify-send LinDVB "\$msg" 2>/dev/null
        fi
    fi
    exit 1
fi
# WebKit finds its helper processes relative to the working directory (see
# build-appdir.sh); LinDVB resolves relative file arguments against the old one.
export LINDVB_ORIG_CWD="\$PWD"
cd "\$HERE/usr/lib"
export PYTHONHOME="\$HERE/usr"
export PYTHONPATH="\$HERE/usr/lib/python$PYVER/site-packages"
export PYTHONDONTWRITEBYTECODE=1
export LD_LIBRARY_PATH="\$HERE/usr/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="\$HERE/usr/lib/girepository-1.0"
export GST_PLUGIN_SYSTEM_PATH_1_0="\$HERE/usr/lib/gstreamer-1.0"
export GST_PLUGIN_PATH_1_0=""
export GST_PLUGIN_SCANNER_1_0="\$HERE/usr/lib/gst-plugin-scanner"
export GST_REGISTRY_1_0="\${XDG_CACHE_HOME:-\$HOME/.cache}/lindvb/gst-registry-appimage.bin"
export GSETTINGS_SCHEMA_DIR="\$HERE/usr/share/glib-2.0/schemas"
export GIO_MODULE_DIR="\$HERE/usr/lib/gio/modules"
export XDG_DATA_DIRS="\$HERE/usr/share:\${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
export PATH="\$HERE/usr/bin:\$PATH"
# The bundled OpenSSL and GnuTLS look for the CA certificates where Ubuntu keeps
# them; point them at the host's bundle (Fedora, openSUSE … keep it elsewhere).
if [ -z "\${SSL_CERT_FILE:-}" ]; then
    for f in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt \\
             /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem /etc/ssl/ca-bundle.pem \\
             /etc/ssl/cert.pem /etc/pki/tls/cacert.pem; do
        if [ -r "\$f" ]; then export SSL_CERT_FILE="\$f"; break; fi
    done
fi
if [ -n "\${SSL_CERT_FILE:-}" ] && [ -z "\${GTLS_SYSTEM_CA_FILE:-}" ]; then
    export GTLS_SYSTEM_CA_FILE="\$SSL_CERT_FILE"
fi
if [ -d "\$HERE/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders" ]; then
    export GDK_PIXBUF_MODULEDIR="\$HERE/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders"
    export GDK_PIXBUF_MODULE_FILE="\${XDG_CACHE_HOME:-\$HOME/.cache}/lindvb/loaders-appimage.cache"
    mkdir -p "\$(dirname "\$GDK_PIXBUF_MODULE_FILE")"
    "\$HERE/usr/bin/gdk-pixbuf-query-loaders" > "\$GDK_PIXBUF_MODULE_FILE" 2>/dev/null || true
fi
exec "\$HERE/usr/bin/python3" -m lindvb "\$@"
EOF
chmod +x "$APPDIR/AppRun"
QL="$(ls "$ARCH_LIB"/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders 2>/dev/null || command -v gdk-pixbuf-query-loaders || true)"
[ -n "$QL" ] && cp "$QL" "$APPDIR/usr/bin/gdk-pixbuf-query-loaders"

du -sh "$APPDIR"
echo "AppDir ready: $APPDIR"
