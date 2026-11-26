#!/bin/bash
# Build gtk4paintablesink (gst-plugins-rs "gtk4", libgstgtk4.so) for a package
# that brings its own GStreamer plugins (AppImage). Ubuntu 24.04 has no binary
# package of it, only its Rust sources (librust-gst-plugin-gtk4-dev with the
# crates it needs under /usr/share/cargo/registry); they are built offline,
# from the distribution's own sources and nothing else.
# Usage: packaging/build-gtk4sink.sh [build folder]; prints the path of the
# built library. Needs cargo and librust-gst-plugin-gtk4-dev.
set -euo pipefail
REGISTRY=/usr/share/cargo/registry
OUT="$(realpath -m "${1:-$(dirname "$0")/../build}")/gtk4sink"
CRATE="$(ls -d "$REGISTRY"/gst-plugin-gtk4-* 2>/dev/null | sort -V | tail -n1)"
if [ -z "$CRATE" ] || ! command -v cargo >/dev/null; then
    echo "gtk4paintablesink: needs cargo and librust-gst-plugin-gtk4-dev" >&2
    exit 1
fi
rm -rf "$OUT"
mkdir -p "$OUT"
cp -r "$CRATE" "$OUT/src"
mkdir -p "$OUT/src/.cargo"
cat > "$OUT/src/.cargo/config.toml" <<CFG
[source.crates-io]
replace-with = "distribution"
[source.distribution]
directory = "$REGISTRY"
[net]
offline = true
CFG
rm -f "$OUT/src/Cargo.lock"
# wayland, x11egl, x11glx: OpenGL on both display servers; gtk_v4_14: the
# AppImage brings GTK 4.14 itself
# (GIT_DIR: the version helper would put LinDVB's commit into the plugin's
# version when the build folder is inside LinDVB's checkout)
(cd "$OUT/src" && GIT_DIR=/dev/null cargo build --release --lib --offline \
    --features wayland,x11egl,x11glx,gtk_v4_14 >&2)
LIB="$OUT/src/target/release/libgstgtk4.so"
strip "$LIB" 2>/dev/null || true
echo "$LIB"
