# Fedora / COPR package for LDM. Build locally with packaging/build-rpm.sh; see packaging/COPR.md.
%bcond check 1

Name:           ldm-downloader
Version:        0.4.0
Release:        1%{?dist}
Summary:        Download manager for files, videos and torrents
License:        MIT
URL:            https://github.com/adiashishz/LDM
# GitHub's archive of the v%%{version} tag (top folder LDM-%%{version}); build-rpm.sh makes the same locally.
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz
Source1:        ldm-downloader.rpmlintrc
BuildArch:      noarch
# Renamed from ldm (0.2.0-1, attached to the GitHub release): that name belongs to other projects
# elsewhere (LTSP's display manager, an AUR device mounter). The command is still `ldm`.
Provides:       ldm = %{version}-%{release}
Obsoletes:      ldm < 0.2.0-2

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  systemd-rpm-macros
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
%if %{with check}
BuildRequires:  python3-pytest
# The window's tests draw it offscreen.
BuildRequires:  python3-pyside6
BuildRequires:  qt6-qtsvg
BuildRequires:  dbus-daemon
BuildRequires:  ffmpeg-free
BuildRequires:  rb_libtorrent-python3
%endif

# Python dependencies (httpx[socks], dbus-fast, yt-dlp[default], dnspython, secretstorage, cryptography) are generated
# from pyproject.toml.
# The window (Qt): PySide6, and Qt's SVG support for its icons.
Requires:       python3-pyside6
Requires:       qt6-qtsvg
Recommends:     qt6-qtwayland
Requires:       (ffmpeg-free or ffmpeg)
# The browser bridge talks to the service with gdbus.
Requires:       /usr/bin/gdbus
Recommends:     rb_libtorrent-python3
# YouTube needs a JavaScript runtime for yt-dlp's challenge solver.
Recommends:     nodejs
Recommends:     (wl-clipboard or xclip)

%description
LDM is a download manager for Linux, inspired by 1DM for Android: multi-part
downloads with pause, resume and retry, video and stream downloads (yt-dlp),
torrents (libtorrent), a scheduler, desktop notifications and a browser
extension for Firefox, Chrome and Brave. Downloads run in a background
service, so closing the window never stops them.

%prep
%autosetup -n LDM-%{version}

%generate_buildrequires
cd ldm
%pyproject_buildrequires

%build
cd ldm
%pyproject_wheel
cd ../extension
%{python3} build.py

%install
cd ldm
%pyproject_install
%pyproject_save_files ldm
%{python3} packaging/install-data.py %{buildroot} --prefix %{_prefix} --sysconfdir %{_sysconfdir}
install -Dm644 -t %{buildroot}%{_datadir}/ldm/extension ../extension/dist/*.zip
# Also a standalone script (copied to libexec for browsers), so keep it runnable.
chmod 755 %{buildroot}%{python3_sitelib}/ldm/nmhost.py

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.adiashishz.LDM.desktop
desktop-file-validate %{buildroot}%{_sysconfdir}/xdg/autostart/io.github.adiashishz.LDM.ClipboardWatch.desktop
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/io.github.adiashishz.LDM.metainfo.xml
%if %{with check}
cd ldm
# Everything runs offline against local servers and private buses; the window is drawn offscreen.
%pytest
%endif

%post
%systemd_user_post ldm.service

%preun
%systemd_user_preun ldm.service

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/ldm
%{_bindir}/ldm-gui
%{_datadir}/applications/io.github.adiashishz.LDM.desktop
%{_metainfodir}/io.github.adiashishz.LDM.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.adiashishz.LDM.svg
%{_datadir}/icons/hicolor/symbolic/apps/io.github.adiashishz.LDM-symbolic.svg
%{_datadir}/dbus-1/services/io.github.adiashishz.LDM.Service.service
%{_datadir}/dbus-1/services/io.github.adiashishz.LDM.service
%{_userunitdir}/ldm.service
%config(noreplace) %{_sysconfdir}/xdg/autostart/io.github.adiashishz.LDM.ClipboardWatch.desktop
%dir %{_libexecdir}/ldm
%{_libexecdir}/ldm/ldm_nmhost.py
%dir %{_prefix}/lib/mozilla
%dir %{_prefix}/lib/mozilla/native-messaging-hosts
%{_prefix}/lib/mozilla/native-messaging-hosts/io.github.adiashishz.ldm.json
%dir %{_sysconfdir}/opt/chrome
%dir %{_sysconfdir}/opt/chrome/native-messaging-hosts
%config(noreplace) %{_sysconfdir}/opt/chrome/native-messaging-hosts/io.github.adiashishz.ldm.json
%dir %{_sysconfdir}/chromium
%dir %{_sysconfdir}/chromium/native-messaging-hosts
%config(noreplace) %{_sysconfdir}/chromium/native-messaging-hosts/io.github.adiashishz.ldm.json
%{_datadir}/ldm/

%changelog
* Mon Nov 16 2026 Aditya Ashish <adiashishz@gmail.com> - 0.4.0-1
- A new window, built with Qt: menus and search, a toolbar, a sidebar with categories, queues and a speed graph, and a resizable details panel with each download's connections
- Every dialog redone: Add, Settings, Schedule, Batch download, Checksum, Torrent details and more
- Light and dark, following the system or chosen in View → Appearance
- Ubuntu 24.04 and Linux Mint 22 now use the Flatpak (they lack the Qt libraries LDM needs)

* Sun Nov 15 2026 Aditya Ashish <adiashishz@gmail.com> - 0.3.0-1
- Packages for Debian 13, Ubuntu 24.04 and 26.04 (also Linux Mint 22) and Arch Linux, alongside Fedora
- A Flatpak for every other distribution
- The package is now called ldm-downloader; the command is still ldm, and on Fedora it replaces the old ldm package automatically
- Runs on older GNOME libraries (libadwaita 1.5), so Ubuntu 24.04 and Linux Mint 22 are supported
- Keeps yt-dlp up to date for each user, so YouTube downloads keep working where the distribution's yt-dlp is old
- The browser extension works with more browser setups, and explains what to do with Snap browsers

* Sat Nov 14 2026 Aditya Ashish <adiashishz@gmail.com> - 0.2.0-2
- Rename the package to ldm-downloader (the command is still ldm)

* Thu Oct 22 2026 Aditya Ashish <adiashishz@gmail.com> - 0.2.0-1
- Selection mode: pause, resume, remove or delete several downloads at once
- Signed video streams: send the browser's request headers (fixes HTTP 403)
- Close button on the copied-link banner; monochrome tray icon

* Wed Oct 21 2026 Aditya Ashish <adiashishz@gmail.com> - 0.1.0-1
- First package: downloads, videos, torrents, scheduler, browser integration
