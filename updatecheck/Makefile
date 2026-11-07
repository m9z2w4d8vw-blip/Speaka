# SpeakaUpdateCheck (v9.9.3): Settings > Update notifications, from launchd every 15 minutes (com.parv.speaka.updatecheck).
# Installed as /var/jb/usr/bin/SpeakaUpdateCheck. Its own binary, so the audio helper keeps its signature (no
# entitlements) and nothing about it can affect audio.
THEOS_PACKAGE_SCHEME ?= rootless
GO_EASY_ON_ME = 1
ARCHS := arm64 arm64e
TARGET := iphone:clang:latest:15.0
include $(THEOS)/makefiles/common.mk
TOOL_NAME = SpeakaUpdateCheck
SpeakaUpdateCheck_FILES = UpdateCheck.m ../shared/UpdatePolicy.c
SpeakaUpdateCheck_CFLAGS = -fobjc-arc -fblocks -std=gnu11 -I..
SpeakaUpdateCheck_FRAMEWORKS = Foundation UserNotifications
# Posting as Speaka: -[UNUserNotificationCenter initWithBundleIdentifier:] needs Speaka's bundle ID in this list.
SpeakaUpdateCheck_CODESIGN_FLAGS = -Sentitlements.plist
SpeakaUpdateCheck_INSTALL_PATH = /usr/bin
include $(THEOS_MAKE_PATH)/tool.mk
