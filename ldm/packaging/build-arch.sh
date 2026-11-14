#!/bin/bash
# Build the Arch packages from the committed tree (git HEAD), like build-rpm.sh and build-deb.sh, then
# lint them with namcap.
#
#   build-arch.sh [release|git]...       (default: both)
#
# release: ldm-downloader from a tarball of HEAD, made the way GitHub makes the release tarball
#          (checksum filled in), so the PKGBUILD is built exactly as the AUR one will be.
# git:     ldm-downloader-git, cloning this repository instead of GitHub.
# Output goes to $ARCH_OUT (default ~/archbuild). Run as a normal user with sudo for makepkg -s, or set
# MAKEPKG_FLAGS (e.g. --nocheck). Needs base-devel, git and namcap.
set -euo pipefail
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
OUT=${ARCH_OUT:-$HOME/archbuild}
read -r -a FLAGS <<< "${MAKEPKG_FLAGS:-}"
mkdir -p "$OUT"

build() {
    local name=$1 dir="$OUT/$1"
    rm -rf "$dir"
    mkdir -p "$dir"
    cp "$REPO/ldm/packaging/arch/$name/PKGBUILD" "$dir/"
    cd "$dir"
    if [ "$name" = ldm-downloader ]; then
        local version tarball
        version=$(sed -n 's/^pkgver=//p' PKGBUILD)
        tarball="ldm-downloader-$version.tar.gz"
        git -C "$REPO" archive --format=tar.gz --prefix="LDM-$version/" -o "$dir/$tarball" HEAD
        sed -i "s/^sha256sums=.*/sha256sums=('$(sha256sum "$tarball" | cut -d' ' -f1)')/" PKGBUILD
    else
        sed -i "s|^source=.*|source=(\"LDM::git+file://$REPO#commit=$(git -C "$REPO" rev-parse HEAD)\")|" PKGBUILD
    fi
    namcap PKGBUILD
    makepkg --syncdeps --noconfirm --cleanbuild "${FLAGS[@]}"
    namcap ./*.pkg.tar.zst
    echo "Built: $(ls "$dir"/*.pkg.tar.zst)"
}

for variant in "${@:-release git}"; do
    for v in $variant; do
        case $v in
            release) build ldm-downloader ;;
            git) build ldm-downloader-git ;;
            *) echo "usage: $0 [release|git]..." >&2; exit 2 ;;
        esac
    done
done
