# Releasing LDM

A release is a tag `vX.Y.Z`. GitHub Actions builds and tests every package from it, then creates a **draft**
release with all the files. Nothing is public until you publish the draft.

## Each release

1. **Prepare it on a branch.** Set the version and the release notes (one line per change, written for users):

   ```sh
   git switch -c release/0.3.0
   ldm/packaging/release.py 0.3.0 "First note" "Second note"
   ```

   This updates all nine places that hold the version (Python package, browser extension, RPM spec,
   `debian/changelog`, both PKGBUILDs, AppStream metainfo) and adds the notes to each changelog.

2. **Optionally refresh yt-dlp** for the `.deb` (it bundles a pinned yt-dlp):

   ```sh
   ldm/packaging/release.py --update-ytdlp
   ```

   The Flatpak's Python packages (yt-dlp among them) are pinned in `ldm/packaging/flatpak-python-deps.yml`;
   the header of `ldm/packaging/flatpak-python-deps.py` shows how to regenerate them.

3. **Open a pull request** and merge it when CI is green. CI checks that every file has the same version.

4. **Tag the merge on main** and push the tag:

   ```sh
   git switch main && git pull
   git tag v0.3.0 && git push origin v0.3.0
   ```

5. **Wait for the Release workflow** (Actions → Release). It checks the tag against the files, runs the whole
   CI (every package built and install-tested), pins the AUR PKGBUILD to GitHub's tarball of the tag, and
   creates the draft release.

6. **Check the draft** on GitHub (Releases): the notes, and that the files are there:
   `.rpm` (and `.src.rpm`), two `.deb` (Debian 13, Ubuntu 26.04), the `.flatpak`, `PKGBUILD` and `SRCINFO` for the AUR, the two
   extension zips, the Windows installer `LDM-Setup-<version>.exe`, and `SHA256SUMS`.

   While Windows is a preview, also try the draft's installer on a Windows 10 or 11 machine: install it, add a
   download from the browser extension, open a magnet link, check the tray icon and a notification, then
   uninstall. CI installs and uses it on Windows Server, but can't click through a real desktop.

7. **Publish it.** Publishing also rebuilds the website with links to the new files.

To try the whole pipeline without releasing: Actions → Release → Run workflow (a dry run: no tag check, no
AUR pin, no release).

## One-time setup: the website

The website, https://ldm-downloader.github.io, is served by GitHub Pages from the repository
`ldm-downloader/ldm-downloader.github.io` in a free GitHub organization. The Website workflow pushes the built
site there with a deploy key.

1. Create the organization: https://github.com/organizations/plan, the **Free** plan, named `ldm-downloader`.
2. In it, create a **public** repository named exactly `ldm-downloader.github.io`, with a README (so it has a
   `main` branch).
3. In that repository: **Settings → Pages → Build and deployment → Source: Deploy from a branch**,
   branch `main`, folder `/ (root)`.
4. Make a deploy key on your computer (it stays on your computer and in GitHub's secrets only):

   ```sh
   ssh-keygen -t ed25519 -N "" -C "ldm website" -f ldm-site-key
   ```

5. In `ldm-downloader/ldm-downloader.github.io`: **Settings → Deploy keys → Add deploy key**: paste
   `ldm-site-key.pub`, tick **Allow write access**.
6. In `adiashishz/LDM`: **Settings → Secrets and variables → Actions → New repository secret** named
   `SITE_DEPLOY_KEY`: paste the contents of `ldm-site-key` (the private key). Then delete both key files.
7. Run Actions → Website → Run workflow once: the site appears within a minute or two.
