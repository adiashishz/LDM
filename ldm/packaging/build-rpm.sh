#!/bin/bash
# Build the LDM RPM locally from the committed tree (git HEAD), then lint it.
# Needs: sudo dnf install rpm-build pyproject-rpm-macros python3-devel rpmlint libappstream-glib
set -euo pipefail
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
SPEC=$REPO/ldm/packaging/ldm.spec
VERSION=$(awk '/^Version:/ {print $2}' "$SPEC")
TOP=${RPM_TOPDIR:-$HOME/rpmbuild}
mkdir -p "$TOP"/{SOURCES,SPECS,SRPMS,RPMS,BUILD}
# Only what the package needs: the Python project and the extension (not apk/, notes/, tools/).
git -C "$REPO" archive --format=tar.gz --prefix="ldm-$VERSION/" -o "$TOP/SOURCES/ldm-$VERSION.tar.gz" HEAD ldm extension
cp "$SPEC" "$TOP/SPECS/"
# Build dependencies are generated from pyproject.toml; rpmbuild lists any that are missing.
rpmbuild --define "_topdir $TOP" -ba "$TOP/SPECS/ldm.spec"
echo
rpmlint "$TOP/SPECS/ldm.spec" "$TOP"/RPMS/noarch/ldm-"$VERSION"-*.rpm "$TOP"/SRPMS/ldm-"$VERSION"-*.src.rpm || true
echo
echo "Built:"; ls -1 "$TOP"/RPMS/noarch/ldm-"$VERSION"-*.rpm "$TOP"/SRPMS/ldm-"$VERSION"-*.src.rpm
