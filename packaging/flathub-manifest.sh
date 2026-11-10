#!/bin/bash
# Write the Flathub copy of the Flatpak manifest for a release tag: the same as
# flatpak/io.github.thommy87.LinDVB.yml, but LinDVB itself comes from the tagged
# commit on GitHub instead of the local folder (Flathub builds only from
# pinned sources). See flatpak/FLATHUB.md.
# Usage: packaging/flathub-manifest.sh v0.14.0 [output file]
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${1:?usage: $0 TAG [output]}"
OUT="${2:-$SRC/build/flathub/io.github.thommy87.LinDVB.yml}"
COMMIT="$(git -C "$SRC" rev-list -n 1 "$TAG")"
mkdir -p "$(dirname "$OUT")"
python3 - "$SRC/flatpak/io.github.thommy87.LinDVB.yml" "$OUT" "$TAG" "$COMMIT" <<'PY'
import sys
src, out, tag, commit = sys.argv[1:]
text = open(src, encoding="utf-8").read()
local = """      - type: dir
        path: ..
        skip:
          - build
          - .git
          - .flatpak-builder
"""
pinned = f"""      - type: git
        url: https://github.com/thommy87/LinDVB.git
        tag: {tag}
        commit: {commit}
"""
if text.count(local) != 1:
    sys.exit("the local LinDVB source was not found in " + src)
open(out, "w", encoding="utf-8").write(text.replace(local, pinned))
print(f"{out}: LinDVB {tag} ({commit})")
PY
