#!/bin/bash
# Signs Speaka.app with its entitlements and zips it as Speaka.tipa, plus the same file as Speaka.ipa. As in Caz.
set -euo pipefail
cd "$(dirname "$0")/.."

APP="build/DerivedData/Build/Products/Release-iphoneos/Speaka.app"
[ -d "$APP" ] || { echo "Speaka.app not found at $APP"; exit 1; }

rm -rf "$APP/_CodeSignature" "$APP/embedded.mobileprovision"
ldid -SSpeaka.entitlements "$APP/Speaka"
chmod 755 "$APP/Speaka"

rm -rf build/Payload packages/Speaka.tipa packages/Speaka.ipa
mkdir -p build/Payload packages
cp -R "$APP" build/Payload/
(cd build && zip -qry ../packages/Speaka.tipa Payload)
cp packages/Speaka.tipa packages/Speaka.ipa    # the same file; some tools only open .ipa
echo "Built packages/Speaka.tipa and packages/Speaka.ipa ($(du -h packages/Speaka.tipa | cut -f1))"
