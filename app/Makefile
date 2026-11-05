# Speaka.app for TrollStore, built like Caz: XcodeGen generates the Xcode project from project.yml, xcodebuild
# compiles it, clang builds the update helper (RootHelper/main.c), and scripts/package.sh signs both with their
# entitlements and zips them.
#
#   make -C app tipa      ->  app/packages/Speaka.tipa and app/packages/Speaka.ipa (the same file)
#
# CI passes VERSION (tools/release_version.sh), BUILD (the run number) and DEB, the release's own .deb, which the app
# carries so one .tipa installs both. Locally the version is control's, and without DEB the app carries no tweak.
# Needs macOS with Xcode, plus XcodeGen and ldid (brew install xcodegen ldid). Independent of the tweak's
# Makefile at the repository root: the app never goes into the tweak's .deb, though the tweak goes into the app.

DERIVED = build/DerivedData
VERSION ?= $(shell sed -n 's/^Version:[[:space:]]*//p' ../control | head -n 1)
BUILD ?= 1
DEB ?=

tipa: app helper
	DEB="$(DEB)" bash scripts/package.sh

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
