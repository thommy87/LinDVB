#!/bin/bash
# Build LinDVB-x86_64.AppImage: assemble the AppDir, then pack it with appimagetool.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$SRC/build"
mkdir -p "$BUILD"
"$SRC/packaging/build-appdir.sh" "$BUILD/LinDVB.AppDir"
TOOL="$BUILD/appimagetool"
if [ ! -x "$TOOL" ]; then
    curl -fsSL -o "$TOOL" \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$TOOL"
fi
VERSION="${VERSION:-$(git -C "$SRC" describe --tags --always 2>/dev/null || echo dev)}"
ARCH=x86_64 VERSION="$VERSION" "$TOOL" --appimage-extract-and-run --no-appstream \
    "$BUILD/LinDVB.AppDir" "$BUILD/LinDVB-$VERSION-x86_64.AppImage"
ls -lh "$BUILD"/*.AppImage
