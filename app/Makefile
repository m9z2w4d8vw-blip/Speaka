# Speaka.app for TrollStore, built like Caz: XcodeGen generates the Xcode project from project.yml, xcodebuild
# compiles it, and scripts/package.sh signs it with Speaka.entitlements and zips it.
#
#   make -C app tipa      ->  app/packages/Speaka.tipa and app/packages/Speaka.ipa (the same file)
#
# Needs macOS with Xcode, plus XcodeGen and ldid (brew install xcodegen ldid). Independent of the tweak's
# Makefile at the repository root: the app never goes into the tweak's .deb.

DERIVED = build/DerivedData

tipa: app
	bash scripts/package.sh

ipa: tipa

app:
	xcodegen generate
	xcodebuild -quiet -project Speaka.xcodeproj -scheme Speaka -configuration Release \
	  -sdk iphoneos -destination 'generic/platform=iOS' -derivedDataPath $(DERIVED) \
	  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" build

clean:
	rm -rf build packages Speaka.xcodeproj

.PHONY: tipa ipa app clean
