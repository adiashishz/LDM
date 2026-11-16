#!/bin/sh
# Install LDM's runtime and test dependencies from the distribution's own repositories.
#   install-deps.sh fedora|debian|arch
# Used by the CI containers; the lists are also the reference for the distribution packages.
set -eu

case "$1" in
fedora)
    dnf install -y --setopt=install_weak_deps=False \
        python3 python3-pyside6 qt6-qtsvg \
        python3-httpx python3-socksio python3-dbus-fast python3-dns python3-secretstorage python3-cryptography \
        yt-dlp ffmpeg-free rb_libtorrent-python3 \
        glib2 dbus-daemon xdg-utils desktop-file-utils util-linux
    ;;
debian)  # Debian and Ubuntu
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends \
        python3 python3-venv \
        python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets python3-pyside6.qtsvg qt6-svg-plugins \
        python3-httpx python3-socksio python3-dbus-fast python3-dnspython python3-secretstorage python3-cryptography \
        yt-dlp ffmpeg python3-libtorrent \
        libglib2.0-bin dbus-daemon xdg-utils desktop-file-utils ca-certificates
    ;;
arch)
    pacman -Syu --noconfirm --needed \
        python pyside6 qt6-svg \
        python-httpx python-socksio python-dbus-fast python-dnspython python-secretstorage python-cryptography \
        yt-dlp ffmpeg libtorrent-rasterbar \
        glib2 dbus xdg-utils desktop-file-utils
    ;;
*)
    echo "usage: $0 fedora|debian|arch" >&2
    exit 2
    ;;
esac
