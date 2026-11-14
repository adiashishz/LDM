#!/bin/bash
# Build the LDM .deb from the committed tree (git HEAD), like build-rpm.sh, then lint it.
#
#   build-deb.sh [SUFFIX DISTRIBUTION]     e.g.  build-deb.sh '~deb13' trixie
#                                                build-deb.sh '~ubuntu24.04' noble
#
# SUFFIX goes after the Debian revision (0.2.0-1~ubuntu24.04), so each release gets its own build:
# the Python version in Depends comes from the system it was built on.
# Output goes to $DEB_OUT (default ~/debbuild): the two source tarballs, the source package and the .deb.
#
# Needs devscripts (or dpkg-dev), lintian, curl and git; build dependencies are in debian/control:
#   sudo apt-get build-dep ./        (from the extracted tree, which this script prints)
# With INSTALL_BUILD_DEPS=1 (as root, as in CI), it installs them itself.
set -euo pipefail
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
SUFFIX=${1:-}
DIST=${2:-unstable}
VERSION=$(sed -n '1s/^ldm (\([^-]*\)-.*/\1/p' "$REPO/debian/changelog")
OUT=${DEB_OUT:-$HOME/debbuild}
. <(grep -E '^(version|sha256)=' "$REPO/debian/yt-dlp.pin" | sed 's/^/YTDLP_/')
mkdir -p "$OUT"
cd "$OUT"

# 1. Source tarballs: LDM without debian/, and the pinned yt-dlp build (checked against its checksum).
git -C "$REPO" archive --format=tar.gz --prefix="ldm-$VERSION/" -o "$OUT/ldm_$VERSION.orig.tar.gz" HEAD \
    -- . ':(exclude)debian'
if ! echo "$YTDLP_sha256  dl/yt-dlp-$YTDLP_version" | sha256sum -c --quiet 2>/dev/null; then
    mkdir -p dl
    curl -fsSL -o "dl/yt-dlp-$YTDLP_version" \
        "https://github.com/yt-dlp/yt-dlp/releases/download/$YTDLP_version/yt-dlp"
    echo "$YTDLP_sha256  dl/yt-dlp-$YTDLP_version" | sha256sum -c --quiet
fi
mkdir -p "dl/ytdlp-$YTDLP_version"
install -m644 "dl/yt-dlp-$YTDLP_version" "dl/ytdlp-$YTDLP_version/yt-dlp"
tar --sort=name --mtime="@0" --owner=0 --group=0 --numeric-owner -C dl -czf "ldm_$VERSION.orig-ytdlp.tar.gz" \
    "ytdlp-$YTDLP_version"

# 2. The tree dpkg-source would unpack: LDM, yt-dlp in ytdlp/, and debian/.
SRC="$OUT/ldm-$VERSION"
rm -rf "$SRC"
tar -xzf "ldm_$VERSION.orig.tar.gz"
mkdir "$SRC/ytdlp"
tar -xzf "ldm_$VERSION.orig-ytdlp.tar.gz" -C "$SRC/ytdlp" --strip-components=1
git -C "$REPO" archive HEAD debian | tar -x -C "$SRC"
if [ -n "$SUFFIX" ]; then
    sed -i "1s/^ldm (\([^)]*\)) [^;]*;/ldm (\1$SUFFIX) $DIST;/" "$SRC/debian/changelog"
fi

# 3. Build the source package and the .deb, then lint them.
if [ "${INSTALL_BUILD_DEPS:-}" = 1 ]; then
    apt-get build-dep -y "$SRC"
fi
(cd "$SRC" && dpkg-buildpackage -us -uc)
FULL_VERSION=$(cd "$SRC" && dpkg-parsechangelog -S Version)
CHANGES="$OUT/ldm_${FULL_VERSION}_$(dpkg --print-architecture).changes"
echo
lintian --info --display-info --fail-on error "$CHANGES"
echo
echo "Built:"; grep -E '\.(deb|dsc)$' "$CHANGES" | awk '{print "  '"$OUT"'/" $NF}' | sort -u
