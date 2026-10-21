#!/bin/bash
# Sign the Firefox build as an *unlisted* add-on on addons.mozilla.org, for self-distribution
# (Firefox release only installs signed add-ons). Unlisted add-ons are signed automatically and
# don't appear on the AMO website.
#
# One-time: create API credentials at https://addons.mozilla.org/developers/addon/api/key/
# then run:   AMO_JWT_ISSUER=user:12345:67 AMO_JWT_SECRET=... ./sign-firefox.sh
# Result:     dist/signed/*.xpi — install it from about:addons ("Install Add-on From File…").
set -euo pipefail
: "${AMO_JWT_ISSUER:?set AMO_JWT_ISSUER (the JWT issuer from your AMO API key page)}"
: "${AMO_JWT_SECRET:?set AMO_JWT_SECRET (the JWT secret from your AMO API key page)}"
cd "$(dirname "$0")"
python3 build.py
# Credentials go through the environment, not the command line (visible in `ps`).
WEB_EXT_API_KEY=$AMO_JWT_ISSUER WEB_EXT_API_SECRET=$AMO_JWT_SECRET \
  npx --yes web-ext@8 sign --source-dir dist/firefox --artifacts-dir dist/signed --channel unlisted
ls -1 dist/signed/*.xpi
