#!/bin/bash
# Install Ubuntu packages with a cache of the downloaded .deb files.
#
# Usage: apt-install.sh restore|install|prune CACHE_DIR [packages...]
#
# The cache only saves downloads, never decides versions: the package index is
# always refreshed (apt-get update) first, apt picks the newest version from it,
# and a cached .deb is used only when it is exactly that version (apt checks its
# checksum against the index). Anything else is downloaded fresh. Afterwards the
# requested packages are checked against the index, and the cache keeps only the
# .deb files of the versions that are installed now.
set -euo pipefail
cmd="$1"; cache="$2"; shift 2
# apt downloads straight into the cache directory (Dir::Cache::Archives), so
# nothing has to be copied and no apt clean hook can empty it.

case "$cmd" in
restore)
    # Faster, more patient apt; no man page index rebuild after every install.
    sudo tee /etc/apt/apt.conf.d/90ci >/dev/null <<'CONF'
Acquire::Retries "5";
Acquire::http::Timeout "60";
Acquire::https::Timeout "60";
Acquire::Languages "none";
Dpkg::Use-Pty "0";
CONF
    sudo rm -f /var/lib/man-db/auto-update
    mkdir -p "$cache/partial"
    n=$(find "$cache" -maxdepth 1 -name '*.deb' | wc -l)
    echo "$n cached packages ($(du -sh "$cache" | cut -f1))"
    ;;
install)
    start=$SECONDS
    # A mirror in the middle of a sync makes the update fail ("File has
    # unexpected size"); try again rather than build on an incomplete index.
    for try in 1 2 3 4; do
        sudo apt-get update && break
        [ "$try" -eq 4 ] && { echo "::error::apt-get update failed 4 times"; exit 1; }
        echo "::warning::apt-get update failed, retrying in $((try * 20)) s"
        sleep $((try * 20))
    done
    echo "::notice title=apt index::refreshed in $((SECONDS - start)) s"
    start=$SECONDS
    sudo apt-get install -y --no-install-recommends \
        -o Dir::Cache::Archives="$cache" -o APT::Keep-Downloaded-Packages=true "$@" 2>&1 | tee /tmp/apt-install.log
    fetched="$(grep -E '^(Need to get|Fetched) ' /tmp/apt-install.log | tail -n1 || true)"
    echo "::notice title=apt install::$((SECONDS - start)) s; ${fetched:-nothing downloaded}"
    # Freshness check: every requested package is at the index's candidate version.
    stale=0
    for p in "$@"; do
        p="${p%%=*}"
        inst="$(dpkg-query -W -f='${Version}' "$p" 2>/dev/null || true)"
        cand="$(apt-cache policy "$p" | awk '/Candidate:/{print $2}')"
        if [ "$inst" != "$cand" ]; then
            echo "::error::$p is at ${inst:-not installed}, the archive has $cand"
            stale=1
        fi
    done
    [ "$stale" -eq 0 ] && echo "All $# requested packages are at the archive's current version"
    exit "$stale"
    ;;
prune)
    # Keep only the .deb files of the versions installed now (file names as
    # apt writes them, with the epoch colon as %3a).
    dpkg-query -W -f='${Package}_${Version}_${Architecture}.deb\n' | sed 's/:/%3a/g' | sort -u > /tmp/installed-debs
    (cd "$cache" && ls -1 *.deb 2>/dev/null || true) | sort -u | comm -23 - /tmp/installed-debs |
        while read -r f; do sudo rm -f "$cache/$f"; done
    sudo rm -rf "$cache/partial" "$cache/lock"
    echo "Cache: $(find "$cache" -maxdepth 1 -name '*.deb' | wc -l) packages, $(du -sh "$cache" | cut -f1)"
    ;;
*)
    echo "unknown command $cmd" >&2; exit 2 ;;
esac
