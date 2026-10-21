# Publishing LDM on COPR

COPR builds the RPM on Fedora's servers and gives users a `dnf copr enable` repository.
Everything here runs under **your** Fedora account; nothing is published until you do it.

## Before the first release

1. **Pick a license.** `ldm.spec` has `License: LicenseRef-Not-Yet-Chosen` and the AppStream
   file has no `<project_license>`. Set both, and add a `LICENSE` file at the repository root.
2. **Give the project a home** (e.g. a GitHub/Codeberg repository), then add `URL:` to the spec
   and `<url type="homepage">` to `data/io.github.adiashishz.LDM.metainfo.xml`.
3. Build once locally — this runs the tests and rpmlint the same way COPR will:

   ```
   sudo dnf install rpm-build pyproject-rpm-macros python3-devel rpmlint libappstream-glib
   ldm/packaging/build-rpm.sh
   ```

## One-time COPR setup

1. Log in at <https://copr.fedorainfracloud.org> with your Fedora account (FAS).
2. Copy the API token from <https://copr.fedorainfracloud.org/api/> into `~/.config/copr`.
3. `sudo dnf install copr-cli`
4. `copr-cli create ldm --chroot fedora-43-x86_64 --chroot fedora-44-x86_64 --chroot fedora-rawhide-x86_64`
   (the package is `noarch`; one architecture per release is enough).

## Each release

```
# bump Version: and add a %changelog entry in ldm.spec, commit, then:
ldm/packaging/build-rpm.sh                      # builds ~/rpmbuild/SRPMS/ldm-<version>-1.fc44.src.rpm
copr-cli build ldm ~/rpmbuild/SRPMS/ldm-*.src.rpm
```

## What users run

```
sudo dnf copr enable <your-fas-name>/ldm
sudo dnf install ldm
```

Then, once per user: log out and back in (or run `systemctl --user start ldm.service`) so the
download service starts. The browser extension is in `/usr/share/ldm/extension/`; Firefox needs
the signed `.xpi` from `extension/sign-firefox.sh`. Flatpak browsers (e.g. Brave from Flathub)
still need `ldm install-browser-integration`, because the system-wide manifests are invisible
inside their sandbox.
