#!/bin/bash
# Build the LDM RPM locally from the committed tree (git HEAD), then lint it.
# Needs the build tools plus what LDM's build and tests use:
#   sudo dnf install rpm-build pyproject-rpm-macros python3-devel rpmlint libappstream-glib \
#                    python3-hatchling python3-pytest python3-httpx+socks python3-dns yt-dlp+default \
#                    dbus-daemon ffmpeg-free rb_libtorrent-python3
set -euo pipefail
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
SPEC=$REPO/ldm/packaging/ldm.spec
VERSION=$(awk '/^Version:/ {print $2}' "$SPEC")
TOP=${RPM_TOPDIR:-$HOME/rpmbuild}
mkdir -p "$TOP"/{SOURCES,SPECS,SRPMS,RPMS,BUILD}
# Same layout as GitHub's archive of a release tag (top folder LDM-<version>).
git -C "$REPO" archive --format=tar.gz --prefix="LDM-$VERSION/" -o "$TOP/SOURCES/ldm-$VERSION.tar.gz" HEAD
cp "$SPEC" "$TOP/SPECS/"
cp "$REPO/ldm/packaging/ldm.rpmlintrc" "$TOP/SOURCES/"
# Build dependencies are generated from pyproject.toml; rpmbuild lists any that are missing.
rpmbuild --define "_topdir $TOP" -ba "$TOP/SPECS/ldm.spec"
echo
rpmlint -r "$REPO/ldm/packaging/ldm.rpmlintrc" "$TOP/SPECS/ldm.spec" "$TOP"/RPMS/noarch/ldm-"$VERSION"-*.rpm \
    "$TOP"/SRPMS/ldm-"$VERSION"-*.src.rpm || true
echo
echo "Built:"; ls -1 "$TOP"/RPMS/noarch/ldm-"$VERSION"-*.rpm "$TOP"/SRPMS/ldm-"$VERSION"-*.src.rpm
