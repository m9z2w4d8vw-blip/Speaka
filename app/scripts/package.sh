#!/bin/bash
# Puts the update helper inside Speaka.app, signs both with their entitlements, and zips Speaka.tipa, plus the same
# file as Speaka.ipa. As in Caz (scripts/package.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

APP="build/DerivedData/Build/Products/Release-iphoneos/Speaka.app"
[ -d "$APP" ] || { echo "Speaka.app not found at $APP"; exit 1; }
[ -f build/speakahelper ] || { echo "build/speakahelper not found"; exit 1; }

cp build/speakahelper "$APP/speakahelper"
rm -rf "$APP/_CodeSignature" "$APP/embedded.mobileprovision"

ldid -SRootHelper/entitlements.plist "$APP/speakahelper"
ldid -SSpeaka.entitlements "$APP/Speaka"
chmod 755 "$APP/Speaka" "$APP/speakahelper"

rm -rf build/Payload packages/Speaka.tipa packages/Speaka.ipa
mkdir -p build/Payload packages
cp -R "$APP" build/Payload/
(cd build && zip -qry ../packages/Speaka.tipa Payload)
cp packages/Speaka.tipa packages/Speaka.ipa    # the same file; some tools only open .ipa
echo "Built packages/Speaka.tipa and packages/Speaka.ipa ($(du -h packages/Speaka.tipa | cut -f1))"
