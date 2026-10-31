#!/bin/bash
# Assemble an AppDir for LinDVB from the packages of the build system
# (meant for Ubuntu 24.04; the AppImage then runs on distributions with
# glibc 2.39 or newer). Usage: packaging/build-appdir.sh [AppDir]
set -euo pipefail

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
        [ -d "$base/$mod" ] && cp -r "$base/$mod" "$SITE/" && break
    done
done
cp -r "$SRC/lindvb" "$SITE/"
find "$SITE" -name __pycache__ -prune -exec rm -rf {} +

echo "== GObject introspection typelibs"
mkdir -p "$APPDIR/usr/lib/girepository-1.0"
for t in GLib-2.0 GObject-2.0 Gio-2.0 GModule-2.0 Gtk-4.0 Gdk-4.0 Gsk-4.0 Graphene-1.0 \
         Pango-1.0 PangoCairo-1.0 cairo-1.0 HarfBuzz-0.0 freetype2-2.0 GdkPixbuf-2.0 \
         Gst-1.0 GstBase-1.0 GstVideo-1.0 GstAudio-1.0 GstTag-1.0 GstMpegts-1.0 GstPbutils-1.0 \
         PangoFT2-1.0 Fontconfig-2.0 GioUnix-2.0 GLibUnix-2.0; do
    [ -f "$ARCH_LIB/girepository-1.0/$t.typelib" ] && \
        cp "$ARCH_LIB/girepository-1.0/$t.typelib" "$APPDIR/usr/lib/girepository-1.0/"
done

echo "== GStreamer plugins"
mkdir -p "$APPDIR/usr/lib/gstreamer-$GSTVER"
# Only the plugins LinDVB uses: tuning, demuxing, decoding, network sources, output.
PLUGINS="coreelements playback typefindfunctions app autodetect audioconvert audioresample
  volume audiorate videoconvertscale videoconvert videoscale videorate videofilter deinterlace
  equalizer pbtypes gio subparse pango overlaycomposition dvb mpegtsdemux mpegtsmux teletext
  dvbsuboverlay dvdspu libav audioparsers videoparsersbad mpegpsdemux mpeg2dec mpg123 a52dec
  dtsdec faad opus vorbis ogg flac isomp4 matroska id3demux icydemux soup hls dash
  adaptivedemux2 udp rtp rtpmanager rtpmanagerbad rtsp tcp multifile png alsa pulseaudio
  pipewire gtk4 opengl ximagesink"
for p in $PLUGINS; do
    f="$ARCH_LIB/gstreamer-$GSTVER/libgst$p.so"
    [ -f "$f" ] && cp "$f" "$APPDIR/usr/lib/gstreamer-$GSTVER/"
done
SCANNER="$(ls "$ARCH_LIB"/gstreamer$GSTVER/gstreamer-$GSTVER/gst-plugin-scanner 2>/dev/null || true)"
[ -n "$SCANNER" ] && cp "$SCANNER" "$APPDIR/usr/lib/gst-plugin-scanner"

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
    command -v $tool >/dev/null && cp "$(command -v $tool)" "$APPDIR/usr/bin/"
done
for d in /usr/share/dvb /usr/share/dvbv5; do
    [ -d "$d" ] && cp -r "$d" "$APPDIR/usr/share/"
done

echo "== shared libraries"
# Libraries that must come from the host system (C library, graphics drivers, audio servers).
EXCLUDE='^(libc|libm|libdl|libpthread|librt|libresolv|libutil|libnsl|ld-linux|libGL|libGLX|libGLdispatch|libEGL|libOpenGL|libgbm|libdrm|libvulkan|libstdc\+\+|libgcc_s|libasound|libpulse|libjack|libpipewire|libudev|libsystemd|libdbus-1|libX11-xcb|libxcb|libX11|libwayland-client|libwayland-egl|libwayland-cursor|libexpat|libfontconfig|libfreetype|libz)\.so'
collect() {
    find "$APPDIR" -type f \( -name '*.so*' -o -path '*/usr/bin/*' -o -name gst-plugin-scanner \) |
        while read -r f; do ldd "$f" 2>/dev/null; done |
        awk '/=> \//{print $3}' | sort -u
}
for _ in 1 2 3 4; do
    collect | while read -r lib; do
        name="$(basename "$lib")"
        [[ "$name" =~ $EXCLUDE ]] && continue
        [ -e "$APPDIR/usr/lib/$name" ] || cp -L "$lib" "$APPDIR/usr/lib/$name"
    done
done
# Python extension modules and GObject typelibs reference libraries by soname.
for t in "$ARCH_LIB"/libgtk-4.so.1 "$ARCH_LIB"/libgstmpegts-1.0.so.0 "$ARCH_LIB"/libgstvideo-1.0.so.0 \
         "$ARCH_LIB"/libgstpbutils-1.0.so.0 "$ARCH_LIB"/libgraphene-1.0.so.0 "$ARCH_LIB"/libgirepository-1.0.so.1; do
    [ -e "$t" ] && [ ! -e "$APPDIR/usr/lib/$(basename "$t")" ] && cp -L "$t" "$APPDIR/usr/lib/"
done

echo "== desktop integration"
cp "$SRC/data/io.github.thommy87.LinDVB.desktop" "$APPDIR/"
sed -i 's/^Icon=.*/Icon=lindvb/' "$APPDIR/io.github.thommy87.LinDVB.desktop"
cp "$SRC/data/lindvb.svg" "$APPDIR/lindvb.svg"
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp "$SRC/data/lindvb.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/lindvb.svg"
mkdir -p "$APPDIR/usr/share/metainfo"
cp "$SRC/data/io.github.thommy87.LinDVB.metainfo.xml" "$APPDIR/usr/share/metainfo/"
cp "$SRC/LICENSE" "$APPDIR/usr/share/LICENSE.LinDVB"

cat > "$APPDIR/AppRun" <<EOF
#!/bin/bash
HERE="\$(dirname "\$(readlink -f "\$0")")"
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
