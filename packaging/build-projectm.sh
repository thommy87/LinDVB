#!/bin/bash
# Build libprojectM 4 and the gst-projectm GStreamer plugin into a prefix, and
# fetch a preset and texture collection, for bundling with LinDVB (AppImage).
# Usage: packaging/build-projectm.sh [prefix]   (default: build/projectm)
# Needs: git cmake ninja-build g++ libgl-dev libegl-dev libglm-dev
#        libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
set -euo pipefail
[ -n "${CI:-}" ] && set -x

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="$(realpath -m "${1:-$SRC/build/projectm}")"
WORK="$PREFIX.src"
# Pinned versions: the released libprojectM 4.1 (the unreleased 4.2 changed how
# it finds OpenGL and failed on some desktops) and gst-projectm, which has no
# releases yet; its one call into the 4.2 API (frame time) is patched out.
PROJECTM_TAG=v4.1.8
GST_PROJECTM_COMMIT=54c481c0b063065406cfeedffe0aecfa13a43f0e
PRESETS_COMMIT=${PRESETS_COMMIT:-}
TEXTURES_COMMIT=${TEXTURES_COMMIT:-}

if [ -f "$PREFIX/.done" ]; then
    echo "projectM already built in $PREFIX"
    exit 0
fi
rm -rf "$PREFIX" "$WORK"
mkdir -p "$PREFIX" "$WORK"

clone() {   # url dir [ref]
    git clone -q --filter=blob:none "$1" "$WORK/$2"
    if [ -n "${3:-}" ]; then git -C "$WORK/$2" checkout -q "$3"; fi
}

echo "== libprojectM $PROJECTM_TAG"
git clone -q --depth 1 --branch "$PROJECTM_TAG" --recurse-submodules --shallow-submodules \
    https://github.com/projectM-visualizer/projectm.git "$WORK/projectm"
cmake -S "$WORK/projectm" -B "$WORK/projectm/build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_INSTALL_LIBDIR=lib \
    -DBUILD_SHARED_LIBS=ON -DENABLE_PLAYLIST=ON -DBUILD_TESTING=OFF -DENABLE_SDL_UI=OFF
cmake --build "$WORK/projectm/build"
cmake --install "$WORK/projectm/build"

echo "== gst-projectm"
clone https://github.com/projectM-visualizer/gst-projectm.git gst-projectm "$GST_PROJECTM_COMMIT"
# projectm_set_frame_time() only exists in libprojectM 4.2; 4.1 times frames itself
sed -i '/projectm_set_frame_time(/d' "$WORK/gst-projectm/src/plugin.c"
cmake -S "$WORK/gst-projectm" -B "$WORK/gst-projectm/build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$PREFIX" \
    -DCMAKE_C_FLAGS="-include stdbool.h" \
    -DCMAKE_INSTALL_RPATH='$ORIGIN/..'
cmake --build "$WORK/gst-projectm/build"
mkdir -p "$PREFIX/lib/gstreamer-1.0"
find "$WORK/gst-projectm/build" -name 'libgstprojectm.so' -exec cp {} "$PREFIX/lib/gstreamer-1.0/" \;
[ -f "$PREFIX/lib/gstreamer-1.0/libgstprojectm.so" ]

echo "== presets and textures"
mkdir -p "$PREFIX/share/projectM"
clone https://github.com/projectM-visualizer/presets-cream-of-the-crop.git presets "$PRESETS_COMMIT"
clone https://github.com/projectM-visualizer/presets-milkdrop-texture-pack.git textures "$TEXTURES_COMMIT"
mkdir -p "$PREFIX/share/projectM/presets" "$PREFIX/share/projectM/textures"
tar -C "$WORK/presets" --exclude=.git -cf - . | tar -C "$PREFIX/share/projectM/presets" -xf -
(cd "$WORK/textures" && find . -type f \( -iname '*.jpg' -o -iname '*.png' -o -iname '*.tga' -o -iname '*.bmp' -o -iname '*.dds' \) \
    -print0 | xargs -0 -I{} cp {} "$PREFIX/share/projectM/textures/")
cp "$WORK/projectm/COPYING" "$PREFIX/share/projectM/COPYING.libprojectM" 2>/dev/null || true
cp "$WORK/gst-projectm/LICENSE" "$PREFIX/share/projectM/LICENSE.gst-projectm" 2>/dev/null || true
echo "presets: $(find "$PREFIX/share/projectM/presets" -name '*.milk' | wc -l)," \
     "textures: $(ls "$PREFIX/share/projectM/textures" | wc -l)"
touch "$PREFIX/.done"
