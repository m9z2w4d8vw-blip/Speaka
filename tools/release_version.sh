#!/bin/bash
# Prints the version this build publishes. A pushed tag like v9.4.0 publishes 9.4.0; every other build is control's
# Version plus the run number (9.3 -> 9.3.57), so each release is newer than the last. Both CI jobs use it, so the .deb
# and the app always carry the same number, which is what the app's updater compares.
set -euo pipefail
cd "$(dirname "$0")/.."
base="$(sed -n 's/^Version:[[:space:]]*//p' control | sed -n '1p' | tr -d '\r[:space:]')"
if [[ "${GITHUB_REF:-}" == refs/tags/v* ]]; then
  version="${GITHUB_REF_NAME#v}"
else
  version="${base}.${GITHUB_RUN_NUMBER:-0}"
fi
if ! [[ "$version" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
  echo "version '$version' is not dotted-numeric (control says '$base', ref is ${GITHUB_REF:-none})" >&2
  exit 1
fi
echo "$version"
