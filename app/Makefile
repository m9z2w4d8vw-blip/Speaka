# Speaka.app for TrollStore, built like Caz: XcodeGen generates the Xcode project from project.yml, xcodebuild
# compiles it, clang builds the update helper (RootHelper/main.c), and scripts/package.sh signs both with their
# entitlements and zips them.
#
#   make -C app tipa      ->  app/packages/Speaka.tipa and app/packages/Speaka.ipa (the same file)
#
# CI passes VERSION (tools/release_version.sh) and BUILD (the run number). Locally the version is control's.
# Needs macOS with Xcode, plus XcodeGen and ldid (brew install xcodegen ldid). Independent of the tweak's
# Makefile at the repository root: the app never goes into the tweak's .deb.

DERIVED = build/DerivedData
VERSION ?= $(shell sed -n 's/^Version:[[:space:]]*//p' ../control | head -n 1)
BUILD ?= 1

tipa: app helper
	bash scripts/package.sh

ipa: tipa

app:
	xcodegen generate
	xcodebuild -quiet -project Speaka.xcodeproj -scheme Speaka -configuration Release \
	  -sdk iphoneos -destination 'generic/platform=iOS' -derivedDataPath $(DERIVED) \
	  MARKETING_VERSION="$(VERSION)" CURRENT_PROJECT_VERSION="$(BUILD)" \
	  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" build

helper:
	mkdir -p build
	xcrun --sdk iphoneos clang -arch arm64 -mios-version-min=16.0 -O2 -Wall \
	  -framework CoreFoundation -o build/speakahelper RootHelper/main.c

clean:
	rm -rf build packages Speaka.xcodeproj

.PHONY: tipa ipa app helper clean
