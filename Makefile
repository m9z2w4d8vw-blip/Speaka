# Speaka - bluetoothd tweak for rootless jailbreaks (Dopamine / ElleKit)

# Rootless: files install under /var/jb and the package architecture becomes iphoneos-arm64.
# Rootful build instead:  make package THEOS_PACKAGE_SCHEME=
THEOS_PACKAGE_SCHEME ?= rootless

# Do not turn compiler warnings into build failures.
# MUST stay above the 'include .../common.mk' line: it is read while that file loads.
GO_EASY_ON_ME = 1

# bluetoothd is an arm64e binary: without an arm64e slice this dylib builds fine and never loads.
# iOS 15 is the deployment target Theos recommends for rootless (theos.dev/docs/rootless).
ARCHS  := arm64 arm64e
TARGET := iphone:clang:latest:15.0

include $(THEOS)/makefiles/common.mk

TWEAK_NAME = Speaka

# bluetoothd never plays audio itself: the helper does (audiohelper/), so no audio frameworks load into it.
Speaka_FILES      = tweak/Hooks.xm tweak/SinkEndpoint.c tweak/StreamControl.c tweak/AudioRelay.c tweak/Session.c tweak/Log.c tweak/PeerIdentity.c shared/AudioRing.c shared/AACConfig.c tweak/VolumeControl.m shared/VolumeControl.c shared/VolumeMailbox.c
# -I.: tweak/Hooks.xm reaches tweak/ and shared/ from the root, since Theos compiles it from .theos/obj.
Speaka_CFLAGS     = -fobjc-arc -fvisibility=hidden -fblocks -I.
Speaka_FRAMEWORKS = CoreFoundation Foundation
Speaka_LIBRARIES  = substrate

include $(THEOS)/makefiles/tweak.mk

# The audio helper plays what bluetoothd hands it, so it's always built (folder audiohelper/).
SUBPROJECTS += audiohelper
# v9.9.3: Settings > Update notifications from launchd every 15 minutes (folder updatecheck/): its own binary, so the
# audio helper's signature is unchanged.
SUBPROJECTS += updatecheck
# The Settings > Speaka page with the status and debug log (folder settings/).
# The Settings > Speaka page. If it ever fails to build, run make package SPEAKA_WITH_SETTINGS=0: the tweak still builds.
SPEAKA_WITH_SETTINGS ?= 1
ifeq ($(SPEAKA_WITH_SETTINGS),1)
SUBPROJECTS += settings
endif

# The helper and optional Settings bundle participate in the same package.
ifneq ($(strip $(SUBPROJECTS)),)
include $(THEOS_MAKE_PATH)/aggregate.mk
endif

# Browser uploads store maintainer scripts as 0644. Repair the staged copies
# after Theos creates DEBIAN/control and before its DEB packager validates them.
before-package:: $(THEOS_STAGING_DIR)/DEBIAN/control
	chmod 0755 "$(THEOS_STAGING_DIR)/DEBIAN/postinst" "$(THEOS_STAGING_DIR)/DEBIAN/prerm"
